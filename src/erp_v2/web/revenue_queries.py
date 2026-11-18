"""Tenant-scoped document registers; no financial state is reconstructed here."""

from __future__ import annotations

from uuid import UUID

from dotmac_accounting.models import Account
from dotmac_billing.models import BillingAccount, BillingDocument, ConfirmedSettlement
from dotmac_payables.models import (
    CreditApplication,
    CreditNote,
    CreditNoteLine,
    PaymentObligation,
    SettlementObservation,
    SupplierInvoice,
    SupplierInvoiceLine,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

try:  # payables a6; absent on the released a5 pin
    from dotmac_payables.models import HistoricalCreditReceipt
except ImportError:  # pragma: no cover
    HistoricalCreditReceipt = None  # type: ignore[assignment,misc]

from erp_v2.legacy_ap.models import LegacyInvoiceReference


def billing_register(db: Session, *, tenant_id: UUID) -> dict[str, object]:
    documents = db.scalars(
        select(BillingDocument)
        .where(BillingDocument.tenant_id == tenant_id)
        .order_by(BillingDocument.created_at.desc(), BillingDocument.id)
        .limit(100)
    ).all()
    receipts = db.scalars(
        select(ConfirmedSettlement)
        .where(ConfirmedSettlement.tenant_id == tenant_id)
        .order_by(ConfirmedSettlement.occurred_at.desc(), ConfirmedSettlement.id)
        .limit(100)
    ).all()
    accounts = db.scalars(
        select(BillingAccount)
        .where(BillingAccount.tenant_id == tenant_id)
        .order_by(BillingAccount.external_account_ref)
    ).all()
    return {
        "documents": documents,
        "receipts": receipts,
        "billing_accounts": {a.id: a.external_account_ref for a in accounts},
    }


def supplier_register(db: Session, *, tenant_id: UUID) -> list[SupplierInvoice]:
    return list(
        db.scalars(
            select(SupplierInvoice)
            .where(SupplierInvoice.tenant_id == tenant_id)
            .order_by(SupplierInvoice.invoice_date.desc(), SupplierInvoice.number)
            .limit(100)
        )
    )


def posting_accounts(db: Session, *, tenant_id: UUID) -> list[Account]:
    return list(
        db.scalars(
            select(Account)
            .where(Account.tenant_id == tenant_id, Account.is_active.is_(True), Account.posting_allowed.is_(True))
            .order_by(Account.code)
        )
    )


def supplier_detail(db: Session, *, tenant_id: UUID, invoice_id: UUID) -> dict[str, object] | None:
    invoice = db.scalar(
        select(SupplierInvoice).where(SupplierInvoice.tenant_id == tenant_id, SupplierInvoice.id == invoice_id)
    )
    if invoice is None:
        return None
    lines = db.scalars(
        select(SupplierInvoiceLine)
        .where(SupplierInvoiceLine.tenant_id == tenant_id, SupplierInvoiceLine.invoice_id == invoice_id)
        .order_by(SupplierInvoiceLine.line_number)
    ).all()
    obligations = db.scalars(
        select(PaymentObligation)
        .where(PaymentObligation.tenant_id == tenant_id, PaymentObligation.invoice_id == invoice_id)
        .order_by(PaymentObligation.sequence)
    ).all()
    observations = db.scalars(
        select(SettlementObservation)
        .join(
            PaymentObligation,
            (PaymentObligation.tenant_id == SettlementObservation.tenant_id)
            & (PaymentObligation.id == SettlementObservation.obligation_id),
        )
        .where(PaymentObligation.tenant_id == tenant_id, PaymentObligation.invoice_id == invoice_id)
        .order_by(SettlementObservation.occurred_at.desc(), SettlementObservation.id)
    ).all()
    legacy_reference = db.scalar(
        select(LegacyInvoiceReference)
        .where(LegacyInvoiceReference.tenant_id == tenant_id, LegacyInvoiceReference.invoice_id == invoice_id)
        .limit(1)
    )
    return {
        "invoice": invoice,
        "lines": lines,
        "obligations": obligations,
        "observations": observations,
        "legacy_reference": legacy_reference,
    }


def credit_note_detail(db: Session, *, tenant_id: UUID, credit_note_id: UUID) -> dict[str, object] | None:
    """One supplier credit note with its lines, applications and the original invoice's open obligations."""
    credit = db.scalar(select(CreditNote).where(CreditNote.tenant_id == tenant_id, CreditNote.id == credit_note_id))
    if credit is None:
        return None
    lines = db.scalars(
        select(CreditNoteLine)
        .where(CreditNoteLine.tenant_id == tenant_id, CreditNoteLine.credit_note_id == credit_note_id)
        .order_by(CreditNoteLine.line_number)
    ).all()
    applications = db.scalars(
        select(CreditApplication)
        .where(CreditApplication.tenant_id == tenant_id, CreditApplication.credit_note_id == credit_note_id)
        .order_by(CreditApplication.applied_at)
    ).all()
    cash = HistoricalCreditReceipt is not None and (
        db.scalar(
            select(HistoricalCreditReceipt.id).where(
                HistoricalCreditReceipt.tenant_id == tenant_id, HistoricalCreditReceipt.credit_note_id == credit_note_id
            )
        )
        is not None
    )
    obligations: list[PaymentObligation] = []
    if cash:  # a CASH credit applies to any open obligation of the same supplier and currency
        obligations = list(
            db.scalars(
                select(PaymentObligation)
                .join(
                    SupplierInvoice,
                    (SupplierInvoice.tenant_id == PaymentObligation.tenant_id)
                    & (SupplierInvoice.id == PaymentObligation.invoice_id),
                )
                .where(
                    PaymentObligation.tenant_id == tenant_id,
                    PaymentObligation.outstanding_amount > 0,
                    SupplierInvoice.supplier_ref == credit.supplier_ref,
                    SupplierInvoice.currency_code == credit.currency_code,
                )
                .order_by(PaymentObligation.due_date)
                .limit(200)
            )
        )
    elif credit.original_invoice_id is not None:
        obligations = list(
            db.scalars(
                select(PaymentObligation)
                .where(
                    PaymentObligation.tenant_id == tenant_id, PaymentObligation.invoice_id == credit.original_invoice_id
                )
                .order_by(PaymentObligation.sequence)
            )
        )
    return {
        "credit": credit,
        "lines": lines,
        "applications": applications,
        "obligations": obligations,
        "cash_credit": cash,
    }
