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


def _bank_direction_by_type(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Module check constraint: direction IN ('credit','debit'). Only the
    # literal legacy spellings are ruled; anything else exceptions.
    return {"CREDIT": "credit", "DEBIT": "debit", "credit": "credit", "debit": "debit"}


# --- source (legacy snapshot) -----------------------------------------------


def _employee_party(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Legacy employee.person_id IS the kernel Party UUID (preserved).
    rows = source.execute(sa.text("SELECT employee_id, person_id FROM public.employee"))
    return _scalar(rows, "employee_party")


def _user_party(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    # Legacy user_credentials.id is the user UUID created_by columns
    # reference; person_id is the kernel Party UUID (preserved).
    rows = source.execute(sa.text("SELECT id, person_id FROM public.user_credentials"))
    return _scalar(rows, "user_party")


def _supplier(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text(
            "SELECT supplier_id AS key, COALESCE(NULLIF(legal_name, ''), trading_name) AS name FROM public.supplier"
        )
    ).mappings()
    return _lookup(rows, "supplier", ("name",))


def _payment_intent_by_payment(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text(
            "SELECT customer_payment_id AS key, intent_id AS intent_id "
            "FROM public.payment_intent WHERE customer_payment_id IS NOT NULL"
        )
    ).mappings()
    return _lookup(rows, "payment_intent_by_payment", ("intent_id",))


def _project_template_task(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(
        sa.text("SELECT template_task_id AS key, template_id AS template_id FROM public.project_template_task")
    ).mappings()
    return _lookup(rows, "project_template_task", ("template_id",))


def _bid_submission(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    # UUIDs are preserved, so the legacy rfq_id IS the module event id.
    rows = source.execute(
        sa.text("SELECT response_id AS key, rfq_id AS event_id FROM public.quotation_response")
    ).mappings()
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


def _bank_institution_by_code(source: sa.Connection, dest: sa.Connection) -> dict[Any, Any]:
    rows = dest.execute(sa.text("SELECT code, id FROM mod_banking.bank_institutions"))
    return _scalar(rows, "bank_institution_by_code")


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
        for key, flag in source.execute(
            sa.text("SELECT component_id, depends_on_payment_days FROM public.salary_component")
        )
    }
    mapping: dict[Any, dict[str, Any]] = {}
    rows = dest.execute(sa.text("SELECT id, code, name, kind FROM mod_payroll.pay_components")).mappings()
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
    return {
        str(r[0]) for r in source.execute(sa.text("SELECT abbr FROM public.salary_component WHERE abbr IS NOT NULL"))
    }


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
                f"FROM public.{table}"
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
                f"SELECT {pk} AS key, amount, amount_based_on_formula, formula, condition FROM public.{table}"  # noqa: S608
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
        _norm(r[0]): _norm(r[1])
        for r in source.execute(sa.text("SELECT slip_id, structure_id FROM public.salary_slip"))
    }
    mapping: dict[Any, dict[str, Any]] = {}
    for table in ("salary_slip_earning", "salary_slip_deduction"):
        rows = source.execute(
            sa.text(f"SELECT line_id AS key, slip_id, component_id FROM public.{table}")  # noqa: S608
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
            sa.text(f"SELECT slip_id, component_id, amount FROM public.{table}")  # noqa: S608
        ):
            if _norm(component_id) in employer_components and amount is not None:
                key = _norm(slip_id)
                employer_sums[key] = employer_sums.get(key, Decimal(0)) + Decimal(str(amount))
    mapping: dict[Any, dict[str, Any]] = {}
    slips = source.execute(
        sa.text(
            "SELECT slip_id, employee_id, structure_id, start_date, payment_days, total_working_days "
            "FROM public.salary_slip"
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
    rows = source.execute(
        sa.text("SELECT invoice_id AS key, due_date, total_amount FROM public.supplier_invoice")
    ).mappings()
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
        sa.text("SELECT line_id AS key, cost_center_id, project_id, segment_id FROM public.supplier_invoice_line")
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
            "SELECT receipt_id, po_line_id, quantity_received, line_number "
            "FROM public.goods_receipt_line ORDER BY line_number"
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
        _norm(r[0]): r[1]
        for r in source.execute(sa.text("SELECT workflow_id, workflow_code FROM public.approval_workflow"))
    }
    mapping: dict[Any, dict[str, Any]] = {}
    for request_id, workflow_id in source.execute(
        sa.text("SELECT request_id, workflow_id FROM public.approval_request")
    ):
        code = workflows.get(_norm(workflow_id))
        if code is None:
            continue
        # Policies enrol as v1 (approval_policies pairing constant).
        _put(mapping, "approval_request_policy", request_id, {"policy_code": code, "policy_version": 1})
    return mapping


def _expense_evaluation_batch(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    mapping: dict[Any, dict[str, Any]] = {}
    rows = source.execute(sa.text("SELECT DISTINCT claim_id FROM public.expense_limit_evaluation"))
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

    contracts = [_norm(r[0]) for r in source.execute(sa.text("SELECT contract_id FROM public.contract"))]
    for contract in contracts:
        find(contract)
    for contract_id, original_id in source.execute(
        sa.text(
            "SELECT contract_id, original_contract_id FROM public.contract_amendment_workflow "
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
        _norm(r[0]): r[1]
        for r in source.execute(sa.text("SELECT category_id, category_code FROM public.asset_category"))
    }
    mapping: dict[Any, dict[str, Any]] = {}
    for asset_id, category_id in source.execute(sa.text("SELECT asset_id, category_id FROM public.asset")):
        kind = categories.get(_norm(category_id))
        if kind:
            _put(mapping, "asset_kind", asset_id, {"kind": kind})
    return mapping


def _lot_status(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    """LotStatus from balance evidence: quarantined > depleted > available."""

    totals: dict[Any, Decimal] = {}
    quarantined: set[Any] = set()
    for lot_id, quantity, is_quarantined in source.execute(
        sa.text("SELECT lot_id, quantity_on_hand, is_quarantined FROM public.inventory_lot_balance")
    ):
        key = _norm(lot_id)
        try:
            totals[key] = totals.get(key, Decimal(0)) + Decimal(str(quantity or 0))
        except InvalidOperation:
            continue
        if is_quarantined:
            quarantined.add(key)
    mapping: dict[Any, dict[str, Any]] = {}
    for (lot_id,) in source.execute(sa.text("SELECT lot_id FROM public.inventory_lot")):
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
        sa.text("SELECT quote_id, organization_id, customer_id FROM public.quote")
    ):
        lead = leads.get((_norm(tenant), str(_norm(customer_id)).replace("-", "")))
        if lead is not None:
            _put(mapping, "quote_lead", quote_id, {"lead_id": lead})
    return mapping


def _user_label(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    rows = source.execute(sa.text("SELECT id AS key, username AS label FROM public.user_credentials")).mappings()
    return _lookup(rows, "user_label", ("label",))


def _document_library(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    # The enrolment HR library (minting default code "hr").
    rows = dest.execute(
        sa.text("SELECT tenant_id AS key, id AS library_id FROM mod_documents.document_libraries WHERE code = 'hr'")
    ).mappings()
    return _lookup(rows, "document_library", ("library_id",))


def _document_type(source: sa.Connection, dest: sa.Connection) -> dict[Any, dict[str, Any]]:
    mapping: dict[Any, dict[str, Any]] = {}
    rows = source.execute(sa.text("SELECT document_id AS key, category, file_path FROM public.hr_document")).mappings()
    for row in rows:
        type_code = re.sub(r"[^a-z0-9]+", "-", str(row["category"] or "").lower()).strip("-")[:80].rstrip("-")
        folder = posixpath.dirname(str(row["file_path"] or ""))
        if not type_code or not folder:
            continue
        _put(mapping, "document_type", row["key"], {"type_code": type_code, "folder_path": folder})
    return mapping


MAP_BUILDERS: Final[Mapping[str, Any]] = {
    "agreement_family": _agreement_family,
    "approval_request_policy": _approval_request_policy,
    "asset_kind": _asset_kind,
    "bank_direction_by_type": _bank_direction_by_type,
    "bank_institution_by_code": _bank_institution_by_code,
    "bid_submission": _bid_submission,
    "document_library": _document_library,
    "document_type": _document_type,
    "employee_party": _employee_party,
    "expense_evaluation_batch": _expense_evaluation_batch,
    "fiscal_period": _fiscal_period,
    "inventory_item": _inventory_item,
    "lot_status": _lot_status,
    "pay_component": _pay_component,
    "payment_intent_by_payment": _payment_intent_by_payment,
    "payroll_calculation": _payroll_calculation,
    "payroll_line_method": _payroll_line_method,
    "payroll_rule_conversion": _payroll_rule_conversion,
    "project_template_task": _project_template_task,
    "quote_lead": _quote_lead,
    "receipt_lines": _receipt_lines,
    "sourcing_line_by_requisition_line": _sourcing_line_by_requisition_line,
    "structure_revision": _structure_revision,
    "supplier": _supplier,
    "supplier_line_dimensions": _supplier_line_dimensions,
    "supplier_payment_schedule": _supplier_payment_schedule,
    "tax_authority_by_level": _tax_authority_by_level,
    "user_label": _user_label,
    "user_party": _user_party,
}


def required_maps(specs: Sequence[TransformSpec]) -> frozenset[str]:
    """Every map name the given specs declare (rewrites + lookups)."""

    names: set[str] = set()
    for spec in specs:
        names.update(spec.fk_rewrites.values())
        names.update(map_name for map_name, _key, _field in spec.lookups.values())
    return frozenset(names)


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
    "EnrolmentMaps",
    "MapConflict",
    "required_maps",
    "unbuilt_maps",
]
