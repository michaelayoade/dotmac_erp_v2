"""ERP-owned AR workflow rows (e2_0032): drafts, draft lines, issued line accounts and receipts.

Billing owns the financial truth (documents, settlements, allocations, facts). These tables carry only what
billing does not: the editable draft before issue, the revenue account of each issued line (the accounting fact
carries none; B-15 reads ``erp_ar_line_accounts`` by billing primary key) and the receiving bank of a receipt.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

DRAFTS_TABLE: Final = "erp_ar_drafts"
DRAFT_LINES_TABLE: Final = "erp_ar_draft_lines"
LINE_ACCOUNTS_TABLE: Final = "erp_ar_line_accounts"
RECEIPTS_TABLE: Final = "erp_ar_receipts"
AR_TABLES: Final = (DRAFTS_TABLE, DRAFT_LINES_TABLE, LINE_ACCOUNTS_TABLE, RECEIPTS_TABLE)

KIND_INVOICE: Final = "invoice"
KIND_CREDIT_NOTE: Final = "credit_note"
KIND_RECEIPT: Final = "receipt"
KINDS: Final = (KIND_INVOICE, KIND_CREDIT_NOTE, KIND_RECEIPT)

STATUS_DRAFT: Final = "draft"
STATUS_ISSUED: Final = "issued"
STATUS_DELETED: Final = "deleted"
STATUSES: Final = (STATUS_DRAFT, STATUS_ISSUED, STATUS_DELETED)

MONEY = Numeric(20, 6)


class ArDraft(Base):
    """One editable AR document before it reaches billing. Terminal once ``issued`` or ``deleted``."""

    __tablename__ = DRAFTS_TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name=f"uq_{DRAFTS_TABLE}_tenant_id_id"),
        UniqueConstraint("tenant_id", "request_id", name=f"uq_{DRAFTS_TABLE}_request"),
        CheckConstraint("kind IN ('invoice', 'credit_note', 'receipt')", name=f"ck_{DRAFTS_TABLE}_kind"),
        CheckConstraint("status IN ('draft', 'issued', 'deleted')", name=f"ck_{DRAFTS_TABLE}_status"),
        # D-9: when the draft was stamped with the policy ON, the issuer is not the preparer (a DB backstop).
        CheckConstraint(
            "NOT dual_control OR issued_by IS NULL OR prepared_by <> issued_by", name=f"ck_{DRAFTS_TABLE}_dual"
        ),
        CheckConstraint("(status = 'issued') = (issued_by IS NOT NULL)", name=f"ck_{DRAFTS_TABLE}_issued_by"),
        # Billing ids are set once, only with ``issued``: a document id for invoices and credit notes, a
        # settlement id for receipts, never both.
        CheckConstraint(
            "(billing_document_id IS NULL OR billing_settlement_id IS NULL) "
            "AND ((billing_document_id IS NULL AND billing_settlement_id IS NULL) OR status = 'issued') "
            "AND (kind = 'receipt' OR billing_settlement_id IS NULL) "
            "AND (kind <> 'receipt' OR billing_document_id IS NULL)",
            name=f"ck_{DRAFTS_TABLE}_billing_ids",
        ),
        Index(f"ix_{DRAFTS_TABLE}_status", "tenant_id", "kind", "status"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    customer_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    document_date: Mapped[date] = mapped_column(Date(), nullable=False)
    original_document_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    bank_account_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    bank_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    payment_terms_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=STATUS_DRAFT)
    prepared_by: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    issued_by: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    dual_control: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=true())
    billing_document_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    billing_settlement_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ArDraftLine(Base):
    """A draft line (invoice or credit note) or a receipt allocation line (``target_document_id`` + ``amount``)."""

    __tablename__ = DRAFT_LINES_TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "draft_id", "n", name=f"uq_{DRAFT_LINES_TABLE}_n"),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            [f"{DRAFTS_TABLE}.tenant_id", f"{DRAFTS_TABLE}.id"],
            ondelete="CASCADE",
            name=f"fk_{DRAFT_LINES_TABLE}_draft",
        ),
        CheckConstraint("n >= 1", name=f"ck_{DRAFT_LINES_TABLE}_n"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    draft_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    n: Mapped[int] = mapped_column(Integer(), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    unit_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    revenue_account_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    tax_code_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    target_document_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)


class ArLineAccount(Base):
    """The revenue account and tax treatment of one issued billing document line. Append-only."""

    __tablename__ = LINE_ACCOUNTS_TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "billing_document_id", "line_number", name=f"uq_{LINE_ACCOUNTS_TABLE}_line"),
        CheckConstraint("line_number >= 1", name=f"ck_{LINE_ACCOUNTS_TABLE}_line"),
        CheckConstraint("pre_tax >= 0 AND tax >= 0", name=f"ck_{LINE_ACCOUNTS_TABLE}_amounts"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    billing_document_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    obligation_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    revenue_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    pre_tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    treatment_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ArReceipt(Base):
    """One recorded customer receipt: the bank it landed in (snapshotted) and its evidence reference. Append-only."""

    __tablename__ = RECEIPTS_TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "billing_settlement_id", name=f"uq_{RECEIPTS_TABLE}_settlement"),
        UniqueConstraint("tenant_id", "draft_id", name=f"uq_{RECEIPTS_TABLE}_draft"),
        UniqueConstraint("tenant_id", "bank_account_id", "bank_reference", name=f"uq_{RECEIPTS_TABLE}_bank_ref"),
        UniqueConstraint("tenant_id", "receipt_number", name=f"uq_{RECEIPTS_TABLE}_number"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    billing_settlement_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    draft_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    bank_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    cash_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    bank_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    receipt_number: Mapped[str] = mapped_column(String(80), nullable=False)
    received_on: Mapped[date] = mapped_column(Date(), nullable=False)
    recorded_by: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
