"""B-15 tables (e2_0031_billing_fact_postings): one disposition row per (fact, fact version) and the window ledger.

The mapper runs as the RUNTIME role (W4, approved by Michael 2026-11-19), so these are the first runtime-writable
migration-family tables. Triggers bound that: identity and digest are immutable, terminal dispositions never change,
no DELETE.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import DateTime, Integer, String, UniqueConstraint, Uuid, func
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
