"""B1c supplier credit notes against real PostgreSQL (hosted CI; ERP_TEST_POSTGRES_URL).

Real payables a5, accounting a2 and the real posting bridge. Reuses the B1 world (accounts 5000, 2000,
2040, the input-tax account 1455 and three people).
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import test_b1_supplier_invoice_post_pg as b1
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import NormalBalance
from dotmac_accounting.models import Account, JournalEntry, JournalLine, PostedLedgerLine
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.models import Party, Tenant
from dotmac_payables import service as payables
from dotmac_payables.contracts import CreditNoteStatus
from dotmac_payables.models import (
    AccountingReceipt,
    CreditApplication,
    CreditNote,
    CreditNoteLine,
    LiabilityEvent,
    PaymentObligation,
)
from sqlalchemy.orm import Session
from test_posting_bridge_pg import NOW

from erp_v2.assembly import assembly
from erp_v2.web import credit_note_services as svc
from erp_v2.web.revenue_services import RevenueActionError

engine = bridge_pg.engine

MODELS = (CreditNote, CreditApplication, LiabilityEvent, AccountingReceipt, JournalEntry, JournalLine)


@pytest.fixture(scope="module", autouse=True)
def audit_registry():
    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))


def counts(engine, env) -> dict[str, int]:
    with Session(engine) as db:
        return {
            m.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == env.tenant_id))
            for m in MODELS
        }


def audits(engine, env, action):
    with Session(engine) as db:
        return list(
            db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == action))
        )


def world(engine):
    return b1.world(engine)


def approved_invoice(engine, env, number="AP-1"):
    """An approved, posted B1 invoice for supplier-1 (total 215) and its obligation id."""
    iid = b1.invoice(engine, env, number=number)
    b1.approve(engine, env, iid)
    with Session(engine) as db:
        return iid, db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == iid))


def values(env, *, number="CN-1", quantity="2", price="100", tax="15", tax_account=None, **extra):
    base = {
        "request_id": str(uuid4()),
        "number": number,
        "supplier_ref": "supplier-1",
        "supplier_name": "Supplier One",
        "supplier_document_number": f"SUP-{number}",
        "credit_date": "2026-01-10",
        "description": "Credit for over-billing",
        "currency_code": "NGN",
        "quantity": quantity,
        "unit_price": price,
        "tax_amount": tax,
        "exchange_rate": "1",
        "posting_account_id": str(env.accounts["5000"]),
        "liability_account_id": str(env.accounts["2000"]),
        "tax_account_id": str(tax_account or env.tax_account),
    }
    return {**base, **extra}


def draft(engine, env, *, submit=True, **kw) -> UUID:
    with Session(engine) as db:
        tenant, maker = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[0])
        created = svc.create_credit_note_draft(db, tenant=tenant, party=maker, values=values(env, **kw))
        if submit:
            svc.submit_credit_note_draft(db, tenant=tenant, party=maker, credit_note_id=created.id)
        db.commit()
        return created.id


def approve(engine, env, credit_id, who=1, evidence="FIN-EXC-1", **kw):
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[who])
        try:
            return svc.approve_and_post(
                db, tenant=tenant, party=party, credit_note_id=credit_id, evidence_ref=evidence, now=NOW, **kw
            )
        finally:
            db.commit()  # the request boundary commits after a caught refusal too


def refusal(engine, env, credit_id, code, **kw):
    before = counts(engine, env)
    with pytest.raises(svc.CreditApprovalRefused) as exc:
        approve(engine, env, credit_id, **kw)
    assert exc.value.code == code, exc.value.code
    assert counts(engine, env) == before  # a refusal leaves nothing
    assert any(
        a.details["code"] == code and a.is_success is False for a in audits(engine, env, svc.AUDIT_APPROVAL_REFUSED)
    )


def status(engine, credit_id):
    with Session(engine) as db:
        return db.get(CreditNote, credit_id).status


def balance(engine, env):
    with Session(engine) as db:
        return payables.supplier_liability_balance(
            db, tenant_id=env.tenant_id, supplier_ref="supplier-1", currency_code="NGN"
        )


def ledger(engine, journal_id):
    with Session(engine) as db:
        return {
            db.get(Account, r.account_id).code: (r.debit, r.credit)
            for r in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == UUID(journal_id)))
        }


def test_approve_posts_dr_2000_cr_lines_and_tax_with_one_receipt_and_a_credit_event(engine):
    env = world(engine)
    approved_invoice(engine, env)
    assert balance(engine, env) == Decimal("215")
    cid = draft(engine, env)
    out = approve(engine, env, cid)
    assert out.replayed is False and out.credit_note_id == cid and out.applied_amount == 0
    assert ledger(engine, out.accounting_reference) == {"2000": (215, 0), "5000": (0, 200), "1455": (0, 15)}
    with Session(engine) as db:
        credit = db.get(CreditNote, cid)
        assert credit.status == CreditNoteStatus.APPROVED and credit.available_amount == 215
        assert credit.approved_by == str(env.party_ids[1]) and "evidence:FIN-EXC-1" in credit.approval_reference
        receipt = db.scalar(
            sa.select(AccountingReceipt).where(
                AccountingReceipt.document_kind == "credit_note", AccountingReceipt.document_id == cid
            )
        )
        assert receipt.accounting_reference == out.accounting_reference
        journal = db.get(JournalEntry, UUID(out.accounting_reference))
        assert (journal.source_owner, journal.source_document_kind) == ("payables", "credit_note")
        event = db.scalar(sa.select(LiabilityEvent).where(LiabilityEvent.document_id == cid))
        assert (event.event_kind, event.amount) == ("credit_recognized", Decimal("-215"))
    assert balance(engine, env) == 0  # fell by the credit total
    (audit,) = audits(engine, env, svc.AUDIT_APPROVED_POSTED)
    assert audit.is_success and audit.details["evidence_ref"] == "FIN-EXC-1" and audit.entity_id == str(cid)


def test_the_submitter_may_approve_their_own_credit_note_there_is_no_compulsory_second_person(engine):
    env = world(engine)
    approved_invoice(engine, env)
    cid = draft(engine, env)  # submitted by party 0
    assert approve(engine, env, cid, who=0).replayed is False


def test_replay_is_a_no_op(engine):
    env = world(engine)
    approved_invoice(engine, env)
    cid = draft(engine, env)
    first = approve(engine, env, cid)
    after = counts(engine, env)
    again = approve(engine, env, cid, who=2, evidence="OTHER")
    assert again.replayed is True and again.accounting_reference == first.accounting_reference
    assert counts(engine, env) == after
    assert len(audits(engine, env, svc.AUDIT_APPROVED_POSTED)) == 1


def test_applying_to_the_original_invoice_obligation_reduces_outstanding(engine):
    env = world(engine)
    iid, obligation = approved_invoice(engine, env)
    cid = draft(engine, env, quantity="1", price="100", tax="0", original_invoice_id=str(iid))
    out = approve(engine, env, cid, obligation_id=obligation, apply_amount="60")
    assert out.applied_amount == 60
    with Session(engine) as db:
        row = db.get(PaymentObligation, obligation)
        assert row.outstanding_amount == Decimal("155") and row.status.value == "PARTIALLY_SETTLED"
        assert db.get(CreditNote, cid).available_amount == Decimal("40")
        (application,) = db.scalars(sa.select(CreditApplication).where(CreditApplication.credit_note_id == cid))
        assert application.amount == 60 and application.obligation_id == obligation
    assert balance(engine, env) == Decimal("115")  # 215 - 100


def test_default_apply_amount_is_the_whole_credit(engine):
    env = world(engine)
    iid, obligation = approved_invoice(engine, env)
    cid = draft(engine, env, original_invoice_id=str(iid))  # total 215
    assert approve(engine, env, cid, obligation_id=obligation).applied_amount == 215
    with Session(engine) as db:
        assert db.get(PaymentObligation, obligation).status.value == "SETTLED"


def test_apply_failures_roll_back_the_whole_approval(engine):
    env = world(engine)
    iid, obligation = approved_invoice(engine, env)
    cid = draft(engine, env, quantity="3", tax="0", original_invoice_id=str(iid))  # 300 > outstanding 215
    refusal(engine, env, cid, "apply_exceeds_outstanding", obligation_id=obligation)
    assert status(engine, cid) == CreditNoteStatus.SUBMITTED
    assert balance(engine, env) == Decimal("215")
    refusal(engine, env, cid, "apply_amount_invalid", obligation_id=obligation, apply_amount="0")
    refusal(engine, env, cid, "obligation_not_found", obligation_id=uuid4())
    refusal(engine, env, cid, "apply_amount_without_obligation", apply_amount="10")
    assert approve(engine, env, cid, obligation_id=obligation, apply_amount="100").applied_amount == 100


def test_obligation_must_belong_to_the_original_invoice_when_one_is_named(engine):
    env = world(engine)
    iid, _first = approved_invoice(engine, env, number="AP-1")
    _other, other_obligation = approved_invoice(engine, env, number="AP-2")
    cid = draft(engine, env, tax="0", original_invoice_id=str(iid))
    refusal(engine, env, cid, "obligation_not_on_original_invoice", obligation_id=other_obligation)


def test_original_invoice_must_be_approved_and_match_supplier_and_currency(engine):
    env = world(engine)
    draft_invoice = b1.invoice(engine, env, number="AP-D")  # SUBMITTED, not approved
    iid, _ = approved_invoice(engine, env, number="AP-2")
    before = counts(engine, env)
    for bad, over in (
        (str(draft_invoice), {}),
        (str(iid), {"supplier_ref": "supplier-2"}),
        (str(iid), {"currency_code": "USD"}),
    ):
        with Session(engine) as db:
            with pytest.raises(RevenueActionError):
                svc.create_credit_note_draft(
                    db,
                    tenant=db.get(Tenant, env.tenant_id),
                    party=db.get(Party, env.party_ids[0]),
                    values=values(env, number=f"CN-{uuid4().hex[:6]}", original_invoice_id=bad, **over),
                )
            db.rollback()
    assert counts(engine, env) == before


def test_evidence_reference_is_required_and_a_draft_is_not_approvable(engine):
    env = world(engine)
    approved_invoice(engine, env)
    cid = draft(engine, env)
    refusal(engine, env, cid, "evidence_required", evidence="  ")
    refusal(engine, env, cid, "evidence_required", evidence="x" * 121)
    unsent = draft(engine, env, number="CN-2", submit=False)
    refusal(engine, env, unsent, "not_submitted")


def draft_refused(engine, env, code, **kw):
    before = counts(engine, env)
    with Session(engine) as db:
        with pytest.raises(RevenueActionError) as exc:
            svc.create_credit_note_draft(
                db,
                tenant=db.get(Tenant, env.tenant_id),
                party=db.get(Party, env.party_ids[0]),
                values=values(env, **kw),
            )
        assert exc.value.code == code
        db.commit()
    assert counts(engine, env) == before


def add_account(engine, env, code, balance=NormalBalance.DEBIT):
    return b1.add_account(engine, env, code, balance=balance)


def test_v1_draft_refuses_a_tax_account_that_is_the_line_account_or_not_input_tax(engine):
    env = world(engine)
    draft_refused(engine, env, "tax_account_is_line_account", tax_account=env.accounts["5000"])
    for code in ("2150", "1440", "2120"):
        other = add_account(engine, env, code)
        draft_refused(engine, env, "tax_account_not_input_tax", tax_account=other)


def test_v1_draft_refuses_when_the_input_tax_account_is_not_set_up(engine):
    env = world(engine)
    b1.recode(engine, env.tax_account, "1456")
    draft_refused(engine, env, "input_tax_account_unavailable")


def test_v2_draft_refuses_a_liability_that_is_not_ap_control(engine):
    env = world(engine)
    other = add_account(engine, env, "2001", NormalBalance.CREDIT)
    draft_refused(engine, env, "liability_not_ap_control", liability_account_id=str(other))
    b1.recode(engine, env.accounts["2000"], "2009")
    draft_refused(engine, env, "ap_control_unavailable")


def test_untaxed_credit_ignores_the_tax_account(engine):
    env = world(engine)
    approved_invoice(engine, env)
    cid = draft(engine, env, tax="0", tax_account=env.accounts["5000"])
    out = approve(engine, env, cid)
    assert ledger(engine, out.accounting_reference) == {"2000": (200, 0), "5000": (0, 200)}


def rewrite(engine, cid, **fields):
    """Model a document that did not pass the draft checks (the draft is mutable before submit)."""
    with Session(engine) as db:
        if "liability_account_ref" in fields:
            db.execute(sa.update(CreditNote).where(CreditNote.id == cid).values(**fields))
        else:
            db.execute(sa.update(CreditNoteLine).where(CreditNoteLine.credit_note_id == cid).values(**fields))
        db.commit()


def test_v1_and_v2_are_enforced_again_at_approval(engine):
    env = world(engine)
    approved_invoice(engine, env)
    other = add_account(engine, env, "2150")
    liability_other = add_account(engine, env, "2001", NormalBalance.CREDIT)
    cases = (
        ({"tax_account_ref": str(env.accounts["5000"])}, "tax_account_is_line_account"),
        ({"tax_account_ref": str(other)}, "tax_account_not_input_tax"),
        ({"liability_account_ref": str(liability_other)}, "liability_not_ap_control"),
    )
    for index, (fields, code) in enumerate(cases):
        cid = draft(engine, env, number=f"CN-V{index}", submit=False)
        rewrite(engine, cid, **fields)
        with Session(engine) as db:
            maker = db.get(Party, env.party_ids[0])
            svc.submit_credit_note_draft(db, tenant=db.get(Tenant, env.tenant_id), party=maker, credit_note_id=cid)
            db.commit()
        refusal(engine, env, cid, code)
        assert status(engine, cid) == CreditNoteStatus.SUBMITTED
    cid = draft(engine, env, number="CN-V9")
    b1.recode(engine, env.tax_account, "1456")
    refusal(engine, env, cid, "input_tax_account_unavailable")
    b1.recode(engine, env.tax_account, "1455")
    b1.recode(engine, env.accounts["2000"], "2009")
    refusal(engine, env, cid, "ap_control_unavailable")


def test_closed_period_dimensions_and_missing_ledger_receipt_refuse_without_partial_state(engine):
    env = world(engine)
    approved_invoice(engine, env)
    cid = draft(engine, env, number="CN-C")
    bridge_pg.soft_close(engine, env)
    refusal(engine, env, cid, "posting_period_closed")
    assert balance(engine, env) == Decimal("215")

    env = world(engine)
    approved_invoice(engine, env)
    dim = draft(engine, env, submit=False)
    rewrite(engine, dim, dimension_refs=[["COST_CENTER", str(uuid4())]])
    with Session(engine) as db:
        svc.submit_credit_note_draft(
            db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, env.party_ids[0]), credit_note_id=dim
        )
        db.commit()
    refusal(engine, env, dim, "dimensions_unsupported")

    orphan = draft(engine, env, number="CN-O")
    with Session(engine) as db:  # approved at the owner but never posted
        payables.approve_credit_note(
            db,
            scope=env.scope,
            credit_note_id=orphan,
            approval_reference="x",
            approved_by="someone",
            idempotency_key="k-orphan",
            idempotency_expires_at=None,
            approved_at=NOW,
        )
        db.commit()
    refusal(engine, env, orphan, "approved_without_ledger_receipt")


def test_party_of_another_tenant_and_unknown_credit_note_are_refused(engine):
    env, other = world(engine), world(engine)
    cid = draft(engine, env)
    with Session(engine) as db:
        with pytest.raises(svc.CreditApprovalRefused) as exc:
            svc.approve_and_post(
                db,
                tenant=db.get(Tenant, env.tenant_id),
                party=db.get(Party, other.party_ids[1]),
                credit_note_id=cid,
                evidence_ref="x",
            )
        assert exc.value.code == "actor_tenant"
        with pytest.raises(svc.CreditApprovalRefused) as exc:
            svc.approve_and_post(
                db,
                tenant=db.get(Tenant, env.tenant_id),
                party=db.get(Party, env.party_ids[1]),
                credit_note_id=uuid4(),
                evidence_ref="x",
            )
        assert exc.value.code == "not_found"


def test_v2_a_control_account_is_refused_as_a_credit_line_or_tax_account(engine):
    env = world(engine)
    for field, code in (("posting_account_id", "2040"), ("posting_account_id", "2000"), ("tax_account_id", "2040")):
        draft_refused(engine, env, "line_account_is_control", **{field: str(env.accounts[code])})
    control = add_account(engine, env, "1410")
    draft_refused(engine, env, "line_account_is_control", posting_account_id=str(control), tax="0")
    cid = draft(engine, env, number="CN-L", submit=False)
    rewrite(engine, cid, posting_account_ref=str(control))
    with Session(engine) as db:
        svc.submit_credit_note_draft(
            db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, env.party_ids[0]), credit_note_id=cid
        )
        db.commit()
    refusal(engine, env, cid, "line_account_is_control")
