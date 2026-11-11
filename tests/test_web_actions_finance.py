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
        pay.PaymentObligation.__table__,
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


def test_open_payables_uses_tenant_obligations_without_flushing(session) -> None:
    s, tenant, _party, _emp = session
    other_tenant = Tenant(id=uuid.uuid4(), slug="other", name="Other")
    s.add(other_tenant)

    def invoice(number, *, tenant_id=tenant.id, status="APPROVED"):
        row = pay.SupplierInvoice(
            tenant_id=tenant_id,
            number=number,
            supplier_ref=number,
            supplier_name_snapshot="Supplier",
            supplier_document_number=None,
            invoice_date=date(2026, 1, 5),
            received_date=date(2026, 1, 5),
            currency_code="NGN",
            exchange_rate=Decimal(1),
            liability_account_ref="AP",
            subtotal=Decimal(100),
            tax_amount=Decimal(0),
            total_amount=Decimal(100),
            payment_schedule=[],
            request_fingerprint=number,
            status=status,
        )
        s.add(row)
        s.flush()
        return row

    def obligation(row, sequence, outstanding, *, currency="NGN", tenant_id=None):
        s.add(
            pay.PaymentObligation(
                tenant_id=tenant_id or row.tenant_id,
                invoice_id=row.id,
                sequence=sequence,
                due_date=date(2026, 2, 5),
                currency_code=currency,
                original_amount=Decimal(100),
                outstanding_amount=Decimal(outstanding),
                status="SETTLED" if outstanding == 0 else "OPEN",
            )
        )

    settled = invoice("paid")
    obligation(settled, 1, 0)
    partial = invoice("partial")
    obligation(partial, 1, 10)
    obligation(partial, 2, 15)
    full = invoice("full")
    obligation(full, 1, 100)
    invoice("missing")
    currency_mismatch = invoice("currency-mismatch")
    obligation(currency_mismatch, 1, 0, currency="USD")
    mixed_currency = invoice("mixed-currency")
    obligation(mixed_currency, 1, 20)
    obligation(mixed_currency, 2, 30, currency="USD")
    cross_tenant = invoice("cross-tenant-evidence")
    # SQLite permits this deliberately inconsistent imported structure; the
    # presentation must never use another tenant's evidence for this invoice.
    obligation(cross_tenant, 1, 0, tenant_id=other_tenant.id)
    hidden = invoice("other-tenant", tenant_id=other_tenant.id)
    obligation(hidden, 1, 100)
    for status in ("VOID", "DRAFT", "SUBMITTED"):
        row = invoice(status, status=status)
        obligation(row, 1, 100)
    s.flush()

    # A read must not flush pending writes or present unflushed ORM values.
    full.total_amount = Decimal(999)
    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    sa.event.listen(s.bind, "before_cursor_execute", record_statement)
    try:
        result = {row["number"]: row for row in fq.open_payables(s, tenant_id=tenant.id)}
    finally:
        sa.event.remove(s.bind, "before_cursor_execute", record_statement)
    assert set(result) == {"partial", "full", "missing", "currency-mismatch", "mixed-currency", "cross-tenant-evidence"}
    assert result["partial"]["outstanding"] == Decimal(25)
    assert result["full"]["outstanding"] == Decimal(100)
    assert result["full"]["total"] == Decimal(100)
    assert result["full"]["reconciliation_required"] is False
    for number in ("missing", "currency-mismatch", "mixed-currency", "cross-tenant-evidence"):
        assert result[number]["outstanding"] is None
        assert result[number]["reconciliation_required"] is True
        assert result[number]["supplier_document"] == "—"
    assert full in s.dirty
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
