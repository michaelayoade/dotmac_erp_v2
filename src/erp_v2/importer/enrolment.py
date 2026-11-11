"""Enrolment maps: builders for every id-map the specs declare.

Transform specs reference maps by name in two places (see spec.py):

- `fk_rewrites`: scalar value -> value (e.g. a legacy structure id
  becomes the generated revision id);
- `lookups`: key -> {field: value} (module tables that denormalize
  parent facts, e.g. component code/name/kind on payroll rules).

This module builds each declared map from its authoritative side:

- **static** value tables ruled in the pairing notes;
- **source** maps read from the legacy snapshot (identity links and
  parent rows whose UUIDs are preserved by the import);
- **destination** maps read from already-imported or enrolment-minted
  module rows. Builders only READ: minting the generated rows
  (pay-structure revisions, tax authorities, sourcing lines) is a
  separate enrolment action that must run before the specs that
  consume these maps. Until then the map is empty and dependent
  rows exception - loudly, in the receipt.

Rules: a key with two different values is a MapConflict, never a
silent pick. UUID keys/values are normalized to the 32-char hex form
module CHAR(32) ids use, so Postgres UUID objects and hex strings
meet in the middle (executor.py tries the same candidate forms).
Maps are keyed by globally-unique UUIDs, so they are tenant-extent
agnostic; the tenant extent still bounds which source rows import.
"""

from __future__ import annotations

import json
import posixpath
import re
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import sqlalchemy as sa

from erp_v2.importer.minting import MAP_MINTS, MintContext, MintResult, run_mint_step
from erp_v2.importer.payroll_formula import convert_rule
from erp_v2.importer.spec import TransformSpec


class MapConflict(ValueError):
    """One enrolment-map key produced two different values."""


def _norm(value: Any) -> Any:
    """UUID objects -> 32-char hex (the module id form); else unchanged."""

    return value.hex if isinstance(value, uuid.UUID) else value


def _put(mapping: dict[Any, Any], map_name: str, key: Any, value: Any) -> None:
    key = _norm(key)
    if key is None:
        return
    existing = mapping.get(key)
    if existing is not None and existing != value:
        raise MapConflict(f"enrolment map {map_name!r}: key {key!r} maps to both {existing!r} and {value!r}")
    mapping[key] = value


def _scalar(rows: Any, map_name: str) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key, value in rows:
        _put(mapping, map_name, key, _norm(value))
    return mapping


def _lookup(rows: Any, map_name: str, fields: Sequence[str]) -> dict[Any, dict[str, Any]]:
    mapping: dict[Any, dict[str, Any]] = {}
    for row in rows:
        entry = {field: _norm(row[field]) for field in fields}
        _put(mapping, map_name, row["key"], entry)
    return mapping


# --- static -----------------------------------------------------------------


def _attendance_status_by_legacy(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # e2 AttendanceStatus is lowercase; legacy HR enum is uppercase.
    # Closed literal mapping over both spellings.
    return {
        "PRESENT": "present",
        "ABSENT": "absent",
        "HALF_DAY": "half_day",
        "ON_LEAVE": "on_leave",
        "HOLIDAY": "holiday",
        "WEEKLY_OFF": "weekly_off",
        "present": "present",
        "absent": "absent",
        "half_day": "half_day",
        "on_leave": "on_leave",
        "holiday": "holiday",
        "weekly_off": "weekly_off",
    }


def _form_version_status_by_legacy(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # mod_forms CHECK: draft/published/archived; legacy enum is
    # uppercase. Closed literal mapping over both spellings.
    return {
        "DRAFT": "draft",
        "PUBLISHED": "published",
        "ARCHIVED": "archived",
        "draft": "draft",
        "published": "published",
        "archived": "archived",
    }


def _legacy_lowercase_status(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Closed token set: e2 HR enums are lowercase, legacy HR enums
    # are uppercase. Only tokens that are valid destination values
    # in the columns using this map are listed; anything else
    # (e.g. disciplinary MODERATE/CRITICAL, which e2 does not
    # model) exceptions by name.
    tokens = (
        "DRAFT",
        "SUBMITTED",
        "APPROVED",
        "REJECTED",
        "CANCELLED",
        "IN_PROGRESS",
        "COMPLETED",
        "OPEN",
        "CLOSED",
        "MINOR",
        "MAJOR",
        "GROSS",
        "FINALIZED",
    )
    mapping: dict[Any, Any] = {}
    for token in tokens:
        mapping[token] = token.lower()
        mapping[token.lower()] = token.lower()
    return mapping


def _payroll_run_status_by_legacy(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # mod_payroll run statuses: draft/approved/finalized/cancelled.
    # Legacy POSTED runs are locked with slips + GL evidence, which
    # is the module's finalized. SUBMITTED/SLIPS_CREATED have no
    # exact module state - unmapped, named exceptions (2 live runs).
    return {
        "POSTED": "finalized",
        "FINALIZED": "finalized",
        "APPROVED": "approved",
        "DRAFT": "draft",
        "CANCELLED": "cancelled",
        "draft": "draft",
        "approved": "approved",
        "finalized": "finalized",
        "cancelled": "cancelled",
    }


def _account_class_by_legacy(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Module CHECK uses singular IFRS classes; legacy GL categories
    # are plural. Literal closed mapping (live values verified on
    # the production snapshot at Gate 4).
    return {
        "ASSETS": "ASSET",
        "ASSET": "ASSET",
        "LIABILITIES": "LIABILITY",
        "LIABILITY": "LIABILITY",
        "EQUITY": "EQUITY",
        "REVENUE": "REVENUE",
        "EXPENSES": "EXPENSE",
        "EXPENSE": "EXPENSE",
        "OTHER_COMPREHENSIVE_INCOME": "OTHER_COMPREHENSIVE_INCOME",
    }


def _pay_component_kind_by_legacy(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Module CHECK: earning/deduction/employer_liability/information.
    # Legacy payroll component types are uppercase singular.
    return {
        "EARNING": "earning",
        "DEDUCTION": "deduction",
        "EMPLOYER_LIABILITY": "employer_liability",
        "INFORMATION": "information",
        "earning": "earning",
        "deduction": "deduction",
        "employer_liability": "employer_liability",
        "information": "information",
    }


def _workflow_status_by_legacy(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Module CHECK: pending/running/succeeded/failed/cancelled.
    # Legacy automation executions record SUCCESS/FAILED.
    return {
        "SUCCESS": "succeeded",
        "FAILED": "failed",
        "PENDING": "pending",
        "RUNNING": "running",
        "CANCELLED": "cancelled",
        "pending": "pending",
        "running": "running",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }


def _bank_direction_by_type(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Module check constraint: direction IN ('credit','debit'). Only the
    # literal legacy spellings are ruled; anything else exceptions.
    return {"CREDIT": "credit", "DEBIT": "debit", "credit": "credit", "debit": "debit"}


# --- source (legacy snapshot) -----------------------------------------------


def _employee_party(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Legacy employee.person_id IS the kernel Party UUID (preserved).
    rows = source.execute(sa.text("SELECT employee_id, person_id FROM employee"))
    return _scalar(rows, "employee_party")


def _user_party(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Legacy user_credentials.id is the user UUID created_by columns
    # reference; person_id is the kernel Party UUID (preserved).
    rows = source.execute(sa.text("SELECT id, person_id FROM user_credentials"))
    return _scalar(rows, "user_party")


def _supplier(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text("SELECT supplier_id AS key, COALESCE(NULLIF(legal_name, ''), trading_name) AS name FROM supplier")
    ).mappings()
    return _lookup(rows, "supplier", ("name",))


def _project_template_task(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text("SELECT template_task_id AS key, template_id AS template_id FROM project_template_task")
    ).mappings()
    return _lookup(rows, "project_template_task", ("template_id",))


def _bid_submission(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    # UUIDs are preserved, so the legacy rfq_id IS the module event id.
    rows = source.execute(sa.text("SELECT response_id AS key, rfq_id AS event_id FROM quotation_response")).mappings()
    return _lookup(rows, "bid_submission", ("event_id",))


# --- destination (imported / enrolment-minted module rows) -----------------


def _structure_revision(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Enrolment mints one published v1 revision per structure; the
    # import binds to v1 by ruling (later versions are post-flip facts).
    rows = dest.execute(sa.text("SELECT structure_id, id FROM mod_payroll.pay_structure_revisions WHERE version = 1"))
    return _scalar(rows, "structure_revision")


def _tax_authority_by_level(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Enrolment mints one authority per legacy jurisdiction level with
    # authority_level_code carrying the legacy level value.
    rows = dest.execute(sa.text("SELECT authority_level_code, id FROM mod_tax.tax_authorities"))
    return _scalar(rows, "tax_authority_by_level")


def _sourcing_line_by_requisition_line(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    rows = dest.execute(
        sa.text(
            "SELECT source_requisition_line_id, id FROM mod_procurement.sourcing_event_lines "
            "WHERE source_requisition_line_id IS NOT NULL"
        )
    )
    return _scalar(rows, "sourcing_line_by_requisition_line")


def _bank_institution_by_account(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Legacy accounts reference institutions by free bank_code
    # (CBN sort code for traditional banks, placeholder text for
    # fintechs) plus bank_name. Resolve per account: exact code
    # match first, then case-folded name match (the mint step
    # enrols unmatched names before this map builds).
    by_code: dict[str, Any] = {}
    by_name: dict[str, Any] = {}
    for code, name, inst_id in dest.execute(sa.text("SELECT code, name, id FROM mod_banking.bank_institutions")):
        by_code[str(code)] = _norm(inst_id)
        by_name[str(name).casefold()] = _norm(inst_id)
    mapping: dict[Any, Any] = {}
    rows = source.execute(sa.text("SELECT bank_account_id, bank_name, bank_code FROM bank_accounts")).mappings()
    for row in rows:
        code = str(row["bank_code"] or "").strip()
        name = str(row["bank_name"] or "").strip()
        inst_id = by_code.get(code) if code else None
        if inst_id is None and name:
            inst_id = by_name.get(name.casefold())
        if inst_id is not None:
            _put(mapping, "bank_institution_by_account", row["bank_account_id"], inst_id)
    return mapping


def _journal_line(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Ledger -> entry-line identity (UUIDs preserved), plus
    # phantom resolution: some legacy postings (payroll JE0001/
    # JE0002; 9 rows in production data) wrote ledger rows whose
    # journal_line_id never existed as an entry line, so the FK
    # guard dropped the ledger evidence and the journals could
    # not post. A phantom resolves ONLY to the unique entry line
    # in the same journal with the same account and amounts;
    # ambiguous or unmatched phantoms stay unmapped and the
    # executor names them as exceptions.
    mapping: dict[Any, Any] = {}
    by_fact: dict[tuple[Any, ...], list[Any]] = {}
    lines = source.execute(
        sa.text("SELECT line_id, journal_entry_id, account_id, debit_amount, credit_amount FROM journal_entry_line")
    ).mappings()
    for row in lines:
        line_id = _norm(row["line_id"])
        _put(mapping, "journal_line", line_id, line_id)
        fact = (
            _norm(row["journal_entry_id"]),
            _norm(row["account_id"]),
            row["debit_amount"],
            row["credit_amount"],
        )
        by_fact.setdefault(fact, []).append(line_id)
    candidates: dict[Any, set[Any]] = {}
    ledger_refs = source.execute(
        sa.text(
            "SELECT journal_line_id, journal_entry_id, account_id, debit_amount, credit_amount FROM posted_ledger_line"
        )
    ).mappings()
    for row in ledger_refs:
        ref = _norm(row["journal_line_id"])
        if ref is None or ref in mapping:
            continue
        fact = (
            _norm(row["journal_entry_id"]),
            _norm(row["account_id"]),
            row["debit_amount"],
            row["credit_amount"],
        )
        candidates.setdefault(ref, set()).update(by_fact.get(fact, ()))
    for ref, found in candidates.items():
        if len(found) == 1:
            _put(mapping, "journal_line", ref, next(iter(found)))
    return mapping


def _journal_number_by_entry(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    # Posted ledger lines denormalize the entry's journal number.
    # The ledger's own journal_reference is free text (NULL on 16k
    # rows, up to 100 chars - not the number); the entry row is the
    # authority. Dest-side read: entries already imported.
    rows = dest.execute(
        sa.text("SELECT id AS key, number AS journal_number FROM mod_accounting.journal_entries")
    ).mappings()
    return _lookup(rows, "journal_number_by_entry", ("journal_number",))


def _fiscal_period(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = dest.execute(sa.text("SELECT id AS key, end_date AS end_date FROM mod_accounting.fiscal_periods")).mappings()
    return _lookup(rows, "fiscal_period", ("end_date",))


def _inventory_item(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = dest.execute(sa.text("SELECT id AS key, base_uom AS base_uom FROM mod_inventory.items")).mappings()
    return _lookup(rows, "inventory_item", ("base_uom",))


def _pay_component(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    # code/name/kind from the IMPORTED components (module vocabulary
    # wins); prorates from the legacy depends_on_payment_days flag,
    # which the module stores on rules, not on components.
    prorates = {
        _norm(key): bool(flag)
        for key, flag in source.execute(sa.text("SELECT component_id, depends_on_payment_days FROM salary_component"))
    }
    mapping: dict[Any, dict[str, Any]] = {}
    rows = dest.execute(
        sa.text("SELECT id, component_code AS code, name, kind FROM mod_payroll.pay_components")
    ).mappings()
    for row in rows:
        key = _norm(row["id"])
        if key not in prorates:
            raise MapConflict(
                f"enrolment map 'pay_component': imported component {key!r} has no legacy prorates source"
            )
        _put(
            mapping,
            "pay_component",
            key,
            {"code": row["code"], "name": row["name"], "kind": row["kind"], "prorates": prorates[key]},
        )
    return mapping


# --- enrolment computations (derived values, never copied columns) --------


def _known_abbrs(source: sa.Connection) -> set[str]:
    return {str(r[0]) for r in source.execute(sa.text("SELECT abbr FROM salary_component WHERE abbr IS NOT NULL"))}


def _convert_row(row: Mapping[str, Any], abbrs: set[str]):
    return convert_rule(
        amount=row["amount"],
        amount_based_on_formula=row["amount_based_on_formula"],
        formula=row["formula"],
        condition=row["condition"],
        known_abbrs=abbrs,
    )


def _rule_conversions(source: sa.Connection) -> dict[tuple[Any, Any], Any]:
    """(structure_id, component_id) -> RuleConversion for line methods.

    Ambiguous keys (same component twice in one structure with
    different conversions) are dropped - lines then exception.
    """

    abbrs = _known_abbrs(source)
    conversions: dict[tuple[Any, Any], Any] = {}
    ambiguous: set[tuple[Any, Any]] = set()
    for table in ("salary_structure_earning", "salary_structure_deduction"):
        rows = source.execute(
            sa.text(
                f"SELECT structure_id, component_id, amount, amount_based_on_formula, formula, condition "  # noqa: S608
                f"FROM {table}"
            )
        ).mappings()
        for row in rows:
            conversion = _convert_row(row, abbrs)
            if conversion is None:
                continue
            key = (_norm(row["structure_id"]), _norm(row["component_id"]))
            if key in conversions and conversions[key] != conversion:
                ambiguous.add(key)
            conversions[key] = conversion
    return {k: v for k, v in conversions.items() if k not in ambiguous}


def _payroll_rule_conversion(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """Per legacy rule row: typed method, rate, and fixed amount (fixed only).

    Unconvertible formulas produce NO entry - the executor then
    exception-queues the rule for a human ruling (see payroll_formula).
    """

    abbrs = _known_abbrs(source)
    mapping: dict[Any, dict[str, Any]] = {}
    for table, pk in (("salary_structure_earning", "earning_id"), ("salary_structure_deduction", "deduction_id")):
        rows = source.execute(
            sa.text(
                f"SELECT {pk} AS key, amount, amount_based_on_formula, formula, condition FROM {table}"  # noqa: S608
            )
        ).mappings()
        for row in rows:
            conversion = _convert_row(row, abbrs)
            if conversion is None:
                continue
            _put(
                mapping,
                "payroll_rule_conversion",
                row["key"],
                {
                    "calculation_method": conversion.method,
                    "rate": conversion.rate,
                    "fixed_amount": row["amount"] if conversion.method == "fixed" else None,
                },
            )
    return mapping


def _payroll_line_method(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    conversions = _rule_conversions(source)
    slip_structure = {
        _norm(r[0]): _norm(r[1]) for r in source.execute(sa.text("SELECT slip_id, structure_id FROM salary_slip"))
    }
    mapping: dict[Any, dict[str, Any]] = {}
    for table in ("salary_slip_earning", "salary_slip_deduction"):
        rows = source.execute(
            sa.text(f"SELECT line_id AS key, slip_id, component_id FROM {table}")  # noqa: S608
        ).mappings()
        for row in rows:
            conversion = conversions.get((slip_structure.get(_norm(row["slip_id"])), _norm(row["component_id"])))
            if conversion is None:
                continue
            _put(mapping, "payroll_line_method", row["key"], {"calculation_method": conversion.method})
    return mapping


def _same_id(a: object, b: object) -> bool:
    return str(a).replace("-", "") == str(b).replace("-", "")


def _payroll_calculation(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """Per legacy slip: assignment, proration factor, employer liability.

    Assignment resolution is exact-match only (employee + structure
    revision + slip start inside the effective window); ambiguous or
    unmatched slips get no entry and exception at execution.
    """

    revision_structure = {
        _norm(r[0]): _norm(r[1])
        for r in dest.execute(sa.text("SELECT id, structure_id FROM mod_payroll.pay_structure_revisions"))
    }
    assignments = list(
        dest.execute(
            sa.text(
                "SELECT id, employee_ref, revision_id, effective_from, effective_to "
                "FROM mod_payroll.employee_pay_assignments"
            )
        ).mappings()
    )
    employer_components = {
        _norm(r[0])
        for r in dest.execute(sa.text("SELECT id FROM mod_payroll.pay_components WHERE kind = 'employer_liability'"))
    }
    employer_sums: dict[Any, Decimal] = {}
    for table in ("salary_slip_earning", "salary_slip_deduction"):
        for slip_id, component_id, amount in source.execute(
            sa.text(f"SELECT slip_id, component_id, amount FROM {table}")  # noqa: S608
        ):
            if _norm(component_id) in employer_components and amount is not None:
                key = _norm(slip_id)
                employer_sums[key] = employer_sums.get(key, Decimal(0)) + Decimal(str(amount))
    mapping: dict[Any, dict[str, Any]] = {}
    slips = source.execute(
        sa.text(
            "SELECT slip_id, employee_id, structure_id, start_date, payment_days, total_working_days FROM salary_slip"
        )
    ).mappings()
    for slip in slips:
        try:
            working = Decimal(str(slip["total_working_days"]))
            factor = Decimal(str(slip["payment_days"])) / working if working else None
        except (InvalidOperation, TypeError, ValueError):
            factor = None
        if factor is None:
            continue
        start = str(slip["start_date"])[:10]
        candidates = [
            a
            for a in assignments
            if _same_id(a["employee_ref"], slip["employee_id"])
            and _same_id(revision_structure.get(_norm(a["revision_id"])), slip["structure_id"])
            and str(a["effective_from"])[:10] <= start
            and (a["effective_to"] is None or start <= str(a["effective_to"])[:10])
        ]
        if len(candidates) != 1:
            continue
        slip_key = _norm(slip["slip_id"])
        _put(
            mapping,
            "payroll_calculation",
            slip_key,
            {
                "assignment_id": _norm(candidates[0]["id"]),
                "proration_factor": factor,
                "employer_liability_amount": employer_sums.get(slip_key, Decimal(0)),
            },
        )
    return mapping


def _supplier_payment_schedule(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """Single-instalment schedule JSON (module shape, sums to the total)."""

    mapping: dict[Any, dict[str, Any]] = {}
    rows = source.execute(sa.text("SELECT invoice_id AS key, due_date, total_amount FROM supplier_invoice")).mappings()
    for row in rows:
        if row["due_date"] is None or row["total_amount"] is None:
            continue
        schedule = [{"due_date": str(row["due_date"])[:10], "amount": format(Decimal(str(row["total_amount"])), "f")}]
        _put(mapping, "supplier_payment_schedule", row["key"], {"payment_schedule": json.dumps(schedule)})
    return mapping


def _supplier_line_dimensions(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """dimension_refs JSON: sorted [CODE, value] pairs (module shape).

    Codes COST_CENTER/PROJECT/SEGMENT are the declared enrolment
    dimension codes; the rehearsal dimension enrolment must define
    them (accounting_dimensions) for reporting to join up.
    """

    mapping: dict[Any, dict[str, Any]] = {}
    rows = source.execute(
        sa.text("SELECT line_id AS key, cost_center_id, project_id, segment_id FROM supplier_invoice_line")
    ).mappings()
    for row in rows:
        refs = sorted(
            [code, str(_norm(row[col]))]
            for code, col in (("COST_CENTER", "cost_center_id"), ("PROJECT", "project_id"), ("SEGMENT", "segment_id"))
            if row[col] is not None
        )
        _put(mapping, "supplier_line_dimensions", row["key"], {"dimension_refs": json.dumps(refs)})
    return mapping


def _receipt_lines(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """lines_json in the module's canonical form (sorted-keys compact JSON
    of [{order_line_id, quantity_received}], dashed UUID spellings)."""

    grouped: dict[Any, list[dict[str, str]]] = {}
    rows = source.execute(
        sa.text(
            "SELECT receipt_id, po_line_id, quantity_received, line_number FROM goods_receipt_line ORDER BY line_number"
        )
    ).mappings()
    for row in rows:
        try:
            quantity = Decimal(str(row["quantity_received"]))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if quantity <= 0 or row["po_line_id"] is None:
            continue
        try:
            order_line = str(uuid.UUID(str(_norm(row["po_line_id"]))))
        except ValueError:
            order_line = str(row["po_line_id"])
        grouped.setdefault(_norm(row["receipt_id"]), []).append(
            {"order_line_id": order_line, "quantity_received": format(quantity.normalize(), "f")}
        )
    return {
        key: {"lines_json": json.dumps(lines, sort_keys=True, separators=(",", ":"))} for key, lines in grouped.items()
    }


def _approval_request_policy(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    workflows = {
        _norm(r[0]): r[1] for r in source.execute(sa.text("SELECT workflow_id, workflow_code FROM approval_workflow"))
    }
    mapping: dict[Any, dict[str, Any]] = {}
    for request_id, workflow_id in source.execute(sa.text("SELECT request_id, workflow_id FROM approval_request")):
        code = workflows.get(_norm(workflow_id))
        if code is None:
            continue
        # Policies enrol as v1 (approval_policies pairing constant).
        _put(mapping, "approval_request_policy", request_id, {"policy_code": code, "policy_version": 1})
    return mapping


def _expense_evaluation_batch(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    mapping: dict[Any, dict[str, Any]] = {}
    rows = source.execute(sa.text("SELECT DISTINCT claim_id FROM expense_limit_evaluation"))
    for (claim_id,) in rows:
        batch = uuid.uuid5(uuid.NAMESPACE_URL, f"erp-v2-enrolment:expense_evaluation_batch:{_norm(claim_id)}").hex
        _put(mapping, "expense_evaluation_batch", claim_id, {"batch_id": batch})
    return mapping


def _agreement_family(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """One deterministic family id per contract chain.

    Amendment links (contract_amendment_workflow.contract_id ->
    original_contract_id) union contracts; the family id is uuid5
    of the chain root (smallest id in the component, deterministic).
    """

    parent: dict[Any, Any] = {}

    def find(x: Any) -> Any:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    contracts = [_norm(r[0]) for r in source.execute(sa.text("SELECT contract_id FROM contract"))]
    for contract in contracts:
        find(contract)
    for contract_id, original_id in source.execute(
        sa.text(
            "SELECT contract_id, original_contract_id FROM contract_amendment_workflow "
            "WHERE original_contract_id IS NOT NULL"
        )
    ):
        ra, rb = find(_norm(contract_id)), find(_norm(original_id))
        if ra != rb:
            parent[max(ra, rb, key=str)] = min(ra, rb, key=str)
    mapping: dict[Any, dict[str, Any]] = {}
    for contract in contracts:
        family = uuid.uuid5(uuid.NAMESPACE_URL, f"erp-v2-enrolment:agreement_family:{find(contract)}").hex
        _put(mapping, "agreement_family", contract, {"family_id": family})
    return mapping


def _asset_kind(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    categories = {
        _norm(r[0]): r[1] for r in source.execute(sa.text("SELECT category_id, category_code FROM asset_category"))
    }
    mapping: dict[Any, dict[str, Any]] = {}
    for asset_id, category_id in source.execute(sa.text("SELECT asset_id, category_id FROM asset")):
        kind = categories.get(_norm(category_id))
        if kind:
            _put(mapping, "asset_kind", asset_id, {"kind": kind})
    return mapping


def _lot_status(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """LotStatus from balance evidence: quarantined > depleted > available."""

    totals: dict[Any, Decimal] = {}
    quarantined: set[Any] = set()
    for lot_id, quantity, is_quarantined in source.execute(
        sa.text("SELECT lot_id, quantity_on_hand, is_quarantined FROM inventory_lot_balance")
    ):
        key = _norm(lot_id)
        try:
            totals[key] = totals.get(key, Decimal(0)) + Decimal(str(quantity or 0))
        except InvalidOperation:
            continue
        if is_quarantined:
            quarantined.add(key)
    mapping: dict[Any, dict[str, Any]] = {}
    for (lot_id,) in source.execute(sa.text("SELECT lot_id FROM inventory_lot")):
        key = _norm(lot_id)
        if key in quarantined:
            status = "quarantined"
        elif totals.get(key, Decimal(0)) <= 0:
            status = "depleted"
        else:
            status = "available"
        _put(mapping, "lot_status", lot_id, {"status": status})
    return mapping


def _quote_lead(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    leads = {
        (_norm(r[0]), str(r[1]).replace("-", "")): _norm(r[2])
        for r in dest.execute(sa.text("SELECT tenant_id, subject_opaque_id, id FROM mod_sales.leads"))
    }
    mapping: dict[Any, dict[str, Any]] = {}
    for quote_id, tenant, customer_id in source.execute(
        sa.text("SELECT quote_id, organization_id, customer_id FROM quote")
    ):
        lead = leads.get((_norm(tenant), str(_norm(customer_id)).replace("-", "")))
        if lead is not None:
            _put(mapping, "quote_lead", quote_id, {"lead_id": lead})
    return mapping


def _user_label(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(sa.text("SELECT id AS key, username AS label FROM user_credentials")).mappings()
    return _lookup(rows, "user_label", ("label",))


def _document_library(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    # The enrolment HR library (minting default code "hr").
    rows = dest.execute(
        sa.text("SELECT tenant_id AS key, id AS library_id FROM mod_documents.document_libraries WHERE code = 'hr'")
    ).mappings()
    return _lookup(rows, "document_library", ("library_id",))


def _document_type(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    mapping: dict[Any, dict[str, Any]] = {}
    rows = source.execute(sa.text("SELECT document_id AS key, category, file_path FROM hr_document")).mappings()
    for row in rows:
        type_code = re.sub(r"[^a-z0-9]+", "-", str(row["category"] or "").lower()).strip("-")[:80].rstrip("-")
        folder = posixpath.dirname(str(row["file_path"] or ""))
        if not type_code or not folder:
            continue
        _put(mapping, "document_type", row["key"], {"type_code": type_code, "folder_path": folder})
    return mapping


def _bid_scores(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """scores_json + selected_total_score from legacy per-criterion scores.

    Module canonical shape: [{bid_id, scores: {code: str},
    weighted_total: str, comments}]. Criterion codes slugify from
    legacy criterion_name (declared); weighted_total sums the
    legacy weighted_score column per response.
    """

    selected = {
        _norm(r[0]): _norm(r[1])
        for r in source.execute(sa.text("SELECT evaluation_id, recommended_response_id FROM bid_evaluation"))
    }
    grouped: dict[Any, dict[Any, dict[str, Any]]] = {}
    rows = source.execute(
        sa.text(
            "SELECT evaluation_id, response_id, criterion_name, score, weighted_score, comments "
            "FROM bid_evaluation_score"
        )
    ).mappings()
    for row in rows:
        per_bid = grouped.setdefault(_norm(row["evaluation_id"]), {})
        entry = per_bid.setdefault(_norm(row["response_id"]), {"scores": {}, "weighted": Decimal(0), "comments": []})
        code = re.sub(r"[^a-z0-9]+", "-", str(row["criterion_name"] or "").lower()).strip("-")
        if code:
            entry["scores"][code] = str(row["score"])
        try:
            entry["weighted"] += Decimal(str(row["weighted_score"] or 0))
        except InvalidOperation:
            pass
        if row["comments"]:
            entry["comments"].append(str(row["comments"]))
    mapping: dict[Any, dict[str, Any]] = {}
    for evaluation_id, per_bid in grouped.items():
        documents = []
        selected_total: Decimal | None = None
        for bid_id, entry in sorted(per_bid.items(), key=lambda kv: str(kv[0])):
            total = entry["weighted"]
            documents.append(
                {
                    "bid_id": bid_id,
                    "scores": dict(sorted(entry["scores"].items())),
                    "weighted_total": format(total.normalize(), "f"),
                    "comments": "; ".join(entry["comments"]),
                }
            )
            if selected.get(evaluation_id) == bid_id:
                selected_total = total
        if selected_total is None:
            continue
        _put(
            mapping,
            "bid_scores",
            evaluation_id,
            {
                "scores_json": json.dumps(documents, sort_keys=True, separators=(",", ":")),
                "selected_total_score": selected_total,
            },
        )
    return mapping


def _quote_tax_rate(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """Single-rate quotes only: every line's tax code at one rate.

    Module semantics: tax_total = taxable x tax_rate / 100, so the
    rate is a percent. Legacy tax_code.tax_rate is a fraction
    (0.075) in the census estate - converted x100. Mixed-rate or
    unrated quotes get no entry.
    """

    rates = {_norm(r[0]): r[1] for r in source.execute(sa.text("SELECT tax_code_id, tax_rate FROM tax_code"))}
    per_quote: dict[Any, set[Decimal]] = {}
    for quote_id, tax_code_id in source.execute(sa.text("SELECT quote_id, tax_code_id FROM quote_line")):
        rate = rates.get(_norm(tax_code_id))
        if rate is None:
            continue
        per_quote.setdefault(_norm(quote_id), set()).add(Decimal(str(rate)))
    mapping: dict[Any, dict[str, Any]] = {}
    for quote_id, found in per_quote.items():
        if len(found) == 1:
            _put(mapping, "quote_tax_rate", quote_id, {"tax_rate": next(iter(found)) * 100})
    return mapping


def _tenant_currency(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text("SELECT organization_id AS key, functional_currency_code AS currency_code FROM organization")
    ).mappings()
    return _lookup(rows, "tenant_currency", ("currency_code",))


def _asset_condition(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """Latest assignment condition evidence (enum-exact values only)."""

    valid = {"new", "good", "fair", "poor", "damaged"}
    latest: dict[Any, tuple[str, str]] = {}
    rows = source.execute(
        sa.text(
            "SELECT asset_id, issued_on, returned_on, condition_on_issue, condition_on_return FROM asset_assignment"
        )
    ).mappings()
    for row in rows:
        for when, value in (
            (row["issued_on"], row["condition_on_issue"]),
            (row["returned_on"], row["condition_on_return"]),
        ):
            if when is None or value is None:
                continue
            normalized = str(value).strip().lower()
            if normalized not in valid:
                continue
            key = _norm(row["asset_id"])
            stamp = str(when)[:10]
            if key not in latest or stamp >= latest[key][0]:
                latest[key] = (stamp, normalized)
    return {key: {"condition": value} for key, (_stamp, value) in latest.items()}


def _payment_intent_terms(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """payer/purpose/channel for legacy Paystack intents, derivable only.

    Evidence (legacy model): source_type in {INVOICE, EXPENSE_CLAIM,
    GENERAL}; only INVOICE maps exactly to a module PaymentPurpose.
    Payer derives for INBOUND intents (the paying customer's email);
    outbound payer is the organisation itself, which no legacy
    column names. Channel parses from the gateway response JSON.
    """

    mapping: dict[Any, dict[str, Any]] = {}
    rows = source.execute(
        sa.text("SELECT intent_id AS key, direction, email, source_type, gateway_response FROM payment_intent")
    ).mappings()
    for row in rows:
        entry: dict[str, Any] = {}
        if str(row["direction"] or "").upper().startswith("IN") and row["email"]:
            entry["payer_reference"] = str(row["email"])
        if str(row["source_type"] or "").upper() == "INVOICE":
            entry["purpose"] = "INVOICE_SETTLEMENT"
        gateway = row["gateway_response"]
        if isinstance(gateway, str):
            try:
                gateway = json.loads(gateway)
            except ValueError:
                gateway = None
        if isinstance(gateway, Mapping) and gateway.get("channel"):
            entry["channel"] = str(gateway["channel"])
        if entry:
            _put(mapping, "payment_intent_terms", row["key"], entry)
    return mapping


def _stored_file_map(dest: sa.Connection) -> dict[str, Any]:
    """storage_key -> stored file id (destination mod_files estate)."""

    by_key: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for file_id, storage_key in dest.execute(sa.text("SELECT id, storage_key FROM mod_files.stored_files")):
        key = str(storage_key)
        if key in by_key and by_key[key] != _norm(file_id):
            ambiguous.add(key)
        by_key[key] = _norm(file_id)
    return {k: v for k, v in by_key.items() if k not in ambiguous}


def _file_by_path(source: sa.Connection, dest: sa.Connection, table: str, pk: str, map_name: str):
    stored = _stored_file_map(dest)
    mapping: dict[Any, dict[str, Any]] = {}
    for doc_id, file_path in source.execute(
        sa.text(f"SELECT {pk}, file_path FROM {table} WHERE file_path IS NOT NULL")  # noqa: S608
    ):
        file_id = stored.get(str(file_path))
        if file_id is not None:
            _put(mapping, map_name, doc_id, {"file_id": file_id})
    return mapping


def _case_document_file(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    return _file_by_path(source, dest, "case_document", "document_id", "case_document_file")


def _vehicle_document_file(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    return _file_by_path(source, dest, "vehicle_document", "document_id", "vehicle_document_file")


def _vehicle_asset(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """Vehicle -> fixed-asset link by exact VIN = serial_number match."""

    by_serial: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for asset_id, serial in dest.execute(
        sa.text("SELECT id, serial_number FROM mod_assets.assets WHERE serial_number IS NOT NULL")
    ):
        key = str(serial).strip().upper()
        if key in by_serial and by_serial[key] != _norm(asset_id):
            ambiguous.add(key)
        by_serial[key] = _norm(asset_id)
    mapping: dict[Any, dict[str, Any]] = {}
    for vehicle_id, vin in source.execute(sa.text("SELECT vehicle_id, vin FROM vehicle WHERE vin IS NOT NULL")):
        asset_id = by_serial.get(str(vin).strip().upper())
        if asset_id is not None and str(vin).strip().upper() not in ambiguous:
            _put(mapping, "vehicle_asset", vehicle_id, {"asset_id": asset_id})
    return mapping


MAP_BUILDERS: Final[Mapping[str, Any]] = {
    "agreement_family": _agreement_family,
    "approval_request_policy": _approval_request_policy,
    "asset_condition": _asset_condition,
    "asset_kind": _asset_kind,
    "bid_scores": _bid_scores,
    "account_class_by_legacy": _account_class_by_legacy,
    "attendance_status_by_legacy": _attendance_status_by_legacy,
    "form_version_status_by_legacy": _form_version_status_by_legacy,
    "legacy_lowercase_status": _legacy_lowercase_status,
    "payroll_run_status_by_legacy": _payroll_run_status_by_legacy,
    "pay_component_kind_by_legacy": _pay_component_kind_by_legacy,
    "workflow_status_by_legacy": _workflow_status_by_legacy,
    "bank_direction_by_type": _bank_direction_by_type,
    "bank_institution_by_account": _bank_institution_by_account,
    "bid_submission": _bid_submission,
    "case_document_file": _case_document_file,
    "document_library": _document_library,
    "document_type": _document_type,
    "employee_party": _employee_party,
    "expense_evaluation_batch": _expense_evaluation_batch,
    "journal_line": _journal_line,
    "journal_number_by_entry": _journal_number_by_entry,
    "fiscal_period": _fiscal_period,
    "inventory_item": _inventory_item,
    "lot_status": _lot_status,
    "pay_component": _pay_component,
    "payroll_calculation": _payroll_calculation,
    "payroll_line_method": _payroll_line_method,
    "payment_intent_terms": _payment_intent_terms,
    "payroll_rule_conversion": _payroll_rule_conversion,
    "project_template_task": _project_template_task,
    "quote_lead": _quote_lead,
    "quote_tax_rate": _quote_tax_rate,
    "receipt_lines": _receipt_lines,
    "sourcing_line_by_requisition_line": _sourcing_line_by_requisition_line,
    "structure_revision": _structure_revision,
    "supplier": _supplier,
    "supplier_line_dimensions": _supplier_line_dimensions,
    "supplier_payment_schedule": _supplier_payment_schedule,
    "tax_authority_by_level": _tax_authority_by_level,
    "tenant_currency": _tenant_currency,
    "user_label": _user_label,
    "user_party": _user_party,
    "vehicle_asset": _vehicle_asset,
    "vehicle_document_file": _vehicle_document_file,
}


# Destination rows that must land before the first cached read of a map.
# Parents without an import spec are pre-existing/enrolment-owned, not blockers.
MAP_DESTINATION_DEPENDENCIES: Final[Mapping[str, frozenset[str]]] = {
    "inventory_item": frozenset({"mod_inventory.items"}),
}


def required_maps(specs: Sequence[TransformSpec]) -> frozenset[str]:
    """Every map name the given specs declare (rewrites + lookups)."""

    names: set[str] = set()
    for spec in specs:
        if spec.source_row_refusal is not None:
            continue
        names.update(spec.fk_rewrites.values())
        names.update(map_name for map_name, _key, _field in spec.lookups.values())
    return frozenset(names)


def destination_dependencies(specs: Sequence[TransformSpec]) -> frozenset[str]:
    """Destination tables read by declared maps; source refusals consume none."""

    return frozenset(table for name in required_maps(specs) for table in MAP_DESTINATION_DEPENDENCIES.get(name, ()))


def unbuilt_maps(specs: Sequence[TransformSpec]) -> frozenset[str]:
    """Declared maps with no builder - must stay empty (coverage test)."""

    return required_maps(specs) - MAP_BUILDERS.keys()


class EnrolmentMaps:
    """Lazy, cached access to enrolment maps over caller-owned connections.

    A destination-built map is read at first need, so spec order in
    the run (parents before consumers) is what makes it complete.
    The cache is per-instance: one instance per import run.

    With a `mint_context`, the GENERATED rows a map reads are
    minted (once, in the caller's destination transaction) just
    before that map first builds - see minting.py. Without one,
    maps read whatever enrolment already placed in the destination.
    """

    def __init__(
        self,
        source_connection: sa.Connection,
        destination_connection: sa.Connection,
        *,
        mint_context: MintContext | None = None,
    ) -> None:
        self._source = source_connection
        self._dest = destination_connection
        self._mint_context = mint_context
        self._cache: dict[str, dict[Any, Any]] = {}
        self._mints: dict[str, MintResult] = {}

    def mint(self, step: str) -> MintResult:
        """Run a mint step once (recorded even when refused)."""

        if step not in self._mints:
            if self._mint_context is None:
                raise KeyError(f"mint step {step!r} requires a MintContext")
            self._mints[step] = run_mint_step(step, self._source, self._dest, self._mint_context)
        return self._mints[step]

    def get(self, name: str) -> dict[Any, Any]:
        if name not in self._cache:
            builder = MAP_BUILDERS.get(name)
            if builder is None:
                raise KeyError(f"no enrolment builder for map {name!r}")
            if self._mint_context is not None and name in MAP_MINTS:
                self.mint(MAP_MINTS[name])
            self._cache[name] = builder(self._source, self._dest)
        return self._cache[name]

    def for_spec(self, spec: TransformSpec) -> dict[str, dict[Any, Any]]:
        return {name: self.get(name) for name in sorted(required_maps((spec,)))}

    def summary(self) -> dict[str, int]:
        """Built map sizes, for the import receipt."""

        return {name: len(mapping) for name, mapping in sorted(self._cache.items())}

    def mint_results(self) -> tuple[MintResult, ...]:
        return tuple(self._mints[name] for name in sorted(self._mints))


__all__ = [
    "MAP_BUILDERS",
    "MAP_DESTINATION_DEPENDENCIES",
    "EnrolmentMaps",
    "MapConflict",
    "destination_dependencies",
    "required_maps",
    "unbuilt_maps",
]
