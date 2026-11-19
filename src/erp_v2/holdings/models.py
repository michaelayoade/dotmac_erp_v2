"""``erp_migration_holdings`` (e2_0030): the append-only ledger of holding movements, and the shared constants.

One row per (exception item, movement). ``amount`` is signed: positive is a DEBIT on the holding account. A
``reclass`` is the flip movement of one itemised control difference; a ``release`` (paired with an owner receipt)
or a ``writeoff`` (full outstanding, with evidence) moves it back out. Σ amount over an exception is its
outstanding; the table trigger keeps it the same sign as the reclass, or zero.

Runtime role: SELECT only. The operator CLIs write as the migration role.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid5

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk
from erp_v2.migration_ledger.models import EXCEPTIONS_TABLE

HOLDINGS_TABLE: Final = "erp_migration_holdings"
MONEY = Numeric(20, 6)

KIND_RECLASS: Final = "reclass"
KIND_RELEASE: Final = "release"
KIND_WRITEOFF: Final = "writeoff"
HOLDING_KINDS: Final = (KIND_RECLASS, KIND_RELEASE, KIND_WRITEOFF)

PREPAYMENT: Final = "prepayment"
# The tie codes a legacy difference can be held on (2000 AP, 1400 AR, 2040 payroll, 2045 claims, 1410 staff loans).
HOLDING_CODES: Final = ("1400", "2000", "2040", "2045", "1410", PREPAYMENT)

# Posting-bridge identities: owner ``erp.migration``; ONLY ``erp_v2.holdings`` builds these kinds (a test greps).
OWNER: Final = "erp.migration"
KIND_HOLDING_POST: Final = "migration_holding"
KIND_RELEASE_POST: Final = "holding_release"
KIND_WRITEOFF_POST: Final = "holding_writeoff"
KIND_T1_POST: Final = "ar_opening_credit"
POSTING_KINDS: Final = (KIND_HOLDING_POST, KIND_RELEASE_POST, KIND_WRITEOFF_POST, KIND_T1_POST)

# uuid5 namespaces (constants; never random) for the once-only document identities.
HOLDING_NS: Final = UUID("5b0d1f0e-3f0a-5c7e-9b6a-0b14c0de0001")
T1_NS: Final = UUID("5b0d1f0e-3f0a-5c7e-9b6a-0b14c0de0002")
REL_NS: Final = UUID("5b0d1f0e-3f0a-5c7e-9b6a-0b14c0de0003")
WRITEOFF_NS: Final = UUID("5b0d1f0e-3f0a-5c7e-9b6a-0b14c0de0004")

SOURCE_KIND: Final = "migration_holdings"  # the register source for the items


def holding_document_id(window_id: str, code: str, currency: str) -> str:
    return str(uuid5(HOLDING_NS, f"{window_id}:{code}:{currency}"))


def t1_document_id(window_id: str, currency: str) -> str:
    return str(uuid5(T1_NS, f"{window_id}:{currency}"))


def release_document_id(exception_id: UUID | str, n: int) -> str:
    return str(uuid5(REL_NS, f"{exception_id}:{n}"))


def writeoff_document_id(exception_id: UUID | str) -> str:
    return str(uuid5(WRITEOFF_NS, f"{exception_id}"))


class Holding(Base):
    __tablename__ = HOLDINGS_TABLE
    __table_args__ = (UniqueConstraint("tenant_id", "journal_id", "exception_id", name=f"uq_{HOLDINGS_TABLE}_journal"),)

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    exception_id: Mapped[UUID] = mapped_column(Uuid(), ForeignKey(f"{EXCEPTIONS_TABLE}.id"), nullable=False)
    control_code: Mapped[str] = mapped_column(String(30), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    holding_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    journal_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    journal_source_document_id: Mapped[str] = mapped_column(String(120), nullable=False)
    plan_sha256: Mapped[str | None] = mapped_column(String(64))
    owner_receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    planned_by: Mapped[str] = mapped_column(String(120), nullable=False)
    applied_by: Mapped[str] = mapped_column(String(120), nullable=False)
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
