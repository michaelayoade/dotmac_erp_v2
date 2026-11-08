"""Leases IFRS16 lessee core (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0011_leases"
down_revision = "e2_0010_public_sector"
branch_labels = None
depends_on = None

_TABLES = (
    "lease_contracts",
    "lease_liabilities",
    "lease_rou_assets",
    "lease_payment_schedule",
)


def upgrade() -> None:
    op.create_table(
        "lease_contracts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("lessor_ref", sa.String(80), nullable=True),
        sa.Column("lessor_name", sa.String(200), nullable=True),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("commencement_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("lease_term_months", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("payment_frequency_months", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("base_payment_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("discount_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("initial_direct_costs", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('draft','active','expired','terminated')", name="ck_lease_contracts_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_lease_contracts_tenant_number"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_lease_contracts_tenant_id_id"),
        sa.CheckConstraint("lease_term_months > 0", name="ck_lease_contracts_term"),
    )
    op.create_table(
        "lease_liabilities",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("initial_liability_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("discount_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("current_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("total_interest_expense", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total_payments_made", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "lease_id", name="uq_lease_liabilities_tenant_lease"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "lease_id"],
            ["lease_contracts.tenant_id", "lease_contracts.id"],
            ondelete="CASCADE",
            name="fk_lease_liabilities_tenant_lease",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_lease_liabilities_tenant_id_id"),
    )
    op.create_table(
        "lease_rou_assets",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("initial_value", sa.Numeric(18, 2), nullable=False),
        sa.Column("useful_life_months", sa.Integer(), nullable=False),
        sa.Column("accumulated_depreciation", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("carrying_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "lease_id", name="uq_lease_rou_assets_tenant_lease"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "lease_id"],
            ["lease_contracts.tenant_id", "lease_contracts.id"],
            ondelete="CASCADE",
            name="fk_lease_rou_tenant_lease",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_lease_rou_assets_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "lease_payment_schedule",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("payment_number", sa.Integer(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("total_payment", sa.Numeric(18, 2), nullable=False),
        sa.Column("principal_portion", sa.Numeric(18, 2), nullable=False),
        sa.Column("interest_portion", sa.Numeric(18, 2), nullable=False),
        sa.Column("opening_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("closing_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("actual_payment_date", sa.Date(), nullable=True),
        sa.Column("is_paid", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_lease_payments_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "lease_id"],
            ["lease_contracts.tenant_id", "lease_contracts.id"],
            ondelete="CASCADE",
            name="fk_lease_payments_tenant_lease",
        ),
        sa.CheckConstraint("status IN ('scheduled','paid','voided')", name="ck_lease_payments_status"),
        sa.UniqueConstraint("tenant_id", "lease_id", "payment_number", name="uq_lease_payments_number"),
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
