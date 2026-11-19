"""B-15 tables (e2_0031_billing_fact_postings): one disposition row per (fact, fact version) and the window ledger.

The mapper runs as the RUNTIME role (W4, approved by Michael 2026-11-19), so these are the first runtime-writable
migration-family tables. Triggers bound that: identity and digest are immutable, terminal dispositions never change,
no DELETE.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

POSTINGS_TABLE: Final = "erp_billing_fact_postings"
WINDOWS_TABLE: Final = "erp_billing_fact_windows"

POSTED: Final = "POSTED"
SKIP: Final = "SKIP"
NO_GL: Final = "NO_GL"
REFUSED: Final = "REFUSED"
BLOCKED: Final = "BLOCKED"
WAITING: Final = "WAITING"
DISPOSITIONS: Final = (POSTED, SKIP, NO_GL, REFUSED, BLOCKED, WAITING)
TERMINAL: Final = (POSTED, SKIP, NO_GL)
PENDING: Final = (BLOCKED, WAITING)


class FactPosting(Base):
    __tablename__ = POSTINGS_TABLE
    __table_args__ = (UniqueConstraint("tenant_id", "fact_id", "fact_version", name=f"uq_{POSTINGS_TABLE}_fact"),)

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    fact_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    fact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    effect_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    fact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    billing_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disposition: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(80))
    journal_id: Mapped[UUID | None] = mapped_column(Uuid())
    map_digest: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FactWindow(Base):
    __tablename__ = WINDOWS_TABLE
    __table_args__ = (UniqueConstraint("tenant_id", "occurred_from", name=f"uq_{WINDOWS_TABLE}_from"),)

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    occurred_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rechecked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


VAT_POSITIONS_TABLE: Final = "erp_billing_vat_positions"
VAT_MOVEMENTS_TABLE: Final = "erp_billing_vat_movements"
ORIGIN_NATIVE: Final = "native"
ORIGIN_OPENING: Final = "opening"
# kinds: defer, release, unrelease, credit_reduce, credit_current, closeout, uncloseout, reverse
MOVE_KINDS: Final = (
    "defer",
    "release",
    "unrelease",
    "credit_reduce",
    "credit_current",
    "closeout",
    "uncloseout",
    "reverse",
)


class VatPosition(Base):
    """One output-VAT deferral position per (document, treatment): D_k is what was deferred on 1455 at issue."""

    __tablename__ = VAT_POSITIONS_TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "treatment_code", name=f"uq_{VAT_POSITIONS_TABLE}_key"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    document_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    treatment_code: Mapped[str] = mapped_column(String(80), nullable=False)
    deferral_account_id: Mapped[UUID | None] = mapped_column(Uuid())
    current_account_id: Mapped[UUID | None] = mapped_column(Uuid())
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    deferred: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    open_initial: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class VatMovement(Base):
    """Append-only. ``deferral_delta`` moves R_k (the 1455 balance of the position), ``current_delta`` the 1440 side."""

    __tablename__ = VAT_MOVEMENTS_TABLE
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "fact_id",
            "fact_version",
            "position_id",
            "kind",
            "allocation_ref",
            name=f"uq_{VAT_MOVEMENTS_TABLE}_key",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    position_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    fact_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    fact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    deferral_delta: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    current_delta: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    # Document-level change of the open amount; recorded on ONE carrier position row per fact so it is never doubled.
    open_delta: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=0, server_default="0")
    carrier: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    allocation_ref: Mapped[str] = mapped_column(String(80), nullable=False, default="", server_default="")
    journal_id: Mapped[UUID | None] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
