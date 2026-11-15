"""Payee bank instructions with two-person approval (B8).

Additive: one new table, no change to existing objects. Shape frozen at creation from
erp_v2.payees.models; later model changes need new revisions.

Security posture:
- RLS forced, tenant isolation policy like every other e2 table.
- The runtime role may SELECT and INSERT, and UPDATE only the lifecycle columns. Instruction
  content (bank code, account number, account name, payee) is immutable: a change is a new
  row. No DELETE: history is the audit trail.
- CHECK constraints enforce the two-person rule (approver differs from proposer) and one
  approved / one proposed instruction per payee.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0015_payee_bank_instructions"
down_revision = "e2_0014_manual_journals"
branch_labels = None
depends_on = None

_TABLE = "payee_bank_instructions"
_LIFECYCLE_UPDATE = (
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


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("payee_kind", sa.String(16), nullable=False),
        sa.Column("payee_ref", sa.String(255), nullable=False),
        sa.Column("payee_label", sa.String(200), nullable=False),
        sa.Column("bank_code", sa.String(10), nullable=True),
        sa.Column("bank_name", sa.String(120), nullable=True),
        sa.Column("account_number", sa.String(34), nullable=False),
        sa.Column("account_last4", sa.String(4), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("evidence_ref", sa.String(240), nullable=True),
        sa.Column("source_ref", sa.String(255), nullable=True),
        sa.Column("source_fingerprint", sa.String(64), nullable=True),
        sa.Column("proposed_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(500), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_party_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["superseded_by_id"], [f"{_TABLE}.id"]),
        sa.UniqueConstraint("tenant_id", "id", name="uq_payee_bank_instructions_tenant_id_id"),
        sa.CheckConstraint("payee_kind IN ('supplier', 'employee')", name="ck_payee_bank_instructions_kind"),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'superseded', 'revoked', 'rejected')",
            name="ck_payee_bank_instructions_status",
        ),
        sa.CheckConstraint("origin IN ('manual', 'legacy_import')", name="ck_payee_bank_instructions_origin"),
        sa.CheckConstraint(
            "origin = 'legacy_import' OR proposed_by_party_id IS NOT NULL",
            name="ck_payee_bank_instructions_proposer",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'rejected') OR (approved_at IS NOT NULL "
            "AND decided_by_party_id IS NOT NULL "
            "AND (proposed_by_party_id IS NULL OR decided_by_party_id <> proposed_by_party_id))",
            name="ck_payee_bank_instructions_two_person",
        ),
    )
    op.create_index("ix_payee_bank_instructions_payee", _TABLE, ["tenant_id", "payee_kind", "payee_ref"])
    op.create_index(
        "uq_payee_bank_instructions_one_approved",
        _TABLE,
        ["tenant_id", "payee_kind", "payee_ref"],
        unique=True,
        postgresql_where=sa.text("status = 'approved'"),
    )
    op.create_index(
        "uq_payee_bank_instructions_one_proposed",
        _TABLE,
        ["tenant_id", "payee_kind", "payee_ref"],
        unique=True,
        postgresql_where=sa.text("status = 'proposed'"),
    )
    op.execute(f"ALTER TABLE public.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_TABLE}_tenant_isolation ON public.{_TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    # Runtime: read, insert, and lifecycle-only updates. No DELETE, and no UPDATE on the
    # instruction content columns.
    op.execute(f"GRANT SELECT, INSERT ON public.{_TABLE} TO app_user")
    op.execute(f"GRANT UPDATE ({', '.join(_LIFECYCLE_UPDATE)}) ON public.{_TABLE} TO app_user")


def downgrade() -> None:
    op.drop_table(_TABLE)
