"""Governed re-point of a bank account's cash GL (flush-only; the caller owns the transaction).

PROPOSED -> APPROVED | REJECTED. The proposer and the approver must be different people (service
and DB CHECK); roles are enforced one layer up (``finance_preparer`` proposes, ``finance_approver``
approves, the existing P0 role pair that SoD keeps as two people). Approval is the only writer of
``mod_banking.bank_accounts.cash_account_ref`` in this module, in the same transaction, after
re-checking everything under locks. It never posts, moves or reclassifies money: the balance
move is Finance's B2 journals (docs/cash-gl-split.md).

Lock order (declared with B7): bank account row, then the target GL account row. A reader that
resolves payer references (B3/B5) takes a FOR SHARE lock on the bank account row, so an approval
cannot interleave with a payment being proposed against the old reference.

Refused (stable codes): ``same_person``, ``target_not_asset`` / ``target_not_postable`` /
``target_inactive`` / ``target_currency``, ``target_has_activity`` (any journal line, posted or draft),
``target_retired``, ``already_a_cash_gl`` (some bank account already uses
the target as its cash GL), ``unchanged`` (this bank account already uses it),
``baseline_approved`` / ``reconciliation_in_flight`` (B7), ``payments_in_flight`` (B3 DRAFT or
APPROVED supplier payments, B5 draft/approved/file_issued batches), ``open_request_exists``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalLine
from dotmac_banking.models import BankAccount
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.models import Party
from dotmac_payables.contracts import SupplierPaymentStatus
from dotmac_payables.models import SupplierPayment
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.banking import recon_models as recon
from erp_v2.cash_gl.models import CashAccountRepoint, RepointStatus
from erp_v2.governance import policy as governance
from erp_v2.payroll_payouts import models as payout

AUDIT_PROPOSED: Final = "erp.banking.cash_gl_repoint.proposed"
AUDIT_APPROVED: Final = "erp.banking.cash_gl_repoint.approved"
AUDIT_REJECTED: Final = "erp.banking.cash_gl_repoint.rejected"
AUDIT_ACTIONS: Final = (AUDIT_PROPOSED, AUDIT_APPROVED, AUDIT_REJECTED)
SOURCE_APPLICATION: Final = "dotmac_erp"

_IN_FLIGHT_PAYMENTS: Final = (SupplierPaymentStatus.DRAFT, SupplierPaymentStatus.APPROVED)
_IN_FLIGHT_BATCHES: Final = (payout.DRAFT, payout.APPROVED, payout.FILE_ISSUED)


class RepointRefused(ValueError):
    """A refused command. ``code`` is stable; the message carries no secret."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC)


def _text(value: str | None, name: str, *, limit: int, required: bool = True) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        if required:
            raise RepointRefused("invalid_input", f"{name} is required")
        return None
    if len(cleaned) > limit:
        raise RepointRefused("invalid_input", f"{name} is too long")
    return cleaned


def _actor(db: Session, tenant_id: UUID, party_id: UUID) -> None:
    found = db.scalar(
        select(Party.id).where(Party.id == party_id, Party.tenant_id == tenant_id, Party.is_active.is_(True))
    )
    if found is None:
        raise RepointRefused("actor_invalid", "the acting party is not an active party of this tenant")


def _audit(db: Session, *, tenant_id: UUID, actor: UUID, action: str, row: CashAccountRepoint, **details) -> None:
    write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_party_id=actor,
        actor_type="user",
        actor_id=str(actor),
        source_application=SOURCE_APPLICATION,
        action=action,
        entity_type="cash_account_repoint",
        entity_id=str(row.id),
        is_success=True,
        details={
            "bank_account_id": str(row.bank_account_id),
            "from_ref": row.from_ref,
            "to_account_id": str(row.to_account_id),
            "to_account_code": row.to_account_code,
            "evidence_ref": row.evidence_ref,
            **details,
        },
    )


def _lock_bank_account(db: Session, tenant_id: UUID, bank_account_id: UUID) -> BankAccount:
    bank = db.scalar(
        select(BankAccount)
        .where(BankAccount.tenant_id == tenant_id, BankAccount.id == bank_account_id)
        .with_for_update()
    )
    if bank is None:
        raise RepointRefused("bank_account_unknown", "bank account not found in this tenant")
    if bank.status == "closed":
        raise RepointRefused("bank_account_closed", "a closed bank account cannot be re-pointed")
    return bank


def _uses_gl(ref: str | None, account: Account) -> bool:
    value = (ref or "").strip()
    return bool(value) and value in (str(account.id), account.code)


def _validate(db: Session, tenant_id: UUID, bank: BankAccount, target_id: UUID) -> Account:
    """Every target and bank-side check. Called under the bank-account lock; locks the target."""
    target = db.scalar(select(Account).where(Account.tenant_id == tenant_id, Account.id == target_id))
    if target is None:
        raise RepointRefused("target_unknown", "target account not found in this tenant")
    # Declared lock order: bank account (held by the caller), then the GL account row.
    db.scalar(select(Account.id).where(Account.tenant_id == tenant_id, Account.id == target_id).with_for_update())
    db.refresh(target)
    if not target.is_active:
        raise RepointRefused("target_inactive", "the target account is not active")
    if target.kind != AccountKind.POSTING or not target.posting_allowed:
        raise RepointRefused("target_not_postable", "the target account is not a postable account")
    klass = db.scalar(select(AccountCategory.account_class).where(AccountCategory.id == target.category_id))
    if klass != AccountClass.ASSET or target.normal_balance != NormalBalance.DEBIT:
        raise RepointRefused("target_not_asset", "the target account is not a debit-normal ASSET account")
    if target.currency_code is not None and target.currency_code != bank.currency_code:
        raise RepointRefused("target_currency", "the target account currency differs from the bank account")
    if db.scalar(
        select(CashAccountRepoint.id)
        .where(
            CashAccountRepoint.tenant_id == tenant_id,
            CashAccountRepoint.status == RepointStatus.APPROVED.value,
            CashAccountRepoint.from_ref.in_((str(target.id), target.code)),
        )
        .limit(1)
    ):
        raise RepointRefused("target_retired", "the target account is the retired shared cash GL of a re-point")
    if _uses_gl(bank.cash_account_ref, target):
        raise RepointRefused("unchanged", "the bank account already uses this cash account")
    for other in db.scalars(select(BankAccount).where(BankAccount.tenant_id == tenant_id)):
        if other.id != bank.id and _uses_gl(other.cash_account_ref, target):
            raise RepointRefused(
                "already_a_cash_gl", f"bank account {other.account_code} already uses this account as its cash GL"
            )
    # The target must be empty: re-point FIRST, then Finance posts the reclassification journals. Any journal
    # line, posted or draft, makes it an account with history, not a fresh per-bank cash GL.
    if db.scalar(
        select(JournalLine.id).where(JournalLine.tenant_id == tenant_id, JournalLine.account_id == target.id).limit(1)
    ):
        raise RepointRefused("target_has_activity", "the target account already has journal lines (posted or draft)")
    # B7: re-point happens BEFORE baselines. Any approved reconciliation fixes the account's GL.
    live = {
        status
        for status in db.scalars(
            select(recon.BankReconciliation.status).where(
                recon.BankReconciliation.tenant_id == tenant_id,
                recon.BankReconciliation.account_id == bank.id,
                recon.BankReconciliation.status.in_(recon.LIVE_STATUSES),
            )
        )
    }
    if recon.STATUS_APPROVED in live:
        raise RepointRefused(
            "baseline_approved", "the bank account has an approved reconciliation; re-point before baselines"
        )
    if live:
        raise RepointRefused("reconciliation_in_flight", "the bank account has a reconciliation in progress")
    payments = db.scalar(
        select(func.count())
        .select_from(SupplierPayment)
        .where(
            SupplierPayment.tenant_id == tenant_id,
            SupplierPayment.payer_instrument_ref == str(bank.id),
            SupplierPayment.status.in_(_IN_FLIGHT_PAYMENTS),
        )
    )
    batches = db.scalar(
        select(func.count())
        .select_from(payout.PayrollPayoutBatch)
        .where(
            payout.PayrollPayoutBatch.tenant_id == tenant_id,
            payout.PayrollPayoutBatch.bank_account_id == bank.id,
            payout.PayrollPayoutBatch.status.in_(_IN_FLIGHT_BATCHES),
        )
    )
    if payments or batches:
        raise RepointRefused(
            "payments_in_flight",
            f"unposted payments for this bank account: {payments or 0} supplier payment(s), "
            f"{batches or 0} payout batch(es)",
        )
    return target


def propose(
    db: Session,
    *,
    tenant_id: UUID,
    proposer_party_id: UUID,
    bank_account_id: UUID,
    target_account_id: UUID,
    evidence_ref: str,
    now: datetime | None = None,
) -> CashAccountRepoint:
    _actor(db, tenant_id, proposer_party_id)
    evidence = _text(evidence_ref, "evidence_ref", limit=240)
    bank = _lock_bank_account(db, tenant_id, bank_account_id)
    if db.scalar(
        select(CashAccountRepoint.id).where(
            CashAccountRepoint.tenant_id == tenant_id,
            CashAccountRepoint.bank_account_id == bank.id,
            CashAccountRepoint.status == RepointStatus.PROPOSED.value,
        )
    ):
        raise RepointRefused("open_request_exists", "this bank account already has a re-point awaiting a decision")
    target = _validate(db, tenant_id, bank, target_account_id)
    row = CashAccountRepoint(
        id=uuid4(),
        tenant_id=tenant_id,
        dual_control=governance.dual_control(db, tenant_id),
        bank_account_id=bank.id,
        from_ref=bank.cash_account_ref,
        to_account_id=target.id,
        to_account_code=target.code,
        status=RepointStatus.PROPOSED.value,
        evidence_ref=evidence,
        proposed_by_party_id=proposer_party_id,
        proposed_at=now or _now(),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise RepointRefused(
            "open_request_exists", "this bank account already has a re-point awaiting a decision"
        ) from exc
    _audit(db, tenant_id=tenant_id, actor=proposer_party_id, action=AUDIT_PROPOSED, row=row)
    return row


def _load_for_decision(db: Session, tenant_id: UUID, request_id: UUID) -> tuple[CashAccountRepoint, BankAccount]:
    bank_id = db.scalar(
        select(CashAccountRepoint.bank_account_id).where(
            CashAccountRepoint.tenant_id == tenant_id, CashAccountRepoint.id == request_id
        )
    )
    if bank_id is None:
        raise RepointRefused("not_found", "re-point request not found")
    bank = _lock_bank_account(db, tenant_id, bank_id)  # bank account first, then the request
    row = db.scalar(
        select(CashAccountRepoint)
        .where(CashAccountRepoint.tenant_id == tenant_id, CashAccountRepoint.id == request_id)
        .with_for_update()
    )
    if row is None or row.status != RepointStatus.PROPOSED.value:
        raise RepointRefused("wrong_state", "only a proposed re-point can be decided")
    return row, bank


def approve(
    db: Session,
    *,
    tenant_id: UUID,
    request_id: UUID,
    approver_party_id: UUID,
    note: str | None = None,
    now: datetime | None = None,
) -> CashAccountRepoint:
    _actor(db, tenant_id, approver_party_id)
    decision_note = _text(note, "note", limit=500, required=False)
    row, bank = _load_for_decision(db, tenant_id, request_id)
    if row.dual_control and row.proposed_by_party_id == approver_party_id:
        raise RepointRefused("same_person", "the proposer cannot approve their own re-point")
    if (bank.cash_account_ref or "") != row.from_ref:
        raise RepointRefused("drift", "the bank account's cash reference changed since the proposal")
    target = _validate(db, tenant_id, bank, row.to_account_id)
    if target.code != row.to_account_code:
        raise RepointRefused("drift", "the target account code changed since the proposal")
    stamp = now or _now()
    bank.cash_account_ref = str(target.id)
    row.status = RepointStatus.APPROVED.value
    row.decided_by_party_id = approver_party_id
    row.decided_at = stamp
    row.decision_note = decision_note
    row.applied_ref = str(target.id)
    row.updated_at = stamp
    db.flush()
    _audit(
        db, tenant_id=tenant_id, actor=approver_party_id, action=AUDIT_APPROVED, row=row, applied_ref=row.applied_ref
    )
    return row


def reject(
    db: Session,
    *,
    tenant_id: UUID,
    request_id: UUID,
    decider_party_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> CashAccountRepoint:
    _actor(db, tenant_id, decider_party_id)
    note = _text(reason, "reason", limit=500)
    row, _bank = _load_for_decision(db, tenant_id, request_id)
    stamp = now or _now()
    row.status = RepointStatus.REJECTED.value
    row.decided_by_party_id = decider_party_id
    row.decided_at = stamp
    row.decision_note = note
    row.updated_at = stamp
    db.flush()
    _audit(db, tenant_id=tenant_id, actor=decider_party_id, action=AUDIT_REJECTED, row=row, reason=note)
    return row


def list_requests(db: Session, *, tenant_id: UUID, limit: int = 100) -> list[CashAccountRepoint]:
    return list(
        db.scalars(
            select(CashAccountRepoint)
            .where(CashAccountRepoint.tenant_id == tenant_id)
            .order_by(CashAccountRepoint.proposed_at.desc())
            .limit(limit)
        )
    )
