"""Public sector commitment control (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0010_public_sector"
down_revision = "e2_0009_fleet"
branch_labels = None
depends_on = None

_TABLES = (
    "ps_funds",
    "ps_appropriations",
    "ps_commitments",
    "ps_commitment_lines",
    "ps_virements",
)


def upgrade() -> None:
    op.create_table(
        "ps_funds",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("fund_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("is_restricted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("parent_fund_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ps_funds_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_fund_id"],
            ["ps_funds.tenant_id", "ps_funds.id"],
            ondelete="RESTRICT",
            name="fk_ps_funds_tenant_parent",
        ),
        sa.UniqueConstraint("tenant_id", "code", name="uq_ps_funds_tenant_code"),
    )
    op.create_table(
        "ps_appropriations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("fund_id", sa.Uuid(), nullable=False),
        sa.Column("fiscal_year_ref", sa.String(40), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("appropriation_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("approved_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("revised_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("account_ref", sa.String(80), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('draft','approved','closed')", name="ck_ps_appropriations_status"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ps_appropriations_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_ps_appropriations_tenant_code"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "fund_id"],
            ["ps_funds.tenant_id", "ps_funds.id"],
            ondelete="RESTRICT",
            name="fk_ps_appropriations_tenant_fund",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_ps_appropriations_tenant_fund", "ps_appropriations", ["tenant_id", "fund_id"])
    op.create_table(
        "ps_commitments",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("commitment_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("appropriation_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(60), nullable=True),
        sa.Column("source_ref", sa.String(80), nullable=True),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("committed_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("obligated_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("expended_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("cancelled_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("commitment_date", sa.Date(), nullable=False),
        sa.Column("obligation_date", sa.Date(), nullable=True),
        sa.Column("expenditure_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ps_commitments_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "appropriation_id"],
            ["ps_appropriations.tenant_id", "ps_appropriations.id"],
            ondelete="RESTRICT",
            name="fk_ps_commitments_tenant_appropriation",
        ),
        sa.CheckConstraint(
            "status IN ('committed','obligated','expended','cancelled','closed')", name="ck_ps_commitments_status"
        ),
        sa.UniqueConstraint("tenant_id", "number", name="uq_ps_commitments_tenant_number"),
        sa.CheckConstraint("expended_amount <= obligated_amount", name="ck_ps_commitments_expended"),
        sa.CheckConstraint("obligated_amount <= committed_amount", name="ck_ps_commitments_obligated"),
    )
    op.create_index("ix_ps_commitments_tenant_appropriation", "ps_commitments", ["tenant_id", "appropriation_id"])
    op.create_table(
        "ps_commitment_lines",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("commitment_id", sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("account_ref", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("committed_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("obligated_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("expended_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ps_commitment_lines_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "commitment_id"],
            ["ps_commitments.tenant_id", "ps_commitments.id"],
            ondelete="CASCADE",
            name="fk_ps_commitment_lines_tenant_commitment",
        ),
    )
    op.create_table(
        "ps_virements",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("from_appropriation_id", sa.Uuid(), nullable=False),
        sa.Column("to_appropriation_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ps_virements_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "from_appropriation_id"],
            ["ps_appropriations.tenant_id", "ps_appropriations.id"],
            ondelete="RESTRICT",
            name="fk_ps_virements_tenant_from",
        ),
        sa.UniqueConstraint("tenant_id", "number", name="uq_ps_virements_tenant_number"),
        sa.CheckConstraint("amount > 0", name="ck_ps_virements_amount"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "to_appropriation_id"],
            ["ps_appropriations.tenant_id", "ps_appropriations.id"],
            ondelete="RESTRICT",
            name="fk_ps_virements_tenant_to",
        ),
        sa.CheckConstraint("status IN ('draft','approved','applied','rejected')", name="ck_ps_virements_status"),
    )
    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
            "USING (tenant_id = public.app_current_tenant_id()) "
            "WITH CHECK (tenant_id = public.app_current_tenant_id())"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
