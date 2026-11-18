"""Cash GL re-point requests (e2_0020).

One row is a governed request to change which GL account a bank account's ``cash_account_ref``
names (the per-bank cash GL split, register ruling "Split the GL accounts"). The row is
insert-only evidence: the runtime role may update the decision columns only, and a request
becomes ``approved`` in the same transaction that changes the bank account, so a row never claims
a change that did not happen.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk


class RepointStatus(str, enum.Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


TABLE: Final = "erp_cash_account_repoints"
# Columns the runtime role may UPDATE (decision only); shared with the migration.
DECISION_UPDATE_COLUMNS: Final = (
    "status",
    "decided_by_party_id",
    "decided_at",
    "decision_note",
    "applied_ref",
    "updated_at",
)
_STATUSES = ", ".join(f"'{s.value}'" for s in RepointStatus)


class CashAccountRepoint(Base):
    __tablename__ = TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name=f"uq_{TABLE}_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "bank_account_id"],
            ["mod_banking.bank_accounts.tenant_id", "mod_banking.bank_accounts.id"],
            name=f"fk_{TABLE}_bank_account",
        ),
        CheckConstraint(f"status IN ({_STATUSES})", name=f"ck_{TABLE}_status"),
        CheckConstraint("length(btrim(evidence_ref)) > 0", name=f"ck_{TABLE}_evidence"),
        CheckConstraint(
            "status = 'proposed' OR (decided_by_party_id IS NOT NULL AND decided_at IS NOT NULL)",
            name=f"ck_{TABLE}_decided",
        ),
        # The database enforces the two-person rule as well as the service.
        CheckConstraint(
            "NOT dual_control OR (status <> 'approved' OR "
            "(decided_by_party_id <> proposed_by_party_id AND applied_ref IS NOT NULL))",
            name=f"ck_{TABLE}_two_person",
        ),
        CheckConstraint("status <> 'approved' OR applied_ref IS NOT NULL", name=f"ck_{TABLE}_two_person_integrity"),
        Index(
            f"uq_{TABLE}_one_proposed",
            "tenant_id",
            "bank_account_id",
            unique=True,
            postgresql_where=text("status = 'proposed'"),
        ),
        Index(f"ix_{TABLE}_account", "tenant_id", "to_account_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    # Stamped from the tenant policy at creation; DB default true keeps pre-e2_0022 rows strict (e2_0022).
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    bank_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    # The bank account's cash_account_ref at proposal time (verified again at approval).
    from_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    to_account_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    to_account_code: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    proposed_by_party_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by_party_id: Mapped[UUID | None] = mapped_column(Uuid())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))
    # The cash_account_ref value written on approval (the canonical account id string).
    applied_ref: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
