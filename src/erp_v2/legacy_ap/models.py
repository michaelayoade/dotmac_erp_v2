"""ERP-owned legacy invoice reference (e2 lineage). Append-only for the runtime."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
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


ADMISSION_REVIEWS_TABLE = "erp_admission_reviews"


class AdmissionReview(Base, TimestampMixin):
    """A sha-bound Finance decision on one legacy invoice (reviewed admission, B-1). Written only by
    ``erp_v2.legacy_ap.review_cli``; append-only (no UPDATE, no DELETE); the runtime role may only SELECT."""

    __tablename__ = ADMISSION_REVIEWS_TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_erp_admission_reviews_tenant_id"),
        UniqueConstraint("tenant_id", "legacy_invoice_id", name="uq_erp_admission_reviews_invoice"),
        # The CHECK constraints (decision, vocabulary, one waiver, sha shape, SoD) are PostgreSQL-only and live in
        # the migration e2_0026_admission_reviews.
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    legacy_invoice_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    waived_checks: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    evidence_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_file_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(120), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ias8_account_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    operator_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
