"""Payroll payout batches and lines (e2_0019).

A batch is a set of whole liabilities of ONE finalized run to be paid in one bank action:
``net_pay`` (employee-net-pay liabilities; may have a bank file) or ``statutory`` (component
liabilities to one external destination; never a file, W19). Amounts and membership are immutable
after insert (the runtime role may update lifecycle columns only); the payroll owner stays the
authority for settlement and the ledger. No bank file bytes are stored anywhere. A line of a RECORDED
net-pay batch can be RETURNED (e2_0021, payroll a5): see ``service.return_line``.
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
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

MONEY = Numeric(20, 6)
KIND_NET_PAY: Final = "net_pay"
KIND_STATUTORY: Final = "statutory"
DRAFT: Final = "draft"
APPROVED: Final = "approved"
FILE_ISSUED: Final = "file_issued"
RECORDED: Final = "recorded"
CANCELLED: Final = "cancelled"
STATUSES: Final = (DRAFT, APPROVED, FILE_ISSUED, RECORDED, CANCELLED)
PAYOUT_TABLES: Final = ("erp_payroll_payout_batches", "erp_payroll_payout_lines")

# Columns the runtime role may UPDATE (lifecycle only); shared with the migration.
BATCH_UPDATE_COLUMNS: Final = (
    "status",
    "approved_by_party_id",
    "approved_at",
    "file_issued_by_party_id",
    "file_issued_at",
    "file_sha256",
    "file_row_count",
    "recorded_by_party_id",
    "recorded_at",
    "bank_reference",
    "cancelled_by_party_id",
    "cancelled_at",
    "cancel_reason",
    "recall_reference",
    "updated_at",
)
# e2_0024: the approved payee instruction id each line was approved against (set once at approve; a trigger
# makes it write-once). Record refuses (``payee_instruction_changed``) if the current approved instruction differs.
LINE_INSTRUCTION_COLUMN: Final = "payee_instruction_id"
LINE_UPDATE_COLUMNS: Final = ("active", "settlement_id", "journal_id", "journal_number", "consequence_fingerprint")

# e2_0021 (payroll a5): a RECORDED net-pay line whose bank credit came back. The return columns are
# written once, in the same savepoint as the owner's reversal and the ledger posting; the runtime role
# may update them (and ``active``) but a trigger makes them immutable once set. ``LINE_UPDATE_COLUMNS``
# stays as e2_0019 granted it (a migration must not reference columns that do not exist yet).
LINE_RETURN_COLUMNS: Final = (
    "returned_at",
    "returned_by_party_id",
    "return_reference",
    "return_reason",
    "return_value_date",
    "return_bank_account_id",
    "reversal_id",
    "reversal_journal_id",
    "reversal_journal_number",
    "reversal_fingerprint",
)
LINE_RETURNED: Final = "returned"
LINE_PAID: Final = "paid"

_B = "erp_payroll_payout_batches"
_L = "erp_payroll_payout_lines"
_STATUS_LIST = ", ".join(f"'{s}'" for s in STATUSES)


class PayrollPayoutBatch(Base):
    __tablename__ = _B
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name=f"uq_{_B}_tenant_id_id"),
        UniqueConstraint("tenant_id", "request_id", name=f"uq_{_B}_request"),
        UniqueConstraint("tenant_id", "number", name=f"uq_{_B}_number"),
        # A bank evidence reference is used once per bank account (NULL until recorded).
        UniqueConstraint("tenant_id", "bank_account_id", "bank_reference", name=f"uq_{_B}_bank_reference"),
        CheckConstraint(f"kind IN ('{KIND_NET_PAY}', '{KIND_STATUTORY}')", name=f"ck_{_B}_kind"),
        CheckConstraint(f"status IN ({_STATUS_LIST})", name=f"ck_{_B}_status"),
        CheckConstraint("total_amount > 0 AND line_count > 0", name=f"ck_{_B}_totals"),
        CheckConstraint("kind = 'net_pay' OR file_issued_by_party_id IS NULL", name=f"ck_{_B}_statutory_no_file"),
        # Distinct people, enforced by the database as well as the service.
        CheckConstraint(
            "NOT dual_control OR ((approved_by_party_id IS NULL OR approved_by_party_id <> prepared_by_party_id) "
            "AND (file_issued_by_party_id IS NULL OR (file_issued_by_party_id <> prepared_by_party_id "
            "AND file_issued_by_party_id <> approved_by_party_id)) "
            "AND (recorded_by_party_id IS NULL OR (recorded_by_party_id <> prepared_by_party_id "
            "AND recorded_by_party_id <> approved_by_party_id "
            "AND recorded_by_party_id <> file_issued_by_party_id)))",
            name=f"ck_{_B}_distinct_people",
        ),
        CheckConstraint(
            "status <> 'recorded' OR (recorded_by_party_id IS NOT NULL AND bank_reference IS NOT NULL)",
            name=f"ck_{_B}_recorded",
        ),
        Index(f"ix_{_B}_run", "tenant_id", "run_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    # Stamped from the tenant policy at creation; DB default true keeps pre-e2_0022 rows strict (e2_0022).
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    request_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    destination_ref: Mapped[str | None] = mapped_column(String(240))
    bank_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    value_date: Mapped[date] = mapped_column(nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    prepared_by_party_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by_party_id: Mapped[UUID | None] = mapped_column(Uuid())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_issued_by_party_id: Mapped[UUID | None] = mapped_column(Uuid())
    file_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    file_row_count: Mapped[int | None] = mapped_column(Integer)
    recorded_by_party_id: Mapped[UUID | None] = mapped_column(Uuid())
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bank_reference: Mapped[str | None] = mapped_column(String(255))
    cancelled_by_party_id: Mapped[UUID | None] = mapped_column(Uuid())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    recall_reference: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PayrollPayoutLine(Base):
    __tablename__ = _L
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name=f"uq_{_L}_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "batch_id"], [f"{_B}.tenant_id", f"{_B}.id"], ondelete="RESTRICT", name=f"fk_{_L}_batch"
        ),
        CheckConstraint("amount > 0", name=f"ck_{_L}_amount"),
        Index(f"ix_{_L}_batch", "tenant_id", "batch_id"),
        # RETURNED means: the bank credit came back, the owner reversed the settlement, the ledger was posted,
        # and the line left the live set (active = false) so the liability can go into a new batch.
        CheckConstraint(
            "returned_at IS NULL OR (active = false AND settlement_id IS NOT NULL AND journal_id IS NOT NULL "
            "AND returned_by_party_id IS NOT NULL AND return_reference IS NOT NULL AND return_reason IS NOT NULL "
            "AND return_value_date IS NOT NULL AND return_bank_account_id IS NOT NULL AND reversal_id IS NOT NULL "
            "AND reversal_journal_id IS NOT NULL AND reversal_fingerprint IS NOT NULL)",
            name=f"ck_{_L}_returned",
        ),
        # One bank return reference per bank account and one reversal per line.
        Index(
            f"uq_{_L}_return_reference",
            "tenant_id",
            "return_bank_account_id",
            "return_reference",
            unique=True,
            postgresql_where=text("returned_at IS NOT NULL"),
        ),
        Index(
            f"uq_{_L}_reversal",
            "tenant_id",
            "reversal_id",
            unique=True,
            postgresql_where=text("returned_at IS NOT NULL"),
        ),
        # A liability sits in at most one live batch.
        Index(f"uq_{_L}_live_liability", "tenant_id", "liability_id", unique=True, postgresql_where=text("active")),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    # Stamped from the tenant policy at creation; DB default true keeps pre-e2_0022 rows strict (e2_0022).
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    batch_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    liability_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    beneficiary_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    liability_account_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    payee_instruction_id: Mapped[UUID | None] = mapped_column(Uuid())
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    settlement_id: Mapped[UUID | None] = mapped_column(Uuid())
    journal_id: Mapped[UUID | None] = mapped_column(Uuid())
    journal_number: Mapped[str | None] = mapped_column(String(60))
    consequence_fingerprint: Mapped[str | None] = mapped_column(String(64))
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_by_party_id: Mapped[UUID | None] = mapped_column(Uuid())
    return_reference: Mapped[str | None] = mapped_column(String(255))
    return_reason: Mapped[str | None] = mapped_column(String(500))
    return_value_date: Mapped[date | None] = mapped_column()
    return_bank_account_id: Mapped[UUID | None] = mapped_column(Uuid())
    reversal_id: Mapped[UUID | None] = mapped_column(Uuid())
    reversal_journal_id: Mapped[UUID | None] = mapped_column(Uuid())
    reversal_journal_number: Mapped[str | None] = mapped_column(String(60))
    reversal_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
