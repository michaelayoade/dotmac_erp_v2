"""ERP-owned maker-checker state for manual journals (e2 lineage).

Accounting a1 owns the journal itself (DRAFT -> POSTED -> REVERSED) and records
no preparer or approver. This table records who prepared, requested, approved and
reversed each journal the ERP moves, and the database refuses a self-approval even
if the service were bypassed. The journal id is a plain column (no foreign key into
``mod_accounting``) so this lineage needs no cross-lineage migration dependency;
the service resolves it inside the tenant before every transition.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

ORIGIN_MANUAL = "manual"
ORIGIN_LEGACY_HELD = "legacy_held"
STATE_PREPARED = "PREPARED"
STATE_POST_REQUESTED = "POST_REQUESTED"
STATE_POSTED = "POSTED"
STATE_REJECTED = "REJECTED"
STATE_REVERSAL_REQUESTED = "REVERSAL_REQUESTED"
STATE_REVERSED = "REVERSED"
STATES = (
    STATE_PREPARED,
    STATE_POST_REQUESTED,
    STATE_POSTED,
    STATE_REJECTED,
    STATE_REVERSAL_REQUESTED,
    STATE_REVERSED,
)

_PARTY = "parties.id"


def _party(nullable: bool = True) -> Mapped[UUID | None]:
    return mapped_column(Uuid(), ForeignKey(_PARTY), nullable=nullable)


class ManualJournal(Base, TimestampMixin):
    __tablename__ = "erp_manual_journals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_erp_manual_journals_tenant_id"),
        UniqueConstraint("tenant_id", "journal_id", name="uq_erp_manual_journals_journal"),
        CheckConstraint("origin IN ('manual', 'legacy_held')", name="ck_erp_manual_journals_origin"),
        CheckConstraint(
            "state IN ('PREPARED', 'POST_REQUESTED', 'POSTED', 'REJECTED', 'REVERSAL_REQUESTED', 'REVERSED')",
            name="ck_erp_manual_journals_state",
        ),
        CheckConstraint("length(btrim(evidence_ref)) > 0", name="ck_erp_manual_journals_evidence"),
        # Post SoD: the approver differs from the preparer AND the requester, and
        # an approval cannot exist without a request.
        CheckConstraint(
            "NOT dual_control OR (approved_by_party_id IS NULL OR (requested_by_party_id IS NOT NULL "
            "AND approved_by_party_id <> prepared_by_party_id "
            "AND approved_by_party_id <> requested_by_party_id))",
            name="ck_erp_manual_journals_post_sod",
        ),
        # Reversal SoD: approver differs from the reversal requester and the preparer.
        CheckConstraint(
            "NOT dual_control OR (reversal_approved_by_party_id IS NULL OR (reversal_requested_by_party_id IS NOT NULL "
            "AND reversal_approved_by_party_id <> reversal_requested_by_party_id "
            "AND reversal_approved_by_party_id <> prepared_by_party_id))",
            name="ck_erp_manual_journals_reversal_sod",
        ),
        CheckConstraint(
            "state NOT IN ('POST_REQUESTED', 'POSTED', 'REVERSAL_REQUESTED', 'REVERSED') "
            "OR (requested_by_party_id IS NOT NULL AND requested_at IS NOT NULL)",
            name="ck_erp_manual_journals_requested",
        ),
        CheckConstraint(
            "state NOT IN ('POSTED', 'REVERSAL_REQUESTED', 'REVERSED') "
            "OR (approved_by_party_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_erp_manual_journals_posted",
        ),
        CheckConstraint(
            "state NOT IN ('REVERSAL_REQUESTED', 'REVERSED') "
            "OR (reversal_requested_by_party_id IS NOT NULL AND reversal_reason IS NOT NULL "
            "AND reversal_posting_date IS NOT NULL)",
            name="ck_erp_manual_journals_reversal_requested",
        ),
        CheckConstraint(
            "state <> 'REVERSED' OR (reversal_approved_by_party_id IS NOT NULL AND reversal_journal_id IS NOT NULL)",
            name="ck_erp_manual_journals_reversed",
        ),
        CheckConstraint(
            "state <> 'REJECTED' OR (rejected_by_party_id IS NOT NULL AND rejection_reason IS NOT NULL)",
            name="ck_erp_manual_journals_rejected",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    journal_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    # Stamped from the tenant policy at creation; DB default true keeps pre-e2_0022 rows strict (e2_0022).
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    # Mandatory reference to the supporting evidence (e.g. the Sub report id of an
    # Option B summary journal, or the Finance review reference for a held legacy journal).
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    # Legacy provenance snapshot (source owner:kind:id:version) for held journals.
    legacy_source_key: Mapped[str | None] = mapped_column(String(480))
    prepared_by_party_id: Mapped[UUID] = _party(nullable=False)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_by_party_id: Mapped[UUID | None] = _party()
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_party_id: Mapped[UUID | None] = _party()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by_party_id: Mapped[UUID | None] = _party()
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    reversal_requested_by_party_id: Mapped[UUID | None] = _party()
    reversal_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_reason: Mapped[str | None] = mapped_column(Text)
    reversal_posting_date: Mapped[date | None] = mapped_column(Date)
    reversal_approved_by_party_id: Mapped[UUID | None] = _party()
    reversal_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_journal_id: Mapped[UUID | None] = mapped_column(Uuid())
