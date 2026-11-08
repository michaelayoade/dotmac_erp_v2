"""HR attendance domain (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0002_hr_attendance"
down_revision = "e2_0001_hr_leave"
branch_labels = None
depends_on = None

_TABLES = (
    "hr_shift_types",
    "hr_shift_assignments",
    "hr_attendance",
    "hr_attendance_requests",
)


def upgrade() -> None:
    op.create_table(
        "hr_shift_types",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("working_hours", sa.Numeric(5, 2), nullable=False),
        sa.Column("late_entry_grace_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("early_exit_grace_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("half_day_threshold_hours", sa.Numeric(5, 2), nullable=True),
        sa.Column("overtime_threshold_hours", sa.Numeric(5, 2), nullable=True),
        sa.Column("break_duration_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_shift_types_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_shift_types_tenant_code"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "hr_shift_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("shift_type_id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_shift_assignments_tenant_employee",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "shift_type_id"],
            ["hr_shift_types.tenant_id", "hr_shift_types.id"],
            ondelete="RESTRICT",
            name="fk_hr_shift_assignments_tenant_shift",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_shift_assignments_tenant_id_id"),
    )
    op.create_index("ix_hr_shift_assignments_tenant_employee", "hr_shift_assignments", ["tenant_id", "employee_id"])
    op.create_table(
        "hr_attendance",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("shift_type_id", sa.Uuid(), nullable=True),
        sa.Column("attendance_date", sa.Date(), nullable=False),
        sa.Column("check_in", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("working_hours", sa.Numeric(6, 2), nullable=True),
        sa.Column("overtime_hours", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("late_entry_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("early_exit_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leave_application_id", sa.Uuid(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("marked_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "shift_type_id"],
            ["hr_shift_types.tenant_id", "hr_shift_types.id"],
            ondelete="RESTRICT",
            name="fk_hr_attendance_tenant_shift",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_attendance_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "employee_id", "attendance_date", name="uq_hr_attendance_day"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_application_id"],
            ["hr_leave_applications.tenant_id", "hr_leave_applications.id"],
            ondelete="SET NULL",
            name="fk_hr_attendance_tenant_leave",
        ),
        sa.CheckConstraint(
            "status IN ('present','absent','half_day','on_leave','holiday','weekly_off')",
            name="ck_hr_attendance_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_attendance_tenant_employee",
        ),
    )
    op.create_index(
        "ix_hr_attendance_tenant_employee_date", "hr_attendance", ["tenant_id", "employee_id", "attendance_date"]
    )
    op.create_table(
        "hr_attendance_requests",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("half_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_attendance_requests_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_attendance_requests_tenant_employee",
        ),
        sa.CheckConstraint(
            "status IN ('draft','submitted','approved','rejected','cancelled')", name="ck_hr_attendance_requests_status"
        ),
        sa.CheckConstraint("to_date >= from_date", name="ck_hr_attendance_requests_range"),
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
