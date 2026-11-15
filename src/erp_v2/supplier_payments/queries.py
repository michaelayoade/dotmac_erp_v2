"""Tenant-scoped read models for the supplier payment screens. Read only, bounded, no secrets.

Payee account numbers are never read here (the payee view is masked); bank files are never read.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from dotmac_banking.models import BankAccount
from dotmac_kernel.models import Party
from dotmac_payables.contracts import ObligationStatus, SupplierPaymentStatus
from dotmac_payables.models import (
    AccountingReceipt,
    HistoricalInvoiceReceipt,
    PaymentObligation,
    SupplierInvoice,
    SupplierPayment,
    SupplierPaymentAllocation,
)
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from erp_v2.disbursement.models import KIND_SUPPLIER_PAYMENT, DisbursementFileReceipt
from erp_v2.payees import service as payee_service
from erp_v2.payees.models import InstructionStatus, PayeeBankInstruction, PayeeKind

LIMIT = 200
ACTIVE = (SupplierPaymentStatus.DRAFT, SupplierPaymentStatus.APPROVED)


def bank_accounts(db: Session, *, tenant_id: UUID, currency: str | None) -> list[BankAccount]:
    stmt = select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.status == "active")
    if currency:
        stmt = stmt.where(BankAccount.currency_code == currency)
    return list(db.scalars(stmt.order_by(BankAccount.account_code).limit(100)))


def open_obligations(db: Session, *, tenant_id: UUID, currency: str) -> list[dict[str, object]]:
    """Payable obligations: approved invoice, in the ledger, open, with the unreserved balance."""
    reserved = (
        select(
            SupplierPaymentAllocation.obligation_id.label("obligation_id"),
            func.sum(SupplierPaymentAllocation.amount).label("reserved"),
        )
        .join(
            SupplierPayment,
            (SupplierPayment.tenant_id == SupplierPaymentAllocation.tenant_id)
            & (SupplierPayment.id == SupplierPaymentAllocation.payment_id),
        )
        .where(SupplierPaymentAllocation.tenant_id == tenant_id, SupplierPayment.status.in_(ACTIVE))
        .group_by(SupplierPaymentAllocation.obligation_id)
        .subquery()
    )
    in_ledger = or_(
        select(AccountingReceipt.id)
        .where(
            AccountingReceipt.tenant_id == tenant_id,
            AccountingReceipt.document_kind == "supplier_invoice",
            AccountingReceipt.document_id == SupplierInvoice.id,
        )
        .exists(),
        select(HistoricalInvoiceReceipt.id)
        .where(
            HistoricalInvoiceReceipt.tenant_id == tenant_id, HistoricalInvoiceReceipt.invoice_id == SupplierInvoice.id
        )
        .exists(),
    )
    rows = db.execute(
        select(PaymentObligation, SupplierInvoice, func.coalesce(reserved.c.reserved, 0))
        .join(
            SupplierInvoice,
            (SupplierInvoice.tenant_id == PaymentObligation.tenant_id)
            & (SupplierInvoice.id == PaymentObligation.invoice_id),
        )
        .outerjoin(reserved, reserved.c.obligation_id == PaymentObligation.id)
        .where(
            PaymentObligation.tenant_id == tenant_id,
            PaymentObligation.status.in_((ObligationStatus.OPEN, ObligationStatus.PARTIALLY_SETTLED)),
            PaymentObligation.outstanding_amount > 0,
            PaymentObligation.currency_code == currency,
            SupplierInvoice.exchange_rate == 1,
            in_ledger,
        )
        .order_by(SupplierInvoice.supplier_name_snapshot, PaymentObligation.due_date, SupplierInvoice.number)
        .limit(500)
    )
    result = []
    for obligation, invoice, held in rows:
        available = obligation.outstanding_amount - Decimal(held)
        if available > 0:
            result.append(
                {
                    "obligation": obligation,
                    "invoice": invoice,
                    "reserved": Decimal(held),
                    "available": available,
                }
            )
    return result


def register(
    db: Session, *, tenant_id: UUID, statuses: tuple[SupplierPaymentStatus, ...] | None
) -> list[SupplierPayment]:
    stmt = select(SupplierPayment).where(SupplierPayment.tenant_id == tenant_id)
    if statuses:
        stmt = stmt.where(SupplierPayment.status.in_(statuses))
    return list(db.scalars(stmt.order_by(SupplierPayment.created_at.desc()).limit(LIMIT)))


def _names(db: Session, tenant_id: UUID, raw_ids: list[str | None]) -> dict[str, str]:
    ids: dict[str, UUID] = {}
    for raw in raw_ids:
        if raw:
            try:
                ids[raw] = UUID(raw)
            except ValueError:
                continue
    if not ids:
        return {}
    found = {
        row.id: row.display_name
        for row in db.execute(
            select(Party.id, Party.display_name).where(Party.tenant_id == tenant_id, Party.id.in_(list(ids.values())))
        )
    }
    return {raw: found[uid] for raw, uid in ids.items() if uid in found}


def detail(db: Session, *, tenant_id: UUID, payment_id: UUID) -> dict[str, object] | None:
    payment = db.scalar(
        select(SupplierPayment).where(SupplierPayment.tenant_id == tenant_id, SupplierPayment.id == payment_id)
    )
    if payment is None:
        return None
    lines = list(
        db.execute(
            select(SupplierPaymentAllocation, PaymentObligation, SupplierInvoice)
            .join(
                PaymentObligation,
                (PaymentObligation.tenant_id == SupplierPaymentAllocation.tenant_id)
                & (PaymentObligation.id == SupplierPaymentAllocation.obligation_id),
            )
            .join(
                SupplierInvoice,
                (SupplierInvoice.tenant_id == PaymentObligation.tenant_id)
                & (SupplierInvoice.id == PaymentObligation.invoice_id),
            )
            .where(SupplierPaymentAllocation.tenant_id == tenant_id, SupplierPaymentAllocation.payment_id == payment.id)
            .order_by(SupplierPaymentAllocation.line_number)
        )
    )
    receipts = {
        row.document_kind: row
        for row in db.scalars(
            select(AccountingReceipt).where(
                AccountingReceipt.tenant_id == tenant_id,
                AccountingReceipt.document_kind.in_(("supplier_payment", "supplier_payment_reversal")),
                AccountingReceipt.document_id == payment.id,
            )
        )
    }
    bank = None
    try:
        bank = db.scalar(
            select(BankAccount).where(
                BankAccount.tenant_id == tenant_id, BankAccount.id == UUID(payment.payer_instrument_ref)
            )
        )
    except ValueError:
        bank = None
    files = list(
        db.scalars(
            select(DisbursementFileReceipt)
            .where(
                DisbursementFileReceipt.tenant_id == tenant_id,
                DisbursementFileReceipt.document_kind == KIND_SUPPLIER_PAYMENT,
                DisbursementFileReceipt.document_id == payment.id,
            )
            .order_by(DisbursementFileReceipt.generated_at.desc())
            .limit(20)
        )
    )
    instruction = db.scalar(
        select(PayeeBankInstruction).where(
            PayeeBankInstruction.tenant_id == tenant_id,
            PayeeBankInstruction.payee_kind == PayeeKind.SUPPLIER.value,
            PayeeBankInstruction.payee_ref == payment.supplier_ref,
            PayeeBankInstruction.status == InstructionStatus.APPROVED.value,
        )
    )
    payee_view = payee_service.view(db, instruction) if instruction is not None else None
    people = [payment.prepared_by, payment.approved_by, payment.recorded_by, payment.cancelled_by, payment.reversed_by]
    return {
        "payment": payment,
        "lines": lines,
        "receipts": receipts,
        "bank": bank,
        "file_receipts": files,
        "payee_instruction": payee_view,
        "names": _names(db, tenant_id, people),
    }
