"""Legacy never-posted AP re-entry against real PostgreSQL (hosted CI only)."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_kernel.models import Party, Tenant
from dotmac_payables import service as payables
from dotmac_payables.models import HistoricalInvoiceReceipt, LiabilityEvent, PaymentObligation, SupplierInvoice
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_b1_supplier_invoice_post_pg import NOW, world

from erp_v2.legacy_ap import service
from erp_v2.legacy_ap.models import LegacyInvoiceReference

engine = bridge_pg.engine


def values(env, *, legacy=None, number="L-1", **extra):
    base = {
        "request_id": str(uuid4()),
        "number": number,
        "supplier_ref": "supplier-1",
        "supplier_name": "Supplier One",
        "supplier_document_number": f"SUP-{number}",
        "invoice_date": "2026-01-10",
        "received_date": "2026-01-10",
        "due_date": "2026-02-28",
        "description": "Services",
        "currency_code": "NGN",
        "quantity": "2",
        "unit_price": "100",
        "tax_amount": "0",
        "exchange_rate": "1",
        "posting_account_id": str(env.accounts["5000"]),
        "liability_account_id": str(env.accounts["2000"]),
        "legacy_invoice_id": str(legacy or uuid4()),
        "evidence_ref": "census-R6-7",
        "legacy_posting_state": "never_posted",
    }
    return base | extra


def enter(engine, env, vals):
    with Session(engine) as db:
        try:
            return service.create_legacy_reentry_draft(
                db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, env.party_ids[0]), values=vals
            )
        finally:
            db.commit()


def counts(engine, env):
    with Session(engine) as db:
        return (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(SupplierInvoice)
                .where(SupplierInvoice.tenant_id == env.tenant_id)
            ),
            db.scalar(
                sa.select(sa.func.count())
                .select_from(LegacyInvoiceReference)
                .where(LegacyInvoiceReference.tenant_id == env.tenant_id)
            ),
        )


def refused(engine, env, vals, code):
    before = counts(engine, env)
    with pytest.raises(service.LegacyReentryRefused) as exc:
        enter(engine, env, vals)
    assert exc.value.code == code
    assert counts(engine, env) == before  # nothing written


def test_reentry_creates_a_draft_and_its_legacy_reference(engine):
    env = world(engine)
    legacy = uuid4()
    invoice = enter(engine, env, values(env, legacy=legacy))
    assert counts(engine, env)[1] == 1
    with Session(engine) as db:
        ref = db.scalar(sa.select(LegacyInvoiceReference).where(LegacyInvoiceReference.tenant_id == env.tenant_id))
        assert ref.legacy_invoice_id == legacy and ref.invoice_id == invoice.id
        assert ref.legacy_posting_state == "never_posted" and ref.evidence_ref == "census-R6-7"
        assert ref.entered_by == str(env.party_ids[0])
        assert db.get(SupplierInvoice, invoice.id).status.value == "DRAFT"


def test_same_legacy_invoice_cannot_be_entered_twice(engine):
    env = world(engine)
    legacy = uuid4()
    enter(engine, env, values(env, legacy=legacy))
    refused(engine, env, values(env, legacy=legacy, number="L-2"), "legacy_already_entered")


def test_legacy_id_already_landed_by_the_importer_is_refused(engine):
    env = world(engine)
    landed = enter(engine, env, values(env)).id  # stands in for an importer-landed invoice with that id
    refused(engine, env, values(env, legacy=landed, number="L-2"), "legacy_already_landed")


def test_legacy_id_already_admitted_historically_is_refused(engine):
    env = world(engine)
    legacy = uuid4()
    other = enter(engine, env, values(env)).id
    with Session(engine) as db:
        payables.approve_invoice(
            db,
            scope=env.scope,
            invoice_id=other,
            approval_reference="x",
            approved_by="importer",
            idempotency_key="k",
            idempotency_expires_at=None,
            approved_at=NOW,
        )
        db.add(
            HistoricalInvoiceReceipt(
                tenant_id=env.tenant_id,
                invoice_id=other,
                obligation_id=db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == other)),
                event_id=db.scalar(sa.select(LiabilityEvent.id).where(LiabilityEvent.document_id == other)),
                source_owner="legacy-erp",
                source_invoice_id=str(legacy),
                source_snapshot_sha256="a" * 64,
                evidence_fingerprint="a" * 64,
                facts_fingerprint="a" * 64,
                accounting_evidence_fingerprint="a" * 64,
                source_journal_ref="j",
                destination_journal_ref="j",
                evidence={},
                original_posted_at=NOW,
                admitted_at=NOW,
            )
        )
        db.commit()
    refused(engine, env, values(env, legacy=legacy, number="L-3"), "legacy_already_admitted")


@pytest.mark.parametrize(
    "change,code",
    [
        ({"legacy_invoice_id": "not-a-uuid"}, "legacy_id_invalid"),
        ({"legacy_invoice_id": str(uuid4()).upper()}, "legacy_id_invalid"),
        ({"evidence_ref": "  "}, "evidence_required"),
        ({"legacy_posting_state": ""}, "never_posted_attestation_required"),
        ({"legacy_posting_state": "posted"}, "never_posted_attestation_required"),
    ],
)
def test_mandatory_reference_inputs_are_refused(engine, change, code):
    env = world(engine)
    refused(engine, env, values(env) | change, code)


def test_a_refused_draft_leaves_no_reference(engine):
    env = world(engine)
    refused(engine, env, values(env, posting_account_id=str(uuid4())), "draft_refused")


def test_database_rejects_other_posting_states_and_duplicate_legacy_ids(engine):
    env = world(engine)
    legacy = uuid4()
    enter(engine, env, values(env, legacy=legacy))
    with Session(engine) as db:
        db.add(
            LegacyInvoiceReference(
                tenant_id=env.tenant_id,
                legacy_invoice_id=legacy,
                invoice_id=uuid4(),
                legacy_posting_state="never_posted",
                evidence_ref="x",
                entered_by="x",
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
    with Session(engine) as db:
        db.add(
            LegacyInvoiceReference(
                tenant_id=env.tenant_id,
                legacy_invoice_id=uuid4(),
                invoice_id=uuid4(),
                legacy_posting_state="posted",
                evidence_ref="x",
                entered_by="x",
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()


def test_other_tenants_party_is_refused(engine):
    env, other = world(engine), world(engine)
    with Session(engine) as db:
        with pytest.raises(service.LegacyReentryRefused) as exc:
            service.create_legacy_reentry_draft(
                db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, other.party_ids[0]), values=values(env)
            )
        assert exc.value.code == "actor_tenant"
