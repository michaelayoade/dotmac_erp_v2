"""Payee bank instruction rows (ERP-unique e2 lineage).

One row is one *proposed instruction* for a payee; an approved row is the single
active destination for that payee. Instruction content (bank, account number,
account name) is immutable after insert: the runtime role holds no UPDATE
privilege on those columns (see e2_0014), a change is always a NEW proposal
that supersedes the old row only after a second person approves it.

Security: the account number is PII. It is never part of ``repr``, logs, audit
details or error text; display always goes through ``mask_account``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Final
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from erp_v2.hr.models import _tenant_fk


class PayeeKind(str, enum.Enum):
    SUPPLIER = "supplier"
    EMPLOYEE = "employee"


class InstructionStatus(str, enum.Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    REJECTED = "rejected"


class InstructionOrigin(str, enum.Enum):
    MANUAL = "manual"
    LEGACY_IMPORT = "legacy_import"


PAYEE_BANK_TABLES: Final = ("payee_bank_instructions",)

_STATUSES = ", ".join(f"'{s.value}'" for s in InstructionStatus)
_KINDS = ", ".join(f"'{k.value}'" for k in PayeeKind)

# Columns the runtime role may UPDATE (lifecycle only). Instruction content is
# deliberately absent. Shared with the migration so the two cannot drift.
LIFECYCLE_UPDATE_COLUMNS: Final = (
    "status",
    "decided_by_party_id",
    "decided_at",
    "decision_note",
    "approved_at",
    "revoked_by_party_id",
    "revoked_at",
    "superseded_by_id",
    "superseded_at",
    "updated_at",
)


class PayeeBankInstruction(Base, TimestampMixin):
    __tablename__ = "payee_bank_instructions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_payee_bank_instructions_tenant_id_id"),
        CheckConstraint(f"payee_kind IN ({_KINDS})", name="ck_payee_bank_instructions_kind"),
        CheckConstraint(f"status IN ({_STATUSES})", name="ck_payee_bank_instructions_status"),
        CheckConstraint("origin IN ('manual', 'legacy_import')", name="ck_payee_bank_instructions_origin"),
        # A human proposal always names its proposer; only the importer proposes anonymously.
        CheckConstraint(
            "origin = 'legacy_import' OR proposed_by_party_id IS NOT NULL",
            name="ck_payee_bank_instructions_proposer",
        ),
        # Two-person rule enforced by the database as well as the service: any row that was
        # ever approved carries a decider who is not the proposer.
        CheckConstraint(
            "status IN ('proposed', 'rejected') OR (approved_at IS NOT NULL "
            "AND decided_by_party_id IS NOT NULL "
            "AND (proposed_by_party_id IS NULL OR decided_by_party_id <> proposed_by_party_id))",
            name="ck_payee_bank_instructions_two_person",
        ),
        Index(
            "uq_payee_bank_instructions_one_approved",
            "tenant_id",
            "payee_kind",
            "payee_ref",
            unique=True,
            postgresql_where=text("status = 'approved'"),
            sqlite_where=text("status = 'approved'"),
        ),
        Index(
            "uq_payee_bank_instructions_one_proposed",
            "tenant_id",
            "payee_kind",
            "payee_ref",
            unique=True,
            postgresql_where=text("status = 'proposed'"),
            sqlite_where=text("status = 'proposed'"),
        ),
        Index("ix_payee_bank_instructions_payee", "tenant_id", "payee_kind", "payee_ref"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    payee_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Employee: canonical dashed lowercase mod_people.employees id. Supplier: the
    # supplier reference spelling used by mod_payables.supplier_invoices.supplier_ref.
    payee_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    payee_label: Mapped[str] = mapped_column(String(200), nullable=False)
    bank_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_number: Mapped[str] = mapped_column(String(34), nullable=False)
    account_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=InstructionStatus.PROPOSED.value)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default=InstructionOrigin.MANUAL.value)
    evidence_ref: Mapped[str | None] = mapped_column(String(240), nullable=True)
    # Legacy-migration provenance: which source record, and a sha256 over its bank fields.
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proposed_by_party_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by_party_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_party_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("payee_bank_instructions.id"), nullable=True
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # never include the account number
        return f"<PayeeBankInstruction {self.id} {self.payee_kind} {self.status} ****{self.account_last4}>"
