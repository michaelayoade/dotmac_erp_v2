"""Legacy invoice references for ERP-entered supplier invoices (legacy AP re-entry).

One row binds a NEW ERP supplier invoice to the legacy invoice it re-enters. It is
append-only for the runtime (SELECT, INSERT). There is deliberately no foreign key
into mod_payables: depending on a payables revision would absorb its stamped head
(the historical admission operator asserts it); the service inserts both rows in one
savepoint instead.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0014_legacy_invoice_refs"
down_revision = "e2_0013_banking_observations"
branch_labels = None
depends_on = None

_TABLES = ("legacy_invoice_references", "legacy_never_posted_cohorts", "legacy_never_posted_members")


def upgrade() -> None:
    op.create_table(
        "legacy_invoice_references",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("legacy_invoice_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("legacy_posting_state", sa.String(20), nullable=False),
        sa.Column("evidence_ref", sa.String(255), nullable=False),
        sa.Column("entered_by", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_legacy_invoice_references_tenant_id"),
        # A new attempt is allowed only when every earlier attempt's invoice is VOID (service rule).
        sa.UniqueConstraint(
            "tenant_id", "legacy_invoice_id", "attempt", name="uq_legacy_invoice_references_legacy_attempt"
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_legacy_invoice_references_attempt"),
        sa.UniqueConstraint("tenant_id", "invoice_id", name="uq_legacy_invoice_references_invoice"),
        sa.CheckConstraint("legacy_posting_state = 'never_posted'", name="ck_legacy_invoice_references_state"),
    )
    # The census-classified never-posted cohort, loaded once per tenant by the operator
    # CLI (sha256-bound). Re-entry is off for a tenant until its cohort exists.
    op.create_table(
        "legacy_never_posted_cohorts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("cohort_sha256", sa.String(64), nullable=False),
        sa.Column("census_ref", sa.String(255), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("loaded_by", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", name="uq_legacy_never_posted_cohorts_tenant"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_legacy_never_posted_cohorts_tenant_id"),
    )
    op.create_table(
        "legacy_never_posted_members",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_id", sa.Uuid(), nullable=False),
        sa.Column("legacy_invoice_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "legacy_invoice_id", name="pk_legacy_never_posted_members"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cohort_id"],
            ["legacy_never_posted_cohorts.tenant_id", "legacy_never_posted_cohorts.id"],
        ),
    )
    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
            "USING (tenant_id = public.app_current_tenant_id()) "
            "WITH CHECK (tenant_id = public.app_current_tenant_id())"
        )
    op.execute("GRANT SELECT, INSERT ON public.legacy_invoice_references TO app_user")
    # Cohort tables are written only by the operator (migration identity); runtime reads.
    op.execute("GRANT SELECT ON public.legacy_never_posted_cohorts TO app_user")
    op.execute("GRANT SELECT ON public.legacy_never_posted_members TO app_user")


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        if bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{table})")):  # noqa: S608
            raise RuntimeError(f"{table} holds permanent re-entry evidence; downgrade requires forward correction")
    op.drop_table("legacy_never_posted_members")
    op.drop_table("legacy_never_posted_cohorts")
    op.drop_table("legacy_invoice_references")
