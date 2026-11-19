"""AR opening admission receipts (e2_0029_ar_opening_admissions).

One immutable row per partition (billing account x currency) the opening import admitted: the billing receipt
id, the opening posting group (0 GL: no billing-owned journal exists for it), the command digest, the manifest
fingerprint, the census and snapshot shas and the sealed window. Rows are never updated or deleted; the runtime role
has SELECT only (the operator CLI writes as the migration role).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

TABLE: Final = "erp_ar_opening_admissions"
WRITER_ROLE: Final = "erp_billing_opening_writer"
_SHA = "'^[0-9a-f]{64}$'"


class ArOpeningAdmission(Base):
    __tablename__ = TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "billing_account_id", name=f"uq_{TABLE}_account"),
        UniqueConstraint("tenant_id", "billing_receipt_id", name=f"uq_{TABLE}_receipt"),
        CheckConstraint("length(btrim(source_account_key)) > 0", name=f"ck_{TABLE}_key"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name=f"ck_{TABLE}_currency"),
        CheckConstraint(f"command_digest ~ {_SHA}", name=f"ck_{TABLE}_command_digest"),
        CheckConstraint(f"manifest_fingerprint ~ {_SHA}", name=f"ck_{TABLE}_manifest"),
        CheckConstraint(f"census_sha256 ~ {_SHA}", name=f"ck_{TABLE}_census"),
        CheckConstraint(f"snapshot_sha256 ~ {_SHA}", name=f"ck_{TABLE}_snapshot"),
        CheckConstraint(f"plan_sha256 ~ {_SHA}", name=f"ck_{TABLE}_plan"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    # Legacy customer key and the billing account minted for it (plain columns: no cross-lineage foreign key).
    source_account_key: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    billing_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    billing_receipt_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    posting_group_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    window_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    command_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    census_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_by: Mapped[str] = mapped_column(String(120), nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
