"""Released Payables draft/submit behavior and tenant-scoped finance registers.

Run on hosted CI against the locked released wheels. No provider, payment,
Accounting posting or remote database is involved.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory
from dotmac_billing.models import BillingAccount, BillingDocument, ConfirmedSettlement
from dotmac_kernel.idempotency_models import IdempotencyRecord
from dotmac_kernel.models import Base, Party, PartyType, Tenant
from dotmac_payables.contracts import InvoiceStatus, ObligationStatus
from dotmac_payables.models import PaymentObligation, SettlementObservation, SupplierInvoice, SupplierInvoiceLine
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from erp_v2 import ui
from erp_v2.web import revenue_queries as queries
from erp_v2.web import revenue_services as services
from erp_v2.web.revenue_routes import WRITE_PERMISSION, revenue_web_feature, router


@pytest.fixture()
def data():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        for schema in ("mod_accounting", "mod_payables", "mod_billing"):
            connection.execute(sa.text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            Party.__table__,
            IdempotencyRecord.__table__,
            AccountCategory.__table__,
            Account.__table__,
            SupplierInvoice.__table__,
            SupplierInvoiceLine.__table__,
            PaymentObligation.__table__,
            SettlementObservation.__table__,
            BillingAccount.__table__,
            BillingDocument.__table__,
            ConfirmedSettlement.__table__,
        ],
    )
    with Session(engine) as db:
        tenant = Tenant(id=uuid4(), slug="revenue-a", name="Revenue A")
        other = Tenant(id=uuid4(), slug="revenue-b", name="Revenue B")
        db.add_all([tenant, other])
        db.flush()
        party = Party(tenant_id=tenant.id, party_type=PartyType.person, display_name="Clerk")
        db.add(party)
        accounts = []
        for owner in (tenant, other):
            category = AccountCategory(
                tenant_id=owner.id, code="LIAB", name="Liabilities", account_class=AccountClass.LIABILITY
            )
            db.add(category)
            db.flush()
            account = Account(
                tenant_id=owner.id,
                category_id=category.id,
                code="2000",
                name="Trade payables",
                kind=AccountKind.POSTING,
                normal_balance=NormalBalance.CREDIT,
                is_active=True,
                posting_allowed=True,
            )
            db.add(account)
            db.flush()
            accounts.append(account)
        values = {
            "request_id": str(uuid4()),
            "number": "AP-001",
            "supplier_ref": "supplier-1",
            "supplier_name": "Supplier One",
            "supplier_document_number": "SUP-1",
            "invoice_date": "2026-11-01",
            "received_date": "2026-11-02",
            "due_date": "2026-12-01",
            "description": "Supplier document line",
            "currency_code": "NGN",
            "quantity": "2",
            "unit_price": "100",
            "tax_amount": "0",
            "exchange_rate": "1",
            "posting_account_id": str(accounts[0].id),
            "liability_account_id": str(accounts[0].id),
        }
        yield db, tenant, other, party, accounts, values
    engine.dispose()


def test_cross_tenant_document_and_account_isolation(data):
    db, tenant, other, party, accounts, values = data
    invoice = services.create_supplier_draft(db, tenant=tenant, party=party, values=values)
    assert queries.supplier_detail(db, tenant_id=other.id, invoice_id=invoice.id) is None
    assert queries.supplier_register(db, tenant_id=other.id) == []
    assert queries.posting_accounts(db, tenant_id=other.id) == [accounts[1]]
    with pytest.raises(services.RevenueActionError, match="unavailable for this tenant"):
        services.create_supplier_draft(
            db, tenant=tenant, party=party, values={**values, "posting_account_id": str(accounts[1].id)}
        )
    with pytest.raises(services.RevenueActionError, match="not found"):
        services.submit_supplier_draft(db, tenant=other, party=Party(tenant_id=other.id), invoice_id=invoice.id)
    assert invoice.status == InvoiceStatus.DRAFT


def test_create_replay_conflict_and_submit_without_liability(data):
    db, tenant, _other, party, _accounts, values = data
    invoice = services.create_supplier_draft(db, tenant=tenant, party=party, values=values)
    replay = services.create_supplier_draft(db, tenant=tenant, party=party, values=values)
    assert replay.id == invoice.id
    assert invoice.total_amount == Decimal("200")
    with pytest.raises(services.RevenueActionError, match="different request"):
        services.create_supplier_draft(db, tenant=tenant, party=party, values={**values, "description": "Changed"})
    assert db.scalar(sa.select(sa.func.count()).select_from(SupplierInvoice)) == 1
    services.submit_supplier_draft(db, tenant=tenant, party=party, invoice_id=invoice.id)
    submitted_at = invoice.submitted_at
    services.submit_supplier_draft(db, tenant=tenant, party=party, invoice_id=invoice.id)
    assert invoice.status == InvoiceStatus.SUBMITTED
    assert invoice.submitted_by == str(party.id) and invoice.submitted_at == submitted_at
    assert queries.supplier_detail(db, tenant_id=tenant.id, invoice_id=invoice.id)["obligations"] == []
    assert db.scalar(sa.select(sa.func.count()).select_from(SettlementObservation)) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit_price", "NaN"),
        ("quantity", "Infinity"),
        ("tax_amount", "0.0000001"),
        ("unit_price", "-1"),
        ("exchange_rate", "0"),
        ("due_date", "2026-10-01"),
    ],
)
def test_invalid_input_does_not_leave_draft(data, field, value):
    db, tenant, _other, party, _accounts, values = data
    with pytest.raises(services.RevenueActionError):
        services.create_supplier_draft(db, tenant=tenant, party=party, values={**values, field: value})
    assert queries.supplier_register(db, tenant_id=tenant.id) == []


def test_duplicate_number_failure_keeps_session_usable(data):
    db, tenant, _other, party, _accounts, values = data
    services.create_supplier_draft(db, tenant=tenant, party=party, values=values)
    with pytest.raises(services.RevenueActionError, match="already exists"):
        services.create_supplier_draft(
            db,
            tenant=tenant,
            party=party,
            values={**values, "request_id": str(uuid4()), "supplier_document_number": "SUP-2"},
        )
    assert len(queries.supplier_register(db, tenant_id=tenant.id)) == 1


def test_document_tax_and_owner_obligation_evidence(data):
    db, tenant, other, party, accounts, values = data
    invoice = services.create_supplier_draft(
        db, tenant=tenant, party=party, values={**values, "tax_amount": "15", "tax_account_id": str(accounts[0].id)}
    )
    assert invoice.total_amount == Decimal("215") and invoice.tax_amount == Decimal("15")
    now = datetime.now(timezone.utc)
    obligation = PaymentObligation(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        sequence=1,
        due_date=date(2026, 12, 1),
        currency_code="NGN",
        original_amount=Decimal("215"),
        outstanding_amount=Decimal("115"),
        status=ObligationStatus.PARTIALLY_SETTLED,
    )
    db.add(obligation)
    db.flush()
    observation = SettlementObservation(
        tenant_id=tenant.id,
        obligation_id=obligation.id,
        source_owner="payment-owner",
        source_reference="observed-1",
        source_version="1",
        source_fingerprint="a" * 64,
        currency_code="NGN",
        amount=Decimal("100"),
        occurred_at=now,
        recorded_at=now,
    )
    db.add(observation)
    db.flush()
    detail = queries.supplier_detail(db, tenant_id=tenant.id, invoice_id=invoice.id)
    assert detail["obligations"][0].outstanding_amount == Decimal("115")
    assert detail["observations"] == [observation]
    assert queries.supplier_detail(db, tenant_id=other.id, invoice_id=invoice.id) is None


def test_billing_register_is_tenant_filtered(data):
    db, tenant, other, _party, _accounts, _values = data
    now = datetime.now(timezone.utc)
    for owner in (tenant, other):
        account = BillingAccount(tenant_id=owner.id, external_account_ref=owner.slug, currency="NGN", minor_units=2)
        db.add(account)
        db.flush()
        db.add(
            BillingDocument(
                tenant_id=owner.id,
                billing_account_id=account.id,
                document_kind="invoice",
                lifecycle="issued",
                document_number=owner.slug,
                currency="NGN",
                minor_units=2,
                subtotal=Decimal("200"),
                tax_total=Decimal("0"),
                grand_total=Decimal("200"),
                due_date_basis={},
                seller_snapshot={},
                customer_snapshot={},
                payment_instructions={},
                brand_asset={},
                locale="en",
                timezone="UTC",
                document_profile_code="fixture",
                document_profile_version="1",
                issued_at=now,
            )
        )
        db.add(
            ConfirmedSettlement(
                tenant_id=owner.id,
                billing_account_id=account.id,
                source_system="fixture",
                source_settlement_key=owner.slug,
                source_version="1",
                request_fingerprint="b" * 64,
                amount=Decimal("100"),
                currency="NGN",
                minor_units=2,
                occurred_at=now,
                observed_at=now,
                confirmation_evidence="fixture",
                funding_lane="available_credit",
            )
        )
    db.flush()
    register = queries.billing_register(db, tenant_id=tenant.id)
    assert [d.document_number for d in register["documents"]] == [tenant.slug]
    assert [r.source_settlement_key for r in register["receipts"]] == [tenant.slug]
    assert list(register["billing_accounts"].values()) == [tenant.slug]


def test_templates_render_with_csrf_and_explicit_execution_limit(data):
    db, tenant, _other, party, _accounts, values = data
    invoice = services.create_supplier_draft(db, tenant=tenant, party=party, values=values)
    root = Path(__file__).parents[1] / "src" / "erp_v2" / "templates"
    env = Environment(loader=FileSystemLoader(root), autoescape=select_autoescape())
    context = {**ui.template_globals(), "current_email": "", "csrf_token": "csrf-fixture"}
    draft = env.get_template("erp_v2/revenue/draft.html").render(
        **context, values=values, accounts=queries.posting_accounts(db, tenant_id=tenant.id)
    )
    assert 'name="csrf_token" value="csrf-fixture"' in draft
    assert values["request_id"] in draft
    detail = env.get_template("erp_v2/revenue/supplier_detail.html").render(
        **context, **queries.supplier_detail(db, tenant_id=tenant.id, invoice_id=invoice.id)
    )
    assert 'name="csrf_token" value="csrf-fixture"' in detail
    assert "no outbound payment executor" in detail
    assert "Submit for review" in detail
    assert [(p.code, p.default_roles) for p in revenue_web_feature.permissions] == [(WRITE_PERMISSION, ("admin",))]
    assert {r.path for r in router.routes if "POST" in r.methods} == {
        "/finance/supplier-invoices",
        "/finance/supplier-invoices/{invoice_id}/submit",
    }
