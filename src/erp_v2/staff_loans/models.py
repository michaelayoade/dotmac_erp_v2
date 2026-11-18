"""Staff loan entries (e2_0022_staff_loan_entries).

No Starter owner holds a loan balance, so this ERP table is the per-employee subledger behind the
1410 control account. One row is one governed entry, in one of four kinds:

* ``opening``: a per-employee opening balance from Finance's schedule. Subledger only, no GL (the GL
  already holds the amount).
* ``advance``: a new loan paid from a bank account. Dr 1410 / Cr the bank's cash GL, through the bridge.
* ``adjustment``: Dr/Cr 1410 against an offset account for ONE employee, through the bridge.
* ``control_difference``: a write-off of the measured difference GL(1410) - subledger, Cr 1410 / Dr an
  offset account. It names no employee and does not touch the subledger.

The amount is signed from the loan's point of view: positive raises the employee's balance (Dr 1410),
negative lowers it (Cr 1410). Rows go draft -> approved -> recorded; the approver may be the preparer
(no compulsory second person). The runtime role may update the lifecycle columns only; amounts and
membership are immutable after insert.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

MONEY = Numeric(20, 6)
TABLE: Final = "erp_staff_loan_entries"

KIND_OPENING: Final = "opening"
KIND_ADVANCE: Final = "advance"
KIND_ADJUSTMENT: Final = "adjustment"
KIND_CONTROL_DIFFERENCE: Final = "control_difference"
KINDS: Final = (KIND_OPENING, KIND_ADVANCE, KIND_ADJUSTMENT, KIND_CONTROL_DIFFERENCE)
DRAFT: Final = "draft"
APPROVED: Final = "approved"
RECORDED: Final = "recorded"
STATES: Final = (DRAFT, APPROVED, RECORDED)

# Columns the runtime role may UPDATE (lifecycle only); shared with the migration.
UPDATE_COLUMNS: Final = (
    "state",
    "approved_by_party_id",
    "approved_at",
    "recorded_by_party_id",
    "recorded_at",
    "bank_reference",
    "journal_id",
    "updated_at",
)
_KINDS_SQL = ", ".join(f"'{k}'" for k in KINDS)
_STATES_SQL = ", ".join(f"'{s}'" for s in STATES)


class StaffLoanEntry(Base):
    __tablename__ = TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name=f"uq_{TABLE}_tenant_id_id"),
        UniqueConstraint("tenant_id", "request_id", name=f"uq_{TABLE}_request"),
        ForeignKeyConstraint(
            ["tenant_id", "bank_account_id"],
            ["mod_banking.bank_accounts.tenant_id", "mod_banking.bank_accounts.id"],
            name=f"fk_{TABLE}_bank_account",
        ),
        CheckConstraint(f"kind IN ({_KINDS_SQL})", name=f"ck_{TABLE}_kind"),
        CheckConstraint(f"state IN ({_STATES_SQL})", name=f"ck_{TABLE}_state"),
        CheckConstraint("amount <> 0", name=f"ck_{TABLE}_amount"),
        CheckConstraint("length(btrim(evidence_ref)) > 0", name=f"ck_{TABLE}_evidence"),
        # Shape per kind: what each kind must and must not carry.
        CheckConstraint(
            "(kind = 'control_difference' AND employee_ref IS NULL) OR "
            "(kind <> 'control_difference' AND employee_ref IS NOT NULL AND length(btrim(employee_ref)) > 0)",
            name=f"ck_{TABLE}_employee",
        ),
        CheckConstraint(
            "(kind = 'opening' AND amount > 0 AND bank_account_id IS NULL AND offset_account_id IS NULL) OR "
            "(kind = 'advance' AND amount > 0 AND bank_account_id IS NOT NULL AND offset_account_id IS NULL) OR "
            "(kind = 'adjustment' AND bank_account_id IS NULL AND offset_account_id IS NOT NULL) OR "
            "(kind = 'control_difference' AND amount < 0 AND bank_account_id IS NULL "
            "AND offset_account_id IS NOT NULL)",
            name=f"ck_{TABLE}_kind_shape",
        ),
        CheckConstraint(
            "state <> 'approved' OR (approved_by_party_id IS NOT NULL AND approved_at IS NOT NULL)",
            name=f"ck_{TABLE}_approved",
        ),
        # A recorded row is complete: who, when, the GL journal (every kind except opening) and, for an
        # advance, the bank's evidence reference.
        CheckConstraint(
            "state <> 'recorded' OR (approved_by_party_id IS NOT NULL AND recorded_by_party_id IS NOT NULL "
            "AND recorded_at IS NOT NULL "
            "AND ((kind = 'opening' AND journal_id IS NULL) OR (kind <> 'opening' AND journal_id IS NOT NULL)) "
            "AND (kind <> 'advance' OR (bank_reference IS NOT NULL AND length(btrim(bank_reference)) > 0)))",
            name=f"ck_{TABLE}_recorded",
        ),
        CheckConstraint("bank_reference IS NULL OR kind = 'advance'", name=f"ck_{TABLE}_bank_reference_kind"),
        # Backstop for bank_reference.claim (which serialises and also checks B3/B5): one use per account.
        Index(
            f"uq_{TABLE}_bank_reference",
            "tenant_id",
            "bank_account_id",
            "bank_reference",
            unique=True,
            postgresql_where=text("bank_reference IS NOT NULL"),
        ),
        Index(
            f"uq_{TABLE}_journal",
            "tenant_id",
            "journal_id",
            unique=True,
            postgresql_where=text("journal_id IS NOT NULL"),
        ),
        Index(f"ix_{TABLE}_employee", "tenant_id", "employee_ref", "state"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    # The idempotency key of the prepare command: a repeat replays the same row.
    request_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    # The payroll employee reference (the same string payroll a5 and the payee instructions use).
    employee_ref: Mapped[str | None] = mapped_column(String(240))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    value_date: Mapped[date] = mapped_column(Date, nullable=False)
    bank_account_id: Mapped[UUID | None] = mapped_column(Uuid())
    offset_account_id: Mapped[UUID | None] = mapped_column(Uuid())
    bank_reference: Mapped[str | None] = mapped_column(String(255))
    journal_id: Mapped[UUID | None] = mapped_column(Uuid())
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    prepared_by_party_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by_party_id: Mapped[UUID | None] = mapped_column(Uuid())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_by_party_id: Mapped[UUID | None] = mapped_column(Uuid())
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
