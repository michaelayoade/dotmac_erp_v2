"""Leave write flows + finance workspace queries."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import dotmac_accounting.models as acct
import dotmac_payables.models as pay
import dotmac_people.models as people_models
import pytest
import sqlalchemy as sa
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_kernel.models import Base, Party, PartyPerson, PartyType, Tenant
from sqlalchemy.orm import Session

from erp_v2.hr import models as hr
from erp_v2.web import finance_queries as fq
from erp_v2.web import people_actions as pa


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_people"))
        c.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_accounting"))
        c.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_payables"))
    tables = [
        Tenant.__table__,
        Party.__table__,
        PartyPerson.__table__,
        people_models.Department.__table__,
        people_models.Designation.__table__,
        people_models.EmploymentType.__table__,
        people_models.Employee.__table__,
        acct.AccountCategory.__table__,
        acct.Account.__table__,
        acct.JournalEntry.__table__,
        acct.PostedLedgerLine.__table__,
        pay.SupplierInvoice.__table__,
    ] + [Base.metadata.tables[n] for n in hr.HR_TABLES]
    Base.metadata.create_all(engine, tables=tables)
    s = Session(engine)
    tenant = Tenant(id=uuid.uuid4(), slug="acme", name="Acme")
    s.add(tenant)
    party = Party(
        id=uuid.uuid4(), tenant_id=tenant.id, party_type=PartyType.person, display_name="Ada", email="ada@x.io"
    )
    s.add(party)
    s.add(PartyPerson(party_id=party.id, first_name="Ada", last_name="Test"))
    emp = people_models.Employee(
        tenant_id=tenant.id,
        party_id=party.id,
        employee_code="E001",
        date_of_joining=date(2025, 1, 1),
    )
    s.add(emp)
    lt = hr.LeaveType(tenant_id=tenant.id, code="ANNUAL", name="Annual")
    s.add(lt)
    s.flush()
    s.add(
        hr.LeaveAllocation(
            tenant_id=tenant.id,
            employee_id=emp.id,
            leave_type_id=lt.id,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 12, 31),
            new_leaves_allocated=Decimal(20),
        )
    )
    s.flush()
    yield s, tenant, party, emp
    s.close()


def test_apply_decide_cancel_flow(session) -> None:
    s, tenant, party, _emp = session
    counter = iter(range(1, 100))
    app = pa.apply_for_leave(
        s,
        tenant=tenant,
        party=party,
        leave_type_code="ANNUAL",
        from_date=date(2026, 2, 2),
        to_date=date(2026, 2, 6),  # Mon-Fri
        allocate_number=lambda: f"LV-{next(counter):04d}",
    )
    assert app.number == "LV-0001" and app.total_leave_days == Decimal(5)
    decided = pa.decide_leave(s, tenant=tenant, party=party, application_id=app.id, approve=True)
    assert decided.status == "approved" and decided.decided_by_id == party.id
    cancelled = pa.cancel_leave(s, tenant=tenant, party=party, application_id=app.id)
    assert cancelled.status == "cancelled"
    with pytest.raises(pa.PeopleActionError, match="unknown leave type"):
        pa.apply_for_leave(
            s,
            tenant=tenant,
            party=party,
            leave_type_code="NOPE",
            from_date=date(2026, 3, 2),
            to_date=date(2026, 3, 2),
            allocate_number=lambda: "LV-X",
        )


def test_finance_queries(session) -> None:
    s, tenant, _party, _emp = session
    category = acct.AccountCategory(tenant_id=tenant.id, code="AST", name="Assets", account_class=AccountClass.ASSET)
    s.add(category)
    s.flush()
    cash = acct.Account(
        tenant_id=tenant.id,
        category_id=category.id,
        code="1000",
        name="Cash",
        kind=AccountKind.POSTING,
        normal_balance=NormalBalance.DEBIT,
    )
    s.add(cash)
    s.flush()
    import datetime as dt

    s.add(
        acct.PostedLedgerLine(
            tenant_id=tenant.id,
            journal_id=uuid.uuid4(),
            journal_line_id=uuid.uuid4(),
            fiscal_period_id=uuid.uuid4(),
            account_id=cash.id,
            account_code="1000",
            journal_number="JV-1",
            entry_date=date(2026, 1, 5),
            posting_date=date(2026, 1, 5),
            currency_code="NGN",
            debit=Decimal(500),
            credit=Decimal(0),
            original_debit=Decimal(500),
            original_credit=Decimal(0),
            exchange_rate=Decimal(1),
            source_owner="erp",
            source_document_kind="test",
            source_document_id=str(uuid.uuid4()),
            source_version=1,
            source_fingerprint="x",
            posted_by=str(uuid.uuid4()),
            posted_at=dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc),
        )
    )
    s.flush()
    tb = fq.trial_balance(s, tenant_id=tenant.id)
    assert tb["total_debit"] == Decimal(500) and tb["lines"][0]["balance"] == Decimal(500)
    assert fq.chart_of_accounts(s, tenant_id=tenant.id)[0]["code"] == "1000"
    assert fq.open_payables(s, tenant_id=tenant.id) == []
