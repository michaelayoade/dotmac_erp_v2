"""Supplier payment workflow (B3): propose, approve, record as paid, reverse, optional bank file.

Starter payables a5 owns the lifecycle (DRAFT -> APPROVED -> RECORDED | CANCELLED, RECORDED ->
REVERSED), the obligation settlement and the SoD checks. This module is the ERP composition: it
derives the payer references from the selected bank account (never free text), gates who may act
(routes) and posts the GL consequence through the P0 bridge in the SAME savepoint as the owner
transition:

    record   payables.record_supplier_payment (settles obligations)
             -> build_payment_accounting_consequence (Dr AP control per invoice liability account,
                Cr the paying bank account's cash GL)
             -> erp_v2.posting.post_consequence -> payables.record_accounting_receipt
    reverse  payables.reverse_supplier_payment (re-opens obligations)
             -> build_payment_reversal_accounting_consequence -> post -> receipt

Either everything happens or nothing does: a refusal (closed period, inactive account, SoD, bank
reference reused) rolls the savepoint back, so there is never a settled obligation without ledger
entry or the reverse. Recording is an assertion by a named person, backed by a bank evidence
reference, that money left through the bank portal; the ERP never calls a bank or provider.

Not supported, refused rather than guessed (decisions open, see docs/supplier-payments.md):
- withholding tax (a5 exposes it, but the WHT payable account source and certificate policy are
  undecided): ``wht_unsupported``;
- payments in a currency other than the tenant functional currency, or at a rate other than 1
  (realised FX is not modelled): ``fx_unsupported``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_accounting.contracts import AccountKind
from dotmac_accounting.models import Account
from dotmac_banking.models import BankAccount
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from dotmac_kernel.models import Party, Tenant
from dotmac_payables import service as payables
from dotmac_payables.contracts import (
    AccountingReceiptInput,
    CreateSupplierPayment,
    InvoiceStatus,
    ObligationStatus,
    PayablesError,
    SupplierPaymentAllocationInput,
    SupplierPaymentStatus,
)
from dotmac_payables.models import (
    AccountingReceipt,
    PaymentObligation,
    SupplierInvoice,
    SupplierPayment,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.disbursement import file as bank_file
from erp_v2.disbursement import policy as disbursement_policy
from erp_v2.disbursement.models import KIND_SUPPLIER_PAYMENT, DisbursementFileReceipt
from erp_v2.journals import policy as journal_policy
from erp_v2.payees import service as payee_service
from erp_v2.payees.models import PayeeKind
from erp_v2.posting import rules
from erp_v2.posting.bridge import post_consequence
from erp_v2.posting.rules import PostingRefused

AUDIT_PREPARED: Final = "erp.finance.supplier_payment.prepared"
AUDIT_APPROVED: Final = "erp.finance.supplier_payment.approved"
AUDIT_CANCELLED: Final = "erp.finance.supplier_payment.cancelled"
AUDIT_RECORDED: Final = "erp.finance.supplier_payment.recorded_posted"
AUDIT_REVERSED: Final = "erp.finance.supplier_payment.reversed_posted"
AUDIT_REFUSED: Final = "erp.finance.supplier_payment.refused"
AUDIT_FILE_GENERATED: Final = "erp.treasury.disbursement_file.generated"
AUDIT_ACTIONS: Final = (
    AUDIT_PREPARED,
    AUDIT_APPROVED,
    AUDIT_CANCELLED,
    AUDIT_RECORDED,
    AUDIT_REVERSED,
    AUDIT_REFUSED,
    AUDIT_FILE_GENERATED,
)

DOCUMENT_KIND: Final = "supplier_payment"
REVERSAL_DOCUMENT_KIND: Final = "supplier_payment_reversal"
MAX_ALLOCATIONS: Final = 50
_QUANTUM: Final = Decimal("0.000001")


class PaymentRefused(ValueError):
    """A safe, user-facing refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    payment_id: UUID
    replayed: bool
    accounting_reference: str | None = None


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    obligation_id: UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class BankFile:
    file: bank_file.DisbursementFile
    filename: str
    payment_count: int


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
        entity_type="supplier_payment",
        entity_id=entity_id,
        is_success=success,
        details=details,
    )


def _same(left: str | None, right: str | None) -> bool:
    return left is not None and right is not None and left.strip().casefold() == right.strip().casefold()


def _locked_payment(db: Session, tenant_id: UUID, payment_id: UUID) -> SupplierPayment:
    payment = db.scalar(
        select(SupplierPayment)
        .where(SupplierPayment.tenant_id == tenant_id, SupplierPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None:
        raise PaymentRefused("not_found", "Supplier payment was not found")
    return payment


def _receipt(db: Session, tenant_id: UUID, kind: str, payment_id: UUID) -> AccountingReceipt | None:
    return db.scalar(
        select(AccountingReceipt).where(
            AccountingReceipt.tenant_id == tenant_id,
            AccountingReceipt.document_kind == kind,
            AccountingReceipt.document_id == payment_id,
        )
    )


def _refuse_wht(payment: SupplierPayment) -> None:
    if payment.wht_amount != 0:
        raise PaymentRefused(
            "wht_unsupported",
            "This payment carries withholding tax, which is not supported here yet; refer it to the exceptions review",
        )


def _functional_currency(db: Session, tenant_id: UUID) -> str:
    try:
        return journal_policy.functional_currency(db, tenant_id)
    except journal_policy.FunctionalCurrencyError as exc:
        raise PaymentRefused("functional_currency_unset", "The tenant has no functional currency setting") from exc


def payer_references(db: Session, *, tenant_id: UUID, bank_account_id: UUID, currency: str) -> tuple[str, str]:
    """(cash GL account ref, payer instrument ref) from the selected bank account, or refuse.

    The cash GL account is whatever the bank account record declares; the payer instrument is the
    bank account's own id. Neither is ever taken from a form field.
    """
    account = db.scalar(
        select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.id == bank_account_id)
    )
    if account is None:
        raise PaymentRefused("bank_account_unknown", "Select one of the tenant's bank accounts")
    if account.status != "active":
        raise PaymentRefused("bank_account_inactive", "The selected bank account is not active")
    if account.currency_code != currency:
        raise PaymentRefused("bank_account_currency", "The bank account currency differs from the payment currency")
    try:
        _, value = rules.classify_ref(rules.RefPolicy(), account.cash_account_ref)
    except PostingRefused as exc:
        raise PaymentRefused(
            "cash_account_invalid", "The bank account's cash ledger account is not a canonical account id"
        ) from exc
    gl = db.scalar(select(Account).where(Account.tenant_id == tenant_id, Account.id == UUID(value)))
    if gl is None or not gl.is_active or not gl.posting_allowed or gl.kind != AccountKind.POSTING:
        raise PaymentRefused(
            "cash_account_not_postable", "The bank account's cash ledger account is not open for posting"
        )
    return value, str(account.id)


def _parse_amount(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0 or value != value.quantize(_QUANTUM):
        raise PaymentRefused("invalid_amount", "Allocation amounts must be positive with at most six decimals")
    return value


def _prepare(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    bank_account_id: UUID,
    value_date: date,
    allocations: Sequence[AllocationRequest],
    now: datetime,
) -> PaymentOutcome:
    if not allocations:
        raise PaymentRefused("no_allocations", "Select at least one obligation to pay")
    if len(allocations) > MAX_ALLOCATIONS:
        raise PaymentRefused("too_many_allocations", f"A payment covers at most {MAX_ALLOCATIONS} obligations")
    ids = [a.obligation_id for a in allocations]
    if len(set(ids)) != len(ids):
        raise PaymentRefused("duplicate_obligation", "An obligation can appear once per payment")
    functional = _functional_currency(db, tenant.id)
    cash_ref, payer_ref = payer_references(
        db, tenant_id=tenant.id, bank_account_id=bank_account_id, currency=functional
    )
    rows = list(
        db.execute(
            select(PaymentObligation, SupplierInvoice)
            .join(
                SupplierInvoice,
                (SupplierInvoice.tenant_id == PaymentObligation.tenant_id)
                & (SupplierInvoice.id == PaymentObligation.invoice_id),
            )
            .where(PaymentObligation.tenant_id == tenant.id, PaymentObligation.id.in_(ids))
        )
    )
    if len(rows) != len(ids):
        raise PaymentRefused("obligation_unknown", "An obligation was not found")
    suppliers = {(invoice.supplier_ref, invoice.supplier_name_snapshot) for _, invoice in rows}
    if len({ref for ref, _ in suppliers}) != 1:
        raise PaymentRefused("mixed_suppliers", "One payment pays one supplier")
    for obligation, invoice in rows:
        if invoice.currency_code != functional or invoice.exchange_rate != Decimal(1):
            raise PaymentRefused(
                "fx_unsupported",
                "Only functional-currency invoices at rate 1 can be paid (realised FX is not modelled)",
            )
        if invoice.status != InvoiceStatus.APPROVED or obligation.status not in (
            ObligationStatus.OPEN,
            ObligationStatus.PARTIALLY_SETTLED,
        ):
            raise PaymentRefused("not_payable", "Only open obligations of approved invoices can be paid")
    supplier_ref, supplier_name = sorted(suppliers)[0]
    number = f"SP-{request_id.hex[:16].upper()}"
    existed = (
        db.scalar(
            select(SupplierPayment.id).where(SupplierPayment.tenant_id == tenant.id, SupplierPayment.number == number)
        )
        is not None
    )
    command = CreateSupplierPayment(
        number=number,
        supplier_ref=supplier_ref,
        supplier_name_snapshot=supplier_name,
        currency_code=functional,
        exchange_rate=Decimal(1),
        value_date=value_date,
        cash_account_ref=cash_ref,
        payer_instrument_ref=payer_ref,
        allocations=tuple(
            SupplierPaymentAllocationInput(a.obligation_id, _parse_amount(a.amount)) for a in allocations
        ),
        prepared_by=str(party.id),
    )
    payment = payables.create_supplier_payment(
        db,
        scope=TenantScope(tenant.id),
        command=command,
        idempotency_key=f"erp.b3.create:{request_id}",
        idempotency_expires_at=None,
        prepared_at=now,
    )
    replayed = existed
    if not replayed:
        _audit(
            db,
            tenant=tenant,
            party=party,
            entity_id=str(payment.id),
            action=AUDIT_PREPARED,
            success=True,
            details={"number": payment.number, "gross_amount": str(payment.gross_amount), "supplier_ref": supplier_ref},
        )
    return PaymentOutcome(payment.id, replayed)


def _approve(db: Session, *, tenant: Tenant, party: Party, payment_id: UUID, now: datetime) -> PaymentOutcome:
    actor = str(party.id)
    payment = _locked_payment(db, tenant.id, payment_id)
    if payment.status == SupplierPaymentStatus.APPROVED and _same(payment.approved_by, actor):
        return PaymentOutcome(payment.id, True)
    if payment.status != SupplierPaymentStatus.DRAFT:
        raise PaymentRefused("not_draft", "Only a draft supplier payment can be approved")
    _refuse_wht(payment)
    if _same(payment.prepared_by, actor):
        raise PaymentRefused("sod_approver_is_preparer", "You prepared this payment; another person must approve it")
    payables.approve_supplier_payment(
        db,
        scope=TenantScope(tenant.id),
        payment_id=payment.id,
        approval_reference=f"erp.b3:supplier_payment:{payment.id}:approver:{actor}",
        approved_by=actor,
        idempotency_key=f"erp.b3.approve:{payment.id}",
        idempotency_expires_at=None,
        approved_at=now,
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(payment.id),
        action=AUDIT_APPROVED,
        success=True,
        details={"number": payment.number, "prepared_by": payment.prepared_by},
    )
    return PaymentOutcome(payment.id, False)


def _cancel(
    db: Session, *, tenant: Tenant, party: Party, payment_id: UUID, reason: str, now: datetime
) -> PaymentOutcome:
    actor = str(party.id)
    payment = _locked_payment(db, tenant.id, payment_id)
    if payment.status == SupplierPaymentStatus.CANCELLED and _same(payment.cancelled_by, actor):
        return PaymentOutcome(payment.id, True)
    if payment.status not in (SupplierPaymentStatus.DRAFT, SupplierPaymentStatus.APPROVED):
        raise PaymentRefused("not_cancellable", "Only a draft or approved supplier payment can be cancelled")
    if not reason or not reason.strip():
        raise PaymentRefused("reason_required", "Give a reason for the cancellation")
    payables.cancel_supplier_payment(
        db,
        scope=TenantScope(tenant.id),
        payment_id=payment.id,
        reason=reason,
        cancelled_by=actor,
        idempotency_key=f"erp.b3.cancel:{payment.id}",
        idempotency_expires_at=None,
        cancelled_at=now,
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(payment.id),
        action=AUDIT_CANCELLED,
        success=True,
        details={"number": payment.number},
    )
    return PaymentOutcome(payment.id, False)


def _bank_reference(value: str) -> str:
    text = (value or "").strip()
    if len(text) < 3 or len(text) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise PaymentRefused(
            "bank_reference_invalid", "Enter the bank's evidence reference (3 to 255 printable characters)"
        )
    return text


def _check_file_destination(db: Session, tenant_id: UUID, payment: SupplierPayment) -> None:
    """If a bank file was generated, the payee destination must be unchanged since the file."""
    latest = db.scalar(
        select(DisbursementFileReceipt)
        .where(
            DisbursementFileReceipt.tenant_id == tenant_id,
            DisbursementFileReceipt.document_kind == KIND_SUPPLIER_PAYMENT,
            DisbursementFileReceipt.document_id == payment.id,
        )
        .order_by(DisbursementFileReceipt.generated_at.desc(), DisbursementFileReceipt.created_at.desc())
        .limit(1)
    )
    if latest is None:
        return
    try:
        destination = payee_service.destination_for_payment(
            db, tenant_id=tenant_id, payee_kind=PayeeKind.SUPPLIER.value, payee_ref=payment.supplier_ref
        )
    except payee_service.PayeeBankError as exc:
        raise PaymentRefused(
            "payee_instruction_unavailable", "The payee has no approved bank instruction any more; regenerate the file"
        ) from exc
    if destination.instruction_id != latest.payee_instruction_id:
        raise PaymentRefused(
            "payee_instruction_changed",
            "The payee's bank instruction changed after the file was generated; cancel and prepare it again",
        )


def _record(
    db: Session, *, tenant: Tenant, party: Party, payment_id: UUID, bank_reference: str, now: datetime
) -> PaymentOutcome:
    scope = TenantScope(tenant.id)
    actor = str(party.id)
    reference = _bank_reference(bank_reference)
    payment = _locked_payment(db, tenant.id, payment_id)
    if payment.status in (SupplierPaymentStatus.RECORDED, SupplierPaymentStatus.REVERSED):
        receipt = _receipt(db, tenant.id, DOCUMENT_KIND, payment.id)
        if receipt is None:
            raise PaymentRefused(
                "recorded_without_ledger_receipt",
                "Payment is recorded but has no ledger receipt; refer it to the exceptions review",
            )
        if payment.bank_reference != reference:
            raise PaymentRefused(
                "already_recorded", "This payment was already recorded with a different bank reference"
            )
        return PaymentOutcome(payment.id, True, receipt.accounting_reference)
    if payment.status != SupplierPaymentStatus.APPROVED:
        raise PaymentRefused("not_approved", "Only an approved supplier payment can be recorded")
    _refuse_wht(payment)
    if _same(payment.prepared_by, actor):
        raise PaymentRefused("sod_recorder_is_preparer", "You prepared this payment; someone else must record it")
    if _same(payment.approved_by, actor):
        raise PaymentRefused("sod_recorder_is_approver", "You approved this payment; someone else must record it")
    _check_file_destination(db, tenant.id, payment)
    payables.record_supplier_payment(
        db,
        scope=scope,
        payment_id=payment.id,
        bank_reference=reference,
        recorded_by=actor,
        idempotency_key=f"erp.b3.record:{payment.id}",
        idempotency_expires_at=None,
        recorded_at=now,
    )
    consequence = payables.build_payment_accounting_consequence(db, tenant_id=tenant.id, payment_id=payment.id)
    approval_ref = f"erp.b3:supplier_payment_record:{payment.id}:recorder:{actor}"
    posted = post_consequence(db, scope=scope, consequence=consequence, approval_ref=approval_ref, actor=actor, now=now)
    payables.record_accounting_receipt(
        db,
        scope=scope,
        command=AccountingReceiptInput(
            document_kind=DOCUMENT_KIND,
            document_id=payment.id,
            consequence_fingerprint=consequence.fingerprint,
            accounting_reference=str(posted.journal_id),
            accounting_evidence_fingerprint=posted.evidence_fingerprint,
        ),
        idempotency_key=f"erp.b3.receipt:{payment.id}",
        idempotency_expires_at=None,
        recorded_at=now,
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(payment.id),
        action=AUDIT_RECORDED,
        success=True,
        details={
            "number": payment.number,
            "bank_reference": reference,
            "journal_id": str(posted.journal_id),
            "journal_number": posted.journal_number,
            "consequence_fingerprint": consequence.fingerprint,
            "payer_instrument_ref": payment.payer_instrument_ref,
            "prepared_by": payment.prepared_by,
            "approved_by": payment.approved_by,
        },
    )
    return PaymentOutcome(payment.id, posted.replayed, str(posted.journal_id))


def _reverse(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    payment_id: UUID,
    reason: str,
    reversal_date: date,
    now: datetime,
) -> PaymentOutcome:
    scope = TenantScope(tenant.id)
    actor = str(party.id)
    payment = _locked_payment(db, tenant.id, payment_id)
    if payment.status == SupplierPaymentStatus.REVERSED:
        receipt = _receipt(db, tenant.id, REVERSAL_DOCUMENT_KIND, payment.id)
        if receipt is None:
            raise PaymentRefused(
                "reversed_without_ledger_receipt",
                "Payment is reversed but has no reversal receipt; refer it to the exceptions review",
            )
        if payment.reversal_date != reversal_date or (payment.reversal_reason or "").strip() != (reason or "").strip():
            raise PaymentRefused("already_reversed", "This payment was already reversed with different details")
        return PaymentOutcome(payment.id, True, receipt.accounting_reference)
    if payment.status != SupplierPaymentStatus.RECORDED:
        raise PaymentRefused("not_recorded", "Only a recorded supplier payment can be reversed")
    _refuse_wht(payment)
    if not reason or not reason.strip():
        raise PaymentRefused("reason_required", "Give a reason for the reversal")
    if _same(payment.recorded_by, actor):
        raise PaymentRefused("sod_reverser_is_recorder", "You recorded this payment; someone else must reverse it")
    if _same(payment.approved_by, actor):
        raise PaymentRefused("sod_reverser_is_approver", "You approved this payment; someone else must reverse it")
    if _same(payment.prepared_by, actor):
        raise PaymentRefused("sod_reverser_is_preparer", "You prepared this payment; someone else must reverse it")
    payables.reverse_supplier_payment(
        db,
        scope=scope,
        payment_id=payment.id,
        reason=reason,
        reversal_date=reversal_date,
        reversed_by=actor,
        idempotency_key=f"erp.b3.reverse:{payment.id}",
        idempotency_expires_at=None,
        reversed_at=now,
    )
    consequence = payables.build_payment_reversal_accounting_consequence(db, tenant_id=tenant.id, payment_id=payment.id)
    approval_ref = f"erp.b3:supplier_payment_reversal:{payment.id}:reverser:{actor}"
    posted = post_consequence(db, scope=scope, consequence=consequence, approval_ref=approval_ref, actor=actor, now=now)
    payables.record_accounting_receipt(
        db,
        scope=scope,
        command=AccountingReceiptInput(
            document_kind=REVERSAL_DOCUMENT_KIND,
            document_id=payment.id,
            consequence_fingerprint=consequence.fingerprint,
            accounting_reference=str(posted.journal_id),
            accounting_evidence_fingerprint=posted.evidence_fingerprint,
        ),
        idempotency_key=f"erp.b3.reversal-receipt:{payment.id}",
        idempotency_expires_at=None,
        recorded_at=now,
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(payment.id),
        action=AUDIT_REVERSED,
        success=True,
        details={
            "number": payment.number,
            "reversal_date": reversal_date.isoformat(),
            "journal_id": str(posted.journal_id),
            "consequence_fingerprint": consequence.fingerprint,
            "recorded_by": payment.recorded_by,
        },
    )
    return PaymentOutcome(payment.id, posted.replayed, str(posted.journal_id))


def _generate_file(
    db: Session, *, tenant: Tenant, party: Party, payment_ids: Sequence[UUID], now: datetime
) -> BankFile:
    if not disbursement_policy.bank_file_generation_enabled(db, tenant.id):
        raise PaymentRefused("bank_file_disabled", "Bank file generation is not enabled for this tenant")
    unique = list(dict.fromkeys(payment_ids))
    if not unique:
        raise PaymentRefused("no_payments", "Select at least one approved payment")
    payments = [_locked_payment(db, tenant.id, pid) for pid in sorted(unique, key=str)]
    if len({p.payer_instrument_ref for p in payments}) != 1:
        raise PaymentRefused("mixed_bank_accounts", "A file covers payments from one bank account only")
    bank = db.scalar(
        select(BankAccount).where(
            BankAccount.tenant_id == tenant.id, BankAccount.id == UUID(payments[0].payer_instrument_ref)
        )
    )
    if bank is None or bank.status != "active":
        raise PaymentRefused("bank_account_inactive", "The paying bank account is not active")
    rows: list[bank_file.DisbursementRow] = []
    instruction_ids: list[UUID] = []
    ordered = sorted(payments, key=lambda p: p.number)
    for payment in ordered:
        if payment.status != SupplierPaymentStatus.APPROVED:
            raise PaymentRefused("not_approved", f"Payment {payment.number} is not approved")
        _refuse_wht(payment)
        try:
            destination = payee_service.destination_for_payment(
                db, tenant_id=tenant.id, payee_kind=PayeeKind.SUPPLIER.value, payee_ref=payment.supplier_ref
            )
        except payee_service.PayeeBankError as exc:
            raise PaymentRefused(
                "no_approved_payee_instruction", f"Payment {payment.number}: the payee has no approved bank instruction"
            ) from exc
        instruction_ids.append(destination.instruction_id)
        rows.append(
            bank_file.DisbursementRow(
                reference=payment.number,
                value_date=payment.value_date,
                currency=payment.currency_code,
                amount=payment.net_amount,
                payee_name=destination.account_name,
                bank_code=destination.bank_code,
                account_number=destination.account_number,
                narration=f"Supplier payment {payment.number}",
                debit_account=bank.account_identifier,
            )
        )
    try:
        built = bank_file.build_csv(rows)
    except bank_file.DisbursementFileError as exc:
        raise PaymentRefused(f"file_{exc.code}", str(exc)) from exc
    for payment, instruction_id in zip(ordered, instruction_ids, strict=True):
        db.add(
            DisbursementFileReceipt(
                tenant_id=tenant.id,
                document_kind=KIND_SUPPLIER_PAYMENT,
                document_id=payment.id,
                payee_instruction_id=instruction_id,
                file_format=built.format,
                file_sha256=built.sha256,
                row_count=built.row_count,
                generated_by_party_id=party.id,
                generated_at=now,
            )
        )
    db.flush()
    _audit(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(ordered[0].id),
        action=AUDIT_FILE_GENERATED,
        success=True,
        details={
            "payment_ids": [str(p.id) for p in ordered],
            "instruction_ids": [str(i) for i in instruction_ids],
            "row_count": built.row_count,
            "sha256": built.sha256,
            "format": built.format,
        },
    )
    return BankFile(built, f"supplier-payments-{built.sha256[:12]}.csv", len(ordered))


def _guard(db: Session, *, tenant: Tenant, party: Party, entity_id: str, action: str, work):
    """Run ``work`` in one savepoint; on refusal roll it back and audit the refusal."""
    if party.tenant_id != tenant.id:
        raise PaymentRefused("actor_tenant", "The acting party must belong to this tenant")
    try:
        with db.begin_nested():
            return work()
    except PaymentRefused as exc:
        refusal = exc
    except PostingRefused as exc:
        refusal = PaymentRefused(f"posting_{exc.code}", f"The ledger refused the posting ({exc.code})")
    except (PayablesError, IdempotencyConflict) as exc:
        refusal = PaymentRefused("payables_refused", str(exc))
    except IntegrityError:
        refusal = PaymentRefused("conflict", "A concurrent change prevented the action; reload and retry")
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


def prepare_payment(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    request_id: UUID,
    bank_account_id: UUID,
    value_date: date,
    allocations: Sequence[AllocationRequest],
    now: datetime | None = None,
) -> PaymentOutcome:
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
            bank_account_id=bank_account_id,
            value_date=value_date,
            allocations=allocations,
            now=moment,
        ),
    )


def approve_payment(
    db: Session, *, tenant: Tenant, party: Party, payment_id: UUID, now: datetime | None = None
) -> PaymentOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(payment_id),
        action="approve",
        work=lambda: _approve(db, tenant=tenant, party=party, payment_id=payment_id, now=moment),
    )


def cancel_payment(
    db: Session, *, tenant: Tenant, party: Party, payment_id: UUID, reason: str, now: datetime | None = None
) -> PaymentOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(payment_id),
        action="cancel",
        work=lambda: _cancel(db, tenant=tenant, party=party, payment_id=payment_id, reason=reason, now=moment),
    )


def record_payment(
    db: Session, *, tenant: Tenant, party: Party, payment_id: UUID, bank_reference: str, now: datetime | None = None
) -> PaymentOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(payment_id),
        action="record",
        work=lambda: _record(
            db, tenant=tenant, party=party, payment_id=payment_id, bank_reference=bank_reference, now=moment
        ),
    )


def reverse_payment(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    payment_id: UUID,
    reason: str,
    reversal_date: date,
    now: datetime | None = None,
) -> PaymentOutcome:
    moment = now or datetime.now(UTC)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(payment_id),
        action="reverse",
        work=lambda: _reverse(
            db,
            tenant=tenant,
            party=party,
            payment_id=payment_id,
            reason=reason,
            reversal_date=reversal_date,
            now=moment,
        ),
    )


def generate_bank_file(
    db: Session, *, tenant: Tenant, party: Party, payment_ids: Sequence[UUID], now: datetime | None = None
) -> BankFile:
    moment = now or datetime.now(UTC)
    ids = list(payment_ids)
    return _guard(
        db,
        tenant=tenant,
        party=party,
        entity_id=str(ids[0]) if ids else str(tenant.id),
        action="bank_file",
        work=lambda: _generate_file(db, tenant=tenant, party=party, payment_ids=ids, now=moment),
    )
