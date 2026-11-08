"""HR appraisals domain (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0003_hr_appraisals"
down_revision = "e2_0002_hr_attendance"
branch_labels = None
depends_on = None

_TABLES = (
    "hr_appraisal_cycles",
    "hr_kras",
    "hr_appraisal_templates",
    "hr_appraisal_template_kras",
    "hr_appraisals",
    "hr_appraisal_kra_scores",
)


def upgrade() -> None:
    op.create_table(
        "hr_appraisal_cycles",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("review_period_start", sa.Date(), nullable=False),
        sa.Column("review_period_end", sa.Date(), nullable=False),
        sa.Column("self_assessment_deadline", sa.Date(), nullable=True),
        sa.Column("manager_review_deadline", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_cycles_tenant_id_id"),
        sa.CheckConstraint("status IN ('draft','open','closed')", name="ck_hr_appraisal_cycles_status"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_appraisal_cycles_tenant_code"),
    )
    op.create_table(
        "hr_kras",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("measurement_criteria", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_kras_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_kras_tenant_code"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "hr_appraisal_templates",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rating_scale_max", sa.Numeric(5, 2), nullable=False, server_default="5"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_templates_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_appraisal_templates_tenant_code"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "hr_appraisal_template_kras",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("kra_id", sa.Uuid(), nullable=False),
        sa.Column("weightage", sa.Numeric(6, 2), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_template_kras_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["hr_appraisal_templates.tenant_id", "hr_appraisal_templates.id"],
            ondelete="CASCADE",
            name="fk_hr_template_kras_tenant_template",
        ),
        sa.UniqueConstraint("tenant_id", "template_id", "kra_id", name="uq_hr_template_kra"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "kra_id"],
            ["hr_kras.tenant_id", "hr_kras.id"],
            ondelete="RESTRICT",
            name="fk_hr_template_kras_tenant_kra",
        ),
    )
    op.create_table(
        "hr_appraisals",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("manager_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("self_overall_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("self_summary", sa.Text(), nullable=True),
        sa.Column("manager_overall_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("manager_summary", sa.Text(), nullable=True),
        sa.Column("calibrated_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("calibration_notes", sa.Text(), nullable=True),
        sa.Column("final_score", sa.Numeric(7, 2), nullable=True),
        sa.Column("final_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("completed_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('draft','self_submitted','manager_reviewed','calibrated','completed','cancelled')",
            name="ck_hr_appraisals_status",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_appraisals_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_appraisals_tenant_employee",
        ),
        sa.UniqueConstraint("tenant_id", "cycle_id", "employee_id", name="uq_hr_appraisals_cycle_employee"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["hr_appraisal_cycles.tenant_id", "hr_appraisal_cycles.id"],
            ondelete="RESTRICT",
            name="fk_hr_appraisals_tenant_cycle",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["hr_appraisal_templates.tenant_id", "hr_appraisal_templates.id"],
            ondelete="RESTRICT",
            name="fk_hr_appraisals_tenant_template",
        ),
    )
    op.create_index("ix_hr_appraisals_tenant_employee", "hr_appraisals", ["tenant_id", "employee_id"])
    op.create_table(
        "hr_appraisal_kra_scores",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("appraisal_id", sa.Uuid(), nullable=False),
        sa.Column("kra_id", sa.Uuid(), nullable=False),
        sa.Column("weightage", sa.Numeric(6, 2), nullable=False),
        sa.Column("self_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("self_comments", sa.Text(), nullable=True),
        sa.Column("manager_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("manager_comments", sa.Text(), nullable=True),
        sa.Column("final_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("weighted_score", sa.Numeric(8, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "kra_id"],
            ["hr_kras.tenant_id", "hr_kras.id"],
            ondelete="RESTRICT",
            name="fk_hr_kra_scores_tenant_kra",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_kra_scores_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "appraisal_id", "kra_id", name="uq_hr_appraisal_kra"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "appraisal_id"],
            ["hr_appraisals.tenant_id", "hr_appraisals.id"],
            ondelete="CASCADE",
            name="fk_hr_kra_scores_tenant_appraisal",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
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
