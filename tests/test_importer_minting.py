"""Enrolment minting steps on synthetic legacy/module schemas."""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from erp_v2.importer.enrolment import EnrolmentMaps
from erp_v2.importer.minting import MintContext, run_mint_step
from erp_v2.importer.pipeline import run_import
from erp_v2.importer.spec import DestinationColumn, build_spec

UTC = timezone.utc
_SCHEMAS = ("public", "mod_payroll", "mod_tax", "mod_procurement", "mod_sales", "mod_documents")

CTX = MintContext(
    tenant_id="t1",
    actor_id="actor1",
    occurred_at=datetime(2026, 11, 8, tzinfo=UTC),
    payroll_net_payable_account_ref="acct-payroll-payable",
)


def _conn(ddl: tuple[str, ...]):
    engine = sa.create_engine("sqlite://")
    conn = engine.connect()
    for schema in _SCHEMAS:
        conn.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    for statement in ddl:
        conn.execute(sa.text(statement))
    return conn


def test_pay_structure_revisions_mint_and_idempotence() -> None:
    src = _conn(
        (
            "CREATE TABLE public.salary_structure (structure_id TEXT, organization_id TEXT, created_at TEXT)",
            "INSERT INTO public.salary_structure VALUES ('st1','t1','2025-01-01 00:00:00')",
            "INSERT INTO public.salary_structure VALUES ('stX','t1','2025-01-01 00:00:00')",
        )
    )
    dst = _conn(
        (
            "CREATE TABLE mod_payroll.pay_structures (id TEXT)",
            "INSERT INTO mod_payroll.pay_structures VALUES ('st1')",
            "CREATE TABLE mod_payroll.pay_structure_revisions (id TEXT, tenant_id TEXT, structure_id TEXT, "
            "version INTEGER, effective_from TEXT, effective_to TEXT, net_payable_account_ref TEXT, "
            "published_by_id TEXT, published_at TEXT)",
        )
    )
    refused = run_mint_step(
        "pay_structure_revisions", src, dst, MintContext(tenant_id="t1", occurred_at=CTX.occurred_at)
    )
    assert refused.refused and "net_payable" in refused.refused

    first = run_mint_step("pay_structure_revisions", src, dst, CTX)
    assert (first.created, first.existing) == (1, 0)
    assert any("not yet imported" in b for b in first.blockers)  # stX
    second = run_mint_step("pay_structure_revisions", src, dst, CTX)
    assert (second.created, second.existing) == (0, 1)
    row = dst.execute(
        sa.text("SELECT version, net_payable_account_ref, length(id) FROM mod_payroll.pay_structure_revisions")
    ).one()
    assert tuple(row) == (1, "acct-payroll-payable", 32)


def test_tax_authorities_and_rule_policy_gate() -> None:
    src = _conn(
        (
            "CREATE TABLE public.tax_jurisdiction (organization_id TEXT, jurisdiction_level TEXT, "
            "tax_authority_name TEXT)",
            "INSERT INTO public.tax_jurisdiction VALUES ('t1','federal','FIRS')",
            "INSERT INTO public.tax_jurisdiction VALUES ('t1','state','LIRS')",
            "CREATE TABLE public.tax_code (tax_code_id TEXT, organization_id TEXT, tax_code TEXT, tax_type TEXT, "
            "tax_rate REAL, effective_from TEXT, effective_to TEXT, is_inclusive INTEGER, recovery_rate REAL, "
            "is_recoverable INTEGER, is_active INTEGER)",
            "INSERT INTO public.tax_code VALUES ('tc1','t1','VAT','vat',0.075,'2025-01-01',NULL,0,1.0,1,1)",
            "CREATE TABLE public.tax_band (tax_band_id TEXT, organization_id TEXT, name TEXT, min_amount REAL, "
            "max_amount REAL, rate REAL, sequence INTEGER, is_active INTEGER)",
            "INSERT INTO public.tax_band VALUES ('tb1','t1','PAYE bands',0,300000,0.07,1,1)",
        )
    )
    dst = _conn(
        (
            "CREATE TABLE mod_tax.tax_authorities (id TEXT, tenant_id TEXT, code TEXT, name TEXT, "
            "authority_level_code TEXT, status TEXT, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE mod_tax.tax_rules (id TEXT, tenant_id TEXT, is_published INTEGER, tax_code_id TEXT, "
            "version INTEGER, effective_from TEXT, effective_to TEXT, priority INTEGER, fact_kind TEXT, "
            "recognition_basis_code TEXT, transaction_side TEXT, calculation_method TEXT, rate REAL, "
            "fixed_amount REAL, inclusive INTEGER, recoverable_rate REAL, treatment_code TEXT, "
            "calculation_sequence INTEGER, calculation_base_code TEXT, published_at TEXT)",
            "CREATE TABLE mod_tax.tax_rule_bands (id TEXT, tenant_id TEXT, rule_id TEXT, sequence INTEGER, "
            "lower_bound REAL, upper_bound REAL, rate REAL)",
        )
    )
    authorities = run_mint_step("tax_authorities", src, dst, CTX)
    assert authorities.created == 2
    assert EnrolmentMaps(src, dst).get("tax_authority_by_level").keys() == {"federal", "state"}

    refused = run_mint_step("tax_rules", src, dst, CTX)
    assert refused.refused and "vat" in refused.refused

    policy = {
        "vat": {
            "fact_kind": "supply",
            "recognition_basis_code": "accrual",
            "transaction_side": "output",
            "calculation_method": "percentage",
            "treatment_code": "standard",
            "calculation_base_code": "net_amount",
        }
    }
    ctx = MintContext(
        tenant_id="t1",
        actor_id="actor1",
        occurred_at=CTX.occurred_at,
        tax_rule_policy=policy,
        tax_band_rule_binding={"PAYE bands": "VAT"},
    )
    rules = run_mint_step("tax_rules", src, dst, ctx)
    assert rules.created == 2 and not rules.blockers  # 1 rule + 1 band
    again = run_mint_step("tax_rules", src, dst, ctx)
    assert again.created == 0 and again.existing == 2


def test_sourcing_lines_sales_leads_and_document_types() -> None:
    src = _conn(
        (
            "CREATE TABLE public.purchase_requisition_line (line_id TEXT, requisition_id TEXT, line_number INTEGER, "
            "item_id TEXT, description TEXT, quantity REAL, uom TEXT, estimated_unit_price REAL, delivery_date TEXT)",
            "INSERT INTO public.purchase_requisition_line VALUES "
            "('rl1','req1',1,'it1','Cable',10,'m',5.0,'2026-01-01')",
            "CREATE TABLE public.request_for_quotation (rfq_id TEXT, requisition_id TEXT)",
            "INSERT INTO public.request_for_quotation VALUES ('ev1','req1')",
            "CREATE TABLE public.customer (customer_id TEXT, organization_id TEXT, legal_name TEXT, "
            "trading_name TEXT, currency_code TEXT)",
            "INSERT INTO public.customer VALUES ('cu1','t1','Acme Ltd',NULL,'NGN')",
            "INSERT INTO public.customer VALUES ('cu2','t1','No Currency Ltd',NULL,NULL)",
            "CREATE TABLE public.hr_document (organization_id TEXT, category TEXT)",
            "INSERT INTO public.hr_document VALUES ('t1','Policy Documents')",
            "INSERT INTO public.hr_document VALUES ('t1',NULL)",
        )
    )
    dst = _conn(
        (
            "CREATE TABLE mod_procurement.sourcing_event_lines (id TEXT, tenant_id TEXT, event_id TEXT, "
            "line_number INTEGER, description TEXT, quantity REAL, unit TEXT, source_requisition_line_id TEXT, "
            "item_ref TEXT, target_unit_cost REAL, requested_delivery_date TEXT, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE mod_sales.pipelines (id TEXT, tenant_id TEXT, name TEXT, "
            "description TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE mod_sales.pipeline_stages (id TEXT, tenant_id TEXT, pipeline_id TEXT, name TEXT, "
            "order_index INTEGER, default_probability INTEGER, is_active INTEGER, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE mod_sales.leads (id TEXT, tenant_id TEXT, subject_kind TEXT, subject_opaque_id TEXT, "
            "subject_version TEXT, subject_label TEXT, title TEXT, pipeline_id TEXT, stage_id TEXT, status TEXT, "
            "probability INTEGER, currency TEXT, estimated_value REAL, expected_close_at TEXT, notes TEXT, "
            "won_at TEXT, lost_at TEXT, created_at TEXT, updated_at TEXT)",
            "CREATE TABLE mod_documents.document_libraries (id TEXT, tenant_id TEXT, code TEXT, "
            "name TEXT, description TEXT, created_by TEXT, created_at TEXT)",
            "CREATE TABLE mod_documents.document_type_versions (id TEXT, tenant_id TEXT, "
            "type_code TEXT, version INTEGER, "
            "metadata_schema TEXT, required_fields TEXT, allowed_transitions TEXT, approval_required_states TEXT, "
            "major_minor INTEGER, created_by TEXT, created_at TEXT)",
        )
    )
    lines = run_mint_step("sourcing_event_lines", src, dst, CTX)
    assert lines.created == 1
    assert EnrolmentMaps(src, dst).get("sourcing_line_by_requisition_line") == {"rl1": lines and _only_line_id(dst)}

    leads = run_mint_step("sales_leads", src, dst, CTX)
    assert leads.created == 3  # pipeline + stage + 1 lead
    assert any("cu2" in b for b in leads.blockers)

    docs = run_mint_step("document_libraries_types", src, dst, CTX)
    assert docs.created == 2  # library + policy-documents type
    assert any("no usable category" in b for b in docs.blockers)
    for step in ("sourcing_event_lines", "sales_leads", "document_libraries_types"):
        assert run_mint_step(step, src, dst, CTX).created == 0


def _only_line_id(dst) -> str:
    return dst.execute(sa.text("SELECT id FROM mod_procurement.sourcing_event_lines")).scalar()


def test_lazy_mint_feeds_structure_revision_map_in_pipeline() -> None:
    src = _conn(
        (
            "CREATE TABLE public.salary_structure (structure_id TEXT, organization_id TEXT, created_at TEXT)",
            "INSERT INTO public.salary_structure VALUES ('st1','t1','2025-01-01 00:00:00')",
            "CREATE TABLE salary_structure_assignment (assignment_id TEXT, structure_id TEXT)",
            "INSERT INTO salary_structure_assignment VALUES ('as1','st1')",
        )
    )
    dst = _conn(
        (
            "CREATE TABLE mod_payroll.pay_structures (id TEXT)",
            "INSERT INTO mod_payroll.pay_structures VALUES ('st1')",
            "CREATE TABLE mod_payroll.pay_structure_revisions (id TEXT, tenant_id TEXT, structure_id TEXT, "
            "version INTEGER, effective_from TEXT, effective_to TEXT, net_payable_account_ref TEXT, "
            "published_by_id TEXT, published_at TEXT)",
            "CREATE TABLE employee_pay_assignments (id TEXT, revision_id TEXT)",
        )
    )
    spec = build_spec(
        source_table="salary_structure_assignment",
        destination_table="employee_pay_assignments",
        source_columns=("assignment_id", "structure_id"),
        destination_columns=(
            DestinationColumn("id", nullable=False, has_default=False),
            DestinationColumn("revision_id", nullable=False, has_default=False),
        ),
        renames={"id": "assignment_id", "revision_id": "structure_id"},
        fk_rewrites={"revision_id": "structure_revision"},
    )
    enrolment = EnrolmentMaps(src, dst, mint_context=CTX)
    receipt = run_import(src, dst, (spec,), enrolment=enrolment)
    assert receipt.complete
    assert [(m.step, m.created) for m in receipt.mints] == [("pay_structure_revisions", 1)]
    revision_id = dst.execute(sa.text("SELECT id FROM mod_payroll.pay_structure_revisions")).scalar()
    written = dst.execute(sa.text("SELECT revision_id FROM employee_pay_assignments")).scalar()
    assert written == revision_id


def test_kernel_tenants_mint_and_db_error_refusal() -> None:
    src = _conn(
        (
            "CREATE TABLE organization (organization_id TEXT, organization_code TEXT, legal_name TEXT, "
            "trading_name TEXT, is_active INTEGER)",
            "INSERT INTO organization VALUES ('org1','DOTMAC','Dotmac Technologies Ltd',NULL,1)",
        )
    )
    dst = _conn(
        ("CREATE TABLE tenants (id TEXT, slug TEXT, name TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT)",)
    )
    first = run_mint_step("kernel_tenants", src, dst, CTX)
    assert (first.created, first.existing, first.refused) == (1, 0, None)
    row = dst.execute(sa.text("SELECT id, slug, name FROM tenants")).one()
    assert tuple(row) == ("org1", "dotmac", "Dotmac Technologies Ltd")
    assert run_mint_step("kernel_tenants", src, dst, CTX).existing == 1
    # A destination without the table records a named refusal, no raise.
    empty = _conn(())
    refused = run_mint_step("kernel_tenants", src, empty, CTX)
    assert refused.refused and "database error" in refused.refused


def test_bank_institutions_from_accounts_mint_and_map() -> None:
    src = _conn(
        (
            "CREATE TABLE public.bank_accounts (bank_account_id TEXT, bank_name TEXT, bank_code TEXT)",
            "INSERT INTO public.bank_accounts VALUES ('a1','Zenith Bank','057')",
            "INSERT INTO public.bank_accounts VALUES ('a2','Paystack','0000')",
            "INSERT INTO public.bank_accounts VALUES ('a3','Paystack',NULL)",
        )
    )
    schema_ddl = "ATTACH DATABASE ':memory:' AS mod_banking"
    src.execute(sa.text(schema_ddl))
    dst = _conn((schema_ddl,))
    dst.execute(
        sa.text(
            "CREATE TABLE mod_banking.bank_institutions "
            "(id TEXT, tenant_id TEXT, code TEXT, name TEXT, country_code TEXT, "
            "status TEXT, created_at TEXT, updated_at TEXT)"
        )
    )
    dst.execute(
        sa.text(
            "INSERT INTO mod_banking.bank_institutions VALUES "
            "('inst-zen','t1','057','Zenith Bank','NG','active','2026-01-01','2026-01-01')"
        )
    )
    result = run_mint_step("bank_institutions_from_accounts", src, dst, CTX)
    assert result.created == 1 and result.refused is None
    again = run_mint_step("bank_institutions_from_accounts", src, dst, CTX)
    assert again.created == 0
    enrol = EnrolmentMaps(src, dst, mint_context=CTX)
    mapping = enrol.get("bank_institution_by_account")
    assert mapping["a1"] == "inst-zen"
    assert mapping["a2"] == mapping["a3"]
    assert mapping["a2"] != "inst-zen"
