"""ERP-owned legacy invoice reference (e2 lineage). Append-only for the runtime."""

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import CheckConstraint, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk


class LegacyInvoiceReference(Base, TimestampMixin):
    __tablename__ = "legacy_invoice_references"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_legacy_invoice_references_tenant_id"),
        UniqueConstraint("tenant_id", "legacy_invoice_id", name="uq_legacy_invoice_references_legacy"),
        UniqueConstraint("tenant_id", "invoice_id", name="uq_legacy_invoice_references_invoice"),
        CheckConstraint("legacy_posting_state = 'never_posted'", name="ck_legacy_invoice_references_state"),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    legacy_invoice_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    invoice_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    legacy_posting_state: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    entered_by: Mapped[str] = mapped_column(String(255), nullable=False)
