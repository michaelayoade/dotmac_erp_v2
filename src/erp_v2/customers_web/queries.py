"""Read side of the customer screen. Read-only; nothing here writes or locks."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from dotmac_billing import models as bm
from dotmac_customers.models import CustomerAccount, CustomerProfile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from erp_v2.ar import queries as ar_queries
from erp_v2.customers_web.models import CustomerFinance

PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class Row:
    account: CustomerAccount
    finance: CustomerFinance | None


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def listing(
    db: Session, tenant_id: UUID, *, search: str = "", status: str = "", page: int = 1, size: int = PAGE_SIZE
) -> tuple[list[Row], int]:
    """One page of customers (number order) and the total, optionally filtered by text and status."""
    where = [CustomerAccount.tenant_id == tenant_id]
    search = search.strip()
    if search:
        pattern = _like(search)
        where.append(
            or_(
                CustomerAccount.account_number.ilike(pattern, escape="\\"),
                CustomerAccount.display_name.ilike(pattern, escape="\\"),
                CustomerFinance.trading_name.ilike(pattern, escape="\\"),
            )
        )
    if status:
        where.append(CustomerAccount.status == status)
    join = (
        CustomerFinance,
        (CustomerFinance.tenant_id == CustomerAccount.tenant_id)
        & (CustomerFinance.customer_account_id == CustomerAccount.id),
    )
    total = db.scalar(select(func.count()).select_from(CustomerAccount).outerjoin(*join).where(*where)) or 0
    rows = db.execute(
        select(CustomerAccount, CustomerFinance)
        .outerjoin(*join)
        .where(*where)
        .order_by(CustomerAccount.account_number)
        .limit(size)
        .offset(max(page - 1, 0) * size)
    )
    return [Row(a, f) for a, f in rows], total


def account(db: Session, tenant_id: UUID, account_id: UUID) -> CustomerAccount | None:
    return db.scalar(
        select(CustomerAccount).where(CustomerAccount.tenant_id == tenant_id, CustomerAccount.id == account_id)
    )


def finance(db: Session, tenant_id: UUID, account_id: UUID) -> CustomerFinance | None:
    return db.scalar(
        select(CustomerFinance).where(
            CustomerFinance.tenant_id == tenant_id, CustomerFinance.customer_account_id == account_id
        )
    )


def notes(db: Session, tenant_id: UUID, account_id: UUID) -> str | None:
    return db.scalar(
        select(CustomerProfile.notes).where(
            CustomerProfile.tenant_id == tenant_id, CustomerProfile.account_id == account_id
        )
    )


def billing_accounts(db: Session, tenant_id: UUID, account_id: UUID) -> list[bm.BillingAccount]:
    """The structural link only: billing accounts whose external ref is this customer's id, one per currency."""
    return list(
        db.scalars(
            select(bm.BillingAccount)
            .where(bm.BillingAccount.tenant_id == tenant_id, bm.BillingAccount.external_account_ref == str(account_id))
            .order_by(bm.BillingAccount.currency)
        )
    )


def open_item_count(db: Session, tenant_id: UUID, accounts: list[bm.BillingAccount]) -> int:
    """Issued, unvoided invoices with an outstanding amount, across the customer's billing accounts."""
    return sum(len(ar_queries.open_invoices(db, tenant_id, a.id)) for a in accounts)


def missing_finance_count(db: Session, tenant_id: UUID) -> int:
    """Customers without a finance row (the wave-4 completeness check)."""
    return (
        db.scalar(
            select(func.count())
            .select_from(CustomerAccount)
            .outerjoin(
                CustomerFinance,
                (CustomerFinance.tenant_id == CustomerAccount.tenant_id)
                & (CustomerFinance.customer_account_id == CustomerAccount.id),
            )
            .where(CustomerAccount.tenant_id == tenant_id, CustomerFinance.id.is_(None))
        )
        or 0
    )
