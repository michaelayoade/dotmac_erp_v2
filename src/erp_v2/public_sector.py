"""Public sector / IPSAS commitment control — ERP-unique slice 10.

Funds -> appropriations -> commitments with derived availability:
an appropriation can never be over-committed, a commitment cannot
obligate or expend beyond its committed/obligated stages, and
virements move budget only as balanced paired adjustments applied
exactly once. Journal consequences stay with dotmac-accounting via
opaque refs; allotment/intercompany detail is rehearsal residue.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
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
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from erp_v2.hr.models import _tenant_fk


class CommitmentStatus(str, enum.Enum):
    COMMITTED = "committed"
    OBLIGATED = "obligated"
    EXPENDED = "expended"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class Fund(Base, TimestampMixin):
    __tablename__ = "ps_funds"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ps_funds_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_ps_funds_tenant_code"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_fund_id"],
            ["ps_funds.tenant_id", "ps_funds.id"],
            ondelete="RESTRICT",
            name="fk_ps_funds_tenant_parent",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fund_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    is_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    parent_fund_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


class Appropriation(Base, TimestampMixin):
    __tablename__ = "ps_appropriations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ps_appropriations_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_ps_appropriations_tenant_code"),
        ForeignKeyConstraint(
            ["tenant_id", "fund_id"],
            ["ps_funds.tenant_id", "ps_funds.id"],
            ondelete="RESTRICT",
            name="fk_ps_appropriations_tenant_fund",
        ),
        CheckConstraint("status IN ('draft','approved','closed')", name="ck_ps_appropriations_status"),
        Index("ix_ps_appropriations_tenant_fund", "tenant_id", "fund_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    fund_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    fiscal_year_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    appropriation_type: Mapped[str] = mapped_column(String(40), nullable=False, default="original")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    approved_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    revised_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    account_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class Commitment(Base, TimestampMixin):
    __tablename__ = "ps_commitments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ps_commitments_tenant_id_id"),
        UniqueConstraint("tenant_id", "number", name="uq_ps_commitments_tenant_number"),
        ForeignKeyConstraint(
            ["tenant_id", "appropriation_id"],
            ["ps_appropriations.tenant_id", "ps_appropriations.id"],
            ondelete="RESTRICT",
            name="fk_ps_commitments_tenant_appropriation",
        ),
        CheckConstraint(
            "status IN ('committed','obligated','expended','cancelled','closed')",
            name="ck_ps_commitments_status",
        ),
        CheckConstraint("obligated_amount <= committed_amount", name="ck_ps_commitments_obligated"),
        CheckConstraint("expended_amount <= obligated_amount", name="ck_ps_commitments_expended"),
        Index("ix_ps_commitments_tenant_appropriation", "tenant_id", "appropriation_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    commitment_type: Mapped[str] = mapped_column(String(40), nullable=False, default="expenditure")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="committed")
    appropriation_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    committed_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    obligated_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    expended_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    cancelled_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    commitment_date: Mapped[date] = mapped_column(Date, nullable=False)
    obligation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expenditure_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class CommitmentLine(Base, TimestampMixin):
    __tablename__ = "ps_commitment_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ps_commitment_lines_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "commitment_id"],
            ["ps_commitments.tenant_id", "ps_commitments.id"],
            ondelete="CASCADE",
            name="fk_ps_commitment_lines_tenant_commitment",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    commitment_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    account_ref: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    committed_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    obligated_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    expended_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal(0), server_default="0"
    )


class Virement(Base, TimestampMixin):
    __tablename__ = "ps_virements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ps_virements_tenant_id_id"),
        UniqueConstraint("tenant_id", "number", name="uq_ps_virements_tenant_number"),
        ForeignKeyConstraint(
            ["tenant_id", "from_appropriation_id"],
            ["ps_appropriations.tenant_id", "ps_appropriations.id"],
            ondelete="RESTRICT",
            name="fk_ps_virements_tenant_from",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "to_appropriation_id"],
            ["ps_appropriations.tenant_id", "ps_appropriations.id"],
            ondelete="RESTRICT",
            name="fk_ps_virements_tenant_to",
        ),
        CheckConstraint("status IN ('draft','approved','applied','rejected')", name="ck_ps_virements_status"),
        CheckConstraint("amount > 0", name="ck_ps_virements_amount"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    from_appropriation_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    to_appropriation_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


PUBLIC_SECTOR_TABLES = (
    "ps_funds",
    "ps_appropriations",
    "ps_commitments",
    "ps_commitment_lines",
    "ps_virements",
)


class PublicSectorError(RuntimeError):
    """A commitment-control rule refusal."""


def appropriation_available(session: Session, *, tenant_id: UUID, appropriation_id: UUID) -> Decimal:
    """Revised budget minus live commitments (committed, net of cancels)."""

    appropriation = session.scalar(
        select(Appropriation).where(Appropriation.tenant_id == tenant_id, Appropriation.id == appropriation_id)
    )
    if appropriation is None:
        raise PublicSectorError("appropriation not found")
    used = session.scalar(
        select(func.coalesce(func.sum(Commitment.committed_amount - Commitment.cancelled_amount), 0)).where(
            Commitment.tenant_id == tenant_id,
            Commitment.appropriation_id == appropriation_id,
            Commitment.status != CommitmentStatus.CANCELLED.value,
        )
    ) or Decimal(0)
    return appropriation.revised_amount - Decimal(used)


def create_commitment(
    session: Session,
    *,
    tenant_id: UUID,
    number: str,
    appropriation_id: UUID,
    commitment_date: date,
    lines: list[tuple[str, Decimal]],
    commitment_type: str = "expenditure",
    source_type: str | None = None,
    source_ref: str | None = None,
) -> Commitment:
    """Create a commitment whose lines cannot exceed available budget."""

    if not lines:
        raise PublicSectorError("a commitment needs at least one line")
    appropriation = session.scalar(
        select(Appropriation).where(Appropriation.tenant_id == tenant_id, Appropriation.id == appropriation_id)
    )
    if appropriation is None:
        raise PublicSectorError("appropriation not found")
    if appropriation.status != "approved":
        raise PublicSectorError("commitments require an approved appropriation")
    total = sum((amount for _, amount in lines), Decimal(0))
    if total <= 0:
        raise PublicSectorError("commitment total must be positive")
    available = appropriation_available(session, tenant_id=tenant_id, appropriation_id=appropriation_id)
    if total > available:
        raise PublicSectorError(f"commitment {total} exceeds available {available}")
    commitment = Commitment(
        tenant_id=tenant_id,
        number=number,
        commitment_type=commitment_type,
        appropriation_id=appropriation_id,
        source_type=source_type,
        source_ref=source_ref,
        currency_code=appropriation.currency_code,
        committed_amount=total,
        commitment_date=commitment_date,
    )
    session.add(commitment)
    session.flush()
    for index, (account_ref, amount) in enumerate(lines, start=1):
        session.add(
            CommitmentLine(
                tenant_id=tenant_id,
                commitment_id=commitment.id,
                line_number=index,
                account_ref=account_ref,
                committed_amount=amount,
            )
        )
    session.flush()
    return commitment


def stage_commitment(
    session: Session,
    *,
    tenant_id: UUID,
    commitment_id: UUID,
    obligate: Decimal | None = None,
    expend: Decimal | None = None,
    cancel: Decimal | None = None,
    on: date | None = None,
) -> Commitment:
    """Advance obligated/expended/cancelled totals within their ceilings."""

    commitment = session.scalar(
        select(Commitment).where(Commitment.tenant_id == tenant_id, Commitment.id == commitment_id)
    )
    if commitment is None:
        raise PublicSectorError("commitment not found")
    if commitment.status in (CommitmentStatus.CANCELLED.value, CommitmentStatus.CLOSED.value):
        raise PublicSectorError(f"commitment is {commitment.status!r} (terminal)")
    if cancel is not None:
        if (
            cancel <= 0
            or commitment.cancelled_amount + cancel > commitment.committed_amount - commitment.expended_amount
        ):
            raise PublicSectorError("cancel amount exceeds the unexpended commitment")
        commitment.cancelled_amount += cancel
        if commitment.cancelled_amount == commitment.committed_amount:
            commitment.status = CommitmentStatus.CANCELLED.value
    if obligate is not None:
        if (
            obligate <= 0
            or commitment.obligated_amount + obligate > commitment.committed_amount - commitment.cancelled_amount
        ):
            raise PublicSectorError("obligation exceeds the live commitment")
        commitment.obligated_amount += obligate
        commitment.obligation_date = on or commitment.obligation_date
        if commitment.status == CommitmentStatus.COMMITTED.value:
            commitment.status = CommitmentStatus.OBLIGATED.value
    if expend is not None:
        if expend <= 0 or commitment.expended_amount + expend > commitment.obligated_amount:
            raise PublicSectorError("expenditure exceeds the obligated amount")
        commitment.expended_amount += expend
        commitment.expenditure_date = on or commitment.expenditure_date
        if (
            commitment.expended_amount == commitment.obligated_amount
            and commitment.obligated_amount == commitment.committed_amount - commitment.cancelled_amount
        ):
            commitment.status = CommitmentStatus.EXPENDED.value
    session.flush()
    return commitment


def apply_virement(session: Session, *, tenant_id: UUID, virement_id: UUID) -> Virement:
    """Apply an approved virement exactly once as balanced adjustments."""

    virement = session.scalar(select(Virement).where(Virement.tenant_id == tenant_id, Virement.id == virement_id))
    if virement is None:
        raise PublicSectorError("virement not found")
    if virement.status != "approved":
        raise PublicSectorError(f"virement is {virement.status!r}, not approved")
    if virement.from_appropriation_id == virement.to_appropriation_id:
        raise PublicSectorError("virement source and destination are the same")
    available = appropriation_available(session, tenant_id=tenant_id, appropriation_id=virement.from_appropriation_id)
    if virement.amount > available:
        raise PublicSectorError(f"virement {virement.amount} exceeds source availability {available}")
    source = session.scalar(select(Appropriation).where(Appropriation.id == virement.from_appropriation_id))
    target = session.scalar(select(Appropriation).where(Appropriation.id == virement.to_appropriation_id))
    if source is None or target is None:  # pragma: no cover - FK guarantees
        raise PublicSectorError("appropriation not found")
    if source.currency_code != virement.currency_code or target.currency_code != virement.currency_code:
        raise PublicSectorError("virement currency does not match both appropriations")
    source.revised_amount -= virement.amount
    target.revised_amount += virement.amount
    virement.status = "applied"
    virement.applied_at = datetime.now(timezone.utc)
    session.flush()
    return virement


__all__ = [
    "PUBLIC_SECTOR_TABLES",
    "Appropriation",
    "Commitment",
    "CommitmentLine",
    "CommitmentStatus",
    "Fund",
    "PublicSectorError",
    "Virement",
    "apply_virement",
    "appropriation_available",
    "create_commitment",
    "stage_commitment",
]
