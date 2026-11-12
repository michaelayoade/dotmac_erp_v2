"""Trusted, SELECT-only proof for the bounded unpaid historical AP admission.

The operator supplies a read-only restored snapshot Connection and its held
manifest. Database names and read-only mode are checked, but a manifest digest
alone cannot cryptographically prove the bytes used to restore a database.
The destination Session is supplied by the owner, sharing its invoice lock and
transaction. This verifier never constructs connections, writes, or commits.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.semantic_encoding import ABSENT, digest_of, encode_fields, encode_ordered
from dotmac_payables import HistoricalUnpaidInvoiceCommand, HistoricalUnpaidInvoiceEvidence
from sqlalchemy.orm import Session

from erp_v2.importer.payables_reconciliation import _money, _reflect, _sum
from erp_v2.importer.snapshot import SnapshotManifest


class HistoricalEvidenceError(ValueError):
    """A named fail-closed evidence failure; contains no business row contents."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


NUMBERS = frozenset(
    {
        "exchange_rate",
        "subtotal",
        "tax_amount",
        "total_amount",
        "amount_paid",
        "functional_currency_amount",
        "withholding_tax_amount",
        "prepayment_applied",
        "stamp_duty_amount",
        "quantity",
        "unit_price",
        "line_amount",
        "total_debit",
        "total_credit",
        "total_debit_functional",
        "total_credit_functional",
        "debit_amount",
        "credit_amount",
        "debit_amount_functional",
        "credit_amount_functional",
        "original_debit_amount",
        "original_credit_amount",
        "debit",
        "credit",
        "debit_functional",
        "credit_functional",
        "original_debit",
        "original_credit",
    }
)
DIMENSIONS = (
    ("BUSINESS_UNIT", "business_unit_id"),
    ("COST_CENTER", "cost_center_id"),
    ("PROJECT", "project_id"),
    ("SEGMENT", "segment_id"),
)


# These projections are the frozen v1 decision shape. Adding a database column
# must not silently re-key permanent historical evidence. A new material fact
# requires an explicit domain/version cutover and new reviewed commands.
SOURCE_HEADER = """organization_id invoice_id supplier_id invoice_number supplier_invoice_number invoice_type status
posting_status invoice_date received_date due_date currency_code exchange_rate subtotal tax_amount total_amount
amount_paid functional_currency_amount ap_control_account_id journal_entry_id posted_at withholding_tax_amount
withholding_tax_code_id prepayment_applied is_prepayment stamp_duty_amount submitted_by_user_id submitted_at
approved_by_user_id approved_at approval_request_id created_at""".split()
SOURCE_SUPPLIER = "supplier_id organization_id legal_name trading_name".split()
SOURCE_ORGANIZATION = "organization_id functional_currency_code".split()
SOURCE_INVOICE_LINE = """line_id invoice_id line_number description quantity unit_price line_amount tax_amount
expense_account_id cost_center_id project_id segment_id po_line_id goods_receipt_line_id created_at""".split()
DESTINATION_HEADER = """id tenant_id number supplier_ref supplier_name_snapshot supplier_document_number invoice_date
received_date currency_code exchange_rate liability_account_ref subtotal tax_amount total_amount payment_schedule
procurement_ref receipt_evidence_fingerprint submitted_by submitted_at approved_by approved_at
approval_reference request_fingerprint created_at""".split()
DESTINATION_INVOICE_LINE = """id tenant_id invoice_id line_number description quantity unit_price line_amount tax_amount
posting_account_ref tax_account_ref dimension_refs created_at""".split()
SOURCE_JOURNAL = """journal_entry_id organization_id journal_number journal_type status entry_date posting_date
fiscal_period_id description reference currency_code exchange_rate total_debit total_credit total_debit_functional
total_credit_functional source_module source_document_type source_document_id is_reversal reversed_journal_id
reversal_journal_id posted_by_user_id posted_at created_at""".split()
DESTINATION_JOURNAL = """id tenant_id number kind status entry_date posting_date fiscal_period_id description reference
currency_code exchange_rate total_debit total_credit total_debit_functional total_credit_functional source_owner
source_document_kind source_document_id source_version source_fingerprint request_fingerprint approval_reference
posted_by posted_at reverses_journal_id reversal_journal_id created_at""".split()
SOURCE_JOURNAL_LINE = """line_id journal_entry_id line_number account_id description debit_amount credit_amount
debit_amount_functional credit_amount_functional currency_code exchange_rate business_unit_id cost_center_id
project_id segment_id created_at""".split()
DESTINATION_JOURNAL_LINE = """id tenant_id journal_id line_number account_id description debit credit debit_functional
credit_functional created_at""".split()
SOURCE_LEDGER = """ledger_line_id organization_id journal_entry_id journal_line_id account_id account_code
fiscal_period_id
entry_date posting_date description journal_reference debit_amount credit_amount original_currency_code
original_debit_amount original_credit_amount exchange_rate source_module source_document_type source_document_id
posted_by_user_id posted_at business_unit_id cost_center_id project_id segment_id""".split()
DESTINATION_LEDGER = """id tenant_id journal_id journal_line_id account_id account_code fiscal_period_id journal_number
entry_date posting_date description debit credit original_debit original_credit currency_code exchange_rate source_owner
source_document_kind source_document_id source_version source_fingerprint posted_by posted_at""".split()


# The held legacy PostgreSQL snapshot contains AP and AR tables with the same
# allocation name. Bind the source contract to its evidenced namespaces.
SOURCE_SCHEMAS = {
    "organization": "core_org",
    "supplier": "ap",
    "supplier_invoice": "ap",
    "supplier_invoice_line": "ap",
    "payment_allocation": "ap",
    "journal_entry": "gl",
    "journal_entry_line": "gl",
    "posted_ledger_line": "gl",
}


READ_FIELDS = {
    "supplier_invoice": SOURCE_HEADER,
    "supplier": SOURCE_SUPPLIER,
    "organization": SOURCE_ORGANIZATION,
    "supplier_invoice_line": SOURCE_INVOICE_LINE,
    "supplier_invoices": DESTINATION_HEADER,
    "supplier_invoice_lines": DESTINATION_INVOICE_LINE,
    "journal_entry": SOURCE_JOURNAL,
    "journal_entries": DESTINATION_JOURNAL,
    "journal_entry_line": SOURCE_JOURNAL_LINE,
    "journal_lines": DESTINATION_JOURNAL_LINE,
    "posted_ledger_line": SOURCE_LEDGER,
    "posted_ledger_lines": DESTINATION_LEDGER,
    "payment_allocation": ["invoice_id"],
    "journal_line_dimensions": "id tenant_id journal_line_id dimension_id dimension_value_id".split(),
    "posted_ledger_dimensions": (
        "id tenant_id ledger_line_id dimension_id dimension_code dimension_value_id value_code"
    ).split(),
    "accounting_dimensions": ["id", "tenant_id", "code"],
    "accounting_dimension_values": ["id", "tenant_id", "dimension_id", "code"],
}


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise HistoricalEvidenceError(reason)


def _id(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _equal(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is b
    return _id(a) == _id(b) if isinstance(a, (str, UUID)) and isinstance(b, (str, UUID)) else a == b


def _amount(value: object, reason: str) -> Decimal:
    try:
        return _money(value)
    except ValueError as exc:
        raise HistoricalEvidenceError(reason) from exc


def _at_scale(value: Decimal, places: int) -> bool:
    parts = value.as_tuple()
    extra = -int(parts.exponent) - places
    return extra <= 0 or all(digit == 0 for digit in parts.digits[-extra:])


def _day(value: object, reason: str) -> date:
    try:
        result = date.fromisoformat(value) if isinstance(value, str) else value
        _require(isinstance(result, date) and not isinstance(result, datetime), reason)
        return result
    except ValueError as exc:
        raise HistoricalEvidenceError(reason) from exc


def _instant(value: object, reason: str) -> datetime:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        _require(isinstance(result, datetime) and result.tzinfo is not None and result.utcoffset() is not None, reason)
        return result
    except ValueError as exc:
        raise HistoricalEvidenceError(reason) from exc


def _json(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise HistoricalEvidenceError("invalid_json_evidence") from exc
    return value


def _project(row: dict, names: list[str]) -> dict:
    for name in names:
        _require(name in row, "missing_projection_fact:" + name)
    return {name: row[name] for name in names}


def _project_rows(rows: list[dict], names: list[str], key: str) -> list[dict]:
    return [_project(row, names) for row in sorted(rows, key=lambda item: _id(item[key]))]


def _canonical(value: object) -> object:
    """Explicit typed NULLs, exact decimals, and recursively framed collections."""
    if value is None:
        return ABSENT
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return encode_fields([(str(k), _canonical(v)) for k, v in sorted(value.items())])
    if isinstance(value, (list, tuple)):
        return encode_ordered(_canonical(v) for v in value)
    return value


def _fingerprint(domain: str, value: object) -> str:
    return digest_of(domain, encode_fields([("facts", _canonical(value))]))


def _database(connection: sa.Connection) -> str:
    if connection.dialect.name == "postgresql":
        return connection.execute(sa.text("SELECT current_database()")).scalar_one()
    if connection.dialect.name == "sqlite":
        rows = connection.exec_driver_sql("PRAGMA database_list").all()
        return Path(next(row[2] for row in rows if row[1] == "main")).name
    raise HistoricalEvidenceError("unsupported_database_dialect")


def _readonly(connection: sa.Connection) -> bool:
    if connection.dialect.name == "postgresql":
        return connection.execute(sa.text("SELECT current_setting('transaction_read_only')")).scalar_one() == "on"
    if connection.dialect.name == "sqlite":
        return connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
    return False


def _read(
    connection: sa.Connection, name: str, *, schema: str | None = None, key: str | None = None, value: object = None
) -> list[dict]:
    if schema is None and connection.dialect.name == "postgresql":
        _require(name in SOURCE_SCHEMAS, "unknown_source_table_contract")
        schema = SOURCE_SCHEMAS[name]
    table = _reflect(connection, name, schema)
    columns = []
    for field in READ_FIELDS[name]:
        _require(field in table.c, f"missing_column:{name}.{field}")
        col = table.c[field]
        if col.name in NUMBERS or isinstance(col.type, sa.Numeric):
            _require(not isinstance(col.type, sa.Float), "unsupported_float_numeric")
            columns.append(sa.cast(col, sa.Text).label(col.name))
        else:
            columns.append(col)
    statement = sa.select(*columns)
    if key is not None:
        _require(key in table.c, f"missing_column:{name}.{key}")
        try:
            ident = UUID(str(value))
        except (ValueError, TypeError):
            statement = statement.where(table.c[key] == value)
        else:
            statement = statement.where(sa.func.replace(sa.cast(table.c[key], sa.Text), "-", "") == ident.hex)
    result = []
    for raw in connection.execute(statement).mappings():
        row = dict(raw)
        for column, item in row.items():
            if item is not None and column in NUMBERS:
                row[column] = _amount(item, f"invalid_numeric:{name}.{column}")
        result.append(row)
    return result


def _one(rows: list[dict], reason: str) -> dict:
    _require(len(rows) == 1, reason)
    return rows[0]


def _match(source: dict, target: dict, mapping: dict[str, str], prefix: str) -> None:
    for dest, origin in mapping.items():
        _require(origin in source and dest in target, f"{prefix}_{dest}_missing")
        left, right = source[origin], target[dest]
        if origin.endswith("_at") and left is not None:
            left, right = _instant(left, prefix + "_" + dest), _instant(right, prefix + "_" + dest)
        _require(_equal(left, right), f"{prefix}_{dest}")


def _pair(source: list[dict], target: list[dict], source_id: str, prefix: str):
    _require(bool(source), prefix + "_missing")
    _require(len(source) == len(target), prefix + "_count")
    sm = {_id(row[source_id]): row for row in source}
    tm = {_id(row["id"]): row for row in target}
    _require(len(sm) == len(source) and len(tm) == len(target) and sm.keys() == tm.keys(), prefix + "_ids")
    return [(sm[key], tm[key]) for key in sorted(sm)]


def _tenant(rows: list[dict], tenant: UUID, prefix: str, column: str = "tenant_id") -> None:
    _require(all(column in row and _equal(row[column], tenant) for row in rows), prefix + "_tenant")


@dataclass(frozen=True)
class HistoricalPayablesVerifier:
    source: sa.Connection
    manifest: SnapshotManifest
    source_owner: str = "legacy-erp"

    @staticmethod
    def command(evidence: HistoricalUnpaidInvoiceEvidence) -> HistoricalUnpaidInvoiceCommand:
        return HistoricalUnpaidInvoiceCommand(
            source_owner=evidence.source_owner,
            source_invoice_id=evidence.source_invoice_id,
            invoice_id=evidence.invoice_id,
            source_snapshot_sha256=evidence.source_snapshot_sha256,
            evidence_fingerprint=evidence.fingerprint(),
        )

    def verify(
        self, *, scope, command: HistoricalUnpaidInvoiceCommand, destination: Session
    ) -> HistoricalUnpaidInvoiceEvidence:
        command.validate()
        _require(command.source_owner == self.source_owner, "command_source_owner")
        _require(command.source_snapshot_sha256 == self.manifest.snapshot_sha256, "command_snapshot")
        _require(command.source_invoice_id == str(command.invoice_id), "command_source_invoice")
        evidence = self.evidence(scope=scope, invoice_id=command.invoice_id, destination=destination)
        _require(command.evidence_fingerprint == evidence.fingerprint(), "command_fingerprint")
        return evidence

    def evidence(self, *, scope, invoice_id: UUID, destination: Session) -> HistoricalUnpaidInvoiceEvidence:
        """Preview immutable proof; later verify rebuilds it inside the owner's lock."""
        _require(isinstance(self.source, sa.Connection), "source_connection_required")
        _require(isinstance(destination, Session), "destination_session_required")
        _require(not destination.new and not destination.dirty and not destination.deleted, "destination_dirty_session")
        _require(isinstance(invoice_id, UUID), "invoice_id_required")
        tenant = getattr(scope, "tenant_id", None)
        _require(isinstance(tenant, UUID), "scope_tenant_required")
        manifest = self.manifest
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", manifest.snapshot_sha256)), "manifest_snapshot")
        _require(bool(manifest.candidate_revision) and bool(self.source_owner), "manifest_candidate")
        _require(manifest.source_database != manifest.destination_database, "manifest_database_separation")
        _require(str(tenant) in {_id(value) for value in manifest.tenant_extent}, "tenant_extent")
        _require(_readonly(self.source), "source_not_readonly")
        _require(_database(self.source) == manifest.source_database, "source_database")
        with destination.no_autoflush:
            connection = destination.connection()
            _require(_database(connection) == manifest.destination_database, "destination_database")
            try:
                return self._evidence(tenant, invoice_id, connection)
            except KeyError as exc:
                raise HistoricalEvidenceError(f"missing_required_fact:{exc.args[0]}") from exc

    def _evidence(self, tenant: UUID, invoice_id: UUID, dest: sa.Connection) -> HistoricalUnpaidInvoiceEvidence:
        src = self.source
        header = _one(
            _read(src, "supplier_invoice", key="invoice_id", value=invoice_id), "source_invoice_missing_or_duplicate"
        )
        _tenant([header], tenant, "source_invoice", "organization_id")
        _require(header["invoice_type"] == "STANDARD", "source_type")
        _require(header["status"] == "POSTED" and header["posting_status"] == "POSTED", "source_status")
        _require(header["amount_paid"] == 0, "source_paid")
        _require(
            header["withholding_tax_amount"] == 0 and header["withholding_tax_code_id"] is None, "source_withholding"
        )
        _require(header["is_prepayment"] is False and header["prepayment_applied"] == 0, "source_prepayment")
        _require(header["stamp_duty_amount"] == 0, "unsupported_stamp_duty")
        allocations = _read(src, "payment_allocation", key="invoice_id", value=invoice_id)
        _require(not allocations, "source_allocations")
        for field in ("subtotal", "tax_amount", "total_amount"):
            amount = header[field]
            # Exact remainder, not quantization: six-place source storage must never round.
            _require(isinstance(amount, Decimal) and amount >= 0 and _at_scale(amount, 2), "source_amount_scale")
        _require(
            header["total_amount"] > 0 and _sum([header["subtotal"], header["tax_amount"]]) == header["total_amount"],
            "source_total",
        )
        due = _day(header["due_date"], "source_due_date")
        posted = _instant(header["posted_at"], "source_posted_at")
        organization = _one(_read(src, "organization", key="organization_id", value=tenant), "source_organization")
        _require(
            header["exchange_rate"] == 1
            and organization["functional_currency_code"] == header["currency_code"]
            and header["functional_currency_amount"] == header["total_amount"],
            "unsupported_currency",
        )
        supplier = _one(_read(src, "supplier", key="supplier_id", value=header["supplier_id"]), "source_supplier")
        _tenant([supplier], tenant, "source_supplier", "organization_id")
        name = supplier["legal_name"] if supplier["legal_name"] not in (None, "") else supplier["trading_name"]
        _require(isinstance(name, str) and bool(name), "source_supplier_name")
        target = _one(
            _read(dest, "supplier_invoices", schema="mod_payables", key="id", value=invoice_id),
            "destination_invoice_missing_or_duplicate",
        )
        _tenant([target], tenant, "destination_invoice")
        mapping = {
            "id": "invoice_id",
            "number": "invoice_number",
            "supplier_ref": "supplier_id",
            "supplier_document_number": "supplier_invoice_number",
            "liability_account_ref": "ap_control_account_id",
            "submitted_by": "submitted_by_user_id",
            "approved_by": "approved_by_user_id",
        }
        mapping.update(
            {
                key: key
                for key in (
                    "invoice_date",
                    "received_date",
                    "currency_code",
                    "exchange_rate",
                    "subtotal",
                    "tax_amount",
                    "total_amount",
                    "submitted_at",
                    "approved_at",
                    "created_at",
                )
            }
        )
        _match(header, target, mapping, "header")
        _require(target["supplier_name_snapshot"] == name, "header_supplier_name_snapshot")
        # The legacy approval request is optional evidence, never a fabricated stamp.
        _require(_equal(target["approval_reference"], header["approval_request_id"]), "header_approval_reference")
        _require(
            target["procurement_ref"] is None and target["receipt_evidence_fingerprint"] is None,
            "unsupported_procurement_evidence",
        )
        schedule = _json(target["payment_schedule"])
        _require(
            isinstance(schedule, list)
            and len(schedule) == 1
            and isinstance(schedule[0], dict)
            and set(schedule[0]) == {"due_date", "amount"},
            "header_payment_schedule",
        )
        _require(
            _day(schedule[0]["due_date"], "header_due_date") == due
            and _amount(schedule[0]["amount"], "header_schedule_amount") == header["total_amount"],
            "header_payment_schedule",
        )
        lines = _read(src, "supplier_invoice_line", key="invoice_id", value=invoice_id)
        targets = _read(dest, "supplier_invoice_lines", schema="mod_payables", key="invoice_id", value=invoice_id)
        _tenant(targets, tenant, "invoice_line")
        line_mapping = {
            k: k
            for k in (
                "invoice_id",
                "line_number",
                "description",
                "quantity",
                "unit_price",
                "line_amount",
                "tax_amount",
                "created_at",
            )
        }
        line_mapping["posting_account_ref"] = "expense_account_id"
        for original, imported in _pair(lines, targets, "line_id", "invoice_line"):
            _require(
                all(
                    isinstance(original[field], Decimal) and original[field] >= 0
                    for field in ("quantity", "unit_price", "line_amount", "tax_amount")
                )
                and original["quantity"] > 0,
                "source_invoice_line_amounts",
            )
            _match(original, imported, line_mapping, "invoice_line")
            _require(
                original["po_line_id"] is None and original["goods_receipt_line_id"] is None,
                "unsupported_procurement_link",
            )
            _require(imported["tax_account_ref"] is None, "unsupported_tax_account")
            dimensions = sorted(
                [[code, _id(original[col])] for code, col in DIMENSIONS[1:] if original[col] is not None]
            )
            actual = _json(imported["dimension_refs"])
            _require(
                isinstance(actual, list) and all(isinstance(pair, list) and len(pair) == 2 for pair in actual),
                "invoice_line_dimensions",
            )
            _require(sorted([[pair[0], _id(pair[1])] for pair in actual]) == dimensions, "invoice_line_dimensions")
        # Inclusive line prices are evidence; never recompute quantity * unit_price.
        _require(
            _sum([row["line_amount"] for row in lines]) == header["subtotal"]
            and _sum([row["tax_amount"] for row in lines]) == header["tax_amount"],
            "invoice_line_totals",
        )
        accounting = self._accounting(tenant, header, dest)
        # Only immutable imported fields enter the replay evidence. Current owner
        # invoice status, obligations, events and settlements deliberately do not.
        facts = _fingerprint(
            "erp.payables.historical-facts.v1",
            dict(
                manifest=self.manifest.as_dict(),
                source_header=_project(header, SOURCE_HEADER),
                source_supplier=_project(supplier, SOURCE_SUPPLIER),
                source_organization=_project(organization, SOURCE_ORGANIZATION),
                source_lines=_project_rows(lines, SOURCE_INVOICE_LINE, "line_id"),
                destination_header=_project(target, DESTINATION_HEADER),
                destination_lines=_project_rows(targets, DESTINATION_INVOICE_LINE, "id"),
            ),
        )
        return HistoricalUnpaidInvoiceEvidence(
            tenant_id=tenant,
            source_owner=self.source_owner,
            source_invoice_id=str(invoice_id),
            source_snapshot_sha256=self.manifest.snapshot_sha256,
            invoice_id=invoice_id,
            source_status=header["status"],
            invoice_type=header["invoice_type"],
            paid_amount=header["amount_paid"],
            allocation_count=len(allocations),
            withholding_tax_amount=header["withholding_tax_amount"],
            prepayment_amount=header["prepayment_applied"],
            number=target["number"],
            supplier_ref=target["supplier_ref"],
            supplier_name_snapshot=name,
            supplier_document_number=target["supplier_document_number"],
            invoice_date=_day(header["invoice_date"], "source_invoice_date"),
            received_date=_day(header["received_date"], "source_received_date"),
            due_date=due,
            currency_code=header["currency_code"],
            exchange_rate=header["exchange_rate"],
            subtotal=header["subtotal"],
            tax_amount=header["tax_amount"],
            total_amount=header["total_amount"],
            liability_account_ref=target["liability_account_ref"],
            procurement_ref=None,
            receipt_evidence_fingerprint=None,
            submitted_by=target["submitted_by"],
            approved_by=target["approved_by"],
            approval_reference=target["approval_reference"],
            submitted_at=_instant(header["submitted_at"], "source_submitted_at")
            if header["submitted_at"] is not None
            else None,
            approved_at=_instant(header["approved_at"], "source_approved_at")
            if header["approved_at"] is not None
            else None,
            original_posted_at=posted,
            source_journal_ref=_id(header["journal_entry_id"]),
            destination_journal_ref=_id(header["journal_entry_id"]),
            accounting_evidence_fingerprint=accounting,
            facts_fingerprint=facts,
        )

    def _accounting(self, tenant: UUID, header: dict, dest: sa.Connection) -> str:
        journal_id = header["journal_entry_id"]
        _require(journal_id is not None, "source_journal_missing")
        journal = _one(
            _read(self.source, "journal_entry", key="journal_entry_id", value=journal_id), "source_journal_missing"
        )
        _tenant([journal], tenant, "source_journal", "organization_id")
        _require(
            journal["source_module"] == "AP" and journal["source_document_type"] == "SUPPLIER_INVOICE",
            "source_journal_owner",
        )
        _require(_equal(journal["source_document_id"], header["invoice_id"]), "source_journal_invoice")
        _require(journal["status"] == "POSTED", "source_journal_status")
        _require(
            journal["is_reversal"] is False
            and journal["journal_type"] != "REVERSAL"
            and journal["reversed_journal_id"] is None
            and journal["reversal_journal_id"] is None,
            "source_journal_reversal",
        )
        _require(
            journal["currency_code"] == header["currency_code"] and journal["exchange_rate"] == 1,
            "unsupported_currency",
        )
        target = _one(
            _read(dest, "journal_entries", schema="mod_accounting", key="id", value=journal_id),
            "destination_journal_missing",
        )
        _tenant([target], tenant, "destination_journal")
        mapping = {
            key: key
            for key in (
                "status",
                "currency_code",
                "exchange_rate",
                "entry_date",
                "posting_date",
                "fiscal_period_id",
                "description",
                "reference",
                "total_debit",
                "total_credit",
                "total_debit_functional",
                "total_credit_functional",
                "posted_at",
                "created_at",
            )
        }
        mapping.update(
            number="journal_number",
            kind="journal_type",
            posted_by="posted_by_user_id",
            reverses_journal_id="reversed_journal_id",
            reversal_journal_id="reversal_journal_id",
            source_document_kind="source_document_type",
        )
        _match(journal, target, mapping, "journal")
        # waves36 deliberately uses the legacy journal row as destination document
        # identity, retaining the AP invoice link in source provenance.
        _require(
            target["source_document_id"] == f"legacy:public.journal_entry:{journal['journal_entry_id']}",
            "journal_source_document_id",
        )
        expected_posting_stamp = (
            f"legacy:public.journal_entry:{target['id']}@"
            f"{_day(journal['posting_date'], 'source_journal_posting_date').isoformat()}"
        )
        _require(target["approval_reference"] == expected_posting_stamp, "journal_posting_stamp")
        self._provenance(target, "journal")
        originals = _read(self.source, "journal_entry_line", key="journal_entry_id", value=journal_id)
        imported = _read(dest, "journal_lines", schema="mod_accounting", key="journal_id", value=journal_id)
        _tenant(imported, tenant, "journal_line")
        line_map = {key: key for key in ("line_number", "account_id", "description", "created_at")}
        line_map.update(
            journal_id="journal_entry_id",
            debit="debit_amount",
            credit="credit_amount",
            debit_functional="debit_amount_functional",
            credit_functional="credit_amount_functional",
        )
        dimensions = []
        for original, landed in _pair(originals, imported, "line_id", "journal_line"):
            _require(
                all(
                    isinstance(original[field], Decimal) and original[field] >= 0
                    for field in (
                        "debit_amount",
                        "credit_amount",
                        "debit_amount_functional",
                        "credit_amount_functional",
                    )
                )
                and (
                    (original["debit_amount"] > 0 and original["credit_amount"] == 0)
                    or (original["credit_amount"] > 0 and original["debit_amount"] == 0)
                ),
                "source_journal_line_amounts",
            )
            _match(original, landed, line_map, "journal_line")
            _require(
                original["currency_code"] in (None, header["currency_code"])
                and original["exchange_rate"] in (None, Decimal(1)),
                "unsupported_currency",
            )
            _require(
                original["debit_amount"] == original["debit_amount_functional"]
                and original["credit_amount"] == original["credit_amount_functional"],
                "unsupported_currency",
            )
            dimensions.append(self._dimensions(tenant, original, landed["id"], dest, ledger=False))
        for side in ("debit", "credit"):
            total = _sum([row[side + "_amount"] for row in originals])
            _require(
                total == journal["total_" + side] == journal["total_" + side + "_functional"], "source_journal_totals"
            )
        _require(journal["total_debit"] == journal["total_credit"], "source_journal_unbalanced")
        control = [row for row in originals if _equal(row["account_id"], header["ap_control_account_id"])]
        _require(
            bool(control)
            and all(row["debit_amount"] == 0 and row["credit_amount"] > 0 for row in control)
            and _sum([row["credit_amount"] for row in control]) == header["total_amount"],
            "source_ap_control_credit",
        )
        ledger = _read(self.source, "posted_ledger_line", key="journal_entry_id", value=journal_id)
        posted = _read(dest, "posted_ledger_lines", schema="mod_accounting", key="journal_id", value=journal_id)
        _tenant(ledger, tenant, "source_ledger", "organization_id")
        _tenant(posted, tenant, "ledger")
        _require(len(ledger) == len(originals), "source_ledger_line_count")
        by_line = {_id(row["line_id"]): row for row in originals}
        _require(len({_id(row["journal_line_id"]) for row in ledger}) == len(ledger), "source_ledger_line_duplicate")
        ledger_map = {
            key: key
            for key in (
                "journal_line_id",
                "account_id",
                "account_code",
                "fiscal_period_id",
                "entry_date",
                "posting_date",
                "description",
                "posted_at",
            )
        }
        ledger_map.update(
            journal_id="journal_entry_id",
            debit="debit_amount",
            credit="credit_amount",
            source_document_kind="source_document_type",
            source_document_id="source_document_id",
            posted_by="posted_by_user_id",
        )
        for original, landed in _pair(ledger, posted, "ledger_line_id", "ledger"):
            _match(original, landed, ledger_map, "ledger")
            self._provenance(landed, "ledger")
            _require(landed["journal_number"] == journal["journal_number"], "ledger_journal_number")
            _require(
                original["source_module"] == "AP"
                and original["source_document_type"] == "SUPPLIER_INVOICE"
                and _equal(original["source_document_id"], header["invoice_id"]),
                "source_ledger_invoice",
            )
            _require(
                original["original_currency_code"] in (None, header["currency_code"])
                and original["exchange_rate"] in (None, Decimal(1))
                and landed["currency_code"] == header["currency_code"]
                and landed["exchange_rate"] == 1,
                "unsupported_currency",
            )
            line = by_line.get(_id(original["journal_line_id"]))
            _require(line is not None and _equal(original["account_id"], line["account_id"]), "source_ledger_line")
            for side in ("debit", "credit"):
                value = original["original_" + side + "_amount"]
                value = original[side + "_amount"] if value is None else value
                _require(
                    value == original[side + "_amount"] == line[side + "_amount"]
                    and landed["original_" + side] == value,
                    "ledger_original_" + side,
                )
            dimensions.append(self._dimensions(tenant, original, landed["id"], dest, ledger=True))
        return _fingerprint(
            "erp.payables.historical-accounting.v1",
            dict(
                source_journal=_project(journal, SOURCE_JOURNAL),
                destination_journal=_project(target, DESTINATION_JOURNAL),
                source_lines=_project_rows(originals, SOURCE_JOURNAL_LINE, "line_id"),
                destination_lines=_project_rows(imported, DESTINATION_JOURNAL_LINE, "id"),
                source_ledger=_project_rows(ledger, SOURCE_LEDGER, "ledger_line_id"),
                destination_ledger=_project_rows(posted, DESTINATION_LEDGER, "id"),
                dimensions=dimensions,
            ),
        )

    def _provenance(self, row: dict, prefix: str) -> None:
        _require(row["source_owner"] == self.source_owner, prefix + "_source_owner")
        _require(row["source_version"] == self.manifest.candidate_revision, prefix + "_source_version")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", row["source_fingerprint"])), prefix + "_source_fingerprint")
        if "request_fingerprint" in row:
            _require(bool(re.fullmatch(r"[0-9a-f]{64}", row["request_fingerprint"])), prefix + "_request_fingerprint")

    def _dimensions(
        self, tenant: UUID, source: dict, line_id: object, dest: sa.Connection, *, ledger: bool
    ) -> list[dict]:
        table = "posted_ledger_dimensions" if ledger else "journal_line_dimensions"
        key = "ledger_line_id" if ledger else "journal_line_id"
        rows = _read(dest, table, schema="mod_accounting", key=key, value=line_id)
        _tenant(rows, tenant, "accounting_dimension")
        expected = {code: _id(source[col]) for code, col in DIMENSIONS if source[col] is not None}
        _require(len(expected) == len(rows), "accounting_dimensions_count")
        actual = {}
        frozen = []
        for row in sorted(rows, key=lambda item: (_id(item["dimension_id"]), _id(item["dimension_value_id"]))):
            definition = _one(
                _read(dest, "accounting_dimensions", schema="mod_accounting", key="id", value=row["dimension_id"]),
                "accounting_dimension_missing",
            )
            value = _one(
                _read(
                    dest,
                    "accounting_dimension_values",
                    schema="mod_accounting",
                    key="id",
                    value=row["dimension_value_id"],
                ),
                "accounting_dimension_value_missing",
            )
            _tenant([definition, value], tenant, "accounting_dimension")
            code = definition["code"]
            _require(
                code not in actual and _equal(value["dimension_id"], definition["id"]),
                "accounting_dimension_duplicate_or_parent",
            )
            actual[code] = _id(value["id"])
            if ledger:
                _require(
                    row["dimension_code"] == code and row["value_code"] == value["code"], "ledger_dimension_snapshot"
                )
            names = (
                "id tenant_id ledger_line_id dimension_id dimension_code dimension_value_id value_code".split()
                if ledger
                else "id tenant_id journal_line_id dimension_id dimension_value_id".split()
            )
            frozen.append(
                dict(
                    row=_project(row, names),
                    definition=_project(definition, ["id", "tenant_id", "code"]),
                    value=_project(value, ["id", "tenant_id", "dimension_id", "code"]),
                )
            )
        _require(actual == expected, "accounting_dimensions")
        return frozen


__all__ = ["HistoricalEvidenceError", "HistoricalPayablesVerifier"]
