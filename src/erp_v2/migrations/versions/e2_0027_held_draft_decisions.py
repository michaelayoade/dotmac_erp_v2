"""Held-draft decisions: one immutable bridge line per FY2026 legacy held DRAFT journal (B-2).

Additive: one new table, no change to any existing object. RLS forced; the runtime role gets SELECT only (the
operator CLI writes as the migration role). A trigger refuses UPDATE and DELETE even for the owner role. The
downgrade refuses while any decision exists.

Revision chain: e2_0023 (current main head; the coordinator re-chains at merge: e2_0026 -> this -> e2_0028).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from erp_v2.held_drafts.models import OUTCOMES, TABLE, VOID_REASONS

revision = "e2_0027_held_draft_decisions"
down_revision = "e2_0023_staff_loan_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    outcomes = ", ".join(f"'{o}'" for o in OUTCOMES)
    voids = ", ".join(f"'{r}'" for r in VOID_REASONS)
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.String(60), nullable=False),
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("evidence", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("rules_sha256", sa.String(64), nullable=False),
        sa.Column("plan_sha256", sa.String(64), nullable=False),
        sa.Column("plan_receipt_sha256", sa.String(64), nullable=False),
        sa.Column("manual_journal_id", sa.Uuid(), nullable=True),
        sa.Column("dual_control", sa.Boolean(), nullable=False),
        sa.Column("planned_by", sa.String(120), nullable=False),
        sa.Column("applied_by", sa.String(120), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "journal_id", name=f"uq_{TABLE}_journal"),
        sa.CheckConstraint(f"outcome IN ({outcomes})", name=f"ck_{TABLE}_outcome"),
        sa.CheckConstraint("length(btrim(rule_id)) > 0 AND length(btrim(reason_code)) > 0", name=f"ck_{TABLE}_rule"),
        sa.CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_evidence_sha"),
        sa.CheckConstraint("plan_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_plan_sha"),
        sa.CheckConstraint("plan_receipt_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_receipt_sha"),
        sa.CheckConstraint("rules_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_rules_sha"),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'object'", name=f"ck_{TABLE}_evidence_object"),
        sa.CheckConstraint(
            f"(outcome = 'voided' AND reason_code IN ({voids}) AND manual_journal_id IS NULL) OR "
            "(outcome = 'posted' AND manual_journal_id IS NOT NULL) OR "
            "(outcome = 'queued' AND manual_journal_id IS NULL)",
            name=f"ck_{TABLE}_shape",
        ),
        sa.CheckConstraint("NOT dual_control OR planned_by <> applied_by", name=f"ck_{TABLE}_dual_control"),
    )
    op.create_index(f"ix_{TABLE}_outcome", TABLE, ["tenant_id", "outcome"])
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON public.{TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute(
        f"""
        CREATE FUNCTION public.{TABLE}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'held draft decisions are immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_guard BEFORE UPDATE OR DELETE ON public.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{TABLE}_guard()"
    )
    op.execute(f"GRANT SELECT ON public.{TABLE} TO app_user")


def downgrade() -> None:
    # The decisions are the evidence for every voided or posted held draft: never drop them.
    exists = op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{TABLE})")).scalar()  # noqa: S608
    if exists:
        raise RuntimeError(f"refusing to drop {TABLE}: held draft decisions exist")
    op.drop_table(TABLE)
    op.execute(f"DROP FUNCTION IF EXISTS public.{TABLE}_guard()")
