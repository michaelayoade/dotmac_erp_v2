"""Held-draft decisions (e2_0027_held_draft_decisions).

One immutable row per held legacy DRAFT journal, recording the single outcome it received from the published
rules: ``posted`` (through the B2 legacy-held path), ``voided`` (through the accounting a2 governed void) or
``queued`` (left DRAFT for Finance to decide one by one). A row is the draft's bridge line: rule id, outcome,
reason code, the evidence the rule matched, the plan binding and who planned and applied it. Rows are never
updated or deleted; the runtime role has no write access at all (only the operator CLI writes).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, uuid_pk
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk

TABLE: Final = "erp_held_draft_decisions"
MONEY = Numeric(20, 6)

OUTCOME_POSTED: Final = "posted"
OUTCOME_VOIDED: Final = "voided"
OUTCOME_QUEUED: Final = "queued"
OUTCOMES: Final = (OUTCOME_POSTED, OUTCOME_VOIDED, OUTCOME_QUEUED)

VOID_DUPLICATE_OF_POSTED_FEE: Final = "duplicate_of_posted_fee"
VOID_ALREADY_POSTED_DUPLICATE: Final = "already_posted_duplicate"
VOID_DUPLICATE_OF_POSTED_FEE_EXCESS: Final = "duplicate_of_posted_fee_excess"
VOID_UNAPPROVED_REVERSAL: Final = "unapproved_reversal_of_live_document"
VOID_REASONS: Final = (
    VOID_DUPLICATE_OF_POSTED_FEE,
    VOID_DUPLICATE_OF_POSTED_FEE_EXCESS,
    VOID_ALREADY_POSTED_DUPLICATE,
    VOID_UNAPPROVED_REVERSAL,
)

_OUTCOMES_SQL = ", ".join(f"'{o}'" for o in OUTCOMES)
_VOIDS_SQL = ", ".join(f"'{r}'" for r in VOID_REASONS)


class HeldDraftDecision(Base):
    __tablename__ = TABLE
    __table_args__ = (
        UniqueConstraint("tenant_id", "journal_id", name=f"uq_{TABLE}_journal"),
        CheckConstraint(f"outcome IN ({_OUTCOMES_SQL})", name=f"ck_{TABLE}_outcome"),
        CheckConstraint("length(btrim(rule_id)) > 0 AND length(btrim(reason_code)) > 0", name=f"ck_{TABLE}_rule"),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_evidence_sha"),
        CheckConstraint("plan_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_plan_sha"),
        CheckConstraint("plan_receipt_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_receipt_sha"),
        CheckConstraint("rules_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_rules_sha"),
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name=f"ck_{TABLE}_evidence_object"),
        # Shape per outcome: a void carries one of the two ruled reason codes; a post names its workflow row.
        CheckConstraint(
            f"(outcome = 'voided' AND reason_code IN ({_VOIDS_SQL}) AND manual_journal_id IS NULL) OR "
            "(outcome = 'posted' AND manual_journal_id IS NOT NULL) OR "
            "(outcome = 'queued' AND manual_journal_id IS NULL)",
            name=f"ck_{TABLE}_shape",
        ),
        CheckConstraint(
            "NOT dual_control OR planned_by <> applied_by",
            name=f"ck_{TABLE}_dual_control",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    # The held DRAFT journal (a plain column, like erp_manual_journals: no cross-lineage foreign key).
    journal_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(60), nullable=False)
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)
    # For a void the ruled reason; for a post the rule's posting reason; for a queue the reason it is queued.
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rules_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # sha256 of the plan receipt file the apply was bound to.
    plan_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manual_journal_id: Mapped[UUID | None] = mapped_column(Uuid())
    dual_control: Mapped[bool] = mapped_column(Boolean, nullable=False)
    planned_by: Mapped[str] = mapped_column(String(120), nullable=False)
    applied_by: Mapped[str] = mapped_column(String(120), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
