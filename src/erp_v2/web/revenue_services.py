"""Supplier draft orchestration; Payables remains the only document writer.

No approval, payment confirmation, settlement observation or ledger command is
exposed here. Supplier-stated tax is captured as document data, never calculated
or promoted to Tax authority. The kernel request boundary owns the transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from dotmac_accounting.models import Account
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from dotmac_kernel.models import Party, Tenant
from dotmac_payables import service as payables
from dotmac_payables.contracts import (
    CreateSupplierInvoice,
    InvoiceStatus,
    ObligationSchedule,
    PayableLineInput,
    PayablesError,
)
from dotmac_payables.models import SupplierInvoice
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

_QUANTUM = Decimal("0.000001")
_MAXIMUM = Decimal("99999999999999.999999")


class RevenueActionError(ValueError):
    """A safe, user-facing document refusal."""


def _decimal(values: Mapping[str, str], name: str) -> Decimal:
    try:
        value = Decimal(values.get(name, ""))
        if not value.is_finite() or abs(value) > _MAXIMUM or value != value.quantize(_QUANTUM):
            raise ValueError
    except (InvalidOperation, ValueError) as exc:
        raise RevenueActionError(f"{name.replace('_', ' ')} must be a finite number with at most six decimals") from exc
    return value


def _date(values: Mapping[str, str], name: str) -> date:
    try:
        return date.fromisoformat(values.get(name, ""))
    except ValueError as exc:
        raise RevenueActionError(f"Enter {name.replace('_', ' ')} as YYYY-MM-DD") from exc


def _account(db: Session, tenant_id: UUID, value: str, label: str) -> str:
    try:
        account_id = UUID(value)
    except ValueError as exc:
        raise RevenueActionError(f"Select a {label}") from exc
    account = db.scalar(
        select(Account).where(
            Account.tenant_id == tenant_id,
            Account.id == account_id,
            Account.is_active.is_(True),
            Account.posting_allowed.is_(True),
        )
    )
    if account is None:
        raise RevenueActionError(f"The {label} is unavailable for this tenant")
    return str(account.id)


def _actor(tenant: Tenant, party: Party) -> None:
    if party.tenant_id != tenant.id:
        raise RevenueActionError("The acting party must belong to this tenant")


def create_supplier_draft(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    values: Mapping[str, str],
) -> SupplierInvoice:
    """Capture one supplier line and one due date without recognizing liability."""
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
    if not total.is_finite() or abs(total) > _MAXIMUM or total != total.quantize(_QUANTUM):
        raise RevenueActionError("The document total must fit six decimals without rounding")
    currency = values.get("currency_code", "").strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise RevenueActionError("Currency must be a three-letter code")
    posting = _account(db, tenant.id, values.get("posting_account_id", ""), "line posting account")
    liability = _account(db, tenant.id, values.get("liability_account_id", ""), "liability account")
    tax_account = None
    if tax > 0:
        tax_account = _account(db, tenant.id, values.get("tax_account_id", ""), "tax account")
    command = CreateSupplierInvoice(
        number=values.get("number", ""),
        supplier_ref=values.get("supplier_ref", ""),
        supplier_name_snapshot=values.get("supplier_name", ""),
        supplier_document_number=values.get("supplier_document_number", ""),
        invoice_date=_date(values, "invoice_date"),
        received_date=_date(values, "received_date"),
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
        schedule=(ObligationSchedule(due_date=_date(values, "due_date"), amount=total),),
    )
    try:
        # Preserve a usable request session after a domain/unique refusal.
        with db.begin_nested():
            return payables.create_invoice(
                db,
                scope=TenantScope(tenant.id),
                command=command,
                idempotency_key=f"erp.supplier-draft:{request_id}",
                idempotency_expires_at=None,
                recorded_at=datetime.now(timezone.utc),
            )
    except (PayablesError, IdempotencyConflict) as exc:
        raise RevenueActionError(str(exc)) from exc
    except IntegrityError as exc:
        raise RevenueActionError("That invoice number or supplier document already exists") from exc


def submit_supplier_draft(db: Session, *, tenant: Tenant, party: Party, invoice_id: UUID) -> SupplierInvoice:
    """Submit for review, never approve, post or pay. Exact repeats are harmless."""
    _actor(tenant, party)
    try:
        with db.begin_nested():
            invoice = db.scalar(
                select(SupplierInvoice)
                .where(SupplierInvoice.tenant_id == tenant.id, SupplierInvoice.id == invoice_id)
                .with_for_update()
            )
            if invoice is None:
                raise RevenueActionError("Supplier invoice was not found")
            if invoice.status == InvoiceStatus.SUBMITTED:
                return invoice
            return payables.submit_invoice(
                db,
                scope=TenantScope(tenant.id),
                invoice_id=invoice_id,
                submitted_by=str(party.id),
                submitted_at=datetime.now(timezone.utc),
            )
    except PayablesError as exc:
        raise RevenueActionError(str(exc)) from exc
