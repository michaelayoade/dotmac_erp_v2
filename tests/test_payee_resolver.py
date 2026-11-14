"""resolve_payee reads the real people / payables owner tables (no free-text payee identity)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import dotmac_payables.models as pay
import dotmac_people.models as people
import pytest
import sqlalchemy as sa
from dotmac_kernel.models import Base, Party, PartyPerson, PartyType, Tenant
from sqlalchemy.orm import Session

from erp_v2.payees.service import resolve_payee


@pytest.fixture()
def world():
    # attach on every pooled connection: use a single-connection engine
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool, connect_args={"check_same_thread": False})
    with engine.connect() as c:
        for schema in ("mod_people", "mod_payables"):
            c.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        c.commit()
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            Party.__table__,
            PartyPerson.__table__,
            people.Employee.__table__,
            pay.SupplierInvoice.__table__,
        ],
    )
    with Session(engine) as db:
        tenant, other = uuid4(), uuid4()
        for t in (tenant, other):
            db.add(Tenant(id=t, slug=f"t{t.hex[:8]}", name="T"))
        party = uuid4()
        db.add(Party(id=party, tenant_id=tenant, party_type=PartyType.person, display_name="Ada", email="a@x.io"))
        db.add(PartyPerson(party_id=party, first_name="Ada", last_name="Test"))
        emp = people.Employee(tenant_id=tenant, party_id=party, employee_code="E001", date_of_joining=date(2025, 1, 1))
        db.add(emp)
        db.add(
            pay.SupplierInvoice(
                tenant_id=tenant,
                number="N1",
                supplier_ref="sup-ref-1",
                supplier_name_snapshot="Acme Supplies",
                invoice_date=date(2026, 1, 1),
                received_date=date(2026, 1, 1),
                currency_code="NGN",
                exchange_rate=Decimal(1),
                liability_account_ref="x",
                subtotal=Decimal(10),
                tax_amount=Decimal(0),
                total_amount=Decimal(10),
                payment_schedule=[],
                request_fingerprint="f" * 64,
            )
        )
        db.flush()
        yield db, tenant, other, emp
    engine.dispose()


def test_employee_resolves_by_canonical_id_inside_the_tenant(world):
    db, tenant, other, emp = world
    assert resolve_payee(db, tenant, "employee", str(emp.id)) == "E001 Ada Test"
    assert resolve_payee(db, other, "employee", str(emp.id)) is None
    assert resolve_payee(db, tenant, "employee", emp.id.hex) is None  # non-canonical spelling
    assert resolve_payee(db, tenant, "employee", "not-a-uuid") is None
    assert resolve_payee(db, tenant, "employee", str(uuid4())) is None


def test_supplier_resolves_from_the_payables_reference_set_inside_the_tenant(world):
    db, tenant, other, _ = world
    assert resolve_payee(db, tenant, "supplier", "sup-ref-1") == "Acme Supplies"
    assert resolve_payee(db, other, "supplier", "sup-ref-1") is None
    assert resolve_payee(db, tenant, "supplier", "unknown") is None
    assert resolve_payee(db, tenant, "customer", "sup-ref-1") is None
