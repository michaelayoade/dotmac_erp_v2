"""App-sync receipt ledger + Academy completion observations (e2 lineage).

The receipt row is the dedupe half of the app-sync receiver seam: it is
written in the same transaction as the consequence rows, keyed by the
envelope idempotency key, and carries the envelope fingerprint so a
same-key/different-body replay is a conflict, never a second apply.
Receipts exist only for accepted deliveries — a rejected consequence
(for example an unmatched employee) stays retryable by design.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
    Date,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk


class AcademySyncReceipt(Base, TimestampMixin):
    __tablename__ = "academy_sync_receipts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_academy_sync_receipts_tenant_id_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_academy_sync_receipts_tenant_key"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source_application: Mapped[str] = mapped_column(String(120), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    destination_ref: Mapped[str] = mapped_column(String(240), nullable=False)


class AcademyLearningCompletion(Base, TimestampMixin):
    __tablename__ = "academy_learning_completions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_academy_learning_completions_tenant_id_id"),
        UniqueConstraint("tenant_id", "source_event_id", name="uq_academy_learning_completions_tenant_event"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_academy_learning_completions_tenant_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "certification_id"],
            ["hr_certifications.tenant_id", "hr_certifications.id"],
            ondelete="SET NULL",
            name="fk_academy_learning_completions_tenant_certification",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    employee_ref: Mapped[str] = mapped_column(String(30), nullable=False)
    academy_course_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    academy_enrollment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    course_title: Mapped[str] = mapped_column(String(200), nullable=False)
    course_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    completed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    certificate_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    certification_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    source_event_id: Mapped[str] = mapped_column(String(240), nullable=False)


ACADEMY_SYNC_TABLES = (
    "academy_sync_receipts",
    "academy_learning_completions",
)

__all__ = [
    "ACADEMY_SYNC_TABLES",
    "AcademyLearningCompletion",
    "AcademySyncReceipt",
]
