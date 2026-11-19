"""TEMPORARY STUB of B-16's e2_0032_ar_producer, only the two tables B-15 rev 2 reads (erp_ar_line_accounts,
erp_ar_receipts), with the columns of B16-SPEC 3.7. DROP THIS FILE at re-chain: B-16's real e2_0032 replaces it
(same revision id and down_revision).
"""

import sqlalchemy as sa
from alembic import op

revision = "e2_0032_ar_producer"
down_revision = "e2_0031_billing_fact_postings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "erp_ar_line_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("billing_document_id", sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("obligation_id", sa.Uuid(), nullable=True),
        sa.Column("revenue_account_id", sa.Uuid(), nullable=False),
        sa.Column("pre_tax", sa.Numeric(20, 6), nullable=False),
        sa.Column("tax", sa.Numeric(20, 6), nullable=False),
        sa.Column("treatment_code", sa.String(80), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "billing_document_id", "line_number", name="uq_erp_ar_line_accounts_line"),
    )
    op.create_table(
        "erp_ar_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("billing_settlement_id", sa.Uuid(), nullable=False),
        sa.Column("cash_account_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("billing_settlement_id", name="uq_erp_ar_receipts_settlement"),
    )
    for table in ("erp_ar_line_accounts", "erp_ar_receipts"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
            "USING (tenant_id = public.app_current_tenant_id()) "
            "WITH CHECK (tenant_id = public.app_current_tenant_id())"
        )
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO app_user")


def downgrade() -> None:
    op.drop_table("erp_ar_receipts")
    op.drop_table("erp_ar_line_accounts")
