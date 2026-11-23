"""B3 supplier payments against real PostgreSQL (hosted CI; ERP_TEST_POSTGRES_URL).

Real payables a5, accounting and kernel migrations; real create/approve/record/reverse commands and
the real posting bridge. Invoices are created and approved through the B1 path (reusing its seed),
so the AP control account holds the liability before any payment is proposed.
"""

import os
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import account_maintenance_support as accounts_gov
import pytest
import sqlalchemy as sa
import test_b1_supplier_invoice_post_pg as b1
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, PostedLedgerLine
from dotmac_banking.models import BankAccount, BankInstitution
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, PartyType, Tenant
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dotmac_kernel.settings_resolver import SettingChangeContext, upsert_by_key
from dotmac_payables import service as payables
from dotmac_payables.contracts import (
    CreateSupplierPayment,
    ObligationStatus,
    SupplierPaymentAllocationInput,
    SupplierPaymentStatus,
    SupplierPaymentWithholdingInput,
)
from dotmac_payables.models import (
    AccountingReceipt,
    LiabilityEvent,
    PaymentObligation,
    SettlementObservation,
    SupplierInvoice,
    SupplierPayment,
)
from dual_control_support import set_dual_control_engine
from sqlalchemy.orm import Session
from test_posting_bridge_pg import NOW, soft_close

from erp_v2.assembly import assembly
from erp_v2.disbursement import bank_reference as bank_ref
from erp_v2.disbursement import policy as disbursement_policy
from erp_v2.disbursement.models import DisbursementFileReceipt
from erp_v2.journals import policy as journal_policy
from erp_v2.journals.routes import journals_web_feature
from erp_v2.payees import service as payee_service
from erp_v2.payees.models import PayeeBankInstruction
from erp_v2.payroll_payouts.models import PayrollPayoutBatch
from erp_v2.supplier_payments import separation
from erp_v2.supplier_payments import service as svc
from erp_v2.supplier_payments.routes import supplier_payments_feature
from erp_v2.supplier_payments.service import AllocationRequest, PaymentRefused

engine = bridge_pg.engine  # disposable-database fixture under its own name

VALUE_DATE = date(2026, 1, 12)  # on or before NOW (2026-01-15): recording a future value date is refused
ACCOUNT_NUMBER = "0123456789"


@pytest.fixture(scope="module", autouse=True)
def registries():
    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))
    manifests = [journals_web_feature, supplier_payments_feature, FeatureManifest(name="kernel_stub")]
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


LEGACY_ACCOUNT_NUMBER = "9999999999"  # differs from ACCOUNT_NUMBER so a later seed_payee supersedes it cleanly


def seed_legacy_payee(engine, env, ref="supplier-1", number=LEGACY_ACCOUNT_NUMBER):
    """An approved instruction as the importer mints it (origin legacy_import): satisfies V3, exempt from SoD."""
    with Session(engine) as db:
        db.add(
            PayeeBankInstruction(
                tenant_id=env.tenant_id,
                payee_kind="supplier",
                payee_ref=ref,
                payee_label="Supplier One",
                bank_code="058",
                bank_name="Example Bank",
                account_number=number,
                account_last4=number[-4:],
                account_name="SUPPLIER ONE LTD",
                status="approved",
                origin="legacy_import",
                proposed_at=NOW,
                decided_by_party_id=env.people["approver"],  # the migration actor
                decided_at=NOW,
                approved_at=NOW,
            )
        )
        db.commit()


def world(engine, *, currency="NGN", payee=True):
    """``payee=False`` leaves supplier-1 without an approved instruction (V3 refuses payment then)."""
    env = _world(engine, currency=currency)
    if payee:
        seed_legacy_payee(engine, env)
    return env


def _world(engine, *, currency="NGN"):
    env = b1.world(engine)
    with Session(engine) as db:
        category = AccountCategory(tenant_id=env.tenant_id, code="AST", name="AST", account_class=AccountClass.ASSET)
        db.add(category)
        db.flush()
        cash = Account(
            tenant_id=env.tenant_id,
            category_id=category.id,
            code="1000",
            name="Operating cash",
            kind=AccountKind.POSTING,
            normal_balance=NormalBalance.DEBIT,
        )
        db.add(cash)
        db.flush()
        env.accounts["1000"] = cash.id
        people = {}
        for name in ("preparer", "approver", "recorder", "reverser"):
            party = Party(
                id=uuid4(),
                tenant_id=env.tenant_id,
                party_type=PartyType.person,
                display_name=name,
                email=f"{name}-{env.tenant_id.hex[:6]}@acme.test",
            )
            db.add(party)
            db.flush()
            people[name] = party.id
        env.people = people
        journal_policy.set_functional_currency(db, tenant_id=env.tenant_id, code="NGN", reason="test seed")
        institution = BankInstitution(
            id=uuid4(), tenant_id=env.tenant_id, code="BK", name="Bank", country_code="NG", status="active"
        )
        db.add(institution)
        db.flush()
        bank = BankAccount(
            id=uuid4(),
            tenant_id=env.tenant_id,
            institution_id=institution.id,
            account_code="OP1",
            account_name="Operating",
            account_identifier="payer-acct-1",
            account_type_code="current",
            currency_code=currency,
            minor_units=2,
            cash_account_ref=str(cash.id),
            status="active",
        )
        db.add(bank)
        env.bank_id, env.institution_id = bank.id, institution.id
        db.commit()
    return env


def open_obligation(engine, env, *, number="AP-1", approve_invoice=True) -> UUID:
    iid = b1.invoice(engine, env, number=number)
    if approve_invoice:
        b1.approve(engine, env, iid)
    with Session(engine) as db:
        return db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == iid))


def act(engine, env, who, fn, **kw):
    """One request-like unit: call, then commit (also after a caught refusal)."""
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.people[who])
        kw.setdefault("now", NOW)
        try:
            return fn(db, tenant=tenant, party=party, **kw)
        finally:
            db.commit()


def prepare(engine, env, obligation_id, amount="215", *, who="preparer", request_id=None, bank_id=None):
    return act(
        engine,
        env,
        who,
        svc.prepare_payment,
        request_id=request_id or uuid4(),
        bank_account_id=bank_id or env.bank_id,
        value_date=VALUE_DATE,
        allocations=[AllocationRequest(obligation_id, Decimal(amount))],
    )


def approve(engine, env, payment_id, who="approver"):
    return act(engine, env, who, svc.approve_payment, payment_id=payment_id)


def record(engine, env, payment_id, ref="BANK-REF-1", who="recorder"):
    return act(engine, env, who, svc.record_payment, payment_id=payment_id, bank_reference=ref)


def reverse(engine, env, payment_id, who="reverser", reversal_date=date(2026, 1, 25), reason="bounced"):
    return act(engine, env, who, svc.reverse_payment, payment_id=payment_id, reversal_date=reversal_date, reason=reason)


def approved_payment(engine, env, obligation_id, amount="215") -> UUID:
    created = prepare(engine, env, obligation_id, amount)
    approve(engine, env, created.payment_id)
    return created.payment_id


def recorded_payment(engine, env, obligation_id, ref="BANK-REF-1") -> UUID:
    payment_id = approved_payment(engine, env, obligation_id)
    record(engine, env, payment_id, ref)
    return payment_id


MODELS = (SupplierPayment, LiabilityEvent, AccountingReceipt, SettlementObservation, JournalEntry, PostedLedgerLine)


def counts(engine, env) -> dict[str, int]:
    with Session(engine) as db:
        result = {
            m.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == env.tenant_id))
            for m in MODELS
        }
        result["file_receipts"] = db.scalar(
            sa.select(sa.func.count())
            .select_from(DisbursementFileReceipt)
            .where(DisbursementFileReceipt.tenant_id == env.tenant_id)
        )
        return result


def audits(engine, env, action):
    with Session(engine) as db:
        return list(
            db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == action))
        )


def payment(engine, payment_id) -> SupplierPayment:
    with Session(engine) as db:
        return db.get(SupplierPayment, payment_id)


def obligation(engine, obligation_id) -> PaymentObligation:
    with Session(engine) as db:
        return db.get(PaymentObligation, obligation_id)


def refused(call, code):
    with pytest.raises(PaymentRefused) as exc:
        call()
    assert exc.value.code == code, exc.value.code
    return exc.value


def posting_lines(engine, journal_id) -> dict[str, tuple[Decimal, Decimal]]:
    with Session(engine) as db:
        return {
            db.get(Account, r.account_id).code: (r.debit, r.credit)
            for r in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal_id))
        }


# --- prepare ---------------------------------------------------------------


def test_payer_references_come_from_the_selected_bank_account(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    created = prepare(engine, env, oid)
    row = payment(engine, created.payment_id)
    assert row.status == SupplierPaymentStatus.DRAFT
    assert row.cash_account_ref == str(env.accounts["1000"])
    assert row.payer_instrument_ref == str(env.bank_id)
    assert row.prepared_by == str(env.people["preparer"]) and row.wht_amount == 0
    assert row.supplier_ref == "supplier-1" and row.gross_amount == row.net_amount == Decimal("215")
    assert len(audits(engine, env, svc.AUDIT_PREPARED)) == 1


def test_prepare_replays_on_the_same_request_id_without_a_second_payment_or_audit(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    request_id = uuid4()
    first = prepare(engine, env, oid, request_id=request_id)
    again = prepare(engine, env, oid, request_id=request_id)
    assert again.payment_id == first.payment_id and again.replayed is True
    assert counts(engine, env)["supplier_payments"] == 1
    assert len(audits(engine, env, svc.AUDIT_PREPARED)) == 1


def test_another_tenants_bank_account_is_refused(engine):
    env, other = world(engine), world(engine)
    oid = open_obligation(engine, env)
    refused(lambda: prepare(engine, env, oid, bank_id=other.bank_id), "bank_account_unknown")
    assert counts(engine, env)["supplier_payments"] == 0
    assert any(a.details["code"] == "bank_account_unknown" for a in audits(engine, env, svc.AUDIT_REFUSED))


def test_bank_account_must_be_active_open_cash_ledger_and_functional_currency(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    with Session(engine) as db:
        db.execute(sa.update(BankAccount).where(BankAccount.id == env.bank_id).values(status="suspended"))
        db.commit()
    refused(lambda: prepare(engine, env, oid), "bank_account_inactive")
    with Session(engine) as db:
        db.execute(
            sa.update(BankAccount).where(BankAccount.id == env.bank_id).values(status="active", currency_code="USD")
        )
        db.commit()
    refused(lambda: prepare(engine, env, oid), "bank_account_currency")
    with Session(engine) as db:
        db.execute(
            sa.update(BankAccount)
            .where(BankAccount.id == env.bank_id)
            .values(currency_code="NGN", cash_account_ref="1000")
        )
        db.commit()
    refused(lambda: prepare(engine, env, oid), "cash_account_invalid")
    with Session(engine) as db:
        db.execute(
            sa.update(BankAccount)
            .where(BankAccount.id == env.bank_id)
            .values(cash_account_ref=str(env.accounts["1000"]))
        )
        accounts_gov.retire(db, env.accounts["1000"])
        db.commit()
    refused(lambda: prepare(engine, env, oid), "cash_account_not_postable")
    assert counts(engine, env)["supplier_payments"] == 0


def test_overpayment_and_reservation_are_refused_by_the_owner(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    refused(lambda: prepare(engine, env, oid, "215.01"), "payables_refused")
    first = prepare(engine, env, oid, "200")
    assert payment(engine, first.payment_id).status == SupplierPaymentStatus.DRAFT
    refused(lambda: prepare(engine, env, oid, "16"), "payables_refused")  # only 15 left unreserved
    assert prepare(engine, env, oid, "15").replayed is False
    assert counts(engine, env)["supplier_payments"] == 2


def test_foreign_rate_invoice_is_refused_as_unsupported_fx(engine):
    env = world(engine)
    iid = b1.invoice(engine, env, number="AP-FX", submit=False)
    with Session(engine) as db:
        db.execute(sa.update(SupplierInvoice).where(SupplierInvoice.id == iid).values(exchange_rate=Decimal(2)))
        db.commit()
    b1.submit(engine, env, iid)
    b1.approve(engine, env, iid)
    with Session(engine) as db:
        oid = db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == iid))
    refused(lambda: prepare(engine, env, oid), "fx_unsupported")


def test_two_suppliers_in_one_payment_are_refused(engine):
    env = world(engine)
    first = open_obligation(engine, env, number="AP-A")
    iid = b1.invoice(engine, env, number="AP-B", submit=False)
    with Session(engine) as db:
        db.execute(sa.update(SupplierInvoice).where(SupplierInvoice.id == iid).values(supplier_ref="supplier-2"))
        db.commit()
    b1.submit(engine, env, iid)
    b1.approve(engine, env, iid)
    with Session(engine) as db:
        second = db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == iid))
    refused(
        lambda: act(
            engine,
            env,
            "preparer",
            svc.prepare_payment,
            request_id=uuid4(),
            bank_account_id=env.bank_id,
            value_date=VALUE_DATE,
            allocations=[AllocationRequest(first, Decimal(215)), AllocationRequest(second, Decimal(215))],
        ),
        "mixed_suppliers",
    )


def test_an_approved_invoice_without_a_ledger_receipt_cannot_be_paid(engine):
    env = world(engine)
    iid = b1.invoice(engine, env, number="AP-NR")
    with Session(engine) as db:  # approved by the owner directly: no accounting receipt
        payables.approve_invoice(
            db,
            scope=env.scope,
            invoice_id=iid,
            approval_reference="x",
            approved_by="someone",
            idempotency_key="k-nr",
            idempotency_expires_at=None,
            approved_at=NOW,
        )
        oid = db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == iid))
        db.commit()
    refused(lambda: prepare(engine, env, oid), "payables_refused")


# --- approve: segregation of duties ---------------------------------------


def test_preparer_cannot_approve_their_own_payment(engine):
    env = world(engine)
    created = prepare(engine, env, open_obligation(engine, env))
    refused(lambda: approve(engine, env, created.payment_id, who="preparer"), "sod_approver_is_preparer")
    assert payment(engine, created.payment_id).status == SupplierPaymentStatus.DRAFT
    assert any(a.details["code"] == "sod_approver_is_preparer" for a in audits(engine, env, svc.AUDIT_REFUSED))


def test_approve_replays_for_the_same_approver_and_is_not_repeatable_by_others(engine):
    env = world(engine)
    created = prepare(engine, env, open_obligation(engine, env))
    assert approve(engine, env, created.payment_id).replayed is False
    assert approve(engine, env, created.payment_id).replayed is True
    refused(lambda: approve(engine, env, created.payment_id, who="recorder"), "not_draft")
    assert len(audits(engine, env, svc.AUDIT_APPROVED)) == 1


def test_cancel_releases_the_reservation_and_needs_a_reason(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    created = prepare(engine, env, oid)
    refused(
        lambda: act(engine, env, "approver", svc.cancel_payment, payment_id=created.payment_id, reason=" "),
        "reason_required",
    )
    act(engine, env, "approver", svc.cancel_payment, payment_id=created.payment_id, reason="wrong supplier")
    assert payment(engine, created.payment_id).status == SupplierPaymentStatus.CANCELLED
    assert prepare(engine, env, oid).replayed is False  # reservation was released


def test_wht_payments_are_refused_at_approve_record_and_file(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    with Session(engine) as db:
        wht = payables.create_supplier_payment(
            db,
            scope=env.scope,
            command=CreateSupplierPayment(
                number="SP-WHT",
                supplier_ref="supplier-1",
                supplier_name_snapshot="Supplier One",
                currency_code="NGN",
                exchange_rate=Decimal(1),
                value_date=VALUE_DATE,
                cash_account_ref=str(env.accounts["1000"]),
                payer_instrument_ref=str(env.bank_id),
                allocations=(SupplierPaymentAllocationInput(oid, Decimal(215)),),
                prepared_by=str(env.people["preparer"]),
                withholdings=(SupplierPaymentWithholdingInput(account_ref=str(env.tax_account), amount=Decimal(15)),),
            ),
            idempotency_key="k-wht",
            idempotency_expires_at=None,
            prepared_at=NOW,
        )
        wht_id = wht.id
        db.commit()
    refused(lambda: approve(engine, env, wht_id), "wht_unsupported")
    assert payment(engine, wht_id).status == SupplierPaymentStatus.DRAFT


# --- record ----------------------------------------------------------------


def test_record_settles_posts_dr_2000_cr_bank_cash_and_receipts_in_one_transaction(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = approved_payment(engine, env, oid)
    before = counts(engine, env)
    out = record(engine, env, pid, "BANK-REF-77")
    assert out.replayed is False and out.accounting_reference
    row = payment(engine, pid)
    assert row.status == SupplierPaymentStatus.RECORDED and row.bank_reference == "BANK-REF-77"
    assert row.recorded_by == str(env.people["recorder"])
    ob = obligation(engine, oid)
    assert ob.outstanding_amount == 0 and ob.status == ObligationStatus.SETTLED
    with Session(engine) as db:
        journal = db.get(JournalEntry, UUID(out.accounting_reference))
        assert (journal.source_owner, journal.source_document_kind, journal.source_document_id) == (
            "payables",
            "supplier_payment",
            str(pid),
        )
        receipt = db.scalar(
            sa.select(AccountingReceipt).where(
                AccountingReceipt.document_kind == "supplier_payment", AccountingReceipt.document_id == pid
            )
        )
        assert receipt.accounting_reference == str(journal.id)
        assert receipt.accounting_evidence_fingerprint == journal.request_fingerprint
    lines = posting_lines(engine, UUID(out.accounting_reference))
    assert lines == {"2000": (Decimal(215), Decimal(0)), "1000": (Decimal(0), Decimal(215))}
    after = counts(engine, env)
    assert after["journal_entries"] == before["journal_entries"] + 1
    assert after["accounting_receipts"] == before["accounting_receipts"] + 1
    assert after["settlement_observations"] == before["settlement_observations"] + 1
    (event,) = audits(engine, env, svc.AUDIT_RECORDED)
    assert event.details["bank_reference"] == "BANK-REF-77" and event.actor_id == str(env.people["recorder"])


def test_partial_payment_leaves_a_partially_settled_obligation(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = approved_payment(engine, env, oid, "100")
    out = record(engine, env, pid)
    ob = obligation(engine, oid)
    assert ob.outstanding_amount == Decimal(115) and ob.status == ObligationStatus.PARTIALLY_SETTLED
    assert posting_lines(engine, UUID(out.accounting_reference)) == {
        "2000": (Decimal(100), Decimal(0)),
        "1000": (Decimal(0), Decimal(100)),
    }


def test_recorder_must_be_neither_preparer_nor_approver(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    before = counts(engine, env)
    refused(lambda: record(engine, env, pid, who="preparer"), "sod_recorder_is_preparer")
    refused(lambda: record(engine, env, pid, who="approver"), "sod_recorder_is_approver")
    assert counts(engine, env) == before
    assert payment(engine, pid).status == SupplierPaymentStatus.APPROVED


def test_draft_payment_cannot_be_recorded(engine):
    env = world(engine)
    created = prepare(engine, env, open_obligation(engine, env))
    refused(lambda: record(engine, env, created.payment_id), "not_approved")


def test_double_record_is_an_idempotent_replay_and_never_double_posts(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    first = record(engine, env, pid, "REF-A")
    after_first = counts(engine, env)
    again = record(engine, env, pid, "REF-A")
    assert again.replayed is True and again.accounting_reference == first.accounting_reference
    assert counts(engine, env) == after_first
    assert len(audits(engine, env, svc.AUDIT_RECORDED)) == 1
    refused(lambda: record(engine, env, pid, "REF-B"), "already_recorded")  # a different reference never re-records
    assert counts(engine, env) == after_first


def test_concurrent_record_yields_one_journal_one_settlement(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    holder = Session(engine)
    winner = svc.record_payment(
        holder,
        tenant=holder.get(Tenant, env.tenant_id),
        party=holder.get(Party, env.people["recorder"]),
        payment_id=pid,
        bank_reference="REF-C",
        now=NOW,
    )
    outcome: dict[str, object] = {}

    def second():
        try:
            outcome["result"] = record(engine, env, pid, "REF-C", who="recorder")
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(1.0)  # blocked on the payment row lock
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and "error" not in outcome, outcome
    assert winner.replayed is False and outcome["result"].replayed is True
    c = counts(engine, env)
    assert c["accounting_receipts"] == 2 and c["settlement_observations"] == 1  # invoice receipt + payment receipt
    assert len(audits(engine, env, svc.AUDIT_RECORDED)) == 1


def test_closed_period_refuses_and_rolls_back_settlement_and_ledger(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = approved_payment(engine, env, oid)
    soft_close(engine, env)
    before = counts(engine, env)
    refused(lambda: record(engine, env, pid), "posting_period_closed")
    assert counts(engine, env) == before
    assert payment(engine, pid).status == SupplierPaymentStatus.APPROVED
    assert obligation(engine, oid).outstanding_amount == Decimal(215)  # settlement rolled back with the ledger
    assert any(a.details["code"] == "posting_period_closed" for a in audits(engine, env, svc.AUDIT_REFUSED))


def test_inactive_cash_account_after_approval_refuses_without_partial_state(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = approved_payment(engine, env, oid)
    with Session(engine) as db:
        accounts_gov.retire(db, env.accounts["1000"])
        db.commit()
    before = counts(engine, env)
    refused(lambda: record(engine, env, pid), "posting_account_not_postable")
    assert counts(engine, env) == before
    assert obligation(engine, oid).outstanding_amount == Decimal(215)


def test_bank_reference_cannot_be_reused_on_the_same_bank_account(engine):
    env = world(engine)
    first = recorded_payment(engine, env, open_obligation(engine, env, number="AP-1"), ref="DUP-1")
    assert payment(engine, first).status == SupplierPaymentStatus.RECORDED
    second = approved_payment(engine, env, open_obligation(engine, env, number="AP-2"))
    before = counts(engine, env)
    refused(lambda: record(engine, env, second, "DUP-1"), "payables_refused")
    assert counts(engine, env) == before
    assert payment(engine, second).status == SupplierPaymentStatus.APPROVED


def payroll_payout_row(engine, env, ref, *, bank_id=None):
    """A recorded B5 payout batch holding `ref` on a bank account (the row B5 itself leaves behind)."""
    with Session(engine) as db:
        db.add(
            PayrollPayoutBatch(
                tenant_id=env.tenant_id,
                request_id=uuid4(),
                number=f"PO-{uuid4().hex[:8]}",
                run_id=uuid4(),
                kind="net_pay",
                status="recorded",
                bank_account_id=bank_id or env.bank_id,
                currency_code="NGN",
                value_date=VALUE_DATE,
                total_amount=Decimal("100"),
                line_count=1,
                prepared_by_party_id=uuid4(),
                prepared_at=NOW,
                recorded_by_party_id=uuid4(),
                recorded_at=NOW,
                bank_reference=ref,
            )
        )
        db.commit()


def test_b3_record_after_b5_with_the_same_reference_on_the_same_account_is_refused(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    payroll_payout_row(engine, env, "PAYROLL-1")
    before = counts(engine, env)
    refused(lambda: record(engine, env, pid, "payroll-1"), "bank_reference_reused")  # normalised match
    assert counts(engine, env) == before
    assert payment(engine, pid).status == SupplierPaymentStatus.APPROVED


def test_a_payroll_reference_on_a_different_bank_account_does_not_block_b3(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    with Session(engine) as db:
        other = BankAccount(
            id=uuid4(),
            tenant_id=env.tenant_id,
            institution_id=env.institution_id,
            account_code="OP2",
            account_name="Other",
            account_identifier="payer-acct-2",
            account_type_code="current",
            currency_code="NGN",
            minor_units=2,
            cash_account_ref=str(env.accounts["1000"]),
            status="active",
        )
        db.add(other)
        db.commit()
        other_id = other.id
    payroll_payout_row(engine, env, "PAYROLL-2", bank_id=other_id)
    assert record(engine, env, pid, "PAYROLL-2").replayed is False
    assert payment(engine, pid).bank_reference == "PAYROLL-2"


def test_b3_record_waits_for_a_concurrent_claim_of_the_same_reference_then_refuses(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    holder = Session(engine)  # a payroll-side recorder mid-transaction: claims the key, has not committed
    bank_ref.claim(holder, tenant_id=env.tenant_id, bank_account_id=env.bank_id, reference="RACE-1")
    outcome: dict[str, object] = {}

    def second():
        try:
            outcome["result"] = record(engine, env, pid, "RACE-1")
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(1.0)
    assert thread.is_alive()  # blocked on the advisory lock, not racing past it
    holder.add(
        PayrollPayoutBatch(
            tenant_id=env.tenant_id,
            request_id=uuid4(),
            number="PO-RACE",
            run_id=uuid4(),
            kind="net_pay",
            status="recorded",
            bank_account_id=env.bank_id,
            currency_code="NGN",
            value_date=VALUE_DATE,
            total_amount=Decimal("100"),
            line_count=1,
            prepared_by_party_id=uuid4(),
            prepared_at=NOW,
            recorded_by_party_id=uuid4(),
            recorded_at=NOW,
            bank_reference="RACE-1",
        )
    )
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and "result" not in outcome, outcome
    assert isinstance(outcome["error"], PaymentRefused) and outcome["error"].code == "bank_reference_reused"
    assert payment(engine, pid).status == SupplierPaymentStatus.APPROVED


@pytest.mark.parametrize("bad", ["AB\u0085CD", "AB\u0080CD", "AB\u009fCD"])
def test_c1_control_characters_in_a_bank_reference_are_refused(engine, bad):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    refused(lambda: record(engine, env, pid, bad), "bank_reference_invalid")


def test_bank_reference_is_required_and_printable(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    for bad in ("", "  ", "ab", "x" * 256, "bad\nref"):
        refused(lambda bad=bad: record(engine, env, pid, bad), "bank_reference_invalid")


def test_another_tenants_party_cannot_record(engine):
    env, other = world(engine), world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    with Session(engine) as db:
        with pytest.raises(PaymentRefused) as exc:
            svc.record_payment(
                db,
                tenant=db.get(Tenant, env.tenant_id),
                party=db.get(Party, other.people["recorder"]),
                payment_id=pid,
                bank_reference="REF-X",
                now=NOW,
            )
        assert exc.value.code == "actor_tenant"
    assert payment(engine, pid).status == SupplierPaymentStatus.APPROVED


def test_other_tenants_payment_is_not_found(engine):
    env, other = world(engine), world(engine)
    pid = approved_payment(engine, other, open_obligation(engine, other))
    refused(lambda: record(engine, env, pid), "not_found")
    refused(lambda: approve(engine, env, pid), "not_found")


# --- reversal --------------------------------------------------------------


def test_reversal_reopens_the_obligation_and_posts_the_mirror_in_one_transaction(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = recorded_payment(engine, env, oid)
    before = counts(engine, env)
    out = reverse(engine, env, pid)
    assert out.replayed is False
    row = payment(engine, pid)
    assert row.status == SupplierPaymentStatus.REVERSED and row.reversal_date == date(2026, 1, 25)
    ob = obligation(engine, oid)
    assert ob.outstanding_amount == Decimal(215) and ob.status == ObligationStatus.OPEN
    assert posting_lines(engine, UUID(out.accounting_reference)) == {
        "1000": (Decimal(215), Decimal(0)),
        "2000": (Decimal(0), Decimal(215)),
    }
    after = counts(engine, env)
    assert after["journal_entries"] == before["journal_entries"] + 1
    assert after["accounting_receipts"] == before["accounting_receipts"] + 1
    assert after["settlement_observations"] == before["settlement_observations"]  # evidence is never edited
    with Session(engine) as db:
        kinds = {
            r.document_kind
            for r in db.scalars(sa.select(AccountingReceipt).where(AccountingReceipt.document_id == pid))
        }
        assert kinds == {"supplier_payment", "supplier_payment_reversal"}
    (event,) = audits(engine, env, svc.AUDIT_REVERSED)
    assert event.actor_id == str(env.people["reverser"])
    # the cash account nets to zero across record + reversal, and AP control is back to the invoice credit
    with Session(engine) as db:
        net = db.scalar(
            sa.select(sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit)).where(
                PostedLedgerLine.tenant_id == env.tenant_id, PostedLedgerLine.account_id == env.accounts["1000"]
            )
        )
        assert net == 0


def test_the_obligation_can_be_paid_again_after_a_reversal(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = recorded_payment(engine, env, oid)
    reverse(engine, env, pid)
    again = recorded_payment(engine, env, oid, ref="REF-REPAY")
    assert payment(engine, again).status == SupplierPaymentStatus.RECORDED
    assert obligation(engine, oid).outstanding_amount == 0


def test_reversal_sod_recorder_approver_and_preparer_are_refused(engine):
    env = world(engine)
    pid = recorded_payment(engine, env, open_obligation(engine, env))
    before = counts(engine, env)
    refused(lambda: reverse(engine, env, pid, who="recorder"), "sod_reverser_is_recorder")
    refused(lambda: reverse(engine, env, pid, who="approver"), "sod_reverser_is_approver")
    refused(lambda: reverse(engine, env, pid, who="preparer"), "sod_reverser_is_preparer")
    assert counts(engine, env) == before
    assert payment(engine, pid).status == SupplierPaymentStatus.RECORDED


def test_reversal_replays_and_refuses_different_details(engine):
    env = world(engine)
    pid = recorded_payment(engine, env, open_obligation(engine, env))
    first = reverse(engine, env, pid)
    after = counts(engine, env)
    again = reverse(engine, env, pid)
    assert again.replayed is True and again.accounting_reference == first.accounting_reference
    assert counts(engine, env) == after
    refused(lambda: reverse(engine, env, pid, reason="a different reason"), "already_reversed")
    assert len(audits(engine, env, svc.AUDIT_REVERSED)) == 1


def test_reversal_needs_a_recorded_payment_a_reason_and_a_valid_date(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = approved_payment(engine, env, oid)
    refused(lambda: reverse(engine, env, pid), "not_recorded")
    record(engine, env, pid)
    refused(lambda: reverse(engine, env, pid, reason=" "), "reason_required")
    refused(lambda: reverse(engine, env, pid, reversal_date=date(2026, 1, 10)), "payables_refused")  # before value date
    assert payment(engine, pid).status == SupplierPaymentStatus.RECORDED


def test_reversal_in_a_closed_period_rolls_back_the_reopening_too(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = recorded_payment(engine, env, oid)
    soft_close(engine, env)
    before = counts(engine, env)
    refused(lambda: reverse(engine, env, pid), "posting_period_closed")
    assert counts(engine, env) == before
    assert payment(engine, pid).status == SupplierPaymentStatus.RECORDED
    assert obligation(engine, oid).outstanding_amount == 0  # still settled: reopening rolled back with the ledger


# --- bank file: dormant by default ----------------------------------------


def seed_payee(engine, env, number=ACCOUNT_NUMBER, proposer="preparer", decider="reverser"):
    with Session(engine) as db:
        proposed = payee_service.propose(
            db,
            tenant_id=env.tenant_id,
            proposer_party_id=env.people[proposer],
            payee_kind="supplier",
            payee_ref="supplier-1",
            bank_code="058",
            bank_name="Example Bank",
            account_number=number,
            account_name="SUPPLIER ONE LTD",
            evidence_ref="letter-1",
        )
        row = payee_service.approve(
            db, tenant_id=env.tenant_id, instruction_id=proposed.id, approver_party_id=env.people[decider]
        )
        db.commit()
        return row.id


def enable_file(engine, env, value=True):
    with Session(engine) as db:
        upsert_by_key(
            db,
            disbursement_policy.TREASURY_POLICY_DOMAIN,
            disbursement_policy.BANK_FILE_KEY,
            value,
            tenant_id=env.tenant_id,
            changed_by=SettingChangeContext(actor_party_id=None, reason="test"),
        )
        db.commit()


def generate(engine, env, payment_ids, who="recorder"):
    return act(engine, env, who, svc.generate_bank_file, payment_ids=payment_ids)


def test_bank_file_is_dormant_by_default_and_refused_without_writing(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    seed_payee(engine, env)
    with Session(engine) as db:
        assert disbursement_policy.bank_file_generation_enabled(db, env.tenant_id) is False
    refused(lambda: generate(engine, env, [pid]), "bank_file_disabled")
    assert counts(engine, env)["file_receipts"] == 0
    assert audits(engine, env, svc.AUDIT_FILE_GENERATED) == []


def test_explicitly_false_setting_stays_off_and_setting_is_tenant_scoped(engine):
    env, other = world(engine), world(engine)
    enable_file(engine, env, False)
    enable_file(engine, other, True)
    with Session(engine) as db:
        assert disbursement_policy.bank_file_generation_enabled(db, env.tenant_id) is False
        assert disbursement_policy.bank_file_generation_enabled(db, other.tenant_id) is True


def test_enabled_file_is_built_from_approved_payments_with_a_hash_receipt_and_no_bytes_retained(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    instruction_id = seed_payee(engine, env)
    enable_file(engine, env)
    built = generate(engine, env, [pid])
    assert built.payment_count == 1 and built.file.row_count == 1
    text = built.file.content.decode()
    assert ACCOUNT_NUMBER in text and "SP-" in text and "payer-acct-1" in text
    import hashlib

    assert built.file.sha256 == hashlib.sha256(built.file.content).hexdigest()
    with Session(engine) as db:
        receipts = list(
            db.scalars(sa.select(DisbursementFileReceipt).where(DisbursementFileReceipt.document_id == pid))
        )
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.file_sha256 == built.file.sha256 and receipt.row_count == 1
        assert receipt.payee_instruction_id == instruction_id
        # nothing in the receipt row or the audit trail carries the account number or the bytes
        columns = {c.name for c in DisbursementFileReceipt.__table__.columns}
        assert not {"content", "bytes", "account_number"} & columns
        dump = " ".join(str(getattr(receipt, c)) for c in columns)
        assert ACCOUNT_NUMBER not in dump
        for event in db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == env.tenant_id)):
            assert ACCOUNT_NUMBER not in str(event.details) and built.file.content.decode() not in str(event.details)
    (event,) = audits(engine, env, svc.AUDIT_FILE_GENERATED)
    assert event.details["sha256"] == built.file.sha256 and event.details["row_count"] == 1
    assert event.details["instruction_ids"] == [str(instruction_id)]


def test_file_refuses_unapproved_payments_missing_payee_instruction_and_mixed_accounts(engine):
    env = world(engine)
    enable_file(engine, env)
    draft = prepare(engine, env, open_obligation(engine, env, number="AP-D"))
    refused(lambda: generate(engine, env, [draft.payment_id]), "not_approved")
    pid = approved_payment(engine, env, open_obligation(engine, env, number="AP-E"))
    with Session(engine) as db:  # the instruction is lost after approval (V3 needed it to approve)
        db.execute(sa.delete(PayeeBankInstruction).where(PayeeBankInstruction.tenant_id == env.tenant_id))
        db.commit()
    refused(lambda: generate(engine, env, [pid]), "no_approved_payee_instruction")
    refused(lambda: generate(engine, env, []), "no_payments")
    assert counts(engine, env)["file_receipts"] == 0


def test_a_changed_payee_instruction_after_the_file_refuses_recording(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    first = seed_payee(engine, env)
    enable_file(engine, env)
    generate(engine, env, [pid])
    with Session(engine) as db:  # supersede the destination with a new approved instruction
        proposed = payee_service.propose(
            db,
            tenant_id=env.tenant_id,
            proposer_party_id=env.people["preparer"],
            payee_kind="supplier",
            payee_ref="supplier-1",
            bank_code="044",
            bank_name="Other Bank",
            account_number="1112223334",
            account_name="SUPPLIER ONE LTD",
            evidence_ref="letter-2",
        )
        second = proposed.id
        payee_service.approve(
            db, tenant_id=env.tenant_id, instruction_id=second, approver_party_id=env.people["reverser"]
        )
        db.commit()
    assert first != second
    before = counts(engine, env)
    refused(lambda: record(engine, env, pid), "payee_instruction_changed")
    assert counts(engine, env) == before


def test_an_unchanged_payee_instruction_allows_recording_after_the_file(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    seed_payee(engine, env)
    enable_file(engine, env)
    generate(engine, env, [pid])
    assert record(engine, env, pid).replayed is False


# --- runtime privileges on the receipt table -------------------------------


@pytest.fixture()
def runtime_engine(engine):
    role = f"b3_rt_{uuid4().hex[:12]}"
    admin = sa.create_engine(
        sa.make_url(os.environ["ERP_TEST_POSTGRES_URL"]), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOBYPASSRLS')
        c.exec_driver_sql(f'GRANT app_user TO "{role}"')
    rt = sa.create_engine(engine.url.set(username=role), hide_parameters=True)
    try:
        yield rt
    finally:
        rt.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


def test_receipts_are_insert_only_for_the_runtime_role_and_tenant_isolated(engine, runtime_engine):
    env, other = world(engine), world(engine)
    sha = "a" * 64

    def scoped(tenant):
        db = Session(runtime_engine)
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        return db

    with scoped(env.tenant_id) as db:
        db.add(
            DisbursementFileReceipt(
                tenant_id=env.tenant_id,
                document_kind="supplier_payment",
                document_id=uuid4(),
                payee_instruction_id=uuid4(),
                file_format="generic-csv-v1",
                file_sha256=sha,
                row_count=1,
                generated_by_party_id=env.people["recorder"],
                generated_at=NOW,
            )
        )
        db.commit()
    with scoped(env.tenant_id) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(DisbursementFileReceipt)) == 1
        for statement in (
            "UPDATE erp_disbursement_file_receipts SET row_count = 2",
            "DELETE FROM erp_disbursement_file_receipts",
            "TRUNCATE erp_disbursement_file_receipts",
        ):
            with pytest.raises(sa.exc.ProgrammingError) as err:
                db.execute(sa.text(statement))
            assert "permission denied" in str(err.value.orig).lower(), statement
            db.rollback()
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(env.tenant_id)})
    with scoped(other.tenant_id) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(DisbursementFileReceipt)) == 0
        with pytest.raises(sa.exc.DBAPIError):  # WITH CHECK refuses a row for another tenant
            db.add(
                DisbursementFileReceipt(
                    tenant_id=env.tenant_id,
                    document_kind="supplier_payment",
                    document_id=uuid4(),
                    payee_instruction_id=uuid4(),
                    file_format="generic-csv-v1",
                    file_sha256=sha,
                    row_count=1,
                    generated_by_party_id=env.people["recorder"],
                    generated_at=NOW,
                )
            )
            db.flush()


# --- review round 1: one file per payment ----------------------------------


def test_one_file_per_payment_in_schema_and_service(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    seed_payee(engine, env)
    enable_file(engine, env)
    generate(engine, env, [pid])
    refused(lambda: generate(engine, env, [pid]), "file_already_issued")
    assert counts(engine, env)["file_receipts"] == 1
    with Session(engine) as db:  # the schema refuses a second receipt even when the service is bypassed
        db.add(
            DisbursementFileReceipt(
                tenant_id=env.tenant_id,
                document_kind="supplier_payment",
                document_id=pid,
                payee_instruction_id=uuid4(),
                file_format="generic-csv-v1",
                file_sha256="b" * 64,
                row_count=1,
                generated_by_party_id=env.people["recorder"],
                generated_at=NOW,
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()


def test_cancel_after_a_file_needs_a_recall_reference_which_is_audited(engine):
    env = world(engine)
    oid = open_obligation(engine, env)
    pid = approved_payment(engine, env, oid)
    seed_payee(engine, env)
    enable_file(engine, env)
    generate(engine, env, [pid])
    refused(
        lambda: act(engine, env, "approver", svc.cancel_payment, payment_id=pid, reason="wrong amount"),
        "file_issued",
    )
    assert payment(engine, pid).status == SupplierPaymentStatus.APPROVED
    act(
        engine,
        env,
        "approver",
        svc.cancel_payment,
        payment_id=pid,
        reason="wrong amount",
        recall_reference="BANK-RECALL-9",
    )
    assert payment(engine, pid).status == SupplierPaymentStatus.CANCELLED
    (event,) = audits(engine, env, svc.AUDIT_CANCELLED)
    assert event.details["recall_reference"] == "BANK-RECALL-9"


def test_cancel_without_a_file_needs_no_recall_reference(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    act(engine, env, "approver", svc.cancel_payment, payment_id=pid, reason="duplicate")
    (event,) = audits(engine, env, svc.AUDIT_CANCELLED)
    assert event.details["recall_reference"] is None


def test_bank_file_issuer_is_not_a_payment_or_instruction_party(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    seed_payee(engine, env, proposer="reverser", decider="recorder")
    enable_file(engine, env)
    refused(lambda: generate(engine, env, [pid], who="preparer"), "sod_file_issuer_is_payment_party")
    refused(lambda: generate(engine, env, [pid], who="approver"), "sod_file_issuer_is_payment_party")
    refused(lambda: generate(engine, env, [pid], who="reverser"), "sod_file_issuer_is_instruction_party")  # proposed it
    refused(lambda: generate(engine, env, [pid], who="recorder"), "sod_file_issuer_is_instruction_party")  # approved it
    assert counts(engine, env)["file_receipts"] == 0


# --- review round 1: payment approver vs the payee instruction ---------------


def test_payment_approver_may_not_be_the_instruction_proposer_or_decider_at_approve(engine):
    for proposer, decider in (("preparer", "approver"), ("approver", "reverser")):
        env = world(engine)
        created = prepare(engine, env, open_obligation(engine, env))
        seed_payee(engine, env, proposer=proposer, decider=decider)
        refused(
            lambda env=env, created=created: approve(engine, env, created.payment_id),
            "sod_approver_is_instruction_party",
        )
        assert payment(engine, created.payment_id).status == SupplierPaymentStatus.DRAFT
        approve(engine, env, created.payment_id, who="recorder")  # a different approver is fine


def test_legacy_migrated_instruction_does_not_restrict_the_approver(engine):
    env = world(engine, payee=False)
    created = prepare(engine, env, open_obligation(engine, env))
    seed_legacy_payee(engine, env)
    assert approve(engine, env, created.payment_id).replayed is False


def test_approver_independence_is_rechecked_at_file_and_record(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    seed_payee(engine, env, proposer="preparer", decider="approver")  # instruction approved after the payment
    enable_file(engine, env)
    before = counts(engine, env)
    refused(lambda: generate(engine, env, [pid]), "sod_approver_is_instruction_party")
    refused(lambda: record(engine, env, pid), "sod_approver_is_instruction_party")
    assert counts(engine, env) == before


def test_record_refuses_a_future_value_date_and_accepts_the_day_itself(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    before = counts(engine, env)
    early = datetime(2026, 1, 11, 23, 59, tzinfo=UTC)
    refused(
        lambda: act(engine, env, "recorder", svc.record_payment, payment_id=pid, bank_reference="REF-F", now=early),
        "value_date_future",
    )
    assert counts(engine, env) == before
    on_day = datetime(2026, 1, 12, 0, 0, tzinfo=UTC)
    assert (
        act(engine, env, "recorder", svc.record_payment, payment_id=pid, bank_reference="REF-F", now=on_day).replayed
        is False
    )


def test_bank_reference_is_normalised_before_the_uniqueness_check(engine):
    env = world(engine)
    first = approved_payment(engine, env, open_obligation(engine, env, number="AP-1"))
    out = record(engine, env, first, "  dup   ref-1 ")
    assert payment(engine, first).bank_reference == "DUP REF-1"
    assert record(engine, env, first, "Dup Ref-1").replayed is True  # same reference, different spelling
    assert out.accounting_reference
    second = approved_payment(engine, env, open_obligation(engine, env, number="AP-2"))
    before = counts(engine, env)
    refused(
        lambda: record(engine, env, second, "DUP\u00a0ref-1"), "payables_refused"
    )  # non-breaking space collapses too
    refused(lambda: record(engine, env, second, "dup ref-1"), "payables_refused")
    assert counts(engine, env) == before
    assert payment(engine, second).status == SupplierPaymentStatus.APPROVED


def _run_downgrade(engine, *, with_receipt: bool):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from erp_v2.migrations.versions import e2_0018_disbursement_receipts as migration

    env = world(engine)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if with_receipt:
                connection.execute(
                    sa.insert(DisbursementFileReceipt.__table__).values(
                        id=uuid4(),
                        tenant_id=env.tenant_id,
                        document_kind="supplier_payment",
                        document_id=uuid4(),
                        payee_instruction_id=uuid4(),
                        file_format="generic-csv-v1",
                        file_sha256="c" * 64,
                        row_count=1,
                        generated_by_party_id=env.people["recorder"],
                        generated_at=NOW,
                    )
                )
            if not with_receipt:  # inside the rolled-back transaction: earlier tests' receipts do not matter
                connection.execute(sa.text("DELETE FROM erp_disbursement_file_receipts"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            return connection.execute(sa.text("SELECT to_regclass('public.erp_disbursement_file_receipts')")).scalar()
        finally:
            transaction.rollback()  # DDL is transactional: the shared test schema is untouched


def test_downgrade_refuses_when_receipts_exist(engine):
    with pytest.raises(RuntimeError, match="receipts exist"):
        _run_downgrade(engine, with_receipt=True)
    with engine.connect() as c:
        assert c.scalar(sa.text("SELECT to_regclass('public.erp_disbursement_file_receipts')")) is not None


def test_downgrade_drops_an_empty_table(engine):
    assert _run_downgrade(engine, with_receipt=False) is None


# --- V3: an approved payee instruction is required, whatever the bank-file setting (PR-1) -------


def payment_status(engine, payment_id):
    from dotmac_payables.models import SupplierPayment

    with Session(engine) as db:
        return db.get(SupplierPayment, payment_id).status


def test_v3_approve_refuses_without_an_approved_instruction_and_the_payment_stays_a_draft(engine):
    env = world(engine, payee=False)
    with Session(engine) as db:
        assert disbursement_policy.bank_file_generation_enabled(db, env.tenant_id) is False  # files off
    created = prepare(engine, env, open_obligation(engine, env))
    before = counts(engine, env)
    refused(lambda: approve(engine, env, created.payment_id), "payee_instruction_required")
    assert counts(engine, env) == before
    assert payment_status(engine, created.payment_id) == SupplierPaymentStatus.DRAFT


def test_v3_a_merely_proposed_instruction_does_not_satisfy_it(engine):
    env = world(engine, payee=False)
    created = prepare(engine, env, open_obligation(engine, env))
    with Session(engine) as db:
        proposed = payee_service.propose(
            db,
            tenant_id=env.tenant_id,
            proposer_party_id=env.people["preparer"],
            payee_kind="supplier",
            payee_ref="supplier-1",
            bank_code="058",
            bank_name="Example Bank",
            account_number=ACCOUNT_NUMBER,
            account_name="SUPPLIER ONE LTD",
            evidence_ref="letter-1",
        )
        proposed_id = proposed.id
        db.commit()
    assert proposed_id
    refused(lambda: approve(engine, env, created.payment_id), "payee_instruction_required")


def test_v3_a_human_approved_instruction_satisfies_it(engine):
    env = world(engine, payee=False)
    created = prepare(engine, env, open_obligation(engine, env))
    seed_payee(engine, env)  # proposed by one person, approved by another
    assert approve(engine, env, created.payment_id).replayed is False


def test_v3_a_legacy_import_approved_instruction_satisfies_it(engine):
    env = world(engine, payee=False)
    created = prepare(engine, env, open_obligation(engine, env))
    seed_legacy_payee(engine, env)
    assert approve(engine, env, created.payment_id).replayed is False


def test_v3_record_refuses_when_the_instruction_is_gone_and_no_file_was_issued(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    with Session(engine) as db:
        db.execute(sa.delete(PayeeBankInstruction).where(PayeeBankInstruction.tenant_id == env.tenant_id))
        db.commit()
    before = counts(engine, env)
    refused(lambda: record(engine, env, pid), "payee_instruction_required")
    assert counts(engine, env) == before
    seed_legacy_payee(engine, env)
    assert record(engine, env, pid).replayed is False


def test_v3_with_a_file_the_existing_codes_still_apply(engine):
    env = world(engine)
    pid = approved_payment(engine, env, open_obligation(engine, env))
    seed_payee(engine, env)  # supersedes the world's instruction
    enable_file(engine, env)
    generate(engine, env, [pid])
    with Session(engine) as db:
        db.execute(sa.delete(PayeeBankInstruction).where(PayeeBankInstruction.tenant_id == env.tenant_id))
        db.commit()
    refused(lambda: record(engine, env, pid), "payee_instruction_unavailable")


@pytest.mark.parametrize("code", ["2000", "2040", "1410", "1400", "2045"])
def test_a_bank_whose_cash_gl_is_a_control_account_is_refused(engine, code):
    env = world(engine)
    if code not in env.accounts:
        env.accounts[code] = b1.add_account(engine, env, code)
    oid = open_obligation(engine, env)
    with Session(engine) as db:
        db.execute(
            sa.update(BankAccount).where(BankAccount.id == env.bank_id).values(cash_account_ref=str(env.accounts[code]))
        )
        db.commit()
    before = counts(engine, env)
    refused(lambda: prepare(engine, env, oid), "cash_account_is_control")
    assert counts(engine, env) == before


# --- dual-control policy OFF (D-9) ------------------------------------------------------------------------------
# The ERP-side checks (instruction party, recorder, reverser, file issuer) become no-ops. Until payables a6 pins
# ``separation_of_duties`` the Starter owner still refuses approve-by-preparer (and its own reverse/record rules),
# so those steps keep the owner's refusal; the ERP codes must not appear.


def test_policy_off_erp_checks_do_not_fire_and_the_owner_still_refuses_approve_by_preparer(engine):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    created = prepare(engine, env, open_obligation(engine, env))
    if separation.payables_separation_supported():  # payables a7: ERP passes the policy (OFF), so the owner allows it
        approve(engine, env, created.payment_id, who="preparer")
        assert payment(engine, created.payment_id).status == SupplierPaymentStatus.APPROVED
        return
    with pytest.raises(PaymentRefused) as exc:
        approve(engine, env, created.payment_id, who="preparer")
    assert not exc.value.code.startswith("sod_"), exc.value.code  # the owner (payables a6) refuses, not the ERP check
    assert payment(engine, created.payment_id).status == SupplierPaymentStatus.DRAFT


def test_policy_off_instruction_party_approves_and_issues_the_file(engine):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    created = prepare(engine, env, open_obligation(engine, env))
    seed_payee(engine, env, proposer="approver", decider="approver")  # the approver also decided the instruction
    enable_file(engine, env)
    approve(engine, env, created.payment_id)  # sod_approver_is_instruction_party does not fire
    generate(engine, env, [created.payment_id], who="approver")  # approver issues the file: no ERP refusal
    assert record(engine, env, created.payment_id).replayed is False
    assert payment(engine, created.payment_id).status == SupplierPaymentStatus.RECORDED
    if not separation.payables_separation_supported():  # payables a6 still refuses a recorder-reversal (a7: allowed)
        with pytest.raises(PaymentRefused) as exc:
            reverse(engine, env, created.payment_id, who="recorder")
        assert not exc.value.code.startswith("sod_")
    assert reverse(engine, env, created.payment_id, who="reverser").replayed is False
    assert payment(engine, created.payment_id).status == SupplierPaymentStatus.REVERSED


def test_policy_on_the_refusals_still_fire_for_the_same_actors(engine):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, True)
    created = prepare(engine, env, open_obligation(engine, env))
    refused(lambda: approve(engine, env, created.payment_id, who="preparer"), "sod_approver_is_preparer")
    approve(engine, env, created.payment_id)
    refused(lambda: record(engine, env, created.payment_id, who="approver"), "sod_recorder_is_approver")
    refused(lambda: record(engine, env, created.payment_id, who="preparer"), "sod_recorder_is_preparer")
    record(engine, env, created.payment_id)
    for who, code in (
        ("recorder", "sod_reverser_is_recorder"),
        ("approver", "sod_reverser_is_approver"),
        ("preparer", "sod_reverser_is_preparer"),
    ):
        refused(lambda who=who: reverse(engine, env, created.payment_id, who=who), code)


def test_policy_on_the_instruction_party_cannot_approve_or_issue_the_file(engine):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, True)
    created = prepare(engine, env, open_obligation(engine, env))
    seed_payee(engine, env, proposer="approver", decider="reverser")
    enable_file(engine, env)
    refused(lambda: approve(engine, env, created.payment_id), "sod_approver_is_instruction_party")
