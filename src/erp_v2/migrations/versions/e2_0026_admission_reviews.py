"""Reviewed admission (B-1): sha-bound Finance decisions on legacy invoices.

Additive: one new table, no change to any existing object. FORCE RLS; the runtime role may only SELECT; rows
are written by the operator CLI (migration identity) and are append-only (the database refuses UPDATE and
DELETE even for the owner role). The CHECK constraints hold the closed vocabulary, the one-waiver rule
(D-M3), the sha shapes and, where the tenant policy is on, the dual-control separation.

Revision chain (merge order): e2_0023 -> e2_0024 (statutory payees) -> e2_0025 (reserved) -> this revision.
Until those land, down_revision is e2_0023_staff_loan_entries (the current main head); the coordinator
re-chains at merge time (EXPECTED_HEADS, docs and tests change in the same commit).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from erp_v2.importer.admission_waivers import DECISIONS, WAIVABLE_CHECKS

revision = "e2_0026_admission_reviews"
down_revision = "e2_0023_staff_loan_entries"
branch_labels = None
depends_on = None

TABLE = "erp_admission_reviews"


def upgrade() -> None:
    decisions = ", ".join(f"'{d}'" for d in DECISIONS)
    vocabulary = "[" + ", ".join(f'"{c}"' for c in WAIVABLE_CHECKS) + "]"
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("legacy_invoice_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("waived_checks", JSONB(), nullable=False),
        sa.Column("evidence_sha", sa.String(64), nullable=False),
        sa.Column("decision_file_sha", sa.String(64), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("decided_by", sa.String(120), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ias8_account_id", sa.Uuid(), nullable=True),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("dual_control", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name=f"uq_{TABLE}_tenant_id"),
        sa.UniqueConstraint("tenant_id", "legacy_invoice_id", name=f"uq_{TABLE}_invoice"),
        sa.CheckConstraint(f"decision IN ({decisions})", name=f"ck_{TABLE}_decision"),
        sa.CheckConstraint("jsonb_typeof(waived_checks) = 'array'", name=f"ck_{TABLE}_waivers_array"),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(waived_checks) = 'array' "
            "THEN (decision = 'admit_with_waivers') = (jsonb_array_length(waived_checks) = 1) ELSE false END",
            name=f"ck_{TABLE}_one_waiver",
        ),
        sa.CheckConstraint(f"waived_checks <@ '{vocabulary}'::jsonb", name=f"ck_{TABLE}_vocabulary"),
        sa.CheckConstraint("evidence_sha ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_evidence_sha"),
        sa.CheckConstraint("decision_file_sha ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_file_sha"),
        sa.CheckConstraint("snapshot_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_snapshot_sha"),
        sa.CheckConstraint("length(btrim(decided_by)) > 0", name=f"ck_{TABLE}_decided_by"),
        sa.CheckConstraint("length(btrim(operator_ref)) > 0", name=f"ck_{TABLE}_operator"),
        sa.CheckConstraint("(decision = 'ias8_post') = (ias8_account_id IS NOT NULL)", name=f"ck_{TABLE}_ias8_account"),
        sa.CheckConstraint("NOT dual_control OR operator_ref <> decided_by", name=f"ck_{TABLE}_sod"),
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON public.{TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    # A decision is evidence: it is never changed or removed (a different decision needs a new ruling).
    op.execute(
        f"""
        CREATE FUNCTION public.{TABLE}_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'admission review decisions are append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_append_only BEFORE UPDATE OR DELETE ON public.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{TABLE}_append_only()"
    )
    op.execute(f"GRANT SELECT ON public.{TABLE} TO app_user")


def downgrade() -> None:
    # The decisions are permanent admission evidence: never drop them.
    exists = op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{TABLE})")).scalar()  # noqa: S608
    if exists:
        raise RuntimeError(f"refusing to drop {TABLE}: admission review decisions exist")
    op.drop_table(TABLE)
    op.execute(f"DROP FUNCTION IF EXISTS public.{TABLE}_append_only()")
