"""Read-only historical AP diagnostic; caller owns connections and transactions.

This is evidence, never a settlement, approval, posting or repair operation.
Amounts are compared per tenant/invoice and aggregated per tenant/supplier/currency.
Missing obligations remain structural findings even when the source residual is zero.
Reflection uses dialect read-only metadata queries (SQLite uses PRAGMA).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID

import sqlalchemy as sa

RECOGNIZED_STATUSES = frozenset({"APPROVED", "POSTED", "PARTIALLY_PAID", "PAID"})
OLD_COMPARISON_STATUSES = frozenset({"POSTED", "PARTIALLY_PAID", "PAID"})
NON_RECOGNIZED_STATUSES = frozenset({"DRAFT", "SUBMITTED", "REJECTED", "VOID"})


@dataclass(frozen=True, order=True)
class Finding:
    kind: str
    tenant_id: str
    invoice_id: str
    record_id: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Position:
    tenant_id: str
    invoice_id: str
    supplier_ref: str
    currency_code: str
    source_status: str
    source_residual: Decimal | None
    target_outstanding: Decimal | None
    landed: bool
    obligation_count: int
    structurally_reconciled: bool


@dataclass(frozen=True)
class Aggregate:
    tenant_id: str
    supplier_ref: str
    currency_code: str
    source_residual: Decimal
    target_outstanding: Decimal
    invalid_source_amounts: int
    invalid_target_amounts: int


@dataclass(frozen=True)
class PayablesReconciliation:
    tenant_extent: tuple[UUID, ...]
    counts: tuple[tuple[str, int], ...]
    positions: tuple[Position, ...]
    aggregates: tuple[Aggregate, ...]
    findings: tuple[Finding, ...]

    @property
    def reconciled(self) -> bool:
        return not self.findings


def _identity(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _money(value: object) -> Decimal:
    """No NULL defaults, floats, booleans, quantization or non-finite money."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError("unsupported money type")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal money") from exc
    if not result.is_finite():
        raise ValueError("non-finite money")
    return result


def _sum(values: list[Decimal]) -> Decimal:
    """Exact addition independent of the process Decimal context precision."""
    if not values:
        return Decimal(0)
    exponent = min(int(value.as_tuple().exponent) for value in values)
    coefficient = 0
    for value in values:
        parts = value.as_tuple()
        digits = int("".join(map(str, parts.digits)))
        coefficient += (-digits if parts.sign else digits) * 10 ** (int(parts.exponent) - exponent)
    return Decimal((int(coefficient < 0), tuple(map(int, str(abs(coefficient)))), exponent))


def _reflect(connection: sa.Connection, name: str, schema: str | None = None) -> sa.Table:
    if schema is None:
        inspector = sa.inspect(connection)
        holders = [
            candidate
            for candidate in inspector.get_schema_names()
            if candidate not in ("pg_catalog", "information_schema")
            and name in inspector.get_table_names(schema=candidate)
        ]
        if len(holders) != 1:
            raise ValueError(f"source_table_resolution:{name}:expected_one_holder:found_{len(holders)}")
        schema = holders[0]
    return sa.Table(name, sa.MetaData(), schema=schema, autoload_with=connection, resolve_fks=False)


def _rows(connection: sa.Connection, table: sa.Table, tenant_column: str, scope: tuple[UUID, ...]):
    # Cast exact numeric storage to text before SQLAlchemy's Numeric result
    # processor can quantize it. Refuse approximate SQL numeric types explicitly.
    money = {"total_amount", "amount_paid", "outstanding_amount"}
    for column in table.c:
        if column.name in money and isinstance(column.type, sa.Float):
            raise ValueError(f"unsupported_float_money_column:{table.fullname}.{column.name}")
    columns = [sa.cast(c, sa.Text).label(c.name) if c.name in money else c for c in table.c]
    tenant = table.c[tenant_column]
    # UUID native and reflected text storage both occur in historical snapshots.
    values = scope if isinstance(tenant.type, sa.Uuid) else tuple(str(value) for value in scope)
    return list(connection.execute(sa.select(*columns).where(tenant.in_(values))).mappings())


def reconcile_payables(
    source: sa.Connection, destination: sa.Connection, *, tenant_extent: tuple[UUID, ...]
) -> PayablesReconciliation:
    """Compare scoped historical residuals to obligation evidence using SELECTs.

    Call with SQLAlchemy Core Connections, never ORM Sessions: this function
    neither autoflushes nor opens/closes/commits/rolls back a transaction. Unknown
    status/type rows are retained and named, but are not recognized liabilities.
    STANDARD is the only ruled invoice type; debit/credit notes need owner review.
    ``structurally_reconciled`` means scoped residual equality, preserved header
    evidence and obligation presence only. It does not certify settlement history,
    original obligation amounts/statuses, accounting or owner lifecycle validity.
    """
    if (
        not isinstance(tenant_extent, tuple)
        or not tenant_extent
        or any(not isinstance(value, UUID) for value in tenant_extent)
    ):
        raise ValueError("tenant_extent must be a nonempty tuple of UUIDs")
    if not isinstance(source, sa.Connection) or not isinstance(destination, sa.Connection):
        raise TypeError("source and destination must be SQLAlchemy Core Connections")
    scope = tuple(sorted(set(tenant_extent), key=str))
    sh = _reflect(source, "supplier_invoice")
    dh = _reflect(destination, "supplier_invoices", "mod_payables")
    po = _reflect(destination, "payment_obligations", "mod_payables")
    sources = _rows(source, sh, "organization_id", scope)
    targets = _rows(destination, dh, "tenant_id", scope)
    obligations = _rows(destination, po, "tenant_id", scope)
    findings: list[Finding] = []
    counts = Counter(source_headers=len(sources), target_headers=len(targets), target_obligations=len(obligations))

    def index(rows, tenant, ident, label):
        result = {}
        for row in rows:
            key = (_identity(row[tenant]), _identity(row[ident]))
            if key in result:
                raise ValueError(f"duplicate_{label}_key:{key}")
            result[key] = row
        return result

    source_map = index(sources, "organization_id", "invoice_id", "source")
    target_map = index(targets, "tenant_id", "id", "target")
    source_tenants = defaultdict(set)
    target_tenants = defaultdict(set)
    for tenant, invoice in source_map:
        source_tenants[invoice].add(tenant)
    for tenant, invoice in target_map:
        target_tenants[invoice].add(tenant)
    by_invoice = defaultdict(list)
    for row in obligations:
        by_invoice[(_identity(row["tenant_id"]), _identity(row["invoice_id"]))].append(row)
    grouped_source = defaultdict(list)
    grouped_target = defaultdict(list)
    invalid_source = Counter()
    invalid_target = Counter()

    def finding(kind, key, record="", detail=""):
        findings.append(Finding(kind, *key, record, detail))

    def amount(row, column, key, origin, record=""):
        try:
            return _money(row[column])
        except ValueError as exc:
            finding(f"invalid_{origin}_money", key, record, f"{column}:{exc}")
            return None

    # Independent target inventory preserves extras, orphans and currencies;
    # no inner join can hide missing header/obligation evidence.
    for key in sorted(target_map):
        if key not in source_map:
            finding("extra_target_header", key)
            if source_tenants[key[1]]:
                finding("header_tenant_mismatch", key)
    for key in sorted(by_invoice):
        header = target_map.get(key)
        if key not in source_map:
            for row in by_invoice[key]:
                finding("extra_target_obligation", key, _identity(row["id"]))
                if source_tenants[key[1]]:
                    finding("obligation_tenant_mismatch", key, _identity(row["id"]))
        for row in by_invoice[key]:
            record = _identity(row["id"])
            if header is None:
                finding("orphan_target_obligation", key, record)
            supplier = _identity(header["supplier_ref"]) if header else "<missing_header>"
            group = (*key[:1], supplier, str(row["currency_code"]))
            value = amount(row, "outstanding_amount", key, "target", record)
            if value is None:
                invalid_target[group] += 1
            else:
                grouped_target[group].append(value)
            if header and row["currency_code"] != header["currency_code"]:
                finding("obligation_currency_mismatch", key, record)

    positions = []
    for key, row in sorted(source_map.items()):
        start_findings = len(findings)
        header = target_map.get(key)
        status = str(row["status"])
        invoice_type = str(row["invoice_type"])
        recognized = status in RECOGNIZED_STATUSES and invoice_type == "STANDARD"
        supplier, currency = _identity(row["supplier_id"]), str(row["currency_code"])
        group = (key[0], supplier, currency)
        if status not in RECOGNIZED_STATUSES | NON_RECOGNIZED_STATUSES:
            finding("unknown_source_status", key, detail=status)
        if invoice_type != "STANDARD":
            finding("unsupported_source_type", key, detail=invoice_type)
        total = amount(row, "total_amount", key, "source")
        paid = amount(row, "amount_paid", key, "source")
        residual = _sum([total, paid.copy_negate()]) if total is not None and paid is not None else None
        if recognized:
            counts["recognized_source_headers"] += 1
            if residual is None:
                invalid_source[group] += 1
            else:
                grouped_source[group].append(residual)
        if header is None:
            finding("missing_target_header", key)
            if target_tenants[key[1]]:
                finding("header_tenant_mismatch", key)
        else:
            counts["landed_headers"] += 1
            counts[f"landed_status_{status}"] += 1
            if recognized and str(header["status"]) != "APPROVED":
                finding("recognized_header_status_mismatch", key, detail=str(header["status"]))
            if status in OLD_COMPARISON_STATUSES:
                counts["old_comparison_landed_headers"] += 1
            for field, expected in (("supplier_ref", supplier), ("currency_code", currency)):
                if _identity(header[field]) != expected:
                    finding(f"header_{field}_mismatch", key)
            target_total = amount(header, "total_amount", key, "target_header")
            if total is not None and target_total is not None and total != target_total:
                finding("header_total_amount_mismatch", key)
            if row["supplier_invoice_number"] != header["supplier_document_number"]:
                finding("supplier_document_number_mismatch", key)
        related = by_invoice.get(key, [])
        matching = [r for r in related if str(r["currency_code"]) == currency]
        values = []
        for obligation in matching:
            try:
                values.append(_money(obligation["outstanding_amount"]))
            except ValueError:
                pass  # Named in the independent target inventory above.
        outstanding = _sum(values) if len(values) == len(matching) else None
        if header is not None and status in OLD_COMPARISON_STATUSES:
            # The historical comparator treated absence as a numerical zero.
            # Keep that number visible separately from structural reconciliation;
            # only compare compatible supplier/currency evidence here.
            compatible_old = (
                _identity(header["supplier_ref"]) == supplier
                and str(header["currency_code"]) == currency
                and len(matching) == len(related)
            )
            if compatible_old and residual is not None and outstanding is not None and residual != outstanding:
                counts["old_comparison_monetary_mismatches"] += 1
        if recognized:
            if not related:
                finding("missing_target_obligation", key)
            if len(matching) != len(related):
                finding("source_obligation_currency_mismatch", key)
            compatible = header is not None and _identity(header["supplier_ref"]) == supplier
            if compatible and residual is not None and outstanding is not None and residual != outstanding:
                finding("outstanding_amount_mismatch", key, detail=f"source={residual};target={outstanding}")
        clean = recognized and len(findings) == start_findings and bool(related)
        # Include findings emitted during the independent obligation inventory.
        clean = clean and not any((f.tenant_id, f.invoice_id) == key for f in findings)
        counts["structurally_reconciled_positions"] += int(clean)
        positions.append(
            Position(*key, supplier, currency, status, residual, outstanding, header is not None, len(related), clean)
        )
    groups = sorted(set(grouped_source) | set(grouped_target) | set(invalid_source) | set(invalid_target))
    aggregates = tuple(
        Aggregate(*g, _sum(grouped_source[g]), _sum(grouped_target[g]), invalid_source[g], invalid_target[g])
        for g in groups
    )
    for name in (
        "recognized_source_headers",
        "landed_headers",
        "old_comparison_landed_headers",
        "old_comparison_monetary_mismatches",
        "landed_status_APPROVED",
        "structurally_reconciled_positions",
    ):
        counts.setdefault(name, 0)
    counts.update(f.kind for f in findings)
    return PayablesReconciliation(
        scope, tuple(sorted(counts.items())), tuple(positions), aggregates, tuple(sorted(findings))
    )
