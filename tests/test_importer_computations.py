"""Enrolment computations: derived map values + pay_rule_bases mint."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa

from erp_v2.importer.enrolment import EnrolmentMaps
from erp_v2.importer.minting import MintContext, run_mint_step

UTC = timezone.utc
CTX = MintContext(tenant_id="t1", actor_id="a1", occurred_at=datetime(2026, 11, 8, tzinfo=UTC))
_SCHEMAS = ("public", "mod_payroll", "mod_sales", "mod_documents")


def _conn(ddl: tuple[str, ...]):
    engine = sa.create_engine("sqlite://")
    conn = engine.connect()
    for schema in _SCHEMAS:
        conn.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    for statement in ddl:
        conn.execute(sa.text(statement))
    return conn


_PAYROLL_SRC = (
    "CREATE TABLE public.salary_component (component_id TEXT, organization_id TEXT, abbr TEXT, "
    "depends_on_payment_days INTEGER)",
    "INSERT INTO public.salary_component VALUES ('c-basic','t1','BASIC',0)",
    "INSERT INTO public.salary_component VALUES ('c-pension','t1','PENSION',0)",
    "INSERT INTO public.salary_component VALUES ('c-employer','t1','EMP_PEN',0)",
    "CREATE TABLE public.salary_structure (structure_id TEXT, organization_id TEXT, created_at TEXT)",
    "INSERT INTO public.salary_structure VALUES ('st1','t1','2025-01-01')",
    "CREATE TABLE public.salary_structure_earning (earning_id TEXT, structure_id TEXT, component_id TEXT, "
    "amount REAL, amount_based_on_formula INTEGER, formula TEXT, condition TEXT, display_order INTEGER)",
    "INSERT INTO public.salary_structure_earning VALUES ('e1','st1','c-basic',1000,0,NULL,NULL,1)",
    "INSERT INTO public.salary_structure_earning VALUES ('e2','st1','c-pension',NULL,1,'BASIC * 0.08',NULL,2)",
    "INSERT INTO public.salary_structure_earning VALUES ('e3','st1','c-pension',NULL,1,'BASIC - 5',NULL,3)",
    "CREATE TABLE public.salary_structure_deduction (deduction_id TEXT, structure_id TEXT, component_id TEXT, "
    "amount REAL, amount_based_on_formula INTEGER, formula TEXT, condition TEXT, display_order INTEGER)",
    "CREATE TABLE public.salary_slip (slip_id TEXT, employee_id TEXT, structure_id TEXT, start_date TEXT, "
    "payment_days REAL, total_working_days REAL)",
    "INSERT INTO public.salary_slip VALUES ('s1','emp1','st1','2026-01-05',20,22)",
    "INSERT INTO public.salary_slip VALUES ('s2','empX','st1','2026-01-05',22,22)",
    "CREATE TABLE public.salary_slip_earning (line_id TEXT, slip_id TEXT, component_id TEXT, amount REAL)",
    "INSERT INTO public.salary_slip_earning VALUES ('l1','s1','c-basic',1000)",
    "INSERT INTO public.salary_slip_earning VALUES ('l2','s1','c-employer',80)",
    "CREATE TABLE public.salary_slip_deduction (line_id TEXT, slip_id TEXT, component_id TEXT, amount REAL)",
)

_PAYROLL_DST = (
    "CREATE TABLE mod_payroll.pay_components (id TEXT, code TEXT, name TEXT, kind TEXT)",
    "INSERT INTO mod_payroll.pay_components VALUES ('c-basic','BASIC','Basic','earning')",
    "INSERT INTO mod_payroll.pay_components VALUES ('c-employer','EMP_PEN','Employer pension','employer_liability')",
    "CREATE TABLE mod_payroll.pay_structure_revisions (id TEXT, structure_id TEXT, version INTEGER)",
    "INSERT INTO mod_payroll.pay_structure_revisions VALUES ('rev1','st1',1)",
    "CREATE TABLE mod_payroll.employee_pay_assignments (id TEXT, employee_ref TEXT, revision_id TEXT, "
    "effective_from TEXT, effective_to TEXT)",
    "INSERT INTO mod_payroll.employee_pay_assignments VALUES ('as1','emp1','rev1','2025-06-01',NULL)",
    "CREATE TABLE mod_payroll.pay_rule_bases (id TEXT, tenant_id TEXT, rule_id TEXT, component_id TEXT)",
)


def test_payroll_computation_maps() -> None:
    src, dst = _conn(_PAYROLL_SRC), _conn(_PAYROLL_DST)
    maps = EnrolmentMaps(src, dst)
    conversions = maps.get("payroll_rule_conversion")
    assert conversions["e1"] == {
        "calculation_method": "fixed",
        "rate": None,
        "fixed_amount": 1000.0,
    }
    assert conversions["e2"]["calculation_method"] == "percentage"
    assert conversions["e2"]["rate"] == Decimal("0.08")
    assert "e3" not in conversions  # unconvertible -> exception downstream

    assert maps.get("payroll_line_method") == {"l1": {"calculation_method": "fixed"}}

    calculations = maps.get("payroll_calculation")
    assert set(calculations) == {"s1"}  # s2 has no assignment -> no entry
    assert calculations["s1"]["assignment_id"] == "as1"
    assert calculations["s1"]["proration_factor"] == Decimal(20) / Decimal(22)
    assert calculations["s1"]["employer_liability_amount"] == Decimal(80)

    bases = run_mint_step("pay_rule_bases", src, dst, CTX)
    assert bases.created == 1
    row = dst.execute(sa.text("SELECT rule_id, component_id FROM mod_payroll.pay_rule_bases")).one()
    assert tuple(row) == ("e2", "c-basic")
    assert run_mint_step("pay_rule_bases", src, dst, CTX).existing == 1


def test_json_assemblies_and_misc_maps() -> None:
    src = _conn(
        (
            "CREATE TABLE public.supplier_invoice (invoice_id TEXT, due_date TEXT, total_amount REAL)",
            "INSERT INTO public.supplier_invoice VALUES ('inv1','2026-02-01',1250.5)",
            "INSERT INTO public.supplier_invoice VALUES ('inv2',NULL,10)",
            "CREATE TABLE public.supplier_invoice_line (line_id TEXT, cost_center_id TEXT, project_id TEXT, "
            "segment_id TEXT)",
            "INSERT INTO public.supplier_invoice_line VALUES ('sl1','cc1',NULL,'seg1')",
            "CREATE TABLE public.goods_receipt_line (receipt_id TEXT, po_line_id TEXT, quantity_received REAL, "
            "line_number INTEGER)",
            "INSERT INTO public.goods_receipt_line VALUES ('gr1','pol1',3,1)",
            "INSERT INTO public.goods_receipt_line VALUES ('gr1','pol2',0,2)",
            "CREATE TABLE public.approval_workflow (workflow_id TEXT, workflow_code TEXT)",
            "INSERT INTO public.approval_workflow VALUES ('wf1','PO_APPROVAL')",
            "CREATE TABLE public.approval_request (request_id TEXT, workflow_id TEXT)",
            "INSERT INTO public.approval_request VALUES ('rq1','wf1')",
            "CREATE TABLE public.expense_limit_evaluation (claim_id TEXT)",
            "INSERT INTO public.expense_limit_evaluation VALUES ('cl1')",
            "CREATE TABLE public.contract (contract_id TEXT)",
            "INSERT INTO public.contract VALUES ('ct1')",
            "INSERT INTO public.contract VALUES ('ct2')",
            "INSERT INTO public.contract VALUES ('ct3')",
            "CREATE TABLE public.contract_amendment_workflow (contract_id TEXT, original_contract_id TEXT)",
            "INSERT INTO public.contract_amendment_workflow VALUES ('ct2','ct1')",
            "CREATE TABLE public.asset_category (category_id TEXT, category_code TEXT)",
            "INSERT INTO public.asset_category VALUES ('cat1','vehicle')",
            "CREATE TABLE public.asset (asset_id TEXT, category_id TEXT)",
            "INSERT INTO public.asset VALUES ('a1','cat1')",
            "CREATE TABLE public.inventory_lot (lot_id TEXT)",
            "INSERT INTO public.inventory_lot VALUES ('lot1')",
            "INSERT INTO public.inventory_lot VALUES ('lot2')",
            "INSERT INTO public.inventory_lot VALUES ('lot3')",
            "CREATE TABLE public.inventory_lot_balance (lot_id TEXT, quantity_on_hand REAL, is_quarantined INTEGER)",
            "INSERT INTO public.inventory_lot_balance VALUES ('lot1',5,0)",
            "INSERT INTO public.inventory_lot_balance VALUES ('lot2',0,0)",
            "INSERT INTO public.inventory_lot_balance VALUES ('lot3',9,1)",
            "CREATE TABLE public.quote (quote_id TEXT, organization_id TEXT, customer_id TEXT)",
            "INSERT INTO public.quote VALUES ('q1','t1','cu1')",
            "CREATE TABLE public.user_credentials (id TEXT, person_id TEXT, username TEXT)",
            "INSERT INTO public.user_credentials VALUES ('u1','p1','michael')",
            "CREATE TABLE public.hr_document (document_id TEXT, category TEXT, file_path TEXT)",
            "INSERT INTO public.hr_document VALUES ('d1','Policy Documents','hr/policies/handbook.pdf')",
            "INSERT INTO public.hr_document VALUES ('d2','Policy Documents','bare.pdf')",
        )
    )
    dst = _conn(
        (
            "CREATE TABLE mod_sales.leads (id TEXT, tenant_id TEXT, subject_opaque_id TEXT)",
            "INSERT INTO mod_sales.leads VALUES ('lead1','t1','cu1')",
            "CREATE TABLE mod_documents.document_libraries (id TEXT, tenant_id TEXT, code TEXT)",
            "INSERT INTO mod_documents.document_libraries VALUES ('lib1','t1','hr')",
        )
    )
    maps = EnrolmentMaps(src, dst)
    schedule = json.loads(maps.get("supplier_payment_schedule")["inv1"]["payment_schedule"])
    assert schedule == [{"amount": "1250.5", "due_date": "2026-02-01"}]
    assert set(maps.get("supplier_payment_schedule")) == {"inv1"}
    assert json.loads(maps.get("supplier_line_dimensions")["sl1"]["dimension_refs"]) == [
        ["COST_CENTER", "cc1"],
        ["SEGMENT", "seg1"],
    ]
    assert maps.get("receipt_lines")["gr1"]["lines_json"] == ('[{"order_line_id":"pol1","quantity_received":"3"}]')
    assert maps.get("approval_request_policy") == {"rq1": {"policy_code": "PO_APPROVAL", "policy_version": 1}}
    assert set(maps.get("expense_evaluation_batch")) == {"cl1"}
    families = maps.get("agreement_family")
    assert families["ct1"] == families["ct2"] != families["ct3"]
    assert maps.get("asset_kind") == {"a1": {"kind": "vehicle"}}
    assert maps.get("lot_status") == {
        "lot1": {"status": "available"},
        "lot2": {"status": "depleted"},
        "lot3": {"status": "quarantined"},
    }
    assert maps.get("quote_lead") == {"q1": {"lead_id": "lead1"}}
    assert maps.get("user_label") == {"u1": {"label": "michael"}}
    assert maps.get("document_library") == {"t1": {"library_id": "lib1"}}
    assert maps.get("document_type") == {"d1": {"type_code": "policy-documents", "folder_path": "hr/policies"}}
