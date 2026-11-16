"""Payroll payouts (B5): net-pay batches (optional bank file) and statutory remittances.

A batch covers WHOLE liabilities of one finalized payroll run, chosen by kind:

* ``net_pay``: liabilities with key ``employee-net-pay`` (beneficiary type employee). Lifecycle
  DRAFT -> APPROVED -> [FILE_ISSUED] -> RECORDED. The bank file step is optional (typed tenant
  setting, default OFF, plus the treasury permission); recording is an assertion by a named person
  that the money left through the bank portal under a bank evidence reference.
* ``statutory``: component liabilities (PAYE, pension, NHIS) to ONE external destination.
  DRAFT -> APPROVED -> RECORDED; never a file (W19).

Nothing else is ever paid: deductions and employer liabilities are never in a net-pay batch, and
internal recoveries (asset-class liability accounts; from payroll a4 ``settlement_mode`` other than
``external``) are never in any batch. Recording calls the owner's ``record_liability_settlement`` per
line, builds the settlement consequence (Dr liability account, e.g. 2040 / Cr the selected bank
account's cash GL, posting date = the typed bank value date) and posts it through the bridge, all in
ONE savepoint: either the whole batch is recorded and posted or nothing is. Payroll a3 has no
accounting-receipt table, so each line stores its settlement id, journal id and consequence
fingerprint (the ERP-side receipt); the bridge's source identity makes replays no-ops.

Segregation of duties (service AND, for the people columns, the database): preparer, approver, file
issuer and recorder are four distinct people. Approver and file issuer must not be the proposer or
decider of any net-pay line's CURRENT payee bank instruction (migrated legacy instructions are
exempt), re-checked at approve, file and record. Recording refuses if any line's instruction changed
since the file, a future value date, a reused bank reference, or if any line is no longer
outstanding at exactly its amount. One file per batch (receipt per line, UNIQUE), cancel after a file
needs an audited recall reference. File bytes are never stored or logged; only sha256 and row count.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_accounting.contracts import AccountClass
from dotmac_accounting.models import Account, AccountCategory
from dotmac_banking.models import BankAccount
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.money import Currency, Money
from dotmac_payroll import service as payroll
from dotmac_payroll.consequences import build_liability_settlement_consequence
from dotmac_payroll.models import PayrollLiability, PayrollRun
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.disbursement import file as bank_file
from erp_v2.disbursement import policy as disbursement_policy
from erp_v2.disbursement.models import KIND_PAYROLL_PAYOUT, DisbursementFileReceipt
from erp_v2.journals import policy as journal_policy
from erp_v2.payees import service as payee_service
from erp_v2.payees.models import InstructionOrigin, InstructionStatus, PayeeBankInstruction, PayeeKind
from erp_v2.payroll_payouts.models import (
    APPROVED,
    CANCELLED,
    DRAFT,
    FILE_ISSUED,
    KIND_NET_PAY,
    KIND_STATUTORY,
    RECORDED,
    PayrollPayoutBatch,
    PayrollPayoutLine,
)
from erp_v2.posting import rules
from erp_v2.posting.payroll import post_payroll_consequence
from erp_v2.posting.rules import PostingRefused
from erp_v2.supplier_payments.service import PaymentRefused, payer_references

AUDIT_PREPARED: Final = "erp.payroll.payout.prepared"
AUDIT_APPROVED: Final = "erp.payroll.payout.approved"
AUDIT_CANCELLED: Final = "erp.payroll.payout.cancelled"
AUDIT_RECORDED: Final = "erp.payroll.payout.recorded_posted"
AUDIT_FILE_GENERATED: Final = "erp.payroll.payout.file_generated"
AUDIT_REFUSED: Final = "erp.payroll.payout.refused"
AUDIT_ACTIONS: Final = (
    AUDIT_PREPARED,
    AUDIT_APPROVED,
    AUDIT_CANCELLED,
    AUDIT_RECORDED,
    AUDIT_FILE_GENERATED,
    AUDIT_REFUSED,
)

NET_PAY_KEY: Final = "employee-net-pay"
COMPONENT_PREFIX: Final = "component:"
MAX_LINES: Final = bank_file.MAX_ROWS
EXTERNAL: Final = "external"
_NET_PAYABLE_CODES: Final = rules.RefPolicy(frozenset({"2040"}))


class PayoutRefused(ValueError):
    """A safe, user-facing refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PayoutOutcome:
    batch_id: UUID
    replayed: bool
    journals: int = 0


@dataclass(frozen=True, slots=True)
class BankFile:
    file: bank_file.DisbursementFile
    filename: str
    line_count: int


def _audit(
    db: Session, *, tenant: Tenant, party: Party, entity_id: str, action: str, success: bool, details: dict[str, object]
) -> None:
    write_audit_event(
        db,
        tenant_id=tenant.id,
        actor_party_id=party.id,
        actor_type="user",
        actor_id=str(party.id),
        actor_label=getattr(party, "email", None) or None,
        source_application="dotmac_erp",
        action=action,
        entity_type="payroll_payout_batch",
        entity_id=entity_id,
        is_success=success,
        details=details,
    )


def _same(left: object, right: object) -> bool:
    return left is not None and right is not None and str(left).strip().casefold() == str(right).strip().casefold()


def _locked_batch(db: Session, tenant_id: UUID, batch_id: UUID) -> PayrollPayoutBatch:
    batch = db.scalar(
        select(PayrollPayoutBatch)
        .where(PayrollPayoutBatch.tenant_id == tenant_id, PayrollPayoutBatch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if batch is None:
        raise PayoutRefused("not_found", "Payout batch was not found")
    return batch


def _lines(db: Session, tenant_id: UUID, batch_id: UUID) -> list[PayrollPayoutLine]:
    return list(
        db.scalars(
            select(PayrollPayoutLine)
            .where(PayrollPayoutLine.tenant_id == tenant_id, PayrollPayoutLine.batch_id == batch_id)
            .order_by(PayrollPayoutLine.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _functional_currency(db: Session, tenant_id: UUID) -> str:
    try:
        return journal_policy.functional_currency(db, tenant_id)
    except journal_policy.FunctionalCurrencyError as exc:
        raise PayoutRefused("functional_currency_unset", "The tenant has no functional currency setting") from exc


def _payer(db: Session, tenant_id: UUID, bank_account_id: UUID, currency: str) -> tuple[str, str]:
    try:
        return payer_references(db, tenant_id=tenant_id, bank_account_id=bank_account_id, currency=currency)
    except PaymentRefused as exc:
        raise PayoutRefused(exc.code, str(exc)) from exc


# --- eligibility ------------------------------------------------------------------------------


def settlement_mode_allows_payout(liability: object) -> bool:
    """Payroll a4 gate. a3 liabilities have no ``settlement_mode``: the explicit kind assertions in
    ``check_liability`` are the guard. From a4 (attribute present) only ``external`` may be paid out;
    internal recoveries are never a cash outflow. The a4 pin PR needs no change here: the attribute
    appears with the release and this check turns on by itself (see tests).
    """
    if not hasattr(liability, "settlement_mode"):
        return True
    return getattr(liability, "settlement_mode") == EXTERNAL  # noqa: B009


def _account_class(db: Session, tenant_id: UUID, ref: str) -> AccountClass | None:
    try:
        form, value = rules.classify_ref(_NET_PAYABLE_CODES, ref)
    except PostingRefused:
        return None
    column = Account.id == UUID(value) if form == "uuid" else Account.code == value
    accounts = list(db.scalars(select(Account).where(Account.tenant_id == tenant_id, column)))
    if len(accounts) != 1:
        return None
    return db.scalar(
        select(AccountCategory.account_class).where(
            AccountCategory.tenant_id == tenant_id, AccountCategory.id == accounts[0].category_id
        )
    )


def check_liability(
    db: Session, *, kind: str, run: PayrollRun, liability: PayrollLiability, destination_ref: str | None
) -> None:
    """Refuse unless this liability is exactly what a batch of ``kind`` may pay. Pure reads."""
    if liability.run_id != run.id or liability.tenant_id != run.tenant_id:
        raise PayoutRefused("liability_not_in_run", "A liability does not belong to the run")
    if kind == KIND_NET_PAY:
        if (
            liability.liability_key != NET_PAY_KEY
            or liability.beneficiary_type != "employee"
            or liability.component_id is not None
        ):
            raise PayoutRefused("liability_not_net_pay", "Only employee net pay can be in a net-pay payout")
    elif kind == KIND_STATUTORY:
        if (
            not liability.liability_key.startswith(COMPONENT_PREFIX)
            or liability.beneficiary_type != EXTERNAL
            or liability.component_id is None
        ):
            raise PayoutRefused("liability_not_statutory", "Only external component liabilities can be remitted")
        if destination_ref is not None and liability.beneficiary_ref != destination_ref:
            raise PayoutRefused("mixed_destinations", "A remittance batch pays one destination")
    else:
        raise PayoutRefused("invalid_kind", "Unknown payout kind")
    if not settlement_mode_allows_payout(liability):
        raise PayoutRefused("internal_recovery_excluded", "Internal recoveries are never paid out")
    if _account_class(db, liability.tenant_id, liability.liability_account_ref) != AccountClass.LIABILITY:
        raise PayoutRefused(
            "internal_recovery_excluded", "The liability account is not a payable (asset-class recoveries are not paid)"
        )
    if liability.status != "outstanding" or liability.amount_settled != 0 or liability.amount <= 0:
        raise PayoutRefused("liability_not_outstanding", "A liability is already (partly) settled or empty")


def _revalidate(db: Session, batch: PayrollPayoutBatch, lines: Sequence[PayrollPayoutLine]) -> None:
    """Before approve/record every line's liability must still be whole, outstanding and unchanged."""
    run = db.scalar(select(PayrollRun).where(PayrollRun.tenant_id == batch.tenant_id, PayrollRun.id == batch.run_id))
    if run is None or run.status != "finalized":
        raise PayoutRefused("run_not_finalized", "The payroll run is not finalized")
    total = Decimal(0)
    for line in lines:
        liability = db.scalar(
            select(PayrollLiability)
            .where(PayrollLiability.tenant_id == batch.tenant_id, PayrollLiability.id == line.liability_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if liability is None:
            raise PayoutRefused("liability_changed", "A liability of this batch no longer exists")
        if liability.amount != line.amount or liability.liability_account_ref != line.liability_account_ref:
            raise PayoutRefused("liability_changed", "A liability changed after the batch was prepared")
        check_liability(db, kind=batch.kind, run=run, liability=liability, destination_ref=batch.destination_ref)
        total += line.amount
    if total != batch.total_amount or len(lines) != batch.line_count:
        raise PayoutRefused("batch_total_changed", "The batch lines no longer add up to the batch total")


# --- payee instruction SoD ------------------------------------------------------------------


def _current_instruction(db: Session, tenant_id: UUID, employee_ref: str) -> PayeeBankInstruction | None:
    return db.scalar(
        select(PayeeBankInstruction).where(
            PayeeBankInstruction.tenant_id == tenant_id,
            PayeeBankInstruction.payee_kind == PayeeKind.EMPLOYEE.value,
            PayeeBankInstruction.payee_ref == employee_ref,
            PayeeBankInstruction.status == InstructionStatus.APPROVED.value,
        )
    )


def _refuse_instruction_party(
    db: Session, batch: PayrollPayoutBatch, lines: Sequence[PayrollPayoutLine], person: object, *, code: str, role: str
) -> None:
    """``person`` must not have proposed or decided any net-pay line's CURRENT instruction (legacy exempt)."""
    if batch.kind != KIND_NET_PAY or person is None:
        return
    for line in lines:
        instruction = _current_instruction(db, batch.tenant_id, line.beneficiary_ref)
        if instruction is None or instruction.origin == InstructionOrigin.LEGACY_IMPORT.value:
            continue
        if _same(person, instruction.proposed_by_party_id) or _same(person, instruction.decided_by_party_id):
            raise PayoutRefused(
                code,
                f"The {role} proposed or approved the current bank instruction of a payee in this batch; "
                "a different person must act",
            )


def _file_receipts(db: Session, tenant_id: UUID, lines: Sequence[PayrollPayoutLine]) -> list[DisbursementFileReceipt]:
    return list(
        db.scalars(
            select(DisbursementFileReceipt).where(
                DisbursementFileReceipt.tenant_id == tenant_id,
                DisbursementFileReceipt.document_kind == KIND_PAYROLL_PAYOUT,
                DisbursementFileReceipt.document_id.in_([line.id for line in lines]),
            )
        )
    )


def _check_file_destinations(db: Session, batch: PayrollPayoutBatch, lines: Sequence[PayrollPayoutLine]) -> None:
    receipts = {r.document_id: r for r in _file_receipts(db, batch.tenant_id, lines)}
    if len(receipts) != len(lines) or any(
        r.file_sha256 != batch.file_sha256 or r.row_count != len(lines) for r in receipts.values()
    ):
        raise PayoutRefused("file_receipt_mismatch", "The bank file receipts do not match this batch")
    for line in lines:
        try:
            destination = payee_service.destination_for_payment(
                db, tenant_id=batch.tenant_id, payee_kind=PayeeKind.EMPLOYEE.value, payee_ref=line.beneficiary_ref
            )
        except payee_service.PayeeBankError as exc:
            raise PayoutRefused(
                "payee_instruction_unavailable",
                "A payee has no approved bank instruction any more; cancel the batch and prepare it again",
            ) from exc
        if destination.instruction_id != receipts[line.id].payee_instruction_id:
            raise PayoutRefused(
                "payee_instruction_changed",
                "A payee's bank instruction changed after the file was generated; cancel and prepare it again",
            )


# --- commands -----------------------------------------------------------------------------


def _prepare(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    run_id: UUID,
    kind: str,
    bank_account_id: UUID,
    value_date: date,
    destination_ref: str | None,
    liability_ids: Sequence[UUID] | None,
    now: datetime,
) -> PayoutOutcome:
    if kind not in (KIND_NET_PAY, KIND_STATUTORY):
        raise PayoutRefused("invalid_kind", "Choose net pay or a statutory remittance")
    existing = db.scalar(
        select(PayrollPayoutBatch).where(
            PayrollPayoutBatch.tenant_id == tenant.id, PayrollPayoutBatch.request_id == request_id
        )
    )
    if existing is not None:
        if existing.run_id != run_id or existing.kind != kind:
            raise PayoutRefused("request_reused", "This form was already used for a different payout")
        return PayoutOutcome(existing.id, True)
    destination = (destination_ref or "").strip() or None
    if kind == KIND_STATUTORY and destination is None:
        raise PayoutRefused("destination_required", "Choose the remittance destination")
    if kind == KIND_NET_PAY:
        destination = None
    run = db.scalar(select(PayrollRun).where(PayrollRun.tenant_id == tenant.id, PayrollRun.id == run_id))
    if run is None:
        raise PayoutRefused("run_not_found", "Payroll run was not found")
    if run.status != "finalized":
        raise PayoutRefused("run_not_finalized", "Only a finalized payroll run can be paid out")
    functional = _functional_currency(db, tenant.id)
    if run.currency_code != functional:
        raise PayoutRefused("non_functional_currency", "Payouts are in the tenant functional currency only")
    _payer(db, tenant.id, bank_account_id, functional)
    stmt = select(PayrollLiability).where(PayrollLiability.tenant_id == tenant.id, PayrollLiability.run_id == run.id)
    if kind == KIND_NET_PAY:
        stmt = stmt.where(PayrollLiability.liability_key == NET_PAY_KEY)
    else:
        stmt = stmt.where(
            PayrollLiability.liability_key.like(COMPONENT_PREFIX + "%"), PayrollLiability.beneficiary_ref == destination
        )
    candidates = {
        row.id: row
        for row in db.scalars(
            stmt.order_by(PayrollLiability.id).with_for_update().execution_options(populate_existing=True)
        )
    }
    live = set(
        db.scalars(
            select(PayrollPayoutLine.liability_id).where(
                PayrollPayoutLine.tenant_id == tenant.id, PayrollPayoutLine.active.is_(True)
            )
        )
    )
    if liability_ids is None:
        chosen = [c for c in candidates.values() if c.status == "outstanding" and c.id not in live]
    else:
        if len(set(liability_ids)) != len(list(liability_ids)):
            raise PayoutRefused("duplicate_liability", "A liability can appear once per batch")
        try:
            chosen = [candidates[i] for i in liability_ids]
        except KeyError as exc:
            raise PayoutRefused("liability_not_eligible", "A selected liability is not payable in this batch") from exc
    if not chosen:
        raise PayoutRefused("nothing_to_pay", "There are no outstanding liabilities to include")
    if len(chosen) > MAX_LINES:
        raise PayoutRefused("too_many_lines", f"A batch holds at most {MAX_LINES} lines; select a subset")
    for liability in chosen:
        if liability.id in live:
            raise PayoutRefused("liability_in_batch", "A liability is already in another live payout batch")
        check_liability(db, kind=kind, run=run, liability=liability, destination_ref=destination)
        if (
            kind == KIND_NET_PAY
            and payee_service.resolve_payee(db, tenant.id, PayeeKind.EMPLOYEE.value, liability.beneficiary_ref) is None
        ):
            # A beneficiary that is not a known employee would be silently skipped by every instruction check.
            raise PayoutRefused("payee_unresolved", "A net-pay beneficiary is not a known employee of this tenant")
        if liability.currency_code != functional:
            raise PayoutRefused("non_functional_currency", "Payouts are in the tenant functional currency only")
    total = sum((c.amount for c in chosen), Decimal(0))
    batch = PayrollPayoutBatch(
        tenant_id=tenant.id,
        request_id=request_id,
        number=f"PO-{request_id.hex[:16].upper()}",
        run_id=run.id,
        kind=kind,
        status=DRAFT,
        destination_ref=destination,
        bank_account_id=bank_account_id,
        currency_code=functional,
        value_date=value_date,
        total_amount=total,
        line_count=len(chosen),
        prepared_by_party_id=party.id,
        prepared_at=now,
    )
    db.add(batch)
    db.flush()
    for liability in chosen:
        db.add(
            PayrollPayoutLine(
                tenant_id=tenant.id,
                batch_id=batch.id,
                liability_id=liability.id,
                beneficiary_ref=liability.beneficiary_ref,
                liability_account_ref=liability.liability_account_ref,
                amount=liability.amount,
                active=True,
            )
        )
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch.id),
        action=AUDIT_PREPARED,
        success=True,
        details={
            "number": batch.number,
            "kind": kind,
            "run_id": str(run.id),
            "line_count": len(chosen),
            "total_amount": str(total),
        },
    )
    return PayoutOutcome(batch.id, False)


def _approve(db: Session, *, tenant: Tenant, party: Party, batch_id: UUID, now: datetime) -> PayoutOutcome:
    batch = _locked_batch(db, tenant.id, batch_id)
    if batch.status == APPROVED and _same(batch.approved_by_party_id, party.id):
        return PayoutOutcome(batch.id, True)
    if batch.status != DRAFT:
        raise PayoutRefused("not_draft", "Only a draft payout batch can be approved")
    if _same(batch.prepared_by_party_id, party.id):
        raise PayoutRefused("sod_approver_is_preparer", "You prepared this batch; another person must approve it")
    lines = _lines(db, tenant.id, batch.id)
    _revalidate(db, batch, lines)
    _refuse_instruction_party(db, batch, lines, party.id, code="sod_approver_is_instruction_party", role="approver")
    batch.status = APPROVED
    batch.approved_by_party_id = party.id
    batch.approved_at = now
    batch.updated_at = now
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch.id),
        action=AUDIT_APPROVED,
        success=True,
        details={"number": batch.number, "prepared_by": str(batch.prepared_by_party_id)},
    )
    return PayoutOutcome(batch.id, False)


def _cancel(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    batch_id: UUID,
    reason: str,
    recall_reference: str | None,
    now: datetime,
) -> PayoutOutcome:
    batch = _locked_batch(db, tenant.id, batch_id)
    if batch.status == CANCELLED and _same(batch.cancelled_by_party_id, party.id):
        return PayoutOutcome(batch.id, True)
    if batch.status not in (DRAFT, APPROVED, FILE_ISSUED):
        raise PayoutRefused("not_cancellable", "Only a draft, approved or file-issued batch can be cancelled")
    if not reason or not reason.strip():
        raise PayoutRefused("reason_required", "Give a reason for the cancellation")
    recall = (recall_reference or "").strip()
    lines = _lines(db, tenant.id, batch.id)
    if batch.status == FILE_ISSUED or _file_receipts(db, tenant.id, lines):
        # The bank may already hold the file: cancelling needs proof it was rejected or recalled.
        if not recall:
            raise PayoutRefused(
                "file_issued",
                "A bank file was issued for this batch; give the bank's rejection or recall reference to cancel it",
            )
        if len(recall) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in recall):
            raise PayoutRefused("recall_reference_invalid", "The recall reference must be 255 printable characters")
    batch.status = CANCELLED
    batch.cancelled_by_party_id = party.id
    batch.cancelled_at = now
    batch.cancel_reason = reason.strip()[:500]
    batch.recall_reference = recall or None
    batch.updated_at = now
    for line in lines:
        line.active = False  # releases the liabilities for a new batch
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch.id),
        action=AUDIT_CANCELLED,
        success=True,
        details={"number": batch.number, "recall_reference": recall or None},
    )
    return PayoutOutcome(batch.id, False)


def _bank_reference(value: str) -> str:
    raw = value or ""
    if any(ord(c) < 32 or ord(c) == 127 for c in raw.strip()):
        raise PayoutRefused("bank_reference_invalid", "The bank reference must not contain control characters")
    # Trim, collapse inner whitespace, upper-case BEFORE the uniqueness check; the normalised form is stored.
    text = " ".join(raw.split()).upper()
    if len(text) < 3 or len(text) > 255:
        raise PayoutRefused("bank_reference_invalid", "Enter the bank's evidence reference (3 to 255 characters)")
    return text


def _generate_file(db: Session, *, tenant: Tenant, party: Party, batch_id: UUID, now: datetime) -> BankFile:
    if not disbursement_policy.bank_file_generation_enabled(db, tenant.id):
        raise PayoutRefused("bank_file_disabled", "Bank file generation is not enabled for this tenant")
    batch = _locked_batch(db, tenant.id, batch_id)
    if batch.kind != KIND_NET_PAY:
        raise PayoutRefused("statutory_no_file", "Statutory remittances are recorded without a bank file")
    lines = _lines(db, tenant.id, batch.id)
    if batch.status == FILE_ISSUED or _file_receipts(db, tenant.id, lines):
        raise PayoutRefused(
            "file_already_issued",
            "A bank file was already issued for this batch; cancel it with the bank's recall reference "
            "and prepare it again",
        )
    if batch.status != APPROVED:
        raise PayoutRefused("not_approved", "Only an approved payout batch can have a bank file")
    if _same(batch.prepared_by_party_id, party.id) or _same(batch.approved_by_party_id, party.id):
        raise PayoutRefused(
            "sod_file_issuer_is_batch_party", "You prepared or approved this batch; someone else must issue its file"
        )
    _revalidate(db, batch, lines)
    _refuse_instruction_party(
        db, batch, lines, party.id, code="sod_file_issuer_is_instruction_party", role="file issuer"
    )
    _refuse_instruction_party(
        db, batch, lines, batch.approved_by_party_id, code="sod_approver_is_instruction_party", role="approver"
    )
    bank = db.scalar(
        select(BankAccount).where(BankAccount.tenant_id == tenant.id, BankAccount.id == batch.bank_account_id)
    )
    if bank is None or bank.status != "active":
        raise PayoutRefused("bank_account_inactive", "The paying bank account is not active")
    rows: list[bank_file.DisbursementRow] = []
    instruction_ids: list[UUID] = []
    for line in lines:
        try:
            destination = payee_service.destination_for_payment(
                db, tenant_id=tenant.id, payee_kind=PayeeKind.EMPLOYEE.value, payee_ref=line.beneficiary_ref
            )
        except payee_service.PayeeBankError as exc:
            raise PayoutRefused(
                "no_approved_payee_instruction", "A payee in this batch has no approved bank instruction"
            ) from exc
        instruction_ids.append(destination.instruction_id)
        rows.append(
            bank_file.DisbursementRow(
                reference=f"{batch.number}-{line.id.hex[:8].upper()}",
                value_date=batch.value_date,
                currency=batch.currency_code,
                amount=line.amount,
                payee_name=destination.account_name,
                bank_code=destination.bank_code,
                account_number=destination.account_number,
                narration=f"Salary {batch.number}",
                debit_account=bank.account_identifier,
            )
        )
    try:
        built = bank_file.build_csv(rows)
    except bank_file.DisbursementFileError as exc:
        raise PayoutRefused(f"file_{exc.code}", str(exc)) from exc
    for line, instruction_id in zip(lines, instruction_ids, strict=True):
        db.add(
            DisbursementFileReceipt(
                tenant_id=tenant.id,
                document_kind=KIND_PAYROLL_PAYOUT,
                document_id=line.id,  # one receipt per line: UNIQUE per line makes it one file per batch
                payee_instruction_id=instruction_id,
                file_format=built.format,
                file_sha256=built.sha256,
                row_count=built.row_count,
                generated_by_party_id=party.id,
                generated_at=now,
            )
        )
    batch.status = FILE_ISSUED
    batch.file_issued_by_party_id = party.id
    batch.file_issued_at = now
    batch.file_sha256 = built.sha256
    batch.file_row_count = built.row_count
    batch.updated_at = now
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch.id),
        action=AUDIT_FILE_GENERATED,
        success=True,
        details={
            "number": batch.number,
            "instruction_ids": [str(i) for i in instruction_ids],
            "row_count": built.row_count,
            "sha256": built.sha256,
            "format": built.format,
        },
    )
    return BankFile(built, f"payroll-payout-{built.sha256[:12]}.csv", len(lines))


def _record(
    db: Session, *, tenant: Tenant, party: Party, batch_id: UUID, bank_reference: str, now: datetime
) -> PayoutOutcome:
    scope = TenantScope(tenant.id)
    actor = str(party.id)
    reference = _bank_reference(bank_reference)
    batch = _locked_batch(db, tenant.id, batch_id)
    lines = _lines(db, tenant.id, batch.id)
    if batch.status == RECORDED:
        if batch.bank_reference != reference:
            raise PayoutRefused("already_recorded", "This batch was already recorded with a different bank reference")
        if any(line.journal_id is None for line in lines):
            raise PayoutRefused(
                "recorded_without_ledger_receipt",
                "Batch is recorded but a line has no journal; refer it to the exceptions review",
            )
        return PayoutOutcome(batch.id, True, len(lines))
    if batch.status not in (APPROVED, FILE_ISSUED):
        raise PayoutRefused("not_approved", "Only an approved payout batch can be recorded")
    if batch.value_date > now.astimezone(UTC).date():
        raise PayoutRefused("value_date_future", "The value date is in the future; record on or after that date")
    if _same(batch.prepared_by_party_id, party.id):
        raise PayoutRefused("sod_recorder_is_preparer", "You prepared this batch; someone else must record it")
    if _same(batch.approved_by_party_id, party.id):
        raise PayoutRefused("sod_recorder_is_approver", "You approved this batch; someone else must record it")
    if _same(batch.file_issued_by_party_id, party.id):
        raise PayoutRefused("sod_recorder_is_file_issuer", "You issued this batch's file; someone else must record it")
    if batch.kind == KIND_NET_PAY:
        if disbursement_policy.bank_file_generation_enabled(db, tenant.id):
            # Files are on for this tenant: the money must go out by the file that was issued for this batch.
            if batch.status != FILE_ISSUED:
                raise PayoutRefused(
                    "bank_file_required",
                    "Bank file generation is enabled: issue the batch's bank file before recording it",
                )
        else:
            # No file step: the recorder is the last independent check on the destinations.
            _refuse_instruction_party(
                db, batch, lines, party.id, code="sod_recorder_is_instruction_party", role="recorder"
            )
    reused = db.scalar(
        select(PayrollPayoutBatch.id).where(
            PayrollPayoutBatch.tenant_id == tenant.id,
            PayrollPayoutBatch.bank_account_id == batch.bank_account_id,
            PayrollPayoutBatch.bank_reference == reference,
            PayrollPayoutBatch.id != batch.id,
        )
    )
    if reused is not None:
        raise PayoutRefused("bank_reference_reused", "This bank reference was already used on this bank account")
    _revalidate(db, batch, lines)
    _refuse_instruction_party(
        db, batch, lines, batch.approved_by_party_id, code="sod_approver_is_instruction_party", role="approver"
    )
    if batch.status == FILE_ISSUED:
        _refuse_instruction_party(
            db,
            batch,
            lines,
            batch.file_issued_by_party_id,
            code="sod_file_issuer_is_instruction_party",
            role="file issuer",
        )
        _check_file_destinations(db, batch, lines)
    functional = _functional_currency(db, tenant.id)
    if batch.currency_code != functional:
        raise PayoutRefused("non_functional_currency", "Payouts are in the tenant functional currency only")
    cash_ref, _payer_ref = _payer(db, tenant.id, batch.bank_account_id, functional)
    run = db.scalar(select(PayrollRun).where(PayrollRun.tenant_id == tenant.id, PayrollRun.id == batch.run_id))
    currency = Currency(batch.currency_code, run.minor_units)
    quantum = Decimal(1).scaleb(-run.minor_units)
    approval_ref = f"erp.b5:payroll_payout_record:{batch.id}:recorder:{actor}"
    settled = Decimal(0)
    for line in lines:
        settlement = payroll.record_liability_settlement(
            db,
            tenant_id=tenant.id,
            liability_id=line.liability_id,
            amount=Money.of(line.amount.quantize(quantum), currency),
            settlement_ref=f"{batch.number}:{line.id.hex[:12]}",
            evidence_ref=reference,
            recorded_by_id=party.id,
            recorded_at=now,
        )
        consequence = build_liability_settlement_consequence(
            db,
            tenant_id=tenant.id,
            settlement_id=settlement.id,
            bank_account_ref=cash_ref,
            posting_date=batch.value_date,
        )
        posted = post_payroll_consequence(
            db,
            scope=scope,
            consequence=consequence,
            functional_currency=functional,
            approval_ref=approval_ref,
            actor=actor,
        )
        line.settlement_id = settlement.id
        line.journal_id = posted.journal_id
        line.journal_number = posted.journal_number
        line.consequence_fingerprint = consequence.fingerprint
        settled += settlement.amount
    if settled != batch.total_amount:
        raise PayoutRefused("batch_total_changed", "The settled total differs from the batch total")
    batch.status = RECORDED
    batch.recorded_by_party_id = party.id
    batch.recorded_at = now
    batch.bank_reference = reference
    batch.updated_at = now
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch.id),
        action=AUDIT_RECORDED,
        success=True,
        details={
            "number": batch.number,
            "kind": batch.kind,
            "bank_reference": reference,
            "line_count": len(lines),
            "total_amount": str(settled),
            "prepared_by": str(batch.prepared_by_party_id),
            "approved_by": str(batch.approved_by_party_id),
            "file_issued_by": str(batch.file_issued_by_party_id) if batch.file_issued_by_party_id else None,
        },
    )
    return PayoutOutcome(batch.id, False, len(lines))


def _guard(db: Session, *, tenant: Tenant, party: Party, entity_id: str, action: str, work):
    """Run ``work`` in one savepoint; on refusal roll it back and audit the refusal."""
    if party.tenant_id != tenant.id:
        raise PayoutRefused("actor_tenant", "The acting party must belong to this tenant")
    try:
        with db.begin_nested():
            return work()
    except PayoutRefused as exc:
        refusal = exc
    except PostingRefused as exc:
        refusal = PayoutRefused(f"posting_{exc.code}", f"The ledger refused the posting ({exc.code})")
    except (
        payroll.PayrollConflict,
        payroll.PayrollNotFound,
        payroll.PayrollRuleViolation,
        IdempotencyConflict,
    ) as exc:
        refusal = PayoutRefused("payroll_refused", str(exc))
    except IntegrityError:
        refusal = PayoutRefused("conflict", "A concurrent change prevented the action; reload and retry")
    if refusal.code != "not_found":
        _audit(
            db,
            tenant=tenant,
            party=party,
            entity_id=entity_id,
            action=AUDIT_REFUSED,
            success=False,
            details={"code": refusal.code, "attempted": action},
        )
    raise refusal


def prepare_batch(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    run_id: UUID,
    kind: str,
    bank_account_id: UUID,
    value_date: date,
    destination_ref: str | None = None,
    liability_ids: Sequence[UUID] | None = None,
    now: datetime | None = None,
) -> PayoutOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(request_id),
        action="prepare",
        work=lambda: _prepare(
            db,
            tenant=tenant,
            party=party,
            request_id=request_id,
            run_id=run_id,
            kind=kind,
            bank_account_id=bank_account_id,
            value_date=value_date,
            destination_ref=destination_ref,
            liability_ids=liability_ids,
            now=moment,
        ),
    )


def approve_batch(
    db: Session, *, tenant: Tenant, party: Party, batch_id: UUID, now: datetime | None = None
) -> PayoutOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch_id),
        action="approve",
        work=lambda: _approve(db, tenant=tenant, party=party, batch_id=batch_id, now=moment),
    )


def cancel_batch(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    batch_id: UUID,
    reason: str,
    recall_reference: str | None = None,
    now: datetime | None = None,
) -> PayoutOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch_id),
        action="cancel",
        work=lambda: _cancel(
            db,
            tenant=tenant,
            party=party,
            batch_id=batch_id,
            reason=reason,
            recall_reference=recall_reference,
            now=moment,
        ),
    )


def generate_bank_file(
    db: Session, *, tenant: Tenant, party: Party, batch_id: UUID, now: datetime | None = None
) -> BankFile:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch_id),
        action="bank_file",
        work=lambda: _generate_file(db, tenant=tenant, party=party, batch_id=batch_id, now=moment),
    )


def record_batch(
    db: Session, *, tenant: Tenant, party: Party, batch_id: UUID, bank_reference: str, now: datetime | None = None
) -> PayoutOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(batch_id),
        action="record",
        work=lambda: _record(
            db, tenant=tenant, party=party, batch_id=batch_id, bank_reference=bank_reference, now=moment
        ),
    )
