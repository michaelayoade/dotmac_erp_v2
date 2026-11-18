"""B1 supplier invoice approve + post against real PostgreSQL (hosted CI only).

Real payables, accounting and kernel migrations; real create/approve/post commands.
Reuses the disposable-database fixture and tenant/account seed of the bridge test.
"""

import threading
import time
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, JournalLine, PostedLedgerLine
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.models import Party, PartyType, Tenant, UserCredential
from dotmac_payables import service as payables
from dotmac_payables.contracts import InvoiceStatus
from dotmac_payables.models import (
    AccountingReceipt,
    HistoricalInvoiceReceipt,
    LiabilityEvent,
    PaymentObligation,
    SupplierInvoice,
    SupplierInvoiceLine,
)
from sqlalchemy.orm import Session
from test_posting_bridge_pg import NOW, seed, soft_close

from erp_v2.assembly import assembly
from erp_v2.web import revenue_approval_services as svc
from erp_v2.web import revenue_services as drafts

engine = bridge_pg.engine  # the disposable-database fixture, re-exposed under its own name

MODELS = (SupplierInvoice, PaymentObligation, LiabilityEvent, AccountingReceipt, JournalEntry, JournalLine)


@pytest.fixture(scope="module", autouse=True)
def audit_registry():
    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))


def counts(engine, tenant_id) -> dict[str, int]:
    with Session(engine) as db:
        return {
            m.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == tenant_id))
            for m in MODELS
        }


def audits(engine, tenant_id, action):
    with Session(engine) as db:
        return list(
            db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == action))
        )


def world(engine):
    env = seed(engine)
    with Session(engine) as db:
        liability = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "LIA")
        )
        tax = Account(
            tenant_id=env.tenant_id,
            category_id=liability.id,
            code="1455",
            name="Deferred input VAT",
            kind=AccountKind.POSTING,
            normal_balance=NormalBalance.DEBIT,
        )
        db.add(tax)
        parties = [
            Party(tenant_id=env.tenant_id, party_type=PartyType.person, display_name=n)
            for n in ("submitter", "approver", "other")
        ]
        db.add_all(parties)
        db.flush()
        env.tax_account = tax.id
        env.party_ids = [p.id for p in parties]
        env.tenant = db.get(Tenant, env.tenant_id)
        db.commit()
    return env


def draft_values(env, *, number="AP-1", tax="15", period_date="2026-01-10", tax_account=None):
    return {
        "request_id": str(uuid4()),
        "number": number,
        "supplier_ref": "supplier-1",
        "supplier_name": "Supplier One",
        "supplier_document_number": f"SUP-{number}",
        "invoice_date": period_date,
        "received_date": period_date,
        "due_date": "2026-02-28",
        "description": "Services",
        "currency_code": "NGN",
        "quantity": "2",
        "unit_price": "100",
        "tax_amount": tax,
        "exchange_rate": "1",
        "posting_account_id": str(env.accounts["5000"]),
        "liability_account_id": str(env.accounts["2000"]),
        "tax_account_id": str(tax_account or env.tax_account),
    }


def invoice(engine, env, *, number="AP-1", submit=True, tax="15", period_date="2026-01-10", tax_account=None):
    """A native draft (V1-valid). A different ``tax_account`` models an IMPORTED row: the draft is created
    against the input-tax account and the line is then rewritten in SQL, because the importer's SQL executor
    never goes through the V1 draft check (section 6 of the pre-flip spec)."""
    values = draft_values(env, number=number, tax=tax, period_date=period_date)
    with Session(engine) as db:
        tenant, submitter = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[0])
        created = drafts.create_supplier_draft(db, tenant=tenant, party=submitter, values=values)
        if tax_account is not None and tax_account != env.tax_account:
            db.execute(
                sa.update(SupplierInvoiceLine)
                .where(SupplierInvoiceLine.invoice_id == created.id)
                .values(tax_account_ref=str(tax_account))
            )
        if submit:
            drafts.submit_supplier_draft(db, tenant=tenant, party=submitter, invoice_id=created.id)
        db.commit()
        return created.id


def submit(engine, env, invoice_id):
    with Session(engine) as db:
        drafts.submit_supplier_draft(
            db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, env.party_ids[0]), invoice_id=invoice_id
        )
        db.commit()


def approve(engine, env, invoice_id, party_index=1, *, commit=True, caller_write=False):
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[party_index])
        if caller_write:
            db.add(
                AccountCategory(
                    tenant_id=env.tenant_id,
                    code=f"CALLER-{invoice_id.hex[:12]}",
                    name="c",
                    account_class=AccountClass.EXPENSE,
                )
            )
            db.flush()
        try:
            return svc.approve_and_post(db, tenant=tenant, party=party, invoice_id=invoice_id, now=NOW)
        finally:
            if commit:
                db.commit()  # the request boundary commits after a caught refusal too


def refusal(engine, env, invoice_id, code, party_index=1):
    before = counts(engine, env.tenant_id)
    with pytest.raises(svc.ApprovalRefused) as exc:
        approve(engine, env, invoice_id, party_index, caller_write=True)
    assert exc.value.code == code
    assert counts(engine, env.tenant_id) == before  # refusal leaves nothing
    with Session(engine) as db:  # the caller's earlier write survives
        assert db.scalar(
            sa.select(AccountCategory.id).where(
                AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == f"CALLER-{invoice_id.hex[:12]}"
            )
        )
    assert any(
        a.details["code"] == code and a.is_success is False
        for a in audits(engine, env.tenant_id, svc.AUDIT_APPROVAL_REFUSED)
    )


def status(engine, invoice_id):
    with Session(engine) as db:
        return db.get(SupplierInvoice, invoice_id).status


def test_approve_posts_balanced_to_2000_in_one_transaction(engine):
    env = world(engine)
    iid = invoice(engine, env)
    out = approve(engine, env, iid)
    assert out.replayed is False and out.invoice_id == iid
    with Session(engine) as db:
        inv = db.get(SupplierInvoice, iid)
        assert inv.status == InvoiceStatus.APPROVED and inv.approved_by == str(env.party_ids[1])
        receipt = db.scalar(sa.select(AccountingReceipt).where(AccountingReceipt.document_id == iid))
        journal = db.get(JournalEntry, UUID(receipt.accounting_reference))
        assert journal.source_owner == "payables" and journal.source_document_id == str(iid)
        assert receipt.accounting_evidence_fingerprint == journal.request_fingerprint
        lines = {
            db.get(Account, r.account_id).code: (r.debit, r.credit)
            for r in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal.id))
        }
        assert lines["5000"][0] == 200 and lines["1455"][0] == 15 and lines["2000"][1] == 215
        assert sum(d for d, _ in lines.values()) == sum(c for _, c in lines.values()) == 215
    c = counts(engine, env.tenant_id)
    assert c["journal_entries"] == 1 and c["payment_obligations"] == 1 and c["liability_events"] == 1
    assert c["accounting_receipts"] == 1
    (event,) = audits(engine, env.tenant_id, svc.AUDIT_APPROVED_POSTED)
    assert event.is_success and event.entity_id == str(iid) and event.actor_id == str(env.party_ids[1])


def test_replay_is_a_no_op(engine):
    env = world(engine)
    iid = invoice(engine, env)
    first = approve(engine, env, iid)
    after_first = counts(engine, env.tenant_id)
    again = approve(engine, env, iid, party_index=2)
    assert again.replayed is True and again.accounting_reference == first.accounting_reference
    assert counts(engine, env.tenant_id) == after_first
    assert len(audits(engine, env.tenant_id, svc.AUDIT_APPROVED_POSTED)) == 1


def test_submitter_cannot_approve_own_invoice(engine):
    env = world(engine)
    iid = invoice(engine, env)
    refusal(engine, env, iid, "sod_submitter_is_approver", party_index=0)
    assert status(engine, iid) == InvoiceStatus.SUBMITTED


def test_draft_is_not_approvable(engine):
    env = world(engine)
    iid = invoice(engine, env, submit=False)
    refusal(engine, env, iid, "not_submitted")


def test_closed_period_refuses_and_leaves_no_liability_or_ledger(engine):
    env = world(engine)
    iid = invoice(engine, env)
    soft_close(engine, env)
    refusal(engine, env, iid, "posting_period_closed")
    assert status(engine, iid) == InvoiceStatus.SUBMITTED


def test_inactive_liability_account_refuses_without_partial_state(engine):
    env = world(engine)
    iid = invoice(engine, env)
    with Session(engine) as db:
        db.execute(sa.update(Account).where(Account.id == env.accounts["2000"]).values(is_active=False))
        db.commit()
    refusal(engine, env, iid, "posting_account_not_postable")
    assert status(engine, iid) == InvoiceStatus.SUBMITTED


def test_non_canonical_account_ref_refuses(engine):
    env = world(engine)
    iid = invoice(engine, env, submit=False)  # submitted documents are immutable (DB trigger)
    with Session(engine) as db:
        db.execute(
            sa.update(SupplierInvoice)
            .where(SupplierInvoice.id == iid)
            .values(liability_account_ref=env.accounts["2000"].hex)
        )
        db.commit()
    submit(engine, env, iid)
    refusal(engine, env, iid, "posting_account_ref_form_refused")


def test_dimensioned_lines_are_refused_not_dropped(engine):
    env = world(engine)
    iid = invoice(engine, env, submit=False)  # submitted documents are immutable (DB trigger)
    with Session(engine) as db:
        db.execute(
            sa.update(SupplierInvoiceLine)
            .where(SupplierInvoiceLine.invoice_id == iid)
            .values(dimension_refs=[["COST_CENTER", str(uuid4())]])
        )
        db.commit()
    submit(engine, env, iid)
    refusal(engine, env, iid, "dimensions_unsupported")


def test_approved_without_ledger_receipt_is_refused(engine):
    env = world(engine)
    iid = invoice(engine, env)
    with Session(engine) as db:
        payables.approve_invoice(
            db,
            scope=env.scope,
            invoice_id=iid,
            approval_reference="x",
            approved_by="someone",
            idempotency_key="k",
            idempotency_expires_at=None,
            approved_at=NOW,
        )
        db.commit()
    refusal(engine, env, iid, "approved_without_ledger_receipt")


def test_historical_admitted_invoice_is_refused(engine):
    env = world(engine)
    iid = invoice(engine, env)
    with Session(engine) as db:
        payables.approve_invoice(
            db,
            scope=env.scope,
            invoice_id=iid,
            approval_reference="x",
            approved_by="importer",
            idempotency_key="k",
            idempotency_expires_at=None,
            approved_at=NOW,
        )
        obligation = db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == iid))
        event = db.scalar(sa.select(LiabilityEvent.id).where(LiabilityEvent.document_id == iid))
        db.add(
            HistoricalInvoiceReceipt(
                tenant_id=env.tenant_id,
                invoice_id=iid,
                obligation_id=obligation,
                event_id=event,
                source_owner="legacy-erp",
                source_invoice_id="L-1",
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
    refusal(engine, env, iid, "historical_invoice")


def test_party_of_another_tenant_is_refused(engine):
    env, other = world(engine), world(engine)
    iid = invoice(engine, env)
    with Session(engine) as db:
        with pytest.raises(svc.ApprovalRefused) as exc:
            svc.approve_and_post(
                db,
                tenant=db.get(Tenant, env.tenant_id),
                party=db.get(Party, other.party_ids[1]),
                invoice_id=iid,
                now=NOW,
            )
        assert exc.value.code == "actor_tenant"
    assert status(engine, iid) == InvoiceStatus.SUBMITTED


def test_other_tenants_invoice_is_not_found(engine):
    env, other = world(engine), world(engine)
    iid = invoice(engine, other)
    with Session(engine) as db:
        with pytest.raises(svc.ApprovalRefused) as exc:
            svc.approve_and_post(
                db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, env.party_ids[1]), invoice_id=iid, now=NOW
            )
        assert exc.value.code == "not_found"


def _submit_as(engine, env, iid, submitted_by):
    with Session(engine) as db:
        payables.submit_invoice(db, scope=env.scope, invoice_id=iid, submitted_by=submitted_by, submitted_at=NOW)
        db.commit()


def test_credential_id_submitter_cannot_self_approve_but_others_can(engine):
    env = world(engine)
    with Session(engine) as db:
        credential = UserCredential(tenant_id=env.tenant_id, party_id=env.party_ids[1], password_hash="x")  # noqa: S106
        db.add(credential)
        db.commit()
        credential_id = str(credential.id)
    iid = invoice(engine, env, submit=False)
    _submit_as(engine, env, iid, credential_id)  # imported-style: legacy credential id
    refusal(engine, env, iid, "sod_submitter_is_approver", party_index=1)
    assert status(engine, iid) == InvoiceStatus.SUBMITTED
    assert approve(engine, env, iid, party_index=2).replayed is False


def test_party_id_submitter_resolves_and_unresolvable_submitter_refuses(engine):
    env = world(engine)
    iid = invoice(engine, env, submit=False)
    _submit_as(engine, env, iid, str(env.party_ids[1]).upper())  # non-canonical spelling still the same person
    refusal(engine, env, iid, "sod_submitter_is_approver", party_index=1)
    for bad in (str(uuid4()), "legacy-user-7"):
        jid = invoice(engine, env, number=f"AP-{bad[:6]}", submit=False)
        _submit_as(engine, env, jid, bad)
        refusal(engine, env, jid, "submitter_unresolved", party_index=1)


def test_concurrent_approvers_yield_one_journal_and_one_receipt(engine):
    env = world(engine)
    iid = invoice(engine, env)
    holder = Session(engine)
    winner = svc.approve_and_post(
        holder,
        tenant=holder.get(Tenant, env.tenant_id),
        party=holder.get(Party, env.party_ids[1]),
        invoice_id=iid,
        now=NOW,
    )
    outcome: dict[str, object] = {}

    def second():
        try:
            outcome["result"] = approve(engine, env, iid, party_index=2)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(1.0)  # the second approver is blocked on the invoice row lock
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and "error" not in outcome, outcome
    assert winner.replayed is False
    assert outcome["result"].replayed is True
    assert outcome["result"].accounting_reference == winner.accounting_reference
    c = counts(engine, env.tenant_id)
    assert c["journal_entries"] == 1 and c["accounting_receipts"] == 1 and c["payment_obligations"] == 1
    assert len(audits(engine, env.tenant_id, svc.AUDIT_APPROVED_POSTED)) == 1


# --- V1 / V2 invoice account rules (PR-1) ---------------------------------------------------------


def add_account(engine, env, code, *, balance=NormalBalance.DEBIT):
    with Session(engine) as db:
        category = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "LIA")
        )
        account = Account(
            tenant_id=env.tenant_id,
            category_id=category.id,
            code=code,
            name=f"Account {code}",
            kind=AccountKind.POSTING,
            normal_balance=balance,
        )
        db.add(account)
        db.commit()
        return account.id


def recode(engine, account_id, code):
    with Session(engine) as db:
        db.execute(sa.update(Account).where(Account.id == account_id).values(code=code))
        db.commit()


def draft_refused(engine, env, code, **overrides):
    values = {**draft_values(env), **overrides}
    before = counts(engine, env.tenant_id)
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[0])
        with pytest.raises(drafts.RevenueActionError) as exc:
            drafts.create_supplier_draft(db, tenant=tenant, party=party, values=values)
        assert exc.value.code == code
        db.commit()
    assert counts(engine, env.tenant_id) == before  # zero rows
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(SupplierInvoice)
                .where(SupplierInvoice.tenant_id == env.tenant_id)
            )
            == 0
        )


def test_v1_draft_taxed_to_the_input_tax_account_is_created_and_posts_dr_lines_cr_2000(engine):
    env = world(engine)
    iid = invoice(engine, env)
    with Session(engine) as db:
        (line,) = db.scalars(sa.select(SupplierInvoiceLine).where(SupplierInvoiceLine.invoice_id == iid))
        assert line.tax_account_ref == str(env.tax_account)
    out = approve(engine, env, iid)
    with Session(engine) as db:
        rows = {
            db.get(Account, r.account_id).code: (r.debit, r.credit)
            for r in db.scalars(
                sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == UUID(out.accounting_reference))
            )
        }
    assert rows == {"5000": (200, 0), "1455": (15, 0), "2000": (0, 215)}


def test_v1_untaxed_lines_are_unaffected_by_the_tax_account(engine):
    env = world(engine)
    values = draft_values(env, tax="0", tax_account=env.accounts["5000"])  # ignored: no tax on the line
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[0])
        created = drafts.create_supplier_draft(db, tenant=tenant, party=party, values=values)
        drafts.submit_supplier_draft(db, tenant=tenant, party=party, invoice_id=created.id)
        db.commit()
        iid = created.id
    assert approve(engine, env, iid).replayed is False


def test_v1_draft_refuses_the_tax_account_being_the_line_account(engine):
    env = world(engine)
    draft_refused(engine, env, "tax_account_is_line_account", tax_account_id=str(env.accounts["5000"]))


@pytest.mark.parametrize("code", ["2150", "1440", "2120"])  # WHT payable, input VAT, VAT payable
def test_v1_draft_refuses_tax_accounts_outside_the_declared_set(engine, code):
    env = world(engine)
    other = add_account(engine, env, code)
    draft_refused(engine, env, "tax_account_not_input_tax", tax_account_id=str(other))


def test_v1_draft_refuses_an_expense_account_other_than_the_line_as_the_tax_account(engine):
    env = world(engine)
    expense = add_account(engine, env, "5100")
    draft_refused(engine, env, "tax_account_not_input_tax", tax_account_id=str(expense))


def test_v1_draft_refuses_when_the_input_tax_account_is_not_set_up(engine):
    env = world(engine)
    recode(engine, env.tax_account, "1456")
    draft_refused(engine, env, "input_tax_account_unavailable")


def test_v2_draft_refuses_a_liability_that_is_not_ap_control(engine):
    env = world(engine)
    other = add_account(engine, env, "2001", balance=NormalBalance.CREDIT)
    draft_refused(engine, env, "liability_not_ap_control", liability_account_id=str(other))


def test_v2_draft_refuses_when_ap_control_is_not_set_up(engine):
    env = world(engine)
    recode(engine, env.accounts["2000"], "2009")
    draft_refused(engine, env, "ap_control_unavailable")


@pytest.mark.parametrize("code", ["2150", "1440", "2120"])
def test_v1_b1_refuses_an_imported_shape_invoice_taxed_outside_the_declared_set(engine, code):
    env = world(engine)
    other = add_account(engine, env, code)
    iid = invoice(engine, env, tax_account=other)  # rewritten in SQL, as the importer would land it
    refusal(engine, env, iid, "tax_account_not_input_tax")
    assert status(engine, iid) == InvoiceStatus.SUBMITTED


def test_v1_b1_refuses_a_tax_account_equal_to_the_line_account(engine):
    env = world(engine)
    iid = invoice(engine, env, tax_account=env.accounts["5000"])
    refusal(engine, env, iid, "tax_account_is_line_account")


def test_v1_b1_refuses_when_the_input_tax_account_is_not_set_up(engine):
    env = world(engine)
    iid = invoice(engine, env)
    recode(engine, env.tax_account, "1456")
    refusal(engine, env, iid, "input_tax_account_unavailable")


def test_v2_b1_refuses_a_liability_that_is_not_ap_control(engine):
    env = world(engine)
    other = add_account(engine, env, "2001", balance=NormalBalance.CREDIT)
    iid = invoice(engine, env, submit=False)  # submitted documents are immutable (DB trigger)
    with Session(engine) as db:
        db.execute(sa.update(SupplierInvoice).where(SupplierInvoice.id == iid).values(liability_account_ref=str(other)))
        db.commit()
    submit(engine, env, iid)
    refusal(engine, env, iid, "liability_not_ap_control")


def test_v2_b1_refuses_when_ap_control_is_not_set_up(engine):
    env = world(engine)
    iid = invoice(engine, env)
    recode(engine, env.accounts["2000"], "2009")
    refusal(engine, env, iid, "ap_control_unavailable")


def test_v1_refusals_come_after_the_historical_and_receipt_checks(engine):
    env = world(engine)
    bad = env.accounts["5000"]  # a tax account that V1 would refuse
    hist = invoice(engine, env, number="AP-H", tax_account=bad)
    approved = invoice(engine, env, number="AP-R", tax_account=bad)
    with Session(engine) as db:
        for iid in (hist, approved):
            payables.approve_invoice(
                db,
                scope=env.scope,
                invoice_id=iid,
                approval_reference="x",
                approved_by="importer",
                idempotency_key=f"k-{iid}",
                idempotency_expires_at=None,
                approved_at=NOW,
            )
        obligation = db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == hist))
        event = db.scalar(sa.select(LiabilityEvent.id).where(LiabilityEvent.document_id == hist))
        db.add(
            HistoricalInvoiceReceipt(
                tenant_id=env.tenant_id,
                invoice_id=hist,
                obligation_id=obligation,
                event_id=event,
                source_owner="legacy-erp",
                source_invoice_id="L-9",
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
    refusal(engine, env, hist, "historical_invoice")
    refusal(engine, env, approved, "approved_without_ledger_receipt")
