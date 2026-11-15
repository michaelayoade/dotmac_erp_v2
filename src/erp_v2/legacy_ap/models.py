"""ERP-owned legacy invoice reference (e2 lineage). Append-only for the runtime."""

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk


class LegacyInvoiceReference(Base, TimestampMixin):
    __tablename__ = "legacy_invoice_references"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_legacy_invoice_references_tenant_id"),
        UniqueConstraint(
            "tenant_id", "legacy_invoice_id", "attempt", name="uq_legacy_invoice_references_legacy_attempt"
        ),
        CheckConstraint("attempt >= 1", name="ck_legacy_invoice_references_attempt"),
        UniqueConstraint("tenant_id", "invoice_id", name="uq_legacy_invoice_references_invoice"),
        CheckConstraint("legacy_posting_state = 'never_posted'", name="ck_legacy_invoice_references_state"),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    legacy_invoice_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    invoice_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    legacy_posting_state: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    entered_by: Mapped[str] = mapped_column(String(255), nullable=False)


class LegacyNeverPostedCohort(Base, TimestampMixin):
    __tablename__ = "legacy_never_posted_cohorts"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_legacy_never_posted_cohorts_tenant"),
        UniqueConstraint("tenant_id", "id", name="uq_legacy_never_posted_cohorts_tenant_id"),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    cohort_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    census_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    loaded_by: Mapped[str] = mapped_column(String(255), nullable=False)


class LegacyNeverPostedMember(Base):
    __tablename__ = "legacy_never_posted_members"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "legacy_invoice_id", name="pk_legacy_never_posted_members"),
        ForeignKeyConstraint(
            ["tenant_id", "cohort_id"],
            ["legacy_never_posted_cohorts.tenant_id", "legacy_never_posted_cohorts.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    cohort_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    legacy_invoice_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
