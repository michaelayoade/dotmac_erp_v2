"""Batch-2 enrolment computations + multi-schema source resolution."""

from __future__ import annotations

import json
from decimal import Decimal

import sqlalchemy as sa

from erp_v2.importer.cli import _resolve_sources
from erp_v2.importer.enrolment import EnrolmentMaps
from erp_v2.importer.wave2 import Pairing

_SCHEMAS = ("public", "hr", "ap", "mod_payables", "mod_sales")


def _engine(ddl: tuple[str, ...]):
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        for schema in _SCHEMAS:
            conn.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        for statement in ddl:
            conn.execute(sa.text(statement))
        conn.commit()
    return engine


def _conn(engine):
    return engine.connect()


def test_source_resolution_across_schemas() -> None:
    engine = _engine(
        (
            "CREATE TABLE hr.employee (employee_id TEXT, person_id TEXT)",
            "CREATE TABLE public.department (department_id TEXT)",
            "CREATE TABLE hr.shared (id TEXT)",
            "CREATE TABLE public.shared (id TEXT)",
        )
    )
    pairings = (
        Pairing("mod_people.employees", "public.employee"),
        Pairing("mod_people.departments", "public.department"),
        Pairing("mod_x.shared", "public.shared"),
        Pairing("mod_x.absent", "public.absent_table"),
        Pairing("mod_x.generated", None),
    )
    resolved, columns, report, schemas = _resolve_sources(engine, pairings)
    by_dest = {p.destination: p.source for p in resolved}
    assert by_dest["mod_people.employees"] == "hr.employee"
    assert by_dest["mod_people.departments"] == "public.department"
    assert "mod_x.shared" not in by_dest  # ambiguous excluded
    assert "mod_x.absent" not in by_dest
    assert report["resolved"] == {"public.employee": "hr.employee"}
    assert report["ambiguous"] == ["public.shared"]
    assert report["missing"] == ["public.absent_table"]
    assert columns["hr.employee"] == ("employee_id", "person_id")
    assert set(schemas) == {"hr", "public"}


def test_unqualified_builder_sql_finds_nonpublic_schema() -> None:
    engine = _engine(
        (
            "CREATE TABLE hr.employee (employee_id TEXT, person_id TEXT)",
            "INSERT INTO hr.employee VALUES ('e1','p1')",
        )
    )
    with _conn(engine) as src, _conn(_engine(())) as dst:
        assert EnrolmentMaps(src, dst).get("employee_party") == {"e1": "p1"}


def test_batch2_computations() -> None:
    src_engine = _engine(
        (
            "CREATE TABLE bid_evaluation (evaluation_id TEXT, recommended_response_id TEXT)",
            "INSERT INTO bid_evaluation VALUES ('ev1','bid2')",
            "CREATE TABLE bid_evaluation_score (evaluation_id TEXT, response_id TEXT, criterion_name TEXT, "
            "score REAL, weighted_score REAL, comments TEXT)",
            "INSERT INTO bid_evaluation_score VALUES ('ev1','bid1','Price',80,24,'cheap')",
            "INSERT INTO bid_evaluation_score VALUES ('ev1','bid2','Price',90,27,NULL)",
            "INSERT INTO bid_evaluation_score VALUES ('ev1','bid2','Quality',70,35,'solid')",
            "CREATE TABLE tax_code (tax_code_id TEXT, tax_rate REAL)",
            "INSERT INTO tax_code VALUES ('tc1',0.075)",
            "INSERT INTO tax_code VALUES ('tc2',0.05)",
            "CREATE TABLE quote_line (quote_id TEXT, tax_code_id TEXT)",
            "INSERT INTO quote_line VALUES ('q1','tc1')",
            "INSERT INTO quote_line VALUES ('q1','tc1')",
            "INSERT INTO quote_line VALUES ('q2','tc1')",
            "INSERT INTO quote_line VALUES ('q2','tc2')",
            "CREATE TABLE organization (organization_id TEXT, functional_currency_code TEXT)",
            "INSERT INTO organization VALUES ('t1','NGN')",
            "CREATE TABLE asset_assignment (asset_id TEXT, issued_on TEXT, returned_on TEXT, "
            "condition_on_issue TEXT, condition_on_return TEXT)",
            "INSERT INTO asset_assignment VALUES ('a1','2025-01-01','2025-06-01','Good','FAIR')",
            "INSERT INTO asset_assignment VALUES ('a2','2025-01-01',NULL,'mint',NULL)",
            "CREATE TABLE payment_allocation (payment_id TEXT, invoice_id TEXT, allocated_amount REAL)",
            "INSERT INTO payment_allocation VALUES ('pay1','inv1',100)",
            "INSERT INTO payment_allocation VALUES ('pay2','inv1',40)",
            "INSERT INTO payment_allocation VALUES ('pay2','inv2',60)",
            "CREATE TABLE supplier_payment (payment_id TEXT, amount REAL)",
            "INSERT INTO supplier_payment VALUES ('pay1',100)",
            "INSERT INTO supplier_payment VALUES ('pay2',100)",
            "CREATE TABLE payment_intent (intent_id TEXT, direction TEXT, email TEXT, source_type TEXT, "
            "gateway_response TEXT)",
            "INSERT INTO payment_intent VALUES ('pi1','INBOUND','a@b.c','INVOICE','{\"channel\": \"card\"}')",
            "INSERT INTO payment_intent VALUES ('pi2','OUTBOUND','x@y.z','EXPENSE_CLAIM',NULL)",
        )
    )
    dst_engine = _engine(
        (
            "CREATE TABLE mod_payables.payment_obligations (id TEXT, invoice_id TEXT)",
            "INSERT INTO mod_payables.payment_obligations VALUES ('ob1','inv1')",
        )
    )
    with _conn(src_engine) as src, _conn(dst_engine) as dst:
        maps = EnrolmentMaps(src, dst)
        scores = maps.get("bid_scores")["ev1"]
        assert scores["selected_total_score"] == Decimal(62)
        docs = json.loads(scores["scores_json"])
        assert docs[1] == {
            "bid_id": "bid2",
            "comments": "solid",
            "scores": {"price": "90.0", "quality": "70.0"},
            "weighted_total": "62",
        }
        assert maps.get("quote_tax_rate") == {"q1": {"tax_rate": Decimal("7.5")}}
        assert maps.get("tenant_currency") == {"t1": {"currency_code": "NGN"}}
        assert maps.get("asset_condition") == {"a1": {"condition": "fair"}}
        assert maps.get("settlement_obligation") == {"pay1": {"obligation_id": "ob1"}}
        terms = maps.get("payment_intent_terms")
        assert terms["pi1"] == {
            "payer_reference": "a@b.c",
            "purpose": "INVOICE_SETTLEMENT",
            "channel": "card",
        }
        assert "pi2" not in terms
