"""B1c against payables a6 (cash-basis release of deferred input VAT), real PostgreSQL.

Needs the a6 owner (``declare_tax_deferral``): skipped on the released a5 pin. The ERP computes no VAT split;
these tests prove it posts what the OWNER's split and release rows say, in one savepoint, and accepts 1440
only from them.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_b1_supplier_invoice_post_pg as b1
import test_b1c_credit_notes_pg as cn
import test_b3_supplier_payments_pg as b3
import test_posting_bridge_pg as bridge_pg
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.models import Party, Tenant
from dotmac_payables import models as pm
from dotmac_payables import service as payables
from dotmac_payables.contracts import CreditNoteStatus
from dotmac_payables.models import (
    AccountingReceipt,
    CreditApplication,
    CreditNote,
    PaymentObligation,
)
from sqlalchemy.orm import Session
from test_posting_bridge_pg import NOW

from erp_v2.web import credit_note_services as svc

pytestmark = pytest.mark.skipif(
    not hasattr(payables, "declare_tax_deferral"),
    reason="payables a6 (cash-basis VAT release) is not installed; the ERP pin follows its release",
)

engine = bridge_pg.engine
registries = b3.registries
audit_registry = cn.audit_registry


def a6_world(engine):
    """B3 world (bank, people, supplier-1 payee) + a 1440 account + the declared 1455 -> 1440 policy."""
    from dotmac_payables.contracts import DeclareTaxDeferral

    env = b3.world(engine)
    release = b1.add_account(engine, env, "1440")
    env.accounts["1440"] = release
    with Session(engine) as db:
        payables.declare_tax_deferral(
            db,
            scope=TenantScope(env.tenant_id),
            command=DeclareTaxDeferral(str(env.tax_account), str(release), 2, "finance", "FIN-VAT-POLICY-1"),
            idempotency_key=f"declare:{uuid4()}",
            idempotency_expires_at=None,
            declared_at=NOW,
        )
        db.commit()
    return env


def invoice_with_obligation(engine, env, number="AP-1"):
    return cn.approved_invoice(engine, env, number)


def pay(engine, env, obligation_id, amount, ref):
    created = b3.prepare(engine, env, obligation_id, amount)
    b3.approve(engine, env, created.payment_id)
    b3.record(engine, env, created.payment_id, ref)
    return created.payment_id


def outstanding(engine, obligation_id):
    with Session(engine) as db:
        return db.get(PaymentObligation, obligation_id).outstanding_amount


def splits(engine, credit_id):
    with Session(engine) as db:
        return [
            (row.line_number, row.deferred_amount, row.released_amount)
            for row in db.scalars(
                sa.select(pm.CreditNoteTaxSplit).where(pm.CreditNoteTaxSplit.credit_note_id == credit_id)
            )
        ]


def lines_of(engine, journal_id):
    return cn.ledger(engine, journal_id)


def credit(engine, env, iid, *, number, quantity="2", price="100", tax="15"):
    return cn.draft(engine, env, number=number, quantity=quantity, price=price, tax=tax, original_invoice_id=str(iid))


def test_a_credit_against_an_unpaid_original_reverses_only_1455(engine):
    env = a6_world(engine)
    iid, _ob = invoice_with_obligation(engine, env)
    cid = credit(engine, env, iid, number="CN-U")
    out = cn.approve(engine, env, cid)
    assert lines_of(engine, out.accounting_reference) == {"2000": (215, 0), "5000": (0, 200), "1455": (0, 15)}
    assert splits(engine, cid) == [(1, Decimal("15"), Decimal("0"))]


def test_a_credit_against_a_fully_paid_original_reverses_1440_not_1455(engine):
    env = a6_world(engine)
    iid, ob = invoice_with_obligation(engine, env)
    pay(engine, env, ob, "215", "PAY-FULL")
    assert outstanding(engine, ob) == 0
    cid = credit(engine, env, iid, number="CN-P")
    out = cn.approve(engine, env, cid)
    assert lines_of(engine, out.accounting_reference) == {"2000": (215, 0), "5000": (0, 200), "1440": (0, 15)}
    assert splits(engine, cid) == [(1, Decimal("0"), Decimal("15"))]


def test_a_credit_against_a_part_paid_original_splits_between_1455_and_1440(engine):
    env = a6_world(engine)
    iid, ob = invoice_with_obligation(engine, env)
    pay(engine, env, ob, "107.5", "PAY-HALF")  # releases 7.50 of the 15
    cid = credit(engine, env, iid, number="CN-H")
    out = cn.approve(engine, env, cid)
    assert lines_of(engine, out.accounting_reference) == {
        "2000": (215, 0),
        "5000": (0, 200),
        "1455": (0, Decimal("7.5")),
        "1440": (0, Decimal("7.5")),
    }
    assert splits(engine, cid) == [(1, Decimal("7.5"), Decimal("7.5"))]


def test_a_credit_tax_above_the_originals_vat_is_refused_to_the_exceptions_review(engine):
    env = a6_world(engine)
    iid, _ob = invoice_with_obligation(engine, env)
    cid = credit(engine, env, iid, number="CN-X", quantity="1", price="100", tax="20")  # original VAT is 15
    before = cn.counts(engine, env)
    with pytest.raises(svc.CreditApprovalRefused) as exc:
        cn.approve(engine, env, cid)
    assert exc.value.code == "credit_tax_exceeds_invoice_tax" and "exceptions review" in str(exc.value)
    assert cn.counts(engine, env) == before
    assert cn.status(engine, cid) == CreditNoteStatus.SUBMITTED


def test_a_taxed_credit_without_an_original_is_refused_even_under_a_policy(engine):
    env = a6_world(engine)
    invoice_with_obligation(engine, env)
    cn.draft_refused(engine, env, "deferred_tax_credit_requires_invoice")


def test_the_user_can_never_type_1440_as_a_credit_tax_account(engine):
    env = a6_world(engine)
    cn.draft_refused(engine, env, "tax_account_not_input_tax", tax_account=env.accounts["1440"])
    iid, _ob = invoice_with_obligation(engine, env)
    cid = cn.draft(engine, env, number="CN-T", submit=False, original_invoice_id=str(iid))
    cn.rewrite(engine, cid, tax_account_ref=str(env.accounts["1440"]))
    with Session(engine) as db:
        svc.submit_credit_note_draft(
            db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, env.party_ids[0]), credit_note_id=cid
        )
        db.commit()
    cn.refusal(engine, env, cid, "tax_account_not_input_tax")


def test_closing_the_original_with_its_own_credit_releases_the_remaining_deferral_and_posts_it(engine):
    env = a6_world(engine)
    iid, ob = invoice_with_obligation(engine, env)
    pay(engine, env, ob, "107.5", "PAY-HALF")  # R = 7.50, O = 107.50
    cid = credit(engine, env, iid, number="CN-OWN", quantity="1", price="107.5", tax="0")
    out = cn.approve(engine, env, cid, obligation_id=ob)  # applies 107.50 == O: the closing application
    assert out.applied_amount == Decimal("107.5") and out.application_reference is not None
    assert lines_of(engine, out.application_reference) == {"1440": (Decimal("7.5"), 0), "1455": (0, Decimal("7.5"))}
    assert outstanding(engine, ob) == 0
    with Session(engine) as db:
        (application,) = db.scalars(sa.select(CreditApplication).where(CreditApplication.credit_note_id == cid))
        receipt = db.scalar(
            sa.select(AccountingReceipt).where(
                AccountingReceipt.document_kind == "supplier_credit_application",
                AccountingReceipt.document_id == application.id,
            )
        )
        assert receipt.accounting_reference == out.application_reference
        (row,) = db.scalars(
            sa.select(pm.CreditApplicationTaxRelease).where(
                pm.CreditApplicationTaxRelease.application_id == application.id
            )
        )
        assert row.amount == Decimal("7.5")


def test_a_non_final_own_application_releases_nothing_and_posts_no_journal(engine):
    env = a6_world(engine)
    iid, ob = invoice_with_obligation(engine, env)
    pay(engine, env, ob, "107.5", "PAY-HALF")
    cid = credit(engine, env, iid, number="CN-PART", quantity="1", price="100", tax="0")
    before = cn.counts(engine, env)
    out = cn.approve(engine, env, cid, obligation_id=ob, apply_amount="50")
    assert out.applied_amount == 50 and out.application_reference is None
    assert cn.counts(engine, env)["journal_entries"] == before["journal_entries"] + 1  # the credit only
    assert outstanding(engine, ob) == Decimal("57.5")


def test_a_document_credit_cannot_be_set_off_against_another_invoice(engine):
    env = a6_world(engine)
    iid, _first = invoice_with_obligation(engine, env, "AP-1")
    _other, other_ob = invoice_with_obligation(engine, env, "AP-2")
    cid = credit(engine, env, iid, number="CN-SETOFF", quantity="1", price="100", tax="0")
    cn.refusal(engine, env, cid, "obligation_not_on_original_invoice", obligation_id=other_ob)
    cn.approve(engine, env, cid)  # approved, not applied
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[1])
        with pytest.raises(svc.CreditApprovalRefused) as exc:
            svc.apply_credit(
                db,
                tenant=tenant,
                party=party,
                credit_note_id=cid,
                obligation_id=other_ob,
                apply_amount=None,
                evidence_ref="x",
                request_id=str(uuid4()),
                now=NOW,
            )
        assert exc.value.code == "obligation_not_on_original_invoice"
        db.commit()


def test_a_failing_release_posting_rolls_the_application_back(engine):
    env = a6_world(engine)
    iid, ob = invoice_with_obligation(engine, env)
    pay(engine, env, ob, "107.5", "PAY-HALF")
    cid = credit(engine, env, iid, number="CN-RB", quantity="1", price="107.5", tax="0")
    bridge_pg.soft_close(engine, env)  # the period is closed: the credit's own posting is refused first
    before = cn.counts(engine, env)
    with pytest.raises(svc.CreditApprovalRefused):
        cn.approve(engine, env, cid, obligation_id=ob)
    assert cn.counts(engine, env) == before
    assert outstanding(engine, ob) == Decimal("107.5")


# --- CASH credits (historical overpayments and advances) ------------------------------------------


class _Fixed:
    def __init__(self, evidence):
        self.evidence = evidence

    def verify(self, *, scope, command, destination):
        return self.evidence


def cash_credit(engine, env, amount="100"):
    """Admit a historical overpayment as an approved CASH credit for supplier-1 (a6 owner admission)."""
    from dotmac_payables.contracts import (
        HistoricalSettlement,
        HistoricalSupplierCreditCommand,
        HistoricalSupplierCreditEvidence,
    )

    n = uuid4().hex[:8].upper()
    shell = b1.invoice(engine, env, number=f"SHELL-{n}", tax="0")
    with Session(engine) as db:
        db.execute(
            sa.text(
                "UPDATE mod_payables.supplier_invoices SET status='APPROVED', approved_by='legacy:a', "
                "approved_at=now(), approval_reference='legacy' WHERE id=:id"
            ),
            {"id": shell},
        )
        db.commit()
    credit_amount = Decimal(amount)
    settled = Decimal("200") + credit_amount
    evidence = HistoricalSupplierCreditEvidence(
        tenant_id=env.tenant_id,
        source_owner="legacy",
        source_invoice_id=f"src:{n}",
        source_snapshot_sha256="a" * 64,
        invoice_id=shell,
        source_status="POSTED",
        invoice_type="STANDARD",
        number=f"SHELL-{n}",
        supplier_ref="supplier-1",
        supplier_name_snapshot="Supplier One",
        currency_code="NGN",
        exchange_rate=Decimal(1),
        total_amount=Decimal("200"),
        liability_account_ref=str(env.accounts["2000"]),
        settlements=(
            HistoricalSettlement(
                f"hc-alloc:{n}:1",
                "PAY-1",
                date(2026, 1, 12),
                settled,
                Decimal("0"),
                "NGN",
                Decimal(1),
                "cv1:" + "1" * 64,
                "legacy:p1",
                "v2:p1",
            ),
        ),
        settled_amount=settled,
        withholding_tax_amount=Decimal("0"),
        credit_amount=credit_amount,
        carried_control_amount=-credit_amount,
        carried_evidence_ref="opening:row-1",
        original_posted_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_journal_ref="s",
        destination_journal_ref="d",
        accounting_evidence_fingerprint="cv1:" + "b" * 64,
        facts_fingerprint="cv1:" + "c" * 64,
    )
    with Session(engine) as db:
        credit_note = payables.admit_historical_supplier_credit(
            db,
            scope=TenantScope(env.tenant_id),
            command=HistoricalSupplierCreditCommand(
                evidence.source_owner,
                evidence.source_invoice_id,
                shell,
                f"HCN-{n}",
                evidence.source_snapshot_sha256,
                evidence.fingerprint(),
                "finance:r5b#19",
                "u:fin",
            ),
            verifier=_Fixed(evidence),
            idempotency_key=f"cash:{n}",
            idempotency_expires_at=None,
            admitted_at=NOW,
        )
        credit_id = credit_note.id
        db.commit()
    return credit_id


def apply(engine, env, cid, obligation, amount=None, who=1):
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[who])
        try:
            return svc.apply_credit(
                db,
                tenant=tenant,
                party=party,
                credit_note_id=cid,
                obligation_id=obligation,
                apply_amount=amount,
                evidence_ref="FIN-APPLY-1",
                request_id=str(uuid4()),
                now=NOW,
            )
        finally:
            db.commit()


def test_a_cash_credit_applies_to_another_invoice_and_counts_as_payment_for_deferred_vat(engine):
    env = a6_world(engine)
    _iid, ob = invoice_with_obligation(engine, env)  # 215 open, R = 15
    cid = cash_credit(engine, env, "100")
    assert svc.is_cash_credit(Session(engine), env.tenant_id, cid) is True
    out = apply(engine, env, cid, ob, "100")
    assert out.amount == 100 and out.release_journal is not None
    # release = 15 x 100 / 215 = 6.98 (half up): Dr 1440 / Cr 1455, posted with the application
    assert lines_of(engine, out.release_journal) == {"1440": (Decimal("6.98"), 0), "1455": (0, Decimal("6.98"))}
    assert outstanding(engine, ob) == Decimal("115")
    with Session(engine) as db:
        assert db.get(CreditNote, cid).available_amount == 0


def test_a_cash_credit_that_clears_the_invoice_releases_the_exact_remainder(engine):
    env = a6_world(engine)
    _iid, ob = invoice_with_obligation(engine, env)
    cid = cash_credit(engine, env, "215")
    out = apply(engine, env, cid, ob)  # default amount: the whole available credit
    assert lines_of(engine, out.release_journal) == {"1440": (15, 0), "1455": (0, 15)}
    assert outstanding(engine, ob) == 0


def test_cash_credit_apply_failures_roll_back_and_are_audited(engine):
    env = a6_world(engine)
    _iid, ob = invoice_with_obligation(engine, env)
    cid = cash_credit(engine, env, "100")
    before = cn.counts(engine, env)
    with pytest.raises(svc.CreditApprovalRefused) as exc:
        apply(engine, env, cid, ob, "300")  # more than the credit
    assert exc.value.code == "apply_amount_invalid"
    assert cn.counts(engine, env) == before and outstanding(engine, ob) == Decimal("215")
    assert any(a.details["code"] == "apply_amount_invalid" for a in cn.audits(engine, env, svc.AUDIT_APPLY_REFUSED))


def test_an_unapproved_credit_cannot_be_applied(engine):
    env = a6_world(engine)
    _iid, ob = invoice_with_obligation(engine, env)
    cid = cn.draft(engine, env, number="CN-NA", tax="0")
    with pytest.raises(svc.CreditApprovalRefused) as exc:
        apply(engine, env, cid, ob)
    assert exc.value.code == "not_approved"


def test_a_double_submitted_application_does_not_apply_or_post_twice(engine):
    env = a6_world(engine)
    _iid, ob = invoice_with_obligation(engine, env)
    cid = cash_credit(engine, env, "215")
    request = str(uuid4())
    args = dict(
        credit_note_id=cid, obligation_id=ob, apply_amount="215", evidence_ref="FIN-APPLY-1", request_id=request
    )
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[1])
        svc.apply_credit(db, tenant=tenant, party=party, now=NOW, **args)
        db.commit()
        after = cn.counts(engine, env)
        with pytest.raises(svc.CreditApprovalRefused) as exc:
            svc.apply_credit(db, tenant=tenant, party=party, now=NOW, **args)  # the credit is spent
        assert exc.value.code == "apply_amount_invalid"
        db.commit()
    assert cn.counts(engine, env) == after
    assert outstanding(engine, ob) == 0


def test_the_account_check_rejects_a_credit_entry_on_an_account_not_on_the_document(engine):
    from types import SimpleNamespace

    env = a6_world(engine)
    iid, _ob = invoice_with_obligation(engine, env)
    cid = credit(engine, env, iid, number="CN-CHK")
    with Session(engine) as db:
        credit_row = db.get(CreditNote, cid)
        rows = [SimpleNamespace(posting_account_ref=str(env.accounts["5000"]), tax_account_ref=str(env.tax_account))]
        stray = SimpleNamespace(entries=[SimpleNamespace(account_ref=str(env.accounts["1000"]))])
        with pytest.raises(svc.CreditApprovalRefused) as exc:
            svc._check_consequence_accounts(db, env.tenant_id, credit_row, rows, stray)
        assert exc.value.code == "credit_consequence_account_unexpected"
        ok = SimpleNamespace(entries=[SimpleNamespace(account_ref=str(env.accounts["2000"]))])
        svc._check_consequence_accounts(db, env.tenant_id, credit_row, rows, ok)
