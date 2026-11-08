"""Payroll + inventory workspace queries."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import dotmac_inventory.models as inv
import dotmac_payroll.models as pay
import sqlalchemy as sa
from dotmac_kernel.models import Base, Tenant
from sqlalchemy.orm import Session

from erp_v2.web import inventory_queries as iq
from erp_v2.web import payroll_queries as pq


def _session(tables, *schemas):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        for schema in schemas:
            c.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    Base.metadata.create_all(engine, tables=[Tenant.__table__, *tables])
    s = Session(engine)
    tenant = Tenant(id=uuid.uuid4(), slug="acme", name="Acme")
    s.add(tenant)
    s.flush()
    return s, tenant


def test_payroll_run_totals_from_calculations() -> None:
    tables = [
        pay.PayComponent.__table__,
        pay.PayStructure.__table__,
        pay.EmployeePayAssignment.__table__,
        pay.PayrollRun.__table__,
        pay.PayrollCalculation.__table__,
    ]
    s, tenant = _session(tables, "mod_payroll")
    s.add(pay.PayComponent(tenant_id=tenant.id, component_code="BASIC", name="Basic", kind="earning", status="active"))
    run = pay.PayrollRun(
        tenant_id=tenant.id,
        run_ref="2026-01",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        currency_code="NGN",
        minor_units=2,
        status="approved",
        created_by_id=uuid.uuid4(),
        created_at=datetime(2026, 1, 31, tzinfo=UTC),
    )
    s.add(run)
    s.flush()
    for ref, gross, ded in (("E1", Decimal(1000), Decimal(100)), ("E2", Decimal(2000), Decimal(250))):
        s.add(
            pay.PayrollCalculation(
                tenant_id=tenant.id,
                run_id=run.id,
                employee_ref=ref,
                assignment_id=uuid.uuid4(),
                revision_id=uuid.uuid4(),
                revision_version=1,
                proration_factor=Decimal(1),
                gross_amount=gross,
                deduction_amount=ded,
                net_amount=gross - ded,
                employer_liability_amount=Decimal(0),
                calculated_at=datetime(2026, 1, 31, tzinfo=UTC),
            )
        )
    s.flush()
    (row,) = pq.payroll_runs(s, tenant_id=tenant.id)
    assert row["calculations"] == 2
    assert row["gross"] == Decimal(3000) and row["net"] == Decimal(2650)
    assert pq.pay_components(s, tenant_id=tenant.id)[0]["code"] == "BASIC"


def test_inventory_overview_available_and_value() -> None:
    tables = [inv.Item.__table__, inv.Warehouse.__table__, inv.StockBalance.__table__, inv.StockMovement.__table__]
    s, tenant = _session(tables, "mod_inventory")
    item = inv.Item(
        tenant_id=tenant.id,
        sku="SKU-1",
        name="Widget",
        base_uom="ea",
        costing_method="weighted_average",
        currency_code="NGN",
    )
    wh = inv.Warehouse(tenant_id=tenant.id, code="MAIN", name="Main store")
    s.add_all([item, wh])
    s.flush()
    s.add(
        inv.StockBalance(
            tenant_id=tenant.id,
            item_id=item.id,
            warehouse_id=wh.id,
            quantity_on_hand=Decimal(10),
            quantity_reserved=Decimal(3),
            total_value=Decimal(1000),
            current_unit_cost=Decimal(100),
        )
    )
    s.add(
        inv.StockMovement(
            tenant_id=tenant.id,
            movement_group_id=uuid.uuid4(),
            kind="receipt",
            item_id=item.id,
            warehouse_id=wh.id,
            quantity_delta=Decimal(10),
            unit_cost=Decimal(100),
            value_delta=Decimal(1000),
            cost_variance=Decimal(0),
            quantity_after=Decimal(10),
            value_after=Decimal(1000),
            currency_code="NGN",
            source_ref="PO-1",
            occurred_at=datetime(2026, 1, 5, tzinfo=UTC),
        )
    )
    s.flush()
    overview = iq.stock_overview(s, tenant_id=tenant.id)
    assert overview["lines"][0]["available"] == Decimal(7)
    assert overview["total_value"] == Decimal(1000)
    assert iq.recent_movements(s, tenant_id=tenant.id)[0]["sku"] == "SKU-1"
