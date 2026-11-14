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

revision = "e2_0014_legacy_invoice_references"
down_revision = "e2_0013_banking_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legacy_invoice_references",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("legacy_invoice_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("legacy_posting_state", sa.String(20), nullable=False),
        sa.Column("evidence_ref", sa.String(255), nullable=False),
        sa.Column("entered_by", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_legacy_invoice_references_tenant_id"),
        sa.UniqueConstraint("tenant_id", "legacy_invoice_id", name="uq_legacy_invoice_references_legacy"),
        sa.UniqueConstraint("tenant_id", "invoice_id", name="uq_legacy_invoice_references_invoice"),
        sa.CheckConstraint("legacy_posting_state = 'never_posted'", name="ck_legacy_invoice_references_state"),
    )
    op.execute("ALTER TABLE public.legacy_invoice_references ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.legacy_invoice_references FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY legacy_invoice_references_tenant_isolation ON public.legacy_invoice_references "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute("GRANT SELECT, INSERT ON public.legacy_invoice_references TO app_user")


def downgrade() -> None:
    op.drop_table("legacy_invoice_references")
