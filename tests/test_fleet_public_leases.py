"""Fleet, public-sector and lease slices: guards and money math."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import dotmac_assets.models as asset_models
import dotmac_people.models as people_models
import pytest
import sqlalchemy as sa
from dotmac_kernel.models import Base, Tenant
from sqlalchemy.orm import Session

from erp_v2 import fleet as fl
from erp_v2 import leases as ls
from erp_v2 import public_sector as ps


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_people"))
        c.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_assets"))
    tables = [
        Tenant.__table__,
        people_models.Department.__table__,
        people_models.Designation.__table__,
        people_models.EmploymentType.__table__,
        people_models.Employee.__table__,
        asset_models.Base.metadata.tables["mod_assets.assets"],
    ] + [Base.metadata.tables[n] for n in fl.FLEET_TABLES + ps.PUBLIC_SECTOR_TABLES + ls.LEASE_TABLES]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as s:
        tenant_id, employee_id, asset_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        s.add(Tenant(id=tenant_id, slug="acme", name="Acme"))
        s.add(
            people_models.Employee(
                id=employee_id,
                tenant_id=tenant_id,
                party_id=uuid.uuid4(),
                employee_code="E001",
                date_of_joining=date(2025, 1, 1),
            )
        )
        asset_table = asset_models.Base.metadata.tables["mod_assets.assets"]
        s.execute(
            asset_table.insert().values(
                id=asset_id,
                tenant_id=tenant_id,
                code="VEH-1",
                name="Vehicle",
                kind="vehicle",
                state="in_service",
                condition="good",
            )
        )
        s.flush()
        yield s, tenant_id, employee_id, asset_id


def test_fleet_fuel_and_reservation_overlap(session) -> None:
    s, tenant_id, employee_id, asset_id = session
    profile = fl.VehicleProfile(tenant_id=tenant_id, asset_id=asset_id, code="V1", registration_number="ABC-123")
    s.add(profile)
    s.flush()
    entry = fl.log_fuel(
        s,
        tenant_id=tenant_id,
        profile_id=profile.id,
        log_date=date(2026, 1, 5),
        quantity_liters=Decimal(40),
        price_per_liter=Decimal("950.5"),
        odometer_reading=Decimal(10000),
    )
    assert entry.total_cost == Decimal("38020.00")
    with pytest.raises(fl.FleetError, match="odometer"):
        fl.log_fuel(
            s,
            tenant_id=tenant_id,
            profile_id=profile.id,
            log_date=date(2026, 1, 6),
            quantity_liters=Decimal(1),
            price_per_liter=Decimal(1),
            odometer_reading=Decimal(9999),
        )
    tz = timezone.utc
    r1 = fl.VehicleReservation(
        tenant_id=tenant_id,
        profile_id=profile.id,
        employee_id=employee_id,
        start_datetime=datetime(2026, 2, 2, 9, tzinfo=tz),
        end_datetime=datetime(2026, 2, 2, 12, tzinfo=tz),
    )
    r2 = fl.VehicleReservation(
        tenant_id=tenant_id,
        profile_id=profile.id,
        employee_id=employee_id,
        start_datetime=datetime(2026, 2, 2, 11, tzinfo=tz),
        end_datetime=datetime(2026, 2, 2, 13, tzinfo=tz),
    )
    s.add_all([r1, r2])
    s.flush()
    fl.approve_reservation(s, tenant_id=tenant_id, reservation_id=r1.id)
    with pytest.raises(fl.FleetError, match="already reserved"):
        fl.approve_reservation(s, tenant_id=tenant_id, reservation_id=r2.id)
    done = fl.complete_reservation(s, tenant_id=tenant_id, reservation_id=r1.id, end_odometer=Decimal(10120))
    assert done.status == "completed" and profile.current_odometer == Decimal(10120)


def test_public_sector_commitment_control(session) -> None:
    s, tenant_id, _, _ = session
    fund = ps.Fund(tenant_id=tenant_id, code="CAPEX", name="Capital", fund_type="capital")
    s.add(fund)
    s.flush()
    a1 = ps.Appropriation(
        tenant_id=tenant_id,
        fund_id=fund.id,
        fiscal_year_ref="FY26",
        code="A1",
        name="Works",
        status="approved",
        approved_amount=Decimal(1000),
        revised_amount=Decimal(1000),
        currency_code="NGN",
    )
    a2 = ps.Appropriation(
        tenant_id=tenant_id,
        fund_id=fund.id,
        fiscal_year_ref="FY26",
        code="A2",
        name="Ops",
        status="approved",
        approved_amount=Decimal(100),
        revised_amount=Decimal(100),
        currency_code="NGN",
    )
    s.add_all([a1, a2])
    s.flush()
    with pytest.raises(ps.PublicSectorError, match="exceeds available"):
        ps.create_commitment(
            s,
            tenant_id=tenant_id,
            number="C-X",
            appropriation_id=a1.id,
            commitment_date=date(2026, 2, 1),
            lines=[("6000", Decimal(1001))],
        )
    c = ps.create_commitment(
        s,
        tenant_id=tenant_id,
        number="C-1",
        appropriation_id=a1.id,
        commitment_date=date(2026, 2, 1),
        lines=[("6000", Decimal(600)), ("6100", Decimal(200))],
    )
    assert ps.appropriation_available(s, tenant_id=tenant_id, appropriation_id=a1.id) == Decimal(200)
    ps.stage_commitment(s, tenant_id=tenant_id, commitment_id=c.id, obligate=Decimal(500))
    with pytest.raises(ps.PublicSectorError, match="obligated"):
        ps.stage_commitment(s, tenant_id=tenant_id, commitment_id=c.id, expend=Decimal(501))
    ps.stage_commitment(s, tenant_id=tenant_id, commitment_id=c.id, expend=Decimal(500), obligate=Decimal(300))
    assert c.status == "obligated"
    ps.stage_commitment(s, tenant_id=tenant_id, commitment_id=c.id, expend=Decimal(300))
    assert c.status == "expended"

    v = ps.Virement(
        tenant_id=tenant_id,
        number="V-1",
        from_appropriation_id=a1.id,
        to_appropriation_id=a2.id,
        amount=Decimal(150),
        currency_code="NGN",
        status="approved",
    )
    s.add(v)
    s.flush()
    ps.apply_virement(s, tenant_id=tenant_id, virement_id=v.id)
    assert a1.revised_amount == Decimal(850) and a2.revised_amount == Decimal(250)
    with pytest.raises(ps.PublicSectorError):
        ps.apply_virement(s, tenant_id=tenant_id, virement_id=v.id)


def test_lease_pv_schedule_and_payments(session) -> None:
    s, tenant_id, _, _ = session
    # PV of 12 x 100 at 0% is exactly 1200; at 12%/yr it is below that.
    assert ls.present_value(Decimal(100), Decimal(0), 12, 12) == Decimal("1200.00")
    pv = ls.present_value(Decimal(100), Decimal("0.12"), 12, 12)
    assert pv == Decimal("1125.51")
    lease = ls.LeaseContract(
        tenant_id=tenant_id,
        number="LS-1",
        name="Office",
        commencement_date=date(2026, 1, 31),
        end_date=date(2026, 12, 31),
        lease_term_months=12,
        currency_code="NGN",
        base_payment_amount=Decimal(100),
        discount_rate=Decimal("0.12"),
    )
    s.add(lease)
    s.flush()
    liability = ls.commence_lease(s, tenant_id=tenant_id, lease_id=lease.id)
    assert liability.initial_liability_amount == pv
    payments = list(s.scalars(sa.select(ls.LeasePayment).order_by(ls.LeasePayment.payment_number)))
    assert len(payments) == 12
    assert payments[0].payment_date == date(2026, 2, 28)  # month-end clamp
    assert payments[-1].closing_balance == Decimal("0.00")
    with pytest.raises(ls.LeaseError, match="earlier"):
        ls.record_payment(s, tenant_id=tenant_id, payment_id=payments[1].id, paid_on=date(2026, 3, 31))
    ls.record_payment(s, tenant_id=tenant_id, payment_id=payments[0].id, paid_on=date(2026, 2, 28))
    assert liability.current_balance == payments[0].closing_balance
    assert liability.total_payments_made == payments[0].total_payment
