"""ERP-owned bank reconciliation state (B7, e2_0017). Shapes frozen by the migration.

Banking a1 owns statements, lines and accounts; accounting a1 owns the ledger. This lineage records
only what neither owner records: which statement line is matched to which posted ledger line, the
typed reconciling items, and the prepare/approve maker-checker trail. Ledger line and statement line
ids are plain columns (no cross-lineage foreign key); the service resolves them inside the tenant.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

MONEY = Numeric(20, 6)
_PARTY = "parties.id"

STATUS_OPEN: Final = "open"
STATUS_PREPARED: Final = "prepared"
STATUS_APPROVED: Final = "approved"
STATUS_CANCELLED: Final = "cancelled"
LIVE_STATUSES: Final = (STATUS_OPEN, STATUS_PREPARED, STATUS_APPROVED)

KIND_BASELINE: Final = "baseline"
KIND_PERIODIC: Final = "periodic"

MATCH_ACTIVE: Final = "active"
MATCH_UNMATCHED: Final = "unmatched"

SIDE_BANK: Final = "bank"
SIDE_GL: Final = "gl"

# GL-side timing items (the book has it, the bank does not yet) and bank-side items (the bank
# has it, the book does not). Bank-side items are never auto-posted: they block preparation until
# a B2 manual journal records them and the resulting ledger line is matched.
ITEM_DEPOSIT_IN_TRANSIT: Final = "deposit_in_transit"
ITEM_OUTSTANDING_PAYMENT: Final = "outstanding_payment"
ITEM_UNRECORDED_BANK_CHARGE: Final = "unrecorded_bank_charge"
ITEM_UNRECORDED_BANK_RECEIPT: Final = "unrecorded_bank_receipt"
TIMING_TYPES: Final = (ITEM_DEPOSIT_IN_TRANSIT, ITEM_OUTSTANDING_PAYMENT)
UNRECORDED_TYPES: Final = (ITEM_UNRECORDED_BANK_CHARGE, ITEM_UNRECORDED_BANK_RECEIPT)
ITEM_TYPES: Final = TIMING_TYPES + UNRECORDED_TYPES


def _party(nullable: bool = True) -> Mapped[UUID | None]:
    return mapped_column(Uuid(), ForeignKey(_PARTY), nullable=nullable)


class BankReconciliation(Base, TimestampMixin):
    __tablename__ = "erp_bank_reconciliations"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_erp_bank_reconciliations_tenant_id"),)

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    statement_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    gl_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default=KIND_PERIODIC, server_default=KIND_PERIODIC)
    statement_closing_balance: Mapped[Decimal | None] = mapped_column(MONEY)
    gl_balance: Mapped[Decimal | None] = mapped_column(MONEY)
    items_total: Mapped[Decimal | None] = mapped_column(MONEY)
    difference: Mapped[Decimal | None] = mapped_column(MONEY)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    opened_by_party_id: Mapped[UUID] = _party(nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prepared_by_party_id: Mapped[UUID | None] = _party()
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_party_id: Mapped[UUID | None] = _party()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_by_party_id: Mapped[UUID | None] = _party()
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_reason: Mapped[str | None] = mapped_column(String(500))
    cancelled_by_party_id: Mapped[UUID | None] = _party()
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))


class BankReconMatch(Base, TimestampMixin):
    __tablename__ = "erp_bank_recon_matches"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_erp_bank_recon_matches_tenant_id"),)

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    member_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    latest_date: Mapped[date] = mapped_column(Date, nullable=False)
    matched_by_party_id: Mapped[UUID] = _party(nullable=False)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    unmatched_by_party_id: Mapped[UUID | None] = _party()
    unmatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unmatch_reason: Mapped[str | None] = mapped_column(String(500))
    locked_by_reconciliation_id: Mapped[UUID | None] = mapped_column(Uuid())


class BankReconMatchLine(Base):
    __tablename__ = "erp_bank_recon_match_lines"

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    match_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    ref_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class BankReconItem(Base):
    __tablename__ = "erp_bank_recon_items"

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reconciliation_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    ref_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    signed_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    note: Mapped[str] = mapped_column(String(500), nullable=False)
    journal_id: Mapped[UUID | None] = mapped_column(Uuid())
    created_by_party_id: Mapped[UUID] = _party(nullable=False)
    withdrawn_by_party_id: Mapped[UUID | None] = _party()
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdraw_reason: Mapped[str | None] = mapped_column(String(500))


RECON_TABLES = (
    "erp_bank_reconciliations",
    "erp_bank_recon_matches",
    "erp_bank_recon_match_lines",
    "erp_bank_recon_items",
)
