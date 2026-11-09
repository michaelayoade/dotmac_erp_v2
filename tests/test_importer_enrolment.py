"""Enrolment map builders + pipeline wiring on synthetic schemas."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from erp_v2.importer.enrolment import MAP_BUILDERS, EnrolmentMaps, MapConflict
from erp_v2.importer.pipeline import run_import
from erp_v2.importer.spec import DestinationColumn, build_spec

_SCHEMAS = (
    "public",
    "mod_payroll",
    "mod_tax",
    "mod_procurement",
    "mod_banking",
    "mod_accounting",
    "mod_inventory",
)


def _conn(ddl: tuple[str, ...]):
    engine = sa.create_engine("sqlite://")
    conn = engine.connect()
    for schema in _SCHEMAS:
        conn.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    for statement in ddl:
        conn.execute(sa.text(statement))
    return conn


def test_every_declared_map_has_a_builder() -> None:
    from erp_v2.importer.e2 import E2_PAIRINGS, FLEET_PAIRINGS, LEASE_PAIRINGS, PUBLIC_SECTOR_PAIRINGS
    from erp_v2.importer.wave2 import WAVE2_PAIRINGS
    from erp_v2.importer.waves36 import ALL_WAVE_PAIRINGS

    declared: set[str] = set()
    for pairing in (
        WAVE2_PAIRINGS + ALL_WAVE_PAIRINGS + E2_PAIRINGS + FLEET_PAIRINGS + PUBLIC_SECTOR_PAIRINGS + LEASE_PAIRINGS
    ):
        declared.update(pairing.fk_rewrites.values())
        declared.update(map_name for map_name, _k, _f in pairing.lookups.values())
    assert declared == set(MAP_BUILDERS), declared ^ set(MAP_BUILDERS)


def test_source_built_maps_and_conflict() -> None:
    src = _conn(
        (
            "CREATE TABLE public.employee (employee_id TEXT, person_id TEXT)",
            "INSERT INTO public.employee VALUES ('e1','p1')",
            "CREATE TABLE public.user_credentials (id TEXT, person_id TEXT)",
            "INSERT INTO public.user_credentials VALUES ('u1','p2')",
            "CREATE TABLE public.supplier (supplier_id TEXT, legal_name TEXT, trading_name TEXT)",
            "INSERT INTO public.supplier VALUES ('s1','Legal Ltd','Trading')",
            "INSERT INTO public.supplier VALUES ('s2','','Only Trading')",
            "CREATE TABLE public.payment_intent (intent_id TEXT, customer_payment_id TEXT)",
            "INSERT INTO public.payment_intent VALUES ('i1','cp1')",
            "INSERT INTO public.payment_intent VALUES ('i2',NULL)",
            "CREATE TABLE public.project_template_task (template_task_id TEXT, template_id TEXT)",
            "INSERT INTO public.project_template_task VALUES ('tt1','tpl1')",
            "CREATE TABLE public.quotation_response (response_id TEXT, rfq_id TEXT)",
            "INSERT INTO public.quotation_response VALUES ('r1','ev1')",
        )
    )
    dst = _conn(())
    maps = EnrolmentMaps(src, dst)
    assert maps.get("employee_party") == {"e1": "p1"}
    assert maps.get("user_party") == {"u1": "p2"}
    assert maps.get("supplier") == {"s1": {"name": "Legal Ltd"}, "s2": {"name": "Only Trading"}}
    assert maps.get("payment_intent_by_payment") == {"cp1": {"intent_id": "i1"}}
    assert maps.get("project_template_task") == {"tt1": {"template_id": "tpl1"}}
    assert maps.get("bid_submission") == {"r1": {"event_id": "ev1"}}
    assert maps.get("bank_direction_by_type")["CREDIT"] == "credit"

    src.execute(sa.text("INSERT INTO public.payment_intent VALUES ('i9','cp1')"))
    with pytest.raises(MapConflict):
        EnrolmentMaps(src, dst).get("payment_intent_by_payment")


def test_destination_built_maps() -> None:
    src = _conn(
        (
            "CREATE TABLE public.salary_component (component_id TEXT, depends_on_payment_days INTEGER)",
            "INSERT INTO public.salary_component VALUES ('c1',1)",
            "CREATE TABLE public.bank_accounts (bank_account_id TEXT, bank_name TEXT, bank_code TEXT)",
            "INSERT INTO public.bank_accounts VALUES ('ba1','Access','044')",
        )
    )
    dst = _conn(
        (
            "CREATE TABLE mod_payroll.pay_structure_revisions (id TEXT, structure_id TEXT, version INTEGER)",
            "INSERT INTO mod_payroll.pay_structure_revisions VALUES ('rev1','st1',1)",
            "INSERT INTO mod_payroll.pay_structure_revisions VALUES ('rev2','st1',2)",
            "CREATE TABLE mod_payroll.pay_components (id TEXT, component_code TEXT, name TEXT, kind TEXT)",
            "INSERT INTO mod_payroll.pay_components VALUES ('c1','BASIC','Basic pay','earning')",
            "CREATE TABLE mod_tax.tax_authorities (id TEXT, authority_level_code TEXT)",
            "INSERT INTO mod_tax.tax_authorities VALUES ('a1','federal')",
            "CREATE TABLE mod_procurement.sourcing_event_lines (id TEXT, source_requisition_line_id TEXT)",
            "INSERT INTO mod_procurement.sourcing_event_lines VALUES ('sl1','rl1')",
            "INSERT INTO mod_procurement.sourcing_event_lines VALUES ('sl2',NULL)",
            "CREATE TABLE mod_banking.bank_institutions (id TEXT, code TEXT, name TEXT)",
            "INSERT INTO mod_banking.bank_institutions VALUES ('b1','044','Access')",
            "CREATE TABLE mod_accounting.fiscal_periods (id TEXT, end_date TEXT)",
            "INSERT INTO mod_accounting.fiscal_periods VALUES ('fp1','2026-01-31')",
            "CREATE TABLE mod_inventory.items (id TEXT, base_uom TEXT)",
            "INSERT INTO mod_inventory.items VALUES ('it1','each')",
        )
    )
    maps = EnrolmentMaps(src, dst)
    assert maps.get("structure_revision") == {"st1": "rev1"}  # v1 only
    assert maps.get("tax_authority_by_level") == {"federal": "a1"}
    assert maps.get("sourcing_line_by_requisition_line") == {"rl1": "sl1"}
    assert maps.get("bank_institution_by_account") == {"ba1": "b1"}
    assert maps.get("fiscal_period") == {"fp1": {"end_date": "2026-01-31"}}
    assert maps.get("inventory_item") == {"it1": {"base_uom": "each"}}
    assert maps.get("pay_component") == {
        "c1": {"code": "BASIC", "name": "Basic pay", "kind": "earning", "prorates": True}
    }

    dst.execute(sa.text("INSERT INTO mod_payroll.pay_components VALUES ('cX','X','X','earning')"))
    with pytest.raises(MapConflict, match="prorates"):
        EnrolmentMaps(src, dst).get("pay_component")


def test_pipeline_builds_maps_per_spec_with_uuid_tolerance() -> None:
    structure = uuid.uuid4()
    revision = uuid.uuid4()
    src = _conn(("CREATE TABLE salary_structure_assignment (assignment_id TEXT, structure_id TEXT)",))
    src.execute(
        sa.text("INSERT INTO salary_structure_assignment VALUES ('as1', :sid)"),
        {"sid": str(structure)},  # dashed spelling in the legacy row
    )
    dst = _conn(
        (
            "CREATE TABLE mod_payroll.pay_structure_revisions (id TEXT, structure_id TEXT, version INTEGER)",
            "CREATE TABLE employee_pay_assignments (id TEXT, revision_id TEXT)",
        )
    )
    dst.execute(
        sa.text("INSERT INTO mod_payroll.pay_structure_revisions VALUES (:rid, :sid, 1)"),
        {"rid": revision.hex, "sid": structure.hex},  # hex spelling in module storage
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
    receipt = run_import(src, dst, (spec,), enrolment=EnrolmentMaps(src, dst))
    assert receipt.complete
    assert receipt.enrolment_maps == {"structure_revision": 1}
    row = dst.execute(sa.text("SELECT revision_id FROM employee_pay_assignments")).one()
    assert row[0] == revision.hex
