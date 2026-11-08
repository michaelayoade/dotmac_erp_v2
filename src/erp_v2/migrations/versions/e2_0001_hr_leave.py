"""HR leave domain (ERP-unique e2 lineage root).

Creates the product-owned leave tables in public with tenant RLS,
composite tenant FKs into mod_people.employees. Shapes frozen at
creation from erp_v2.hr.models; later model changes need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0001_hr_leave"
down_revision = None
branch_labels = ("assembly",)
depends_on = ("0030_credential_tenant_key", "pe_0001_people_directory")

_TABLES = (
    "hr_leave_types",
    "hr_holiday_lists",
    "hr_holidays",
    "hr_leave_allocations",
    "hr_leave_applications",
)


def upgrade() -> None:
    op.create_table(
        "hr_leave_types",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("max_days_per_year", sa.Numeric(6, 2), nullable=True),
        sa.Column("max_continuous_days", sa.Numeric(6, 2), nullable=True),
        sa.Column("allow_carry_forward", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_carry_forward_days", sa.Numeric(6, 2), nullable=True),
        sa.Column("allow_encashment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_lwp", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("include_holidays", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_leave_types_tenant_code"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_leave_types_tenant_id_id"),
    )
    op.create_table(
        "hr_holiday_lists",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("weekly_off", sa.String(20), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_holiday_lists_tenant_code"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_holiday_lists_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "hr_holidays",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("holiday_list_id", sa.Uuid(), nullable=False),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_public_holiday", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_optional", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "holiday_list_id", "holiday_date", name="uq_hr_holidays_list_date"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "holiday_list_id"],
            ["hr_holiday_lists.tenant_id", "hr_holiday_lists.id"],
            ondelete="CASCADE",
            name="fk_hr_holidays_tenant_list",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_holidays_tenant_id_id"),
    )
    op.create_index("ix_hr_holidays_tenant_list", "hr_holidays", ["tenant_id", "holiday_list_id"])
    op.create_table(
        "hr_leave_allocations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("new_leaves_allocated", sa.Numeric(7, 2), nullable=False),
        sa.Column("carry_forward_leaves", sa.Numeric(7, 2), nullable=False, server_default="0"),
        sa.Column("leaves_used", sa.Numeric(7, 2), nullable=False, server_default="0"),
        sa.Column("leaves_encashed", sa.Numeric(7, 2), nullable=False, server_default="0"),
        sa.Column("leaves_expired", sa.Numeric(7, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("to_date >= from_date", name="ck_hr_leave_allocations_range"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_leave_allocations_tenant_employee",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_leave_allocations_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["hr_leave_types.tenant_id", "hr_leave_types.id"],
            ondelete="RESTRICT",
            name="fk_hr_leave_allocations_tenant_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_hr_leave_allocations_tenant_employee", "hr_leave_allocations", ["tenant_id", "employee_id"])
    op.create_table(
        "hr_leave_applications",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(30), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("allocation_id", sa.Uuid(), nullable=True),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("half_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("total_leave_days", sa.Numeric(7, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("to_date >= from_date", name="ck_hr_leave_applications_range"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_leave_applications_tenant_employee",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["hr_leave_types.tenant_id", "hr_leave_types.id"],
            ondelete="RESTRICT",
            name="fk_hr_leave_applications_tenant_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_hr_leave_applications_tenant_number"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "allocation_id"],
            ["hr_leave_allocations.tenant_id", "hr_leave_allocations.id"],
            ondelete="RESTRICT",
            name="fk_hr_leave_applications_tenant_allocation",
        ),
        sa.CheckConstraint(
            "status IN ('draft','submitted','approved','rejected','cancelled')", name="ck_hr_leave_applications_status"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_leave_applications_tenant_id_id"),
    )
    op.create_index("ix_hr_leave_applications_tenant_employee", "hr_leave_applications", ["tenant_id", "employee_id"])
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
