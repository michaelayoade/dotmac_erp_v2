"""One policy row per tenant (e2_0022). Runtime SELECT only; written by the operator CLI."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Base, Tenant
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Uuid, false, func
from sqlalchemy.orm import Mapped, mapped_column

TABLE = "erp_governance_policies"


class GovernancePolicy(Base):
    __tablename__ = TABLE
    __table_args__ = (
        CheckConstraint("length(trim(set_by)) > 0", name=f"ck_{TABLE}_set_by"),
        CheckConstraint("length(trim(evidence_ref)) > 0", name=f"ck_{TABLE}_evidence"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"), primary_key=True
    )
    # False (the default, and what a missing row means): every coded two-person check is skipped.
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    set_by: Mapped[str] = mapped_column(String(120), nullable=False)
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
