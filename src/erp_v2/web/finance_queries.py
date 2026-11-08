"""Finance workspace reads over accounting + payables module models.

Read-only presentation, tenant-filtered. Trial balance derives from
posted ledger lines (the immutable evidence), never from journal
drafts, so the workspace cannot disagree with the ledger.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from dotmac_accounting.models import Account, JournalEntry, PostedLedgerLine
from dotmac_payables.models import SupplierInvoice
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def chart_of_accounts(db: Session, *, tenant_id: UUID) -> list[dict[str, object]]:
    rows = db.scalars(select(Account).where(Account.tenant_id == tenant_id).order_by(Account.code))
    return [
        {
            "code": a.code,
            "name": a.name,
            "kind": a.kind,
            "normal_balance": a.normal_balance,
            "currency_code": a.currency_code or "—",
            "is_active": a.is_active,
            "posting_allowed": a.posting_allowed,
        }
        for a in rows
    ]


def recent_journals(db: Session, *, tenant_id: UUID, limit: int = 50) -> list[dict[str, object]]:
    rows = db.scalars(
        select(JournalEntry)
        .where(JournalEntry.tenant_id == tenant_id)
        .order_by(JournalEntry.entry_date.desc(), JournalEntry.number.desc())
        .limit(limit)
    )
    return [
        {
            "number": j.number,
            "kind": j.kind,
            "status": j.status,
            "entry_date": j.entry_date,
            "description": j.description or "",
            "currency_code": j.currency_code,
            "total_debit": j.total_debit,
            "total_credit": j.total_credit,
        }
        for j in rows
    ]


def trial_balance(db: Session, *, tenant_id: UUID) -> dict[str, object]:
    rows = db.execute(
        select(
            Account.code,
            Account.name,
            func.coalesce(func.sum(PostedLedgerLine.debit), 0).label("debit"),
            func.coalesce(func.sum(PostedLedgerLine.credit), 0).label("credit"),
        )
        .join(
            PostedLedgerLine,
            (PostedLedgerLine.tenant_id == Account.tenant_id) & (PostedLedgerLine.account_id == Account.id),
        )
        .where(Account.tenant_id == tenant_id)
        .group_by(Account.code, Account.name)
        .order_by(Account.code)
    ).mappings()
    lines = [
        {
            "code": r["code"],
            "name": r["name"],
            "debit": Decimal(r["debit"]),
            "credit": Decimal(r["credit"]),
            "balance": Decimal(r["debit"]) - Decimal(r["credit"]),
        }
        for r in rows
    ]
    return {
        "lines": lines,
        "total_debit": sum((line["debit"] for line in lines), Decimal(0)),
        "total_credit": sum((line["credit"] for line in lines), Decimal(0)),
    }


def open_payables(db: Session, *, tenant_id: UUID, limit: int = 50) -> list[dict[str, object]]:
    rows = db.scalars(
        select(SupplierInvoice)
        .where(
            SupplierInvoice.tenant_id == tenant_id,
            SupplierInvoice.status.not_in(("voided", "paid")),
        )
        .order_by(SupplierInvoice.invoice_date.desc())
        .limit(limit)
    )
    return [
        {
            "number": i.number,
            "supplier": i.supplier_name_snapshot,
            "supplier_document": i.supplier_document_number or "—",
            "invoice_date": i.invoice_date,
            "currency_code": i.currency_code,
            "total": i.total_amount,
            "status": i.status,
        }
        for i in rows
    ]


__all__ = ["chart_of_accounts", "open_payables", "recent_journals", "trial_balance"]
