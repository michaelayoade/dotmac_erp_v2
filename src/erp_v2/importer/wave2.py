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
    note: str = ""


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
        renames={"revision_id": "structure_id", "sequence": "display_order"},
        fk_rewrites={"revision_id": "structure_revision"},
        note="also fed by salary_structure_deduction (second spec, same destination); "
        "formula/amount_based_on_formula map to typed calculation rules - "
        "expression formulas cannot copy; unconvertible formulas are exceptions",
    ),
    Pairing(
        "mod_payroll.pay_structure_rules",
        "public.salary_structure_deduction",
        renames={"revision_id": "structure_id", "sequence": "display_order"},
        fk_rewrites={"revision_id": "structure_revision"},
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
        renames={"employee_ref": "employee_id"},
        note="historical slips import as calculation evidence; run/assignment/"
        "revision links resolved at rehearsal - parity gate is run-total equality",
    ),
    Pairing(
        "mod_payroll.payroll_calculation_lines",
        "public.salary_slip_earning",
        renames={"calculation_id": "slip_id", "sequence": "display_order"},
        note="also fed by salary_slip_deduction (second spec, same destination)",
    ),
    Pairing(
        "mod_payroll.payroll_calculation_lines",
        "public.salary_slip_deduction",
        renames={"calculation_id": "slip_id", "sequence": "display_order"},
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
        },
        constants={"status": "active"},
        note="authority_id from generated authorities (fk via enrolment map)",
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
        "public.tax_band",
        renames={"lower_bound": "min_amount", "upper_bound": "max_amount"},
        note="rule_id from generated rules (enrolment map); sequence renames in place",
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
            )
        )
    return tuple(specs), tuple(generated)


__all__ = ["WAVE2_PAIRINGS", "Pairing", "build_wave2_specs"]
