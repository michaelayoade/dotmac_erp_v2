"""Legacy never-posted AP re-entry against real PostgreSQL (hosted CI only)."""

import hashlib
import json
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

from erp_v2.legacy_ap import cohort_cli, service
from erp_v2.legacy_ap.models import LegacyInvoiceReference, LegacyNeverPostedCohort, LegacyNeverPostedMember

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
    }
    return base | extra


SHA = "c" * 64


def cohort(engine, env, *ids):
    """Load the census never-posted cohort for env (the operator CLI's function)."""
    with Session(engine) as db, db.begin():
        return cohort_cli.load_cohort(
            db,
            tenant_id=env.tenant_id,
            census_ref="census-flip-final",
            ids=tuple(ids),
            sha256=SHA,
            operator_ref="op-1",
            write=True,
        )


def enter(engine, env, vals):
    with Session(engine) as db:
        try:
            invoice = service.create_legacy_reentry_draft(
                db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, env.party_ids[0]), values=vals
            )
            return invoice.id
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
    cohort(engine, env, legacy)
    invoice_id = enter(engine, env, values(env, legacy=legacy))
    assert counts(engine, env)[1] == 1
    with Session(engine) as db:
        ref = db.scalar(sa.select(LegacyInvoiceReference).where(LegacyInvoiceReference.tenant_id == env.tenant_id))
        assert ref.legacy_invoice_id == legacy and ref.invoice_id == invoice_id
        assert ref.legacy_posting_state == "never_posted" and ref.evidence_ref == SHA and ref.attempt == 1
        assert ref.entered_by == str(env.party_ids[0])
        assert db.get(SupplierInvoice, invoice_id).status.value == "DRAFT"


def test_same_legacy_invoice_cannot_be_entered_twice(engine):
    env = world(engine)
    legacy = uuid4()
    cohort(engine, env, legacy)
    enter(engine, env, values(env, legacy=legacy))
    refused(engine, env, values(env, legacy=legacy, number="L-2"), "legacy_already_entered")


def test_legacy_id_already_landed_by_the_importer_is_refused(engine):
    env = world(engine)
    first = uuid4()
    cohort(engine, env, first)
    landed = enter(engine, env, values(env, legacy=first))  # stands in for an importer-landed invoice
    with Session(engine) as db, db.begin():  # the census list also names the landed id
        db.add(
            LegacyNeverPostedMember(
                tenant_id=env.tenant_id,
                cohort_id=db.scalar(
                    sa.select(LegacyNeverPostedCohort.id).where(LegacyNeverPostedCohort.tenant_id == env.tenant_id)
                ),
                legacy_invoice_id=landed,
            )
        )
    refused(engine, env, values(env, legacy=landed, number="L-2"), "legacy_already_landed")


def test_legacy_id_already_admitted_historically_is_refused(engine):
    env = world(engine)
    legacy = uuid4()
    first = uuid4()
    cohort(engine, env, legacy, first)
    other = enter(engine, env, values(env, legacy=first))
    with Session(engine) as db:
        payables.submit_invoice(db, scope=env.scope, invoice_id=other, submitted_by="importer", submitted_at=NOW)
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


@pytest.mark.parametrize("legacy_text", ["not-a-uuid", str(uuid4()).upper(), "", " " + str(uuid4())])
def test_malformed_legacy_ids_are_refused(engine, legacy_text):
    env = world(engine)
    cohort(engine, env, uuid4())
    refused(engine, env, values(env) | {"legacy_invoice_id": legacy_text}, "legacy_id_invalid")


def test_reentry_is_off_until_a_cohort_is_loaded(engine):
    env = world(engine)
    refused(engine, env, values(env), "reentry_not_enabled")


def test_id_not_on_the_census_list_is_refused(engine):
    env = world(engine)
    cohort(engine, env, uuid4())
    refused(engine, env, values(env), "not_in_never_posted_cohort")


def test_voided_reentry_releases_the_legacy_id_for_a_new_attempt(engine):
    env = world(engine)
    legacy = uuid4()
    cohort(engine, env, legacy)
    first = enter(engine, env, values(env, legacy=legacy))
    refused(engine, env, values(env, legacy=legacy, number="L-2"), "legacy_already_entered")
    with Session(engine) as db:
        payables.void_invoice(db, scope=env.scope, invoice_id=first, reason="entered in error", voided_at=NOW)
        db.commit()
    second = enter(engine, env, values(env, legacy=legacy, number="L-3"))
    assert second != first
    with Session(engine) as db:
        rows = list(
            db.execute(
                sa.select(LegacyInvoiceReference.attempt, LegacyInvoiceReference.invoice_id)
                .where(LegacyInvoiceReference.tenant_id == env.tenant_id)
                .order_by(LegacyInvoiceReference.attempt)
            )
        )
    assert rows == [(1, first), (2, second)]  # the earlier row is kept
    refused(engine, env, values(env, legacy=legacy, number="L-4"), "legacy_already_entered")


def test_detail_query_exposes_the_legacy_reference_to_the_approver(engine):
    from erp_v2.web import revenue_queries

    env = world(engine)
    legacy = uuid4()
    cohort(engine, env, legacy)
    iid = enter(engine, env, values(env, legacy=legacy))
    with Session(engine) as db:
        detail = revenue_queries.supplier_detail(db, tenant_id=env.tenant_id, invoice_id=iid)
        ref = detail["legacy_reference"]
        assert ref.legacy_invoice_id == legacy and ref.entered_by == str(env.party_ids[0]) and ref.evidence_ref == SHA


def test_cohort_load_plan_apply_replay_and_refusals(engine):
    env = world(engine)
    ids = tuple(uuid4() for _ in range(3))
    kw = dict(tenant_id=env.tenant_id, census_ref="c", ids=ids, sha256=SHA, operator_ref="op")
    with Session(engine) as db, db.begin():
        assert cohort_cli.load_cohort(db, write=False, **kw)["status"] == "plan"
    assert service_cohort(engine, env) is None  # plan wrote nothing
    assert cohort(engine, env, *ids)["status"] == "loaded"
    assert service_cohort(engine, env).member_count == 3
    with Session(engine) as db, db.begin():
        assert cohort_cli.load_cohort(db, write=True, **kw)["status"] == "already_loaded"
        with pytest.raises(cohort_cli.CohortRefusal) as exc:
            cohort_cli.load_cohort(db, write=True, **(kw | {"sha256": "d" * 64}))
        assert exc.value.reason == "different_cohort_already_loaded"
    with Session(engine) as db, db.begin():
        with pytest.raises(cohort_cli.CohortRefusal) as exc:
            cohort_cli.load_cohort(db, write=True, **(kw | {"tenant_id": uuid4()}))
        assert exc.value.reason == "tenant_unknown"


def service_cohort(engine, env):
    with Session(engine) as db:
        return service.active_cohort(db, env.tenant_id)


def _cohort_bytes(tenant, ids, **over):
    data = {"version": 1, "tenant_id": str(tenant), "census_ref": "c", "legacy_invoice_ids": [str(i) for i in ids]}
    return json.dumps(data | over).encode()


def test_cohort_file_parsing_refuses_every_mismatch():
    tenant, ids = uuid4(), [uuid4(), uuid4()]
    raw = _cohort_bytes(tenant, ids)
    sha = hashlib.sha256(raw).hexdigest()
    assert cohort_cli.parse_cohort(raw, sha, tenant)[0] == "c"
    for raw_bad, sha_bad, tenant_bad, reason in (
        (raw, "e" * 64, tenant, "cohort_digest_mismatch"),
        (raw, "nothex", tenant, "cohort_sha256_required"),
        (raw, sha, uuid4(), "cohort_tenant_mismatch"),
        (_cohort_bytes(tenant, [ids[0], ids[0]]), None, tenant, "duplicate_legacy_invoice_id"),
        (_cohort_bytes(tenant, []), None, tenant, "bounded_cohort_required"),
        (_cohort_bytes(tenant, ids, version=2), None, tenant, "invalid_cohort_version"),
        (_cohort_bytes(tenant, ids, census_ref=" x"), None, tenant, "census_ref_required"),
        (_cohort_bytes(tenant, ids, extra=1), None, tenant, "invalid_cohort_schema"),
        (b"{not json", None, tenant, "invalid_json_document"),
    ):
        digest = sha_bad or hashlib.sha256(raw_bad).hexdigest()
        with pytest.raises(cohort_cli.CohortRefusal) as exc:
            cohort_cli.parse_cohort(raw_bad, digest, tenant_bad)
        assert exc.value.reason == reason
    upper = _cohort_bytes(tenant, [str(ids[0]).upper()])
    with pytest.raises(cohort_cli.CohortRefusal):
        cohort_cli.parse_cohort(upper, hashlib.sha256(upper).hexdigest(), tenant)


def test_a_refused_draft_leaves_no_reference(engine):
    env = world(engine)
    legacy = uuid4()
    cohort(engine, env, legacy)
    refused(engine, env, values(env, legacy=legacy, posting_account_id=str(uuid4())), "draft_refused")


def test_database_rejects_other_posting_states_and_duplicate_legacy_ids(engine):
    env = world(engine)
    legacy = uuid4()
    cohort(engine, env, legacy)
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
    legacy = uuid4()
    cohort(engine, env, legacy)
    with Session(engine) as db:
        with pytest.raises(service.LegacyReentryRefused) as exc:
            service.create_legacy_reentry_draft(
                db,
                tenant=db.get(Tenant, env.tenant_id),
                party=db.get(Party, other.party_ids[0]),
                values=values(env, legacy=legacy),
            )
        assert exc.value.code == "actor_tenant"
