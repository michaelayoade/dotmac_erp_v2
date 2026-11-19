"""Read side of the AR producer: registers and detail views. Read-only; nothing here writes or locks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_accounting.contracts import AccountKind
from dotmac_accounting.models import Account
from dotmac_banking.models import BankAccount
from dotmac_billing import get_document_exposure
from dotmac_billing import models as bm
from dotmac_billing.errors import BillingError
from dotmac_customers.models import CustomerAccount
from dotmac_kernel.cache import TenantScope
from dotmac_tax.models import TaxCode
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from erp_v2 import ledger_roles
from erp_v2.ar import constants
from erp_v2.ar.models import (
    STATUS_DRAFT,
    ArDraft,
    ArDraftLine,
    ArLineAccount,
    ArReceipt,
)
from erp_v2.migration_ledger.models import STATE_OPEN, MigrationException

HOLDINGS_TABLE: Final = "erp_migration_holdings"
POSTINGS_TABLE: Final = "erp_billing_fact_postings"


def _table_exists(db: Session, name: str) -> bool:
    return db.scalar(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{name}"}) is True


def holding_account_ids(db: Session, tenant_id: UUID) -> set[UUID]:
    """Accounts that hold migration differences (B-14). Empty while that table does not exist yet."""
    if not _table_exists(db, HOLDINGS_TABLE):
        return set()
    rows = db.execute(
        text(f"SELECT DISTINCT holding_account_id FROM public.{HOLDINGS_TABLE} WHERE tenant_id = :t"),  # noqa: S608
        {"t": tenant_id},
    )
    return {row[0] for row in rows}


def customer(db: Session, tenant_id: UUID, customer_account_id: UUID) -> CustomerAccount | None:
    return db.scalar(
        select(CustomerAccount).where(CustomerAccount.tenant_id == tenant_id, CustomerAccount.id == customer_account_id)
    )


def customers(db: Session, tenant_id: UUID) -> list[CustomerAccount]:
    return list(
        db.scalars(
            select(CustomerAccount).where(CustomerAccount.tenant_id == tenant_id).order_by(CustomerAccount.display_name)
        )
    )


def bank_accounts(db: Session, tenant_id: UUID, currency: str | None) -> list[BankAccount]:
    if currency is None:
        return []
    return list(
        db.scalars(
            select(BankAccount)
            .where(
                BankAccount.tenant_id == tenant_id,
                BankAccount.status == "active",
                BankAccount.currency_code == currency,
            )
            .order_by(BankAccount.account_code)
        )
    )


def revenue_account_choices(db: Session, tenant_id: UUID) -> list[Account]:
    """Active postable accounts a line may use: never control, tax-role or holding accounts."""
    blocked_ids = {UUID(i) for i in ledger_roles.tax_role_account_ids(db, tenant_id)} | holding_account_ids(
        db, tenant_id
    )
    blocked_codes = ledger_roles.CONTROL_ACCOUNT_CODES | {constants.AR_CONTROL_CODE}
    rows = db.scalars(
        select(Account)
        .where(
            Account.tenant_id == tenant_id,
            Account.is_active.is_(True),
            Account.posting_allowed.is_(True),
            Account.kind == AccountKind.POSTING,
        )
        .order_by(Account.code)
    )
    return [a for a in rows if a.id not in blocked_ids and a.code not in blocked_codes]


def tax_codes(db: Session, tenant_id: UUID) -> list[TaxCode]:
    return list(
        db.scalars(
            select(TaxCode).where(TaxCode.tenant_id == tenant_id, TaxCode.status == "active").order_by(TaxCode.code)
        )
    )


def draft(db: Session, tenant_id: UUID, draft_id: UUID) -> ArDraft | None:
    return db.scalar(select(ArDraft).where(ArDraft.tenant_id == tenant_id, ArDraft.id == draft_id))


def draft_lines(db: Session, tenant_id: UUID, draft_id: UUID) -> list[ArDraftLine]:
    return list(
        db.scalars(
            select(ArDraftLine)
            .where(ArDraftLine.tenant_id == tenant_id, ArDraftLine.draft_id == draft_id)
            .order_by(ArDraftLine.n)
        )
    )


def drafts(db: Session, tenant_id: UUID, *, status: str = STATUS_DRAFT, limit: int = 100) -> list[ArDraft]:
    return list(
        db.scalars(
            select(ArDraft)
            .where(ArDraft.tenant_id == tenant_id, ArDraft.status == status)
            .order_by(ArDraft.created_at.desc())
            .limit(limit)
        )
    )


def draft_for_document(db: Session, tenant_id: UUID, document_id: UUID) -> ArDraft | None:
    return db.scalar(select(ArDraft).where(ArDraft.tenant_id == tenant_id, ArDraft.billing_document_id == document_id))


def receipt_for_settlement(db: Session, tenant_id: UUID, settlement_id: UUID) -> ArReceipt | None:
    return db.scalar(
        select(ArReceipt).where(ArReceipt.tenant_id == tenant_id, ArReceipt.billing_settlement_id == settlement_id)
    )


def receipt_for_draft(db: Session, tenant_id: UUID, draft_id: UUID) -> ArReceipt | None:
    return db.scalar(select(ArReceipt).where(ArReceipt.tenant_id == tenant_id, ArReceipt.draft_id == draft_id))


def line_accounts(db: Session, tenant_id: UUID, document_id: UUID) -> list[ArLineAccount]:
    return list(
        db.scalars(
            select(ArLineAccount)
            .where(ArLineAccount.tenant_id == tenant_id, ArLineAccount.billing_document_id == document_id)
            .order_by(ArLineAccount.line_number)
        )
    )


def billing_document(db: Session, tenant_id: UUID, document_id: UUID) -> bm.BillingDocument | None:
    return db.scalar(
        select(bm.BillingDocument).where(
            bm.BillingDocument.tenant_id == tenant_id, bm.BillingDocument.id == document_id
        )
    )


def billing_account(db: Session, tenant_id: UUID, account_id: UUID) -> bm.BillingAccount | None:
    return db.scalar(
        select(bm.BillingAccount).where(bm.BillingAccount.tenant_id == tenant_id, bm.BillingAccount.id == account_id)
    )


def billing_account_for_customer(
    db: Session, tenant_id: UUID, customer_account_id: UUID, currency: str
) -> bm.BillingAccount | None:
    return db.scalar(
        select(bm.BillingAccount).where(
            bm.BillingAccount.tenant_id == tenant_id,
            bm.BillingAccount.external_account_ref == str(customer_account_id),
            bm.BillingAccount.currency == currency,
        )
    )


def billing_lines(db: Session, tenant_id: UUID, document_id: UUID) -> list[bm.DocumentLine]:
    return list(
        db.scalars(
            select(bm.DocumentLine)
            .where(bm.DocumentLine.tenant_id == tenant_id, bm.DocumentLine.document_id == document_id)
            .order_by(bm.DocumentLine.line_number)
        )
    )


def is_voided(db: Session, tenant_id: UUID, document_id: UUID) -> bool:
    return (
        db.scalar(
            select(bm.DocumentEvent.id).where(
                bm.DocumentEvent.tenant_id == tenant_id,
                bm.DocumentEvent.document_id == document_id,
                bm.DocumentEvent.event_kind == "voided",
            )
        )
        is not None
    )


def allocation_rows(
    db: Session, tenant_id: UUID, *, document_id: UUID | None = None, settlement_id: UUID | None = None
):
    """Positive allocation rows (the ones a deallocation can target), newest last."""
    statement = select(bm.AllocationEffect).where(
        bm.AllocationEffect.tenant_id == tenant_id,
        bm.AllocationEffect.amount_delta > 0,
        bm.AllocationEffect.document_id.is_not(None),
    )
    if document_id is not None:
        statement = statement.where(bm.AllocationEffect.document_id == document_id)
    if settlement_id is not None:
        statement = statement.where(bm.AllocationEffect.settlement_id == settlement_id)
    return list(db.scalars(statement.order_by(bm.AllocationEffect.created_at, bm.AllocationEffect.id)))


def credit_notes_for(db: Session, tenant_id: UUID, document_id: UUID) -> list[bm.BillingDocument]:
    return list(
        db.scalars(
            select(bm.BillingDocument)
            .where(
                bm.BillingDocument.tenant_id == tenant_id,
                bm.BillingDocument.credits_document_id == document_id,
                bm.BillingDocument.document_kind == "credit_note",
            )
            .order_by(bm.BillingDocument.created_at)
        )
    )


def posting_group_for_source(db: Session, tenant_id: UUID, source_ref: str) -> bm.PostingGroup | None:
    return db.scalar(
        select(bm.PostingGroup).where(bm.PostingGroup.tenant_id == tenant_id, bm.PostingGroup.source_ref == source_ref)
    )


def group_reversed(db: Session, tenant_id: UUID, group_id: UUID) -> bool:
    return (
        db.scalar(
            select(bm.PostingGroup.id).where(
                bm.PostingGroup.tenant_id == tenant_id, bm.PostingGroup.reverses_group_id == group_id
            )
        )
        is not None
    )


def settlement(db: Session, tenant_id: UUID, settlement_id: UUID) -> bm.ConfirmedSettlement | None:
    return db.scalar(
        select(bm.ConfirmedSettlement).where(
            bm.ConfirmedSettlement.tenant_id == tenant_id, bm.ConfirmedSettlement.id == settlement_id
        )
    )


@dataclass(frozen=True, slots=True)
class Exposure:
    outstanding: Decimal | None
    state: str | None


def exposure(db: Session, tenant_id: UUID, document_id: UUID) -> Exposure:
    """Outstanding and state of an issued invoice from billing's sealed exposure; unknown when it cannot be read."""
    try:
        sealed = get_document_exposure(db, scope=TenantScope(tenant_id), document_id=document_id)
    except BillingError:
        return Exposure(None, None)
    return Exposure(sealed.collectible_receivable.amount, sealed.financial_state)


def open_invoices(db: Session, tenant_id: UUID, billing_account_id: UUID) -> list[tuple[bm.BillingDocument, Decimal]]:
    """Issued, unvoided invoices of an account with their outstanding (> 0), oldest first."""
    rows = db.scalars(
        select(bm.BillingDocument)
        .where(
            bm.BillingDocument.tenant_id == tenant_id,
            bm.BillingDocument.billing_account_id == billing_account_id,
            bm.BillingDocument.document_kind == "invoice",
            bm.BillingDocument.issued_at.is_not(None),
        )
        .order_by(bm.BillingDocument.issued_at, bm.BillingDocument.document_number)
    )
    found = []
    for document in rows:
        if is_voided(db, tenant_id, document.id):
            continue
        state = exposure(db, tenant_id, document.id)
        if state.outstanding is not None and state.outstanding > 0:
            found.append((document, state.outstanding))
    return found


def gl_status(db: Session, tenant_id: UUID, document_id: UUID) -> str:
    """posted / pending / blocked for a document's issue fact in the B-15 mapper; ``n/a`` before B-15 exists."""
    if not _table_exists(db, POSTINGS_TABLE):
        return "n/a"
    row = db.execute(
        text(
            "SELECT p.disposition FROM public.erp_billing_fact_postings p "
            "JOIN mod_billing.accounting_facts f ON f.tenant_id = p.tenant_id AND f.id = p.fact_id "
            "JOIN mod_billing.posting_groups g ON g.tenant_id = f.tenant_id AND g.id = f.posting_group_id "
            "WHERE p.tenant_id = :t AND g.source_ref IN (:issue, :credit) "
            "ORDER BY p.first_seen_at LIMIT 1"
        ),
        {"t": tenant_id, "issue": f"document:{document_id}:issue", "credit": f"credit_note:{document_id}"},
    ).first()
    if row is None:
        return "pending"
    return {"POSTED": "posted", "BLOCKED": "blocked", "REFUSED": "blocked"}.get(row[0], "pending")


def credit_note_notice(db: Session, tenant_id: UUID, customer_account_id: UUID) -> list[str]:
    """Open E35 register items that mention this customer (F-D: a read-only safety notice, never a control).

    Legacy credit notes that were not carried are cleared by R8; issuing a new credit note for them would reverse
    revenue twice. The register keys items by the legacy customer key, so an item is shown when the customer's
    account id appears in its key or detail; the form also carries the standing warning regardless.
    """
    needle = str(customer_account_id)
    rows = db.scalars(
        select(MigrationException)
        .where(
            MigrationException.tenant_id == tenant_id,
            MigrationException.class_code == "E35",
            MigrationException.state == STATE_OPEN,
        )
        .order_by(MigrationException.legacy_key)
        .limit(2000)
    )
    return [row.legacy_key for row in rows if needle in row.legacy_key or needle in json.dumps(row.detail, default=str)]
