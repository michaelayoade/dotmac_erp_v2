"""HR lifecycle: onboarding, separation, employment changes (e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0007_hr_lifecycle"
down_revision = "e2_0006_hr_recruitment"
branch_labels = None
depends_on = None

_TABLES = (
    "hr_checklist_templates",
    "hr_checklist_template_items",
    "hr_employee_onboardings",
    "hr_onboarding_activities",
    "hr_employee_separations",
    "hr_clearance_items",
    "hr_employment_changes",
    "hr_employment_change_details",
)


def upgrade() -> None:
    op.create_table(
        "hr_checklist_templates",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("template_type", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_checklist_templates_tenant_code"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_checklist_templates_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("template_type IN ('onboarding','separation')", name="ck_hr_checklist_type"),
    )
    op.create_table(
        "hr_checklist_template_items",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("default_assignee_role", sa.String(60), nullable=True),
        sa.Column("days_from_start", sa.Integer(), nullable=True),
        sa.Column("requires_document", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_checklist_items_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["hr_checklist_templates.tenant_id", "hr_checklist_templates.id"],
            ondelete="CASCADE",
            name="fk_hr_checklist_items_tenant_template",
        ),
    )
    op.create_table(
        "hr_employee_onboardings",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("date_of_joining", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("buddy_employee_id", sa.Uuid(), nullable=True),
        sa.Column("manager_id", sa.Uuid(), nullable=True),
        sa.Column("expected_completion_date", sa.Date(), nullable=True),
        sa.Column("actual_completion_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["hr_checklist_templates.tenant_id", "hr_checklist_templates.id"],
            ondelete="RESTRICT",
            name="fk_hr_onboardings_tenant_template",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_onboardings_tenant_employee",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('in_progress','completed','cancelled')", name="ck_hr_onboardings_status"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_onboardings_tenant_id_id"),
    )
    op.create_index("ix_hr_onboardings_tenant_employee", "hr_employee_onboardings", ["tenant_id", "employee_id"])
    op.create_table(
        "hr_onboarding_activities",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("onboarding_id", sa.Uuid(), nullable=False),
        sa.Column("template_item_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("completed_on", sa.Date(), nullable=True),
        sa.Column("completed_by_id", sa.Uuid(), nullable=True),
        sa.Column("document_file_id", sa.Uuid(), nullable=True),
        sa.Column("completion_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "onboarding_id"],
            ["hr_employee_onboardings.tenant_id", "hr_employee_onboardings.id"],
            ondelete="CASCADE",
            name="fk_hr_activities_tenant_onboarding",
        ),
        sa.CheckConstraint("status IN ('pending','completed','skipped')", name="ck_hr_activities_status"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_onboarding_activities_tenant_id_id"),
    )
    op.create_table(
        "hr_employee_separations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("separation_type", sa.String(40), nullable=False),
        sa.Column("separation_date", sa.Date(), nullable=False),
        sa.Column("resignation_letter_date", sa.Date(), nullable=True),
        sa.Column("reason_for_leaving", sa.Text(), nullable=True),
        sa.Column("exit_interview_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_separations_tenant_employee",
        ),
        sa.CheckConstraint("status IN ('in_progress','completed','cancelled')", name="ck_hr_separations_status"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_separations_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_hr_separations_tenant_employee", "hr_employee_separations", ["tenant_id", "employee_id"])
    op.create_table(
        "hr_clearance_items",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("separation_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(60), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("assigned_to_id", sa.Uuid(), nullable=True),
        sa.Column("is_cleared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cleared_by_id", sa.Uuid(), nullable=True),
        sa.Column("cleared_at", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "separation_id"],
            ["hr_employee_separations.tenant_id", "hr_employee_separations.id"],
            ondelete="CASCADE",
            name="fk_hr_clearance_tenant_separation",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_clearance_items_tenant_id_id"),
    )
    op.create_table(
        "hr_employment_changes",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("change_type", sa.String(20), nullable=False),
        sa.Column("change_date", sa.Date(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_employment_changes_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_changes_tenant_employee",
        ),
        sa.CheckConstraint("change_type IN ('promotion','transfer')", name="ck_hr_changes_type"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_hr_changes_tenant_employee", "hr_employment_changes", ["tenant_id", "employee_id"])
    op.create_table(
        "hr_employment_change_details",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("change_id", sa.Uuid(), nullable=False),
        sa.Column("property_name", sa.String(80), nullable=False),
        sa.Column("current_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "change_id"],
            ["hr_employment_changes.tenant_id", "hr_employment_changes.id"],
            ondelete="CASCADE",
            name="fk_hr_change_details_tenant_change",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_change_details_tenant_id_id"),
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
