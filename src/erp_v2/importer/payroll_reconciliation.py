"""SELECT-only historical payroll transfer evidence, never payroll recomputation.

Caller owns Core connections and transactions. Source tables resolve by unique
holder, as in the importer; no public/payroll schema equivalence is assumed.
Only tenant/currency aggregates and run-level findings leave this diagnostic.
Employee, slip, component-line identifiers, names and YTD values are never output.
Unattached source children cannot be tenant-attributed and are outside its scope.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa

from erp_v2.importer.enrolment import _pay_component_kind_by_legacy, _payroll_run_status_by_legacy
from erp_v2.importer.payables_reconciliation import _identity, _money, _reflect, _sum


@dataclass(frozen=True, order=True)
class Finding:
    kind: str
    tenant_id: str
    run_id: str
    field: str = ""


@dataclass(frozen=True)
class Aggregate:
    tenant_id: str
    currency_code: str
    source_runs: int
    target_runs: int
    source_slips: int
    target_calculations: int
    source_gross: Decimal | None
    source_deductions: Decimal | None
    source_net: Decimal | None
    source_net_outstanding: Decimal | None
    target_gross: Decimal | None
    target_deductions: Decimal | None
    target_net: Decimal | None
    target_liabilities: int
    obligation_evidence: str = "unknown_requires_owner_reconciliation"


@dataclass(frozen=True)
class PayrollReconciliation:
    tenant_extent: tuple[UUID, ...]
    counts: tuple[tuple[str, int], ...]
    aggregates: tuple[Aggregate, ...]
    findings: tuple[Finding, ...]

    @property
    def transfer_matches(self) -> bool:
        """Identity/amount evidence only; YTD and settlement gaps still block this."""
        return bool(dict(self.counts)["source_runs"]) and not self.findings


def _columns(table, names, money=()):
    for name in money:
        if isinstance(table.c[name].type, sa.Float):
            raise ValueError(f"unsupported_float_money_column:{table.fullname}.{name}")
    return [sa.cast(table.c[n], sa.Text).label(n) if n in money else table.c[n] for n in names]


def _scope(column, extent):
    # Native UUID binds stay typed. Reflected historical text/CHAR snapshots
    # store either dashed UUIDs or CHAR(32) hex; both denote the same scope.
    values = (
        extent
        if isinstance(column.type, sa.Uuid)
        else tuple(representation for value in extent for representation in (str(value), value.hex))
    )
    return column.in_(values)


def _rows(connection, table, names, money, tenant_column, extent):
    return list(
        connection.execute(
            sa.select(*_columns(table, names, money)).where(_scope(table.c[tenant_column], extent))
        ).mappings()
    )


def _index(rows, tenant, ident):
    result = {}
    for row in rows:
        key = (_identity(row[tenant]), _identity(row[ident]))
        if key in result:
            # Never include sensitive child identifiers in exception output.
            raise ValueError("duplicate_scoped_payroll_identity")
        result[key] = row
    return result


def reconcile_payroll(
    source: sa.Connection,
    destination: sa.Connection,
    *,
    tenant_extent: tuple[UUID, ...],
    source_schema: str | None = None,
) -> PayrollReconciliation:
    """Compare preserved run/slip/line evidence without writes or lifecycle calls.

    Missing rows are findings, never numeric zero evidence. Aggregate totals are
    None when a cohort is absent or any amount is invalid. Liability inventory
    cannot prove historical settlement, even when rows exist or residual is zero.
    YTD is counted as a preservation gap and is never selected or summed.
    Reflection uses read-only metadata queries (SQLite uses PRAGMA).
    """
    if (
        not isinstance(tenant_extent, tuple)
        or not tenant_extent
        or any(not isinstance(value, UUID) for value in tenant_extent)
    ):
        raise ValueError("tenant_extent must be a nonempty tuple of UUIDs")
    if not isinstance(source, sa.Connection) or not isinstance(destination, sa.Connection):
        raise TypeError("source and destination must be SQLAlchemy Core Connections")
    if source_schema is not None and (not isinstance(source_schema, str) or not source_schema):
        raise ValueError("source_schema must be a nonempty schema name or None")
    extent = tuple(sorted(set(tenant_extent), key=str))
    sh, ss, se, sd = (
        _reflect(source, name, source_schema)
        for name in ("payroll_entry", "salary_slip", "salary_slip_earning", "salary_slip_deduction")
    )
    sc = _reflect(source, "salary_component", source_schema)
    pc = _reflect(destination, "pay_components", "mod_payroll")
    th, tc, tl, obligations = (
        _reflect(destination, name, "mod_payroll")
        for name in ("payroll_runs", "payroll_calculations", "payroll_calculation_lines", "payroll_liabilities")
    )
    source_money = ("gross_pay", "total_deduction", "net_pay", "amount_paid")
    header_money = ("total_gross_pay", "total_deductions", "total_net_pay")
    target_money = ("gross_amount", "deduction_amount", "net_amount", "employer_liability_amount")
    headers = _rows(
        source,
        sh,
        (
            "organization_id",
            "entry_id",
            "entry_number",
            "start_date",
            "end_date",
            "currency_code",
            "status",
            "employee_count",
            *header_money,
        ),
        header_money,
        "organization_id",
        extent,
    )
    slips = _rows(
        source,
        ss,
        (
            "organization_id",
            "slip_id",
            "payroll_entry_id",
            "employee_id",
            "start_date",
            "end_date",
            "currency_code",
            *source_money,
        ),
        source_money,
        "organization_id",
        extent,
    )
    runs = _rows(
        destination,
        th,
        ("tenant_id", "id", "run_ref", "period_start", "period_end", "currency_code", "status"),
        (),
        "tenant_id",
        extent,
    )
    calculations = _rows(
        destination,
        tc,
        ("tenant_id", "id", "run_id", "employee_ref", *target_money),
        target_money,
        "tenant_id",
        extent,
    )
    lines = _rows(
        destination,
        tl,
        ("tenant_id", "id", "calculation_id", "component_id", "component_code", "component_kind", "sequence", "amount"),
        ("amount",),
        "tenant_id",
        extent,
    )
    liabilities = _rows(
        destination,
        obligations,
        ("tenant_id", "id", "run_id", "calculation_id", "currency_code"),
        (),
        "tenant_id",
        extent,
    )
    source_components = _index(
        _rows(
            source,
            sc,
            ("organization_id", "component_id", "component_code", "component_type"),
            (),
            "organization_id",
            extent,
        ),
        "organization_id",
        "component_id",
    )
    target_components = _index(
        _rows(destination, pc, ("tenant_id", "id", "component_code", "kind"), (), "tenant_id", extent),
        "tenant_id",
        "id",
    )
    component_kinds = _pay_component_kind_by_legacy(source, destination)
    # A tenant-filtered parent subquery bounds source children before their join.
    scoped_slips = (
        sa.select(
            ss.c.slip_id,
            ss.c.organization_id,
            ss.c.payroll_entry_id,
        )
        .where(_scope(ss.c.organization_id, extent))
        .subquery()
    )
    source_lines = []
    source_flags = ("statistical_component", "do_not_include_in_total")
    for table, origin in ((se, "earning"), (sd, "deduction")):
        # Reject approximate YTD storage, but do not retrieve cumulative values.
        _columns(table, (), ("amount", "year_to_date"))
        statement = sa.select(
            scoped_slips.c.organization_id,
            scoped_slips.c.payroll_entry_id,
            *_columns(
                table, ("line_id", "slip_id", "component_id", "display_order", "amount", *source_flags), ("amount",)
            ),
        ).select_from(scoped_slips.join(table, table.c.slip_id == scoped_slips.c.slip_id))
        source_lines.extend((origin, row) for row in source.execute(statement).mappings())

    source_map = _index(headers, "organization_id", "entry_id")
    target_map = _index(runs, "tenant_id", "id")
    slip_map = _index(slips, "organization_id", "slip_id")
    calculation_map = _index(calculations, "tenant_id", "id")
    line_map = _index(lines, "tenant_id", "id")
    findings = []
    counts = Counter(
        source_runs=len(headers),
        target_runs=len(runs),
        source_slips=len(slips),
        target_calculations=len(calculations),
        target_lines=len(lines),
        target_liabilities=len(liabilities),
    )
    groups = defaultdict(lambda: defaultdict(list))
    group_counts = defaultdict(Counter)
    source_by_run = defaultdict(list)
    status_map = _payroll_run_status_by_legacy(source, destination)

    def finding(kind, key, field=""):
        findings.append(Finding(kind, key[0], key[1], field))

    def amount(row, field, key, origin):
        try:
            return _money(row[field])
        except ValueError:
            finding(f"invalid_{origin}_money", key, field)
            return None

    def run_key(row, tenant, run):
        return (_identity(row[tenant]), _identity(row[run]))

    for origin, rows, tenant, ident in (
        ("source", headers, "organization_id", "entry_id"),
        ("target", runs, "tenant_id", "id"),
    ):
        for row in rows:
            key = run_key(row, tenant, ident)
            group_counts[(key[0], str(row["currency_code"]))][f"{origin}_runs"] += 1
            if origin == "target" and key not in source_map:
                finding("extra_target_run", key)
    for key, row in source_map.items():
        target = target_map.get(key)
        expected_status = status_map.get(str(row["status"]))
        if expected_status is None:
            finding("unmapped_source_run_status", key)
        if target is None:
            finding("missing_target_run", key)
        else:
            counts["matched_runs"] += 1
            for source_field, target_field in (
                ("entry_number", "run_ref"),
                ("start_date", "period_start"),
                ("end_date", "period_end"),
                ("currency_code", "currency_code"),
            ):
                if row[source_field] != target[target_field]:
                    finding("run_field_mismatch", key, target_field)
            if expected_status is not None and str(target["status"]) != expected_status:
                finding("run_field_mismatch", key, "status")
        finding("historical_obligation_evidence_unknown", key)

    for key, row in slip_map.items():
        rkey = run_key(row, "organization_id", "payroll_entry_id")
        source_by_run[rkey].append(row)
        group = (key[0], str(row["currency_code"]))
        group_counts[group]["source_slips"] += 1
        header = source_map.get(rkey)
        if header is None:
            finding("orphan_source_slip", rkey)
        else:
            for field in ("start_date", "end_date", "currency_code"):
                if row[field] != header[field]:
                    finding("source_slip_run_mismatch", rkey, field)
        values = {field: amount(row, field, rkey, "source") for field in source_money}
        for label, field in zip(("gross", "deductions", "net"), source_money[:3], strict=True):
            groups[group][f"source_{label}"].append(values[field])
        net, paid = values["net_pay"], values["amount_paid"]
        groups[group]["source_net_outstanding"].append(
            _sum([net, paid.copy_negate()]) if net is not None and paid is not None else None
        )
        gross, deduction = values["gross_pay"], values["total_deduction"]
        if None not in (gross, deduction, net) and _sum([gross, deduction.copy_negate()]) != net:
            finding("source_slip_arithmetic_mismatch", rkey)
        target = calculation_map.get(key)
        if target is None:
            finding("missing_target_calculation", rkey)
        else:
            counts["matched_calculations"] += 1
            if (
                row["employee_id"] is None
                or target["employee_ref"] is None
                or _identity(row["employee_id"]) != _identity(target["employee_ref"])
            ):
                finding("employee_identity_mismatch", rkey)
            if _identity(target["run_id"]) != rkey[1]:
                finding("calculation_run_mismatch", rkey)
            for source_field, target_field in zip(source_money[:3], target_money[:3], strict=True):
                other = amount(target, target_field, rkey, "target")
                if values[source_field] is not None and other is not None and values[source_field] != other:
                    finding("calculation_amount_mismatch", rkey, target_field)
    for key, row in calculation_map.items():
        rkey = run_key(row, "tenant_id", "run_id")
        header = target_map.get(rkey)
        group = (key[0], str(header["currency_code"]) if header else "<missing_run>")
        group_counts[group]["target_calculations"] += 1
        if key not in slip_map:
            finding("extra_target_calculation", rkey)
        if header is None:
            finding("orphan_target_calculation", rkey)
        for label, field in zip(("gross", "deductions", "net"), target_money[:3], strict=True):
            groups[group][f"target_{label}"].append(amount(row, field, rkey, "target"))
    for key, header in source_map.items():
        header_values = [amount(header, field, key, "source_header") for field in header_money]
        if None not in header_values:
            gross, deductions, net = header_values
            if _sum([gross, deductions.copy_negate()]) != net:
                finding("source_run_arithmetic_mismatch", key)
        related = source_by_run[key]
        if not related:
            finding("source_run_without_slips", key)
        if header["employee_count"] != len(related):
            finding("source_run_slip_count_mismatch", key)
        for slip_field, header_field in zip(source_money[:3], header_money, strict=True):
            total = amount(header, header_field, key, "source_header")
            values = [amount(row, slip_field, key, "source") for row in related]
            if values and None not in values and total is not None and total != _sum(values):
                finding("source_run_total_mismatch", key, header_field)

    seen_source_lines = set()
    for origin, row in source_lines:
        key = run_key(row, "organization_id", "payroll_entry_id")
        lkey = run_key(row, "organization_id", "line_id")
        if lkey in seen_source_lines:
            raise ValueError("duplicate_scoped_payroll_line_identity")
        seen_source_lines.add(lkey)
        counts[f"source_{origin}_lines"] += 1
        # The target has no equivalent exclusion flags. Retain the gap rather
        # than blindly summing legacy children into source slip totals.
        for flag in source_flags:
            if row[flag] is not False:
                finding("source_line_flag_preservation_gap", key, flag)
        # Every stored snapshot has nowhere to land, including zero/NULL YTD.
        finding("ytd_preservation_gap", key, origin)
        target = line_map.get(lkey)
        value = amount(row, "amount", key, "source_line")
        if target is None:
            finding("missing_target_line", key, origin)
            continue
        counts[f"matched_{origin}_lines"] += 1
        for source_field, target_field in (
            ("slip_id", "calculation_id"),
            ("component_id", "component_id"),
            ("display_order", "sequence"),
        ):
            if _identity(row[source_field]) != _identity(target[target_field]):
                finding("line_field_mismatch", key, target_field)
        source_component = source_components.get((key[0], _identity(row["component_id"])))
        target_component = target_components.get((key[0], _identity(target["component_id"])))
        if source_component is None:
            finding("missing_source_component_evidence", key, origin)
        if target_component is None:
            finding("missing_target_component_evidence", key, origin)
        if source_component is not None:
            expected_kind = component_kinds.get(str(source_component["component_type"]))
            if expected_kind is None:
                finding("unmapped_source_component_kind", key, origin)
            for field, expected in (
                ("component_code", source_component["component_code"]),
                ("component_kind", expected_kind),
            ):
                if expected is not None and target[field] != expected:
                    finding("line_field_mismatch", key, field)
            if target_component is not None:
                if source_component["component_code"] != target_component["component_code"]:
                    finding("component_field_mismatch", key, "component_code")
                if expected_kind is not None and target_component["kind"] != expected_kind:
                    finding("component_field_mismatch", key, "kind")
        if target_component is not None:
            for field, component_field in (("component_code", "component_code"), ("component_kind", "kind")):
                if target[field] != target_component[component_field]:
                    finding("line_component_snapshot_mismatch", key, field)
        other = amount(target, "amount", key, "target_line")
        if value is not None and other is not None and value != other:
            finding("line_amount_mismatch", key, origin)
    for key, row in line_map.items():
        parent = calculation_map.get((key[0], _identity(row["calculation_id"])))
        rkey = (key[0], _identity(parent["run_id"]) if parent else "<missing_run>")
        if key not in seen_source_lines:
            finding("extra_target_line", rkey)
            amount(row, "amount", rkey, "target_line")
        if parent is None:
            finding("orphan_target_line", rkey)
    # Check stored target evidence, never recalculate payroll/tax. Include empty
    # kind cohorts explicitly; a zero sum does not erase missing-line findings.
    lines_by_calculation = defaultdict(list)
    for row in lines:
        lines_by_calculation[(_identity(row["tenant_id"]), _identity(row["calculation_id"]))].append(row)
    for key, calculation in calculation_map.items():
        rkey = (key[0], _identity(calculation["run_id"]))
        related = lines_by_calculation[key]
        if not related:
            finding("target_calculation_without_lines", rkey)
        stored = {field: amount(calculation, field, rkey, "target") for field in target_money}
        gross, deduction, net = (stored[field] for field in target_money[:3])
        if None not in (gross, deduction, net) and _sum([gross, deduction.copy_negate()]) != net:
            finding("target_calculation_arithmetic_mismatch", rkey)
        for kind, field in (
            ("earning", "gross_amount"),
            ("deduction", "deduction_amount"),
            ("employer_liability", "employer_liability_amount"),
        ):
            values = [amount(line, "amount", rkey, "target_line") for line in related if line["component_kind"] == kind]
            if None not in values and stored[field] is not None and _sum(values) != stored[field]:
                finding("target_line_total_mismatch", rkey, field)
        for line in related:
            if line["component_kind"] not in ("earning", "deduction", "employer_liability", "information"):
                finding("unknown_target_line_kind", rkey)
    liability_runs = set()
    for row in liabilities:
        key = run_key(row, "tenant_id", "run_id")
        liability_runs.add(key)
        group_counts[(key[0], str(row["currency_code"]))]["target_liabilities"] += 1
        parent = calculation_map.get((key[0], _identity(row["calculation_id"])))
        if key not in target_map or parent is None or _identity(parent["run_id"]) != key[1]:
            finding("orphan_or_mismatched_target_liability", key)
        if key in target_map and row["currency_code"] != target_map[key]["currency_code"]:
            finding("liability_currency_mismatch", key)
    for key in source_map:
        if key not in liability_runs:
            finding("missing_target_liability_evidence", key)

    def total(group, field):
        values = groups[group][field]
        return _sum(values) if values and None not in values else None

    aggregates = tuple(
        Aggregate(
            *group,
            *(
                group_counts[group][name]
                for name in ("source_runs", "target_runs", "source_slips", "target_calculations")
            ),
            *(
                total(group, name)
                for name in (
                    "source_gross",
                    "source_deductions",
                    "source_net",
                    "source_net_outstanding",
                    "target_gross",
                    "target_deductions",
                    "target_net",
                )
            ),
            group_counts[group]["target_liabilities"],
        )
        for group in sorted(set(groups) | set(group_counts))
    )
    for name in (
        "matched_runs",
        "matched_calculations",
        "source_earning_lines",
        "source_deduction_lines",
        "matched_earning_lines",
        "matched_deduction_lines",
    ):
        counts.setdefault(name, 0)
    counts.update(f.kind for f in findings)
    return PayrollReconciliation(extent, tuple(sorted(counts.items())), aggregates, tuple(sorted(findings)))
