"""Wave-2 pairings against real people metadata + legacy column census."""

from __future__ import annotations

from erp_v2.importer.wave2 import WAVE2_PAIRINGS, build_wave2_specs

# Legacy columns as parsed from dotmac_erp models (2026-11-08 census).
LEGACY = {
    "public.department": (
        "department_id",
        "organization_id",
        "department_code",
        "department_name",
        "description",
        "parent_department_id",
        "head_id",
        "cost_center_id",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "public.designation": (
        "designation_id",
        "organization_id",
        "designation_code",
        "designation_name",
        "description",
        "ncc_staff_category",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "public.employment_type": (
        "employment_type_id",
        "organization_id",
        "type_code",
        "type_name",
        "description",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "public.employee": (
        "employee_id",
        "organization_id",
        "person_id",
        "employee_code",
        "department_id",
        "designation_id",
        "employment_type_id",
        "date_of_joining",
        "date_of_leaving",
        "probation_end_date",
        "confirmation_date",
        "status",
        "bank_account_number",
        "created_at",
        "updated_at",
    ),
    "public.position": (
        "position_id",
        "organization_id",
        "position_code",
        "position_name",
        "designation_id",
        "parent_position_id",
        "department_id",
        "is_vacant",
        "vacancy_routing_policy",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "public.position_assignment": (
        "position_assignment_id",
        "organization_id",
        "employee_id",
        "position_id",
        "start_date",
        "end_date",
        "assignment_type",
        "created_at",
        "updated_at",
    ),
}


def _people_dest():
    from dotmac_people import models

    return {f"mod_people.{name}": models.Base.metadata.tables[f"mod_people.{name}"] for name in models.TENANT_TABLES}


def test_people_specs_all_executable() -> None:
    # People was extracted product-first from ERP, so every directory
    # table must spec clean; a failure here means a released people
    # bump changed shape and the pairing needs re-ruling.
    from unittest.mock import patch

    import erp_v2.importer.wave2 as wave2

    people_pairings = tuple(p for p in WAVE2_PAIRINGS if p.destination.startswith("mod_people."))
    with patch.object(wave2, "WAVE2_PAIRINGS", people_pairings):
        specs, generated = build_wave2_specs(LEGACY, _people_dest())
    assert generated == ()
    assert len(specs) == 6
    assert all(s.executable for s in specs), {
        s.destination_table: s.missing_required for s in specs if not s.executable
    }
    employees = next(s for s in specs if s.destination_table == "mod_people.employees")
    assert employees.column_map["party_id"] == "person_id"
    assert "bank_account_number" in employees.residue


def test_every_wave2_destination_is_paired_once_per_source() -> None:
    dests = [p.destination for p in WAVE2_PAIRINGS if p.source]
    # pay_structure_rules + payroll_calculation_lines each take two
    # legacy sources (earning + deduction); all others are 1:1.
    assert dests.count("mod_payroll.pay_structure_rules") == 2
    assert dests.count("mod_payroll.payroll_calculation_lines") == 2
    singles = [d for d in dests if dests.count(d) == 1]
    assert len(singles) == len(set(singles))
