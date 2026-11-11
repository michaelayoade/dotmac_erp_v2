"""Batch-2 enrolment computations + multi-schema source resolution."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
import sqlalchemy as sa

from erp_v2.importer.cli import _resolve_sources
from erp_v2.importer.enrolment import EnrolmentMaps
from erp_v2.importer.wave2 import Pairing
from erp_v2.importer.waves36 import ALL_WAVE_PAIRINGS

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


@pytest.mark.parametrize("shape", ["resolved", "missing", "ambiguous", "unreadable"])
def test_payment_refusal_resolution_is_preserved_or_named_incomplete(shape, monkeypatch):
    payment = next(p for p in ALL_WAVE_PAIRINGS if p.source == "public.supplier_payment")
    ddl = () if shape == "missing" else ("CREATE TABLE ap.supplier_payment (payment_id TEXT)",)
    if shape == "ambiguous":
        ddl += ("CREATE TABLE public.supplier_payment (payment_id TEXT)",)
    engine = _engine(ddl)
    if shape == "unreadable":
        inspector = sa.inspect(engine)

        def unreadable(*args, **kwargs):
            raise sa.exc.OperationalError("metadata unavailable", {}, Exception("private error"))

        monkeypatch.setattr(inspector, "get_columns", unreadable)
        monkeypatch.setattr("erp_v2.importer.cli.sa.inspect", lambda engine: inspector)
    resolved, columns, report, _ = _resolve_sources(engine, (payment,))
    if shape == "resolved":
        assert resolved == (replace(payment, source="ap.supplier_payment"),)
        assert columns["ap.supplier_payment"] == ("payment_id",)
        assert report["refusal_sources_incomplete"] == []
    else:
        assert resolved == ()
        assert report[shape] == [payment.source]
        assert report["refusal_sources_incomplete"] == [payment.source]


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
        with pytest.raises(KeyError, match="no enrolment builder"):
            maps.get("settlement_obligation")
        terms = maps.get("payment_intent_terms")
        assert terms["pi1"] == {
            "payer_reference": "a@b.c",
            "purpose": "INVOICE_SETTLEMENT",
            "channel": "card",
        }
        assert "pi2" not in terms


def test_id_systematic_prefix_and_s_ending_names() -> None:
    from erp_v2.importer.spec import DestinationColumn, build_spec

    def mapped_id(source_table: str, columns: tuple[str, ...]) -> str | None:
        spec = build_spec(
            source_table=source_table,
            destination_table="dest",
            source_columns=columns,
            destination_columns=(DestinationColumn("id", nullable=False, has_default=False),),
        )
        return spec.column_map.get("id")

    # Gate 4 live findings: s-ending singulars and prefix-form PKs.
    assert mapped_id("hr.case_witness", ("witness_id",)) == "witness_id"
    assert mapped_id("fleet.fuel_log_entry", ("fuel_log_id",)) == "fuel_log_id"
    assert mapped_id("fleet.maintenance_record", ("maintenance_id",)) == "maintenance_id"
    assert mapped_id("lease.lease_contract", ("lease_id",)) == "lease_id"
    # Established forms still win in order.
    assert mapped_id("public.salary_slip", ("slip_id", "salary_id")) == "slip_id"
    assert mapped_id("public.department", ("department_id",)) == "department_id"


def test_file_and_vehicle_asset_links() -> None:
    src_engine = _engine(
        (
            "CREATE TABLE case_document (document_id TEXT, file_path TEXT)",
            "INSERT INTO case_document VALUES ('cd1','cases/a.pdf')",
            "INSERT INTO case_document VALUES ('cd2','missing.pdf')",
            "CREATE TABLE vehicle_document (document_id TEXT, file_path TEXT)",
            "INSERT INTO vehicle_document VALUES ('vd1','fleet/b.pdf')",
            "CREATE TABLE vehicle (vehicle_id TEXT, vin TEXT)",
            "INSERT INTO vehicle VALUES ('v1','vin-123')",
            "INSERT INTO vehicle VALUES ('v2','vin-999')",
        )
    )
    dst_engine = _engine(())
    with _conn(dst_engine) as dst:
        dst.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_files"))
        dst.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_assets"))
        dst.execute(sa.text("CREATE TABLE mod_files.stored_files (id TEXT, storage_key TEXT)"))
        dst.execute(sa.text("INSERT INTO mod_files.stored_files VALUES ('f1','cases/a.pdf')"))
        dst.execute(sa.text("INSERT INTO mod_files.stored_files VALUES ('f2','fleet/b.pdf')"))
        dst.execute(sa.text("CREATE TABLE mod_assets.assets (id TEXT, serial_number TEXT)"))
        dst.execute(sa.text("INSERT INTO mod_assets.assets VALUES ('as1','VIN-123')"))
        with _conn(src_engine) as src:
            maps = EnrolmentMaps(src, dst)
            assert maps.get("case_document_file") == {"cd1": {"file_id": "f1"}}
            assert maps.get("vehicle_document_file") == {"vd1": {"file_id": "f2"}}
            assert maps.get("vehicle_asset") == {"v1": {"asset_id": "as1"}}
