"""B-9 tables (e2_0028_migration_ledger): the exceptions register, sha-bound bridge versions, opening/IAS 8 provenance.

* ``erp_migration_exceptions``: one row per (class, legacy key); open or resolved with a terminal outcome and an
  evidence sha. Rows are never deleted; identity columns never change.
* ``erp_bridge_versions``: one row per bridge version. The body is immutable and sha-bound; the only permitted
  UPDATE is the one-time signature (signed_by, signed_at); a signed version is immutable and never deleted.
* ``erp_opening_journals``: provenance of each system-built opening and IAS 8 journal (insert-only).

Runtime role: SELECT only. The operator CLIs write as the migration role.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

EXCEPTIONS_TABLE: Final = "erp_migration_exceptions"
BRIDGE_TABLE: Final = "erp_bridge_versions"
OPENING_TABLE: Final = "erp_opening_journals"
MONEY = Numeric(20, 6)

STATE_OPEN: Final = "open"
STATE_RESOLVED: Final = "resolved"
# "Resolved" never means parked: posted, admitted, voided with a reason, written off, or confirmed no-action.
TERMINAL_OUTCOMES: Final = ("posted", "admitted", "voided", "written_off", "no_action")
KIND_OPENING: Final = "opening"
KIND_IAS8: Final = "ias8"
OPENING_KINDS: Final = (KIND_OPENING, KIND_IAS8)

_JSON = JSON().with_variant(JSONB(), "postgresql")


class MigrationException(Base):
    __tablename__ = EXCEPTIONS_TABLE
    __table_args__ = (UniqueConstraint("tenant_id", "class_code", "legacy_key", name=f"uq_{EXCEPTIONS_TABLE}_key"),)

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    class_code: Mapped[str] = mapped_column(String(20), nullable=False)
    legacy_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    terminal_outcome: Mapped[str | None] = mapped_column(String(30))
    evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict] = mapped_column(_JSON, nullable=False)
    reopened_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BridgeVersion(Base):
    __tablename__ = BRIDGE_TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name=f"uq_{BRIDGE_TABLE}_version"),
        UniqueConstraint("tenant_id", "sha256", name=f"uq_{BRIDGE_TABLE}_sha"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[dict] = mapped_column(_JSON, nullable=False)
    itemised_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    signed_by: Mapped[str | None] = mapped_column(String(120))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OpeningJournal(Base):
    __tablename__ = OPENING_TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "item_ref", name=f"uq_{OPENING_TABLE}_item"),
        UniqueConstraint("tenant_id", "journal_id", name=f"uq_{OPENING_TABLE}_journal"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    item_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    journal_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    account_code: Mapped[str | None] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_by: Mapped[str] = mapped_column(String(120), nullable=False)
    applied_by: Mapped[str] = mapped_column(String(120), nullable=False)
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
