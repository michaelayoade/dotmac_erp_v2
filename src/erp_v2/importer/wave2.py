"""Wave-2 pairings: people, payroll, tax (legacy -> module storage).

Pairings are data, ruled from the 2026-11-08 schema census (legacy
model columns vs released module models). `build_wave2_specs` turns
them into executability-checked TransformSpecs given the source
columns observed on the actual legacy snapshot (reflect, don't
assume). Destinations with no legacy row source are declared
GENERATED with their generation rule named — they are produced by
module services or derived at enrolment, never row-copied.

Identity rule (cutover Gate 3): legacy UUIDs are preserved, so FK
columns keep their values; `fk_rewrites` is used only where the
destination key is a different generated row (pay revisions).
Employee bank/compensation and provisioning columns in residue are
payroll/adapter facts, not people-directory data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from erp_v2.importer.spec import TransformSpec, build_spec, destination_columns_from_table


@dataclass(frozen=True)
class Pairing:
    destination: str  # schema-qualified module table
    source: str | None  # schema-qualified legacy table, None = GENERATED
    renames: Mapping[str, str] = field(default_factory=dict)
    constants: Mapping[str, object] = field(default_factory=dict)
    fk_rewrites: Mapping[str, str] = field(default_factory=dict)
    lookups: Mapping[str, tuple[str, str, str]] = field(default_factory=dict)
    note: str = ""


# Module rules/lines denormalize component identity; the enrolment
# step builds this map from imported mod_payroll.pay_components rows
# (fields: code, name, kind) plus legacy depends_on_payment_days for
# `prorates`. Keyed by the preserved component UUID.
_COMPONENT_LOOKUP: Final[Mapping[str, tuple[str, str, str]]] = {
    "component_code": ("pay_component", "component_id", "code"),
    "component_name": ("pay_component", "component_id", "name"),
    "component_kind": ("pay_component", "component_id", "kind"),
}

WAVE2_PAIRINGS: Final[tuple[Pairing, ...]] = (
    # --- people (mod_people) ---
    Pairing(
        "mod_people.departments",
        "public.department",
        renames={
            "code": "department_code",
            "name": "department_name",
            "parent_id": "parent_department_id",
        },
        note="residue: head_id/cost_center_id are e2/reporting facts",
    ),
    Pairing(
        "mod_people.designations",
        "public.designation",
        renames={"code": "designation_code", "name": "designation_name"},
        note="residue: ncc_staff_category is ERP-unique (e2)",
    ),
    Pairing(
        "mod_people.employment_types",
        "public.employment_type",
        renames={"code": "type_code", "name": "type_name"},
    ),
    Pairing(
        "mod_people.employees",
        "public.employee",
        note=(
            "party_id from person_id (kernel Party import preserves UUIDs); "
            "status values are legacy uppercase - verify module enum at rehearsal; "
            "residue: bank/ctc/salary (payroll input), mailcow/Sub provisioning "
            "(adapters), grade/reports_to/cost_center (e2 HR suite)"
        ),
    ),
    Pairing(
        "mod_people.positions",
        "public.position",
        renames={
            "code": "position_code",
            "name": "position_name",
            "parent_id": "parent_position_id",
        },
        note="is_department_head has no legacy source - derived at enrolment if required",
    ),
    Pairing("mod_people.position_assignments", "public.position_assignment"),
    # --- payroll (mod_payroll) ---
    Pairing(
        "mod_payroll.pay_components",
        "public.salary_component",
        renames={
            "name": "component_name",
            "kind": "component_type",
            "expense_account_ref": "expense_account_id",
            "liability_account_ref": "liability_account_id",
        },
        constants={"status": "active"},
        note="account refs are opaque strings in the module; legacy UUIDs stringify at enrolment",
    ),
    Pairing(
        "mod_payroll.pay_structures",
        "public.salary_structure",
        renames={"code": "structure_code", "name": "structure_name"},
        constants={"status": "active"},
        note="minor_units derives from tenant currency config at enrolment (no legacy column)",
    ),
    Pairing(
        "mod_payroll.pay_structure_revisions",
        None,
        note="GENERATED: one published v1 revision per structure at enrolment",
    ),
    Pairing(
        "mod_payroll.pay_structure_rules",
        "public.salary_structure_earning",
        renames={
            "revision_id": "structure_id",
            "sequence": "display_order",
            "fixed_amount": "amount",
        },
        fk_rewrites={"revision_id": "structure_revision"},
        lookups={
            **_COMPONENT_LOOKUP,
            "prorates": ("pay_component", "component_id", "prorates"),
        },
        note="also fed by salary_structure_deduction (second spec, same destination); "
        "calculation_method stays MISSING until the enrolment formula "
        "conversion rules it per row (module methods: input/fixed/percentage; "
        "legacy amount_based_on_formula+formula/condition have no typed "
        "equivalent for arbitrary expressions) - unconvertible formulas are "
        "exceptions, so this spec is deliberately not executable yet",
    ),
    Pairing(
        "mod_payroll.pay_structure_rules",
        "public.salary_structure_deduction",
        renames={
            "revision_id": "structure_id",
            "sequence": "display_order",
            "fixed_amount": "amount",
        },
        fk_rewrites={"revision_id": "structure_revision"},
        lookups={
            **_COMPONENT_LOOKUP,
            "prorates": ("pay_component", "component_id", "prorates"),
        },
        note="see earning pairing",
    ),
    Pairing(
        "mod_payroll.pay_rule_bases",
        None,
        note="GENERATED: derived from rule calculation bases at enrolment",
    ),
    Pairing(
        "mod_payroll.employee_pay_assignments",
        "public.salary_structure_assignment",
        renames={
            "employee_ref": "employee_id",
            "revision_id": "structure_id",
            "effective_from": "from_date",
            "effective_to": "to_date",
        },
        constants={"source_ref": "legacy-erp-import", "source_version": 1, "status": "active"},
        fk_rewrites={"revision_id": "structure_revision"},
    ),
    Pairing(
        "mod_payroll.payroll_runs",
        "public.payroll_entry",
        renames={
            "run_ref": "entry_number",
            "period_start": "start_date",
            "period_end": "end_date",
        },
        note="minor_units/created_by from tenant/enrolment context",
    ),
    Pairing(
        "mod_payroll.payroll_calculations",
        "public.salary_slip",
        renames={
            "employee_ref": "employee_id",
            "run_id": "payroll_entry_id",
            "revision_id": "structure_id",
            "gross_amount": "gross_pay",
            "deduction_amount": "total_deduction",
            "net_amount": "net_pay",
            "calculated_at": "created_at",
        },
        constants={"revision_version": 1},
        fk_rewrites={"revision_id": "structure_revision"},
        note="historical slips import as calculation evidence; parity gate is "
        "run-total equality. Still MISSING by design: assignment_id (legacy "
        "slips link employee+structure, not the generated assignment row - "
        "resolved per row at enrolment, unmatched = exception), "
        "proration_factor (legacy payment_days/total_working_days ratio - "
        "enrolment computation, zero-working-days = exception), "
        "employer_liability_amount (sum of employer-kind lines - enrolment "
        "aggregation; never defaulted to zero silently)",
    ),
    Pairing(
        "mod_payroll.payroll_calculation_lines",
        "public.salary_slip_earning",
        renames={"calculation_id": "slip_id", "sequence": "display_order"},
        lookups={
            "component_code": _COMPONENT_LOOKUP["component_code"],
            "component_kind": _COMPONENT_LOOKUP["component_kind"],
        },
        note="also fed by salary_slip_deduction (second spec, same destination); "
        "component_name copies from the legacy line snapshot; calculation_method "
        "stays MISSING (method lives on the structure rule that produced the "
        "line - enrolment resolves it from the converted rule set)",
    ),
    Pairing(
        "mod_payroll.payroll_calculation_lines",
        "public.salary_slip_deduction",
        renames={"calculation_id": "slip_id", "sequence": "display_order"},
        lookups={
            "component_code": _COMPONENT_LOOKUP["component_code"],
            "component_kind": _COMPONENT_LOOKUP["component_kind"],
        },
        note="see earning pairing",
    ),
    Pairing(
        "mod_payroll.payroll_liabilities",
        None,
        note="GENERATED by payroll service from calculations; open legacy "
        "balances enter as enrolment liabilities with evidence refs",
    ),
    Pairing(
        "mod_payroll.payroll_liability_settlements",
        None,
        note="GENERATED post-flip; legacy settlements stay calculation evidence",
    ),
    # --- tax (mod_tax) ---
    Pairing(
        "mod_tax.tax_authorities",
        None,
        note="GENERATED: one authority per legacy jurisdiction level at enrolment",
    ),
    Pairing(
        "mod_tax.tax_jurisdictions",
        "public.tax_jurisdiction",
        renames={
            "code": "jurisdiction_code",
            "name": "jurisdiction_name",
            "subdivision_code": "state_province",
            "authority_id": "jurisdiction_level",
        },
        constants={"status": "active"},
        fk_rewrites={"authority_id": "tax_authority_by_level"},
        note="authority_id: legacy jurisdiction_level rewritten through the "
        "enrolment map built when authorities are GENERATED (one per level); "
        "a level with no generated authority is an exception",
    ),
    Pairing(
        "mod_tax.tax_codes",
        "public.tax_code",
        renames={"code": "tax_code", "name": "tax_name", "tax_kind_code": "tax_type"},
        constants={"status": "active"},
        note="jurisdiction_id renames in place (UUID preserved)",
    ),
    Pairing(
        "mod_tax.tax_rules",
        None,
        note="GENERATED: legacy rate/effective columns re-publish as v1 rules "
        "through the tax service (policy is re-issued, not row-copied)",
    ),
    Pairing(
        "mod_tax.tax_rule_bands",
        None,
        note="GENERATED with tax_rules re-publication: legacy public.tax_band "
        "rows (min_amount/max_amount/rate/sequence per effective window) are "
        "enrolment INPUT to the v1 rules, not a row copy - legacy bands carry "
        "no rule FK, so rule_id cannot be copied or honestly defaulted",
    ),
    Pairing(
        "mod_tax.tax_returns",
        "public.tax_return",
        note="destination requires generated report/obligation links; "
        "rehearsal rules regenerate-vs-archive per return state",
    ),
    # Tax determination/statutory pipeline: evidence is produced by the
    # tax service from re-published policy, never row-copied. Legacy
    # tax_transaction/tax_period/tax_reconciliation rows are rehearsal
    # inputs (re-determination + variance queue), not import sources.
    *(
        Pairing(
            f"mod_tax.{table}",
            None,
            note="GENERATED by tax service from re-published policy (see tax_rules)",
        )
        for table in (
            "statutory_report_boxes",
            "statutory_report_definitions",
            "statutory_report_values",
            "statutory_reports",
            "tax_determination_dependencies",
            "tax_determination_lines",
            "tax_determination_prior_sets",
            "tax_determination_sets",
            "tax_determinations",
            "tax_filing_obligations",
            "tax_return_events",
            "tax_rule_dependency_codes",
            "tax_rule_supersessions",
            "tax_subject_classifications",
        )
    ),
)


def build_wave2_specs(
    source_columns: Mapping[str, Sequence[str]],
    destination_tables: Mapping[str, object],
) -> tuple[tuple[TransformSpec, ...], tuple[Pairing, ...]]:
    """Build specs for row-sourced pairings; return GENERATED pairings apart.

    `source_columns` is keyed by schema-qualified legacy table (reflect
    the snapshot); `destination_tables` by schema-qualified module
    table holding SQLAlchemy Table objects. A pairing whose source or
    destination shape is absent raises KeyError naming it — the census
    must match reality before any spec is trusted.
    """

    specs: list[TransformSpec] = []
    generated: list[Pairing] = []
    for pairing in WAVE2_PAIRINGS:
        if pairing.source is None:
            generated.append(pairing)
            continue
        specs.append(
            build_spec(
                source_table=pairing.source,
                destination_table=pairing.destination,
                source_columns=source_columns[pairing.source],
                destination_columns=destination_columns_from_table(destination_tables[pairing.destination]),
                renames=pairing.renames,
                constants=pairing.constants,
                fk_rewrites=pairing.fk_rewrites,
                lookups=pairing.lookups,
            )
        )
    return tuple(specs), tuple(generated)


__all__ = ["WAVE2_PAIRINGS", "Pairing", "build_wave2_specs"]
