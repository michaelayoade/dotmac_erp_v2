"""Supplier credit notes (B1c): draft, submit, and approve-and-post to the AP control account.

Composes the payables a5 owner calls exactly like B1 does for invoices. Approval runs in ONE
SAVEPOINT inside the caller's request transaction:

    payables.approve_credit_note        -> credit_recognized liability event, available amount
    payables.build_credit_accounting_consequence
    erp_v2.posting.post_consequence     -> Dr liability (AP control 2000), Cr line accounts / input tax
    payables.record_accounting_receipt  (document_kind="credit_note")
    payables.apply_credit_note          -> only when an obligation is named

Either all of it happens or none: a refusal rolls the savepoint back. The V1 tax rule (a taxed line
uses the input-tax account 1455, never the line's own account) and the V2 liability pin (2000) are
enforced at the draft and again at approval, before any owner call. There is no compulsory second
person: approval is an explicit action carrying an evidence reference, recorded in the audit event
and in the owner's approval reference. Approving an already approved and posted credit note replays.
Credit notes that carry dimension refs are refused (the bridge does not map dimensions).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from dotmac_kernel.models import Party, Tenant
from dotmac_payables import service as payables
from dotmac_payables.contracts import (
    AccountingReceiptInput,
    ApplyCredit,
    CreateCreditNote,
    CreditNoteStatus,
    PayableLineInput,
    PayablesError,
)
from dotmac_payables.models import (
    AccountingReceipt,
    CreditNote,
    CreditNoteLine,
    PaymentObligation,
    SupplierInvoice,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.ledger_roles import invoice_account_violation
from erp_v2.posting.bridge import post_consequence
from erp_v2.posting.rules import PostingRefused
from erp_v2.web.revenue_services import RevenueActionError, _account, _actor, _date, _decimal

AUDIT_APPROVED_POSTED: Final = "erp.finance.supplier_credit_note.approved_posted"
AUDIT_APPROVAL_REFUSED: Final = "erp.finance.supplier_credit_note.approval_refused"
DOCUMENT_KIND: Final = "credit_note"
_MAXIMUM: Final = Decimal("99999999999999.999999")


class CreditApprovalRefused(ValueError):
    """A safe, user-facing refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CreditApprovalOutcome:
    credit_note_id: UUID
    accounting_reference: str
    replayed: bool
    applied_amount: Decimal = Decimal(0)


# --- draft and submit (the draft permission) ----------------------------------------------------


def create_credit_note_draft(db: Session, *, tenant: Tenant, party: Party, values: Mapping[str, str]) -> CreditNote:
    """Capture one supplier credit line without recognizing anything."""
    _actor(tenant, party)
    try:
        request_id = str(UUID(values.get("request_id", "")))
    except ValueError as exc:
        raise RevenueActionError("The form has expired; reload it before saving") from exc
    quantity = _decimal(values, "quantity")
    price = _decimal(values, "unit_price")
    tax = _decimal(values, "tax_amount")
    rate = _decimal(values, "exchange_rate")
    total = quantity * price + tax
    if not total.is_finite() or abs(total) > _MAXIMUM or total <= 0:
        raise RevenueActionError("The credit note total must be positive and fit six decimals")
    currency = values.get("currency_code", "").strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise RevenueActionError("Currency must be a three-letter code")
    posting = _account(db, tenant.id, values.get("posting_account_id", ""), "line posting account")
    liability = _account(db, tenant.id, values.get("liability_account_id", ""), "liability account")
    tax_account = None
    if tax > 0:
        tax_account = _account(db, tenant.id, values.get("tax_account_id", ""), "tax account")
    violation = invoice_account_violation(db, tenant.id, liability_ref=liability, lines=((posting, tax, tax_account),))
    if violation is not None:
        raise RevenueActionError(violation[1], violation[0])
    original: UUID | None = None
    if values.get("original_invoice_id", "").strip():
        try:
            original = UUID(values["original_invoice_id"].strip())
        except ValueError as exc:
            raise RevenueActionError("The original invoice reference is not valid") from exc
    command = CreateCreditNote(
        number=values.get("number", ""),
        supplier_ref=values.get("supplier_ref", ""),
        supplier_name_snapshot=values.get("supplier_name", ""),
        supplier_document_number=values.get("supplier_document_number", ""),
        credit_date=_date(values, "credit_date"),
        currency_code=currency,
        exchange_rate=rate,
        liability_account_ref=liability,
        lines=(
            PayableLineInput(
                description=values.get("description", ""),
                quantity=quantity,
                unit_price=price,
                posting_account_ref=posting,
                tax_amount=tax,
                tax_account_ref=tax_account,
            ),
        ),
        original_invoice_id=original,
    )
    try:
        with db.begin_nested():
            return payables.create_credit_note(
                db,
                scope=TenantScope(tenant.id),
                command=command,
                idempotency_key=f"erp.credit-note-draft:{request_id}",
                idempotency_expires_at=None,
                recorded_at=datetime.now(UTC),
            )
    except (PayablesError, IdempotencyConflict) as exc:
        raise RevenueActionError(str(exc)) from exc
    except IntegrityError as exc:
        raise RevenueActionError("That credit note number or supplier document already exists") from exc


def submit_credit_note_draft(db: Session, *, tenant: Tenant, party: Party, credit_note_id: UUID) -> CreditNote:
    """Submit for approval; an exact repeat is harmless."""
    _actor(tenant, party)
    try:
        with db.begin_nested():
            credit = db.scalar(
                select(CreditNote)
                .where(CreditNote.tenant_id == tenant.id, CreditNote.id == credit_note_id)
                .with_for_update()
            )
            if credit is None:
                raise RevenueActionError("Supplier credit note was not found")
            if credit.status == CreditNoteStatus.SUBMITTED:
                return credit
            return payables.submit_credit_note(
                db,
                scope=TenantScope(tenant.id),
                credit_note_id=credit_note_id,
                submitted_by=str(party.id),
                submitted_at=datetime.now(UTC),
            )
    except PayablesError as exc:
        raise RevenueActionError(str(exc)) from exc


# --- approve and post (the approve permission) --------------------------------------------------


def _audit(db: Session, *, tenant: Tenant, party: Party, credit_note_id: UUID, action: str, success: bool, details):
    write_audit_event(
        db,
        tenant_id=tenant.id,
        actor_party_id=party.id,
        actor_type="user",
        actor_id=str(party.id),
        actor_label=getattr(party, "email", None) or None,
        source_application="dotmac_erp",
        action=action,
        entity_type="supplier_credit_note",
        entity_id=str(credit_note_id),
        is_success=success,
        details=details,
    )


def _evidence(value: str | None) -> str:
    text = (value or "").strip()
    if not text or len(text) > 120 or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise CreditApprovalRefused(
            "evidence_required", "Enter the evidence reference (at most 120 characters) for this approval"
        )
    return text


def _obligation_to_apply(
    db: Session, tenant_id: UUID, credit: CreditNote, obligation_id: UUID | None, amount: Decimal | None
) -> tuple[PaymentObligation, Decimal] | None:
    if obligation_id is None:
        if amount is not None:
            raise CreditApprovalRefused("apply_amount_without_obligation", "An amount needs an obligation to apply to")
        return None
    obligation = db.scalar(
        select(PaymentObligation).where(PaymentObligation.tenant_id == tenant_id, PaymentObligation.id == obligation_id)
    )
    invoice = (
        None
        if obligation is None
        else db.scalar(
            select(SupplierInvoice).where(
                SupplierInvoice.tenant_id == tenant_id, SupplierInvoice.id == obligation.invoice_id
            )
        )
    )
    if obligation is None or invoice is None:
        raise CreditApprovalRefused("obligation_not_found", "The obligation to apply the credit to was not found")
    if credit.original_invoice_id is not None and invoice.id != credit.original_invoice_id:
        raise CreditApprovalRefused(
            "obligation_not_on_original_invoice", "The obligation belongs to a different invoice than the credit's"
        )
    if invoice.supplier_ref != credit.supplier_ref or invoice.currency_code != credit.currency_code:
        raise CreditApprovalRefused("obligation_mismatch", "The obligation is for another supplier or currency")
    applied = credit.total_amount if amount is None else amount
    if applied <= 0 or applied > credit.total_amount:
        raise CreditApprovalRefused(
            "apply_amount_invalid", "The amount to apply must be positive and within the credit"
        )
    if applied > obligation.outstanding_amount:
        raise CreditApprovalRefused(
            "apply_exceeds_outstanding", "The amount to apply exceeds the obligation's outstanding balance"
        )
    return obligation, applied


def _guarded(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    credit_note_id: UUID,
    evidence_ref: str | None,
    obligation_id: UUID | None,
    apply_amount: Decimal | None,
    now: datetime,
) -> CreditApprovalOutcome:
    scope = TenantScope(tenant.id)
    actor = str(party.id)
    credit = db.scalar(
        select(CreditNote).where(CreditNote.tenant_id == tenant.id, CreditNote.id == credit_note_id).with_for_update()
    )
    if credit is None:
        raise CreditApprovalRefused("not_found", "Supplier credit note was not found")
    receipt = db.scalar(
        select(AccountingReceipt).where(
            AccountingReceipt.tenant_id == tenant.id,
            AccountingReceipt.document_kind == DOCUMENT_KIND,
            AccountingReceipt.document_id == credit.id,
        )
    )
    if credit.status == CreditNoteStatus.APPROVED:
        if receipt is None:
            raise CreditApprovalRefused(
                "approved_without_ledger_receipt",
                "Credit note is approved but has no ledger receipt; refer it to the exceptions review",
            )
        return CreditApprovalOutcome(credit.id, receipt.accounting_reference, True)
    if credit.status != CreditNoteStatus.SUBMITTED:
        raise CreditApprovalRefused("not_submitted", "Only a submitted supplier credit note can be approved")
    if receipt is not None:
        raise CreditApprovalRefused(
            "receipt_exists", "A submitted credit note already has a ledger receipt; refer it for review"
        )
    evidence = _evidence(evidence_ref)
    lines = list(
        db.scalars(
            select(CreditNoteLine).where(
                CreditNoteLine.tenant_id == tenant.id, CreditNoteLine.credit_note_id == credit.id
            )
        )
    )
    with_dimensions = sorted(line.line_number for line in lines if line.dimension_refs)
    if with_dimensions:
        raise CreditApprovalRefused(
            "dimensions_unsupported",
            "Lines " + ", ".join(map(str, with_dimensions)) + " carry cost-centre/project/segment dimensions, "
            "which cannot be posted yet; refer this credit note to the exceptions review",
        )
    violation = invoice_account_violation(
        db,
        tenant.id,
        liability_ref=credit.liability_account_ref,
        lines=((line.posting_account_ref, line.tax_amount, line.tax_account_ref) for line in lines),
    )
    if violation is not None:
        raise CreditApprovalRefused(violation[0], violation[1])
    target = _obligation_to_apply(db, tenant.id, credit, obligation_id, apply_amount)
    approval_ref = f"erp.b1c:credit_note:{credit.id}:approver:{actor}:evidence:{evidence}"
    payables.approve_credit_note(
        db,
        scope=scope,
        credit_note_id=credit.id,
        approval_reference=approval_ref,
        approved_by=actor,
        idempotency_key=f"erp.b1c.approve:{credit.id}",
        idempotency_expires_at=None,
        approved_at=now,
    )
    consequence = payables.build_credit_accounting_consequence(db, tenant_id=tenant.id, credit_note_id=credit.id)
    posted = post_consequence(db, scope=scope, consequence=consequence, approval_ref=approval_ref, actor=actor, now=now)
    payables.record_accounting_receipt(
        db,
        scope=scope,
        command=AccountingReceiptInput(
            document_kind=DOCUMENT_KIND,
            document_id=credit.id,
            consequence_fingerprint=consequence.fingerprint,
            accounting_reference=str(posted.journal_id),
            accounting_evidence_fingerprint=posted.evidence_fingerprint,
        ),
        idempotency_key=f"erp.b1c.receipt:{credit.id}",
        idempotency_expires_at=None,
        recorded_at=now,
    )
    applied = Decimal(0)
    if target is not None:
        obligation, applied = target
        payables.apply_credit_note(
            db,
            scope=scope,
            command=ApplyCredit(
                credit_note_id=credit.id, obligation_id=obligation.id, amount=applied, applied_by=actor
            ),
            idempotency_key=f"erp.b1c.apply:{credit.id}:{obligation.id}",
            idempotency_expires_at=None,
            applied_at=now,
        )
    _audit(
        db,
        tenant=tenant,
        party=party,
        credit_note_id=credit.id,
        action=AUDIT_APPROVED_POSTED,
        success=True,
        details={
            "journal_id": str(posted.journal_id),
            "journal_number": posted.journal_number,
            "consequence_fingerprint": consequence.fingerprint,
            "submitted_by": credit.submitted_by,
            "evidence_ref": evidence,
            "applied_obligation_id": str(target[0].id) if target else None,
            "applied_amount": str(applied),
        },
    )
    return CreditApprovalOutcome(credit.id, str(posted.journal_id), posted.replayed, applied)


def approve_and_post(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    credit_note_id: UUID,
    evidence_ref: str | None,
    obligation_id: UUID | None = None,
    apply_amount: Decimal | str | None = None,
    now: datetime | None = None,
) -> CreditApprovalOutcome:
    """Approve + post (+ optionally apply) in one savepoint, or refuse leaving no credit/ledger change."""
    if party.tenant_id != tenant.id:
        raise CreditApprovalRefused("actor_tenant", "The acting party must belong to this tenant")
    moment = now or datetime.now(UTC)
    try:
        amount = None if apply_amount in (None, "") else Decimal(str(apply_amount))
        if amount is not None and not amount.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        raise CreditApprovalRefused("apply_amount_invalid", "The amount to apply is not a valid number") from None
    try:
        with db.begin_nested():
            return _guarded(
                db,
                tenant=tenant,
                party=party,
                credit_note_id=credit_note_id,
                evidence_ref=evidence_ref,
                obligation_id=obligation_id,
                apply_amount=amount,
                now=moment,
            )
    except CreditApprovalRefused as exc:
        refusal = exc
    except PostingRefused as exc:
        refusal = CreditApprovalRefused(f"posting_{exc.code}", f"The ledger refused the posting ({exc.code})")
    except (PayablesError, IdempotencyConflict) as exc:
        refusal = CreditApprovalRefused("payables_refused", str(exc))
    except IntegrityError:
        refusal = CreditApprovalRefused("conflict", "A concurrent change prevented the approval; reload and retry")
    if refusal.code != "not_found":
        # The savepoint rolled back; record the refusal in the caller's transaction.
        _audit(
            db,
            tenant=tenant,
            party=party,
            credit_note_id=credit_note_id,
            action=AUDIT_APPROVAL_REFUSED,
            success=False,
            details={"code": refusal.code},
        )
    raise refusal
