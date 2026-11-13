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


# Legacy payroll/tax columns as parsed from dotmac_erp models (2026-11-08 census).
PAYROLL_TAX_LEGACY = {
    "public.salary_component": (
        "component_id",
        "organization_id",
        "component_code",
        "component_name",
        "abbr",
        "component_type",
        "description",
        "expense_account_id",
        "liability_account_id",
        "is_tax_applicable",
        "is_statutory",
        "exempted_from_income_tax",
        "depends_on_payment_days",
        "statistical_component",
        "do_not_include_in_total",
        "is_active",
        "display_order",
        "created_at",
        "updated_at",
    ),
    "public.salary_structure": (
        "structure_id",
        "organization_id",
        "structure_code",
        "structure_name",
        "description",
        "payroll_frequency",
        "currency_code",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "public.salary_structure_earning": (
        "earning_id",
        "structure_id",
        "component_id",
        "amount",
        "amount_based_on_formula",
        "formula",
        "condition",
        "display_order",
    ),
    "public.salary_structure_deduction": (
        "deduction_id",
        "structure_id",
        "component_id",
        "amount",
        "amount_based_on_formula",
        "formula",
        "condition",
        "display_order",
    ),
    "public.salary_structure_assignment": (
        "assignment_id",
        "organization_id",
        "employee_id",
        "structure_id",
        "from_date",
        "to_date",
        "base",
        "variable",
        "income_tax_slab",
        "created_at",
        "updated_at",
    ),
    "public.payroll_entry": (
        "entry_id",
        "organization_id",
        "entry_number",
        "posting_date",
        "start_date",
        "end_date",
        "payroll_frequency",
        "payroll_year",
        "payroll_month",
        "entry_name",
        "currency_code",
        "exchange_rate",
        "department_id",
        "designation_id",
        "employment_type_id",
        "source_bank_account_id",
        "expense_account_id",
        "total_gross_pay",
        "total_deductions",
        "total_net_pay",
        "employee_count",
        "status",
        "salary_slips_created",
        "salary_slips_submitted",
        "payslips_email_status",
        "payslips_email_queued_at",
        "payslips_email_queued_by_id",
        "payslips_email_total_count",
        "payslips_email_processed_count",
        "payslips_email_error_count",
        "payslips_email_last_run_at",
        "journal_entry_id",
        "notes",
        "created_at",
        "updated_at",
    ),
    "public.salary_slip": (
        "slip_id",
        "organization_id",
        "slip_number",
        "employee_id",
        "employee_name",
        "structure_id",
        "posting_date",
        "start_date",
        "end_date",
        "currency_code",
        "exchange_rate",
        "total_working_days",
        "absent_days",
        "payment_days",
        "leave_without_pay",
        "gross_pay",
        "total_deduction",
        "net_pay",
        "amount_paid",
        "balance_due",
        "gross_pay_functional",
        "total_deduction_functional",
        "net_pay_functional",
        "cost_center_id",
        "status",
        "journal_entry_id",
        "posted_at",
        "posted_by_id",
        "paid_at",
        "paid_by_id",
        "payment_reference",
        "bank_name",
        "bank_account_number",
        "bank_account_name",
        "bank_branch_code",
        "payroll_entry_id",
        "notes",
        "is_auto_generated",
        "needs_review",
        "review_reasons",
        "reviewed_at",
        "reviewed_by_id",
        "created_at",
        "updated_at",
    ),
    "public.salary_slip_earning": (
        "line_id",
        "slip_id",
        "component_id",
        "component_name",
        "abbr",
        "amount",
        "default_amount",
        "additional_amount",
        "year_to_date",
        "statistical_component",
        "do_not_include_in_total",
        "display_order",
    ),
    "public.salary_slip_deduction": (
        "line_id",
        "slip_id",
        "component_id",
        "component_name",
        "abbr",
        "amount",
        "default_amount",
        "additional_amount",
        "year_to_date",
        "statistical_component",
        "do_not_include_in_total",
        "display_order",
    ),
    "public.tax_jurisdiction": (
        "jurisdiction_id",
        "organization_id",
        "jurisdiction_code",
        "jurisdiction_name",
        "description",
        "country_code",
        "state_province",
        "jurisdiction_level",
        "current_tax_rate",
        "tax_rate_effective_from",
        "future_tax_rate",
        "future_rate_effective_from",
        "has_reduced_rate",
        "reduced_rate",
        "reduced_rate_threshold",
        "fiscal_year_end_month",
        "filing_due_months",
        "extension_months",
        "currency_code",
        "tax_authority_name",
        "tax_id_number",
        "current_tax_payable_account_id",
        "current_tax_expense_account_id",
        "deferred_tax_asset_account_id",
        "deferred_tax_liability_account_id",
        "deferred_tax_expense_account_id",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "public.tax_code": (
        "tax_code_id",
        "organization_id",
        "tax_code",
        "tax_name",
        "description",
        "tax_type",
        "jurisdiction_id",
        "tax_rate",
        "effective_from",
        "effective_to",
        "is_compound",
        "is_inclusive",
        "is_fixed_amount",
        "is_recoverable",
        "recovery_rate",
        "applies_to_purchases",
        "applies_to_sales",
        "tax_return_box",
        "reporting_code",
        "tax_collected_account_id",
        "tax_paid_account_id",
        "tax_expense_account_id",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "public.tax_return": (
        "return_id",
        "organization_id",
        "tax_period_id",
        "jurisdiction_id",
        "return_type",
        "return_reference",
        "total_output_tax",
        "total_input_tax",
        "net_tax_payable",
        "adjustments",
        "final_amount",
        "box_values",
        "status",
        "filed_date",
        "filed_by_user_id",
        "filing_reference",
        "is_paid",
        "payment_date",
        "payment_reference",
        "payment_journal_entry_id",
        "is_amendment",
        "original_return_id",
        "amendment_reason",
        "prepared_by_user_id",
        "prepared_at",
        "reviewed_by_user_id",
        "reviewed_at",
        "created_at",
        "updated_at",
    ),
}


def _payroll_tax_dest():
    dest = {}
    for modname, schema in (("dotmac_payroll", "mod_payroll"), ("dotmac_tax", "mod_tax")):
        models = __import__(modname + ".models", fromlist=["x"])
        for name in models.TENANT_TABLES:
            key = f"{schema}.{name}"
            tables = models.Base.metadata.tables
            dest[key] = tables[key] if key in tables else tables[name]
    return dest


def test_wave2_payroll_tax_rulings_narrow_missing_to_named_decisions() -> None:
    from unittest.mock import patch

    import erp_v2.importer.wave2 as wave2

    pairings = tuple(p for p in WAVE2_PAIRINGS if p.destination.startswith(("mod_payroll.", "mod_tax.")))
    with patch.object(wave2, "WAVE2_PAIRINGS", pairings):
        specs, generated = build_wave2_specs(PAYROLL_TAX_LEGACY, _payroll_tax_dest())
    by_dest: dict[str, list] = {}
    for s in specs:
        by_dest.setdefault(s.destination_table, []).append(s)

    # Jurisdictions became executable: authority via level rewrite map.
    (jurisdictions,) = by_dest["mod_tax.tax_jurisdictions"]
    assert jurisdictions.executable
    assert jurisdictions.fk_rewrites == {"authority_id": "tax_authority_by_level"}

    # Rules are executable: method/rate/fixed_amount resolve from the
    # enrolment formula conversion (unconvertible rows exception).
    for rules in by_dest["mod_payroll.pay_structure_rules"]:
        assert rules.executable, rules.missing_required
        assert rules.lookups["component_code"] == ("pay_component", "component_id", "code")
        assert rules.lookups["calculation_method"] == (
            "payroll_rule_conversion",
            "id",
            "calculation_method",
        )
        assert "fixed_amount" not in rules.column_map  # lookup-carried

    # Calculations: exact renames landed; enrolment computations cover
    # assignment/proration/employer liability per slip.
    (calculations,) = by_dest["mod_payroll.payroll_calculations"]
    assert calculations.executable, calculations.missing_required
    assert calculations.column_map["run_id"] == "payroll_entry_id"
    assert calculations.column_map["gross_amount"] == "gross_pay"
    assert calculations.lookups["assignment_id"] == ("payroll_calculation", "id", "assignment_id")

    for lines in by_dest["mod_payroll.payroll_calculation_lines"]:
        assert lines.executable, lines.missing_required

    # Bands are GENERATED with rule re-publication, never row-copied.
    assert "mod_tax.tax_rule_bands" in {p.destination for p in generated}


def test_wave2_forwards_explicit_row_hash_fallback(monkeypatch) -> None:
    import sqlalchemy as sa

    from erp_v2.importer import wave2

    pairing = wave2.Pairing(
        "mod_people.import_fixture",
        "public.legacy_fixture",
        row_hash_columns=("request_fingerprint",),
    )
    monkeypatch.setattr(wave2, "WAVE2_PAIRINGS", (pairing,))
    table = sa.Table(
        "import_fixture",
        sa.MetaData(),
        sa.Column("id", sa.String, nullable=False),
        sa.Column("request_fingerprint", sa.String, nullable=False),
        sa.Column("evidence_digest", sa.String, nullable=True),
        schema="mod_people",
    )
    specs, generated = wave2.build_wave2_specs({"public.legacy_fixture": ("id",)}, {pairing.destination: table})
    assert generated == ()
    assert specs[0].executable
    assert specs[0].computed == {"request_fingerprint": "sha256_row"}
    assert "evidence_digest" not in specs[0].column_map


def test_departments_source_order_survives_both_spec_builders() -> None:
    # The CLI executes through waves36.build_specs, not
    # build_wave2_specs; the canonical-department ordering must
    # reach the executor through the builder that actually runs.
    from erp_v2.importer.waves36 import build_specs

    expected = (("is_active", True), ("created_at", False), ("department_id", False))
    people = tuple(p for p in WAVE2_PAIRINGS if p.destination == "mod_people.departments")
    generic_specs, _ = build_specs(people, LEGACY, _people_dest())
    assert generic_specs[0].source_order == expected
    from unittest.mock import patch

    import erp_v2.importer.wave2 as wave2

    with patch.object(wave2, "WAVE2_PAIRINGS", people):
        wave2_specs, _ = wave2.build_wave2_specs(LEGACY, _people_dest())
    assert wave2_specs[0].source_order == expected
