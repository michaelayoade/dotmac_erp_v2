"""HR appraisal feedback + appeals (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0004_hr_feedback"
down_revision = "e2_0003_hr_appraisals"
branch_labels = None
depends_on = None

_TABLES = (
    "hr_appraisal_feedback",
    "hr_appraisal_appeals",
    "hr_appraisal_outcome_actions",
)


def upgrade() -> None:
    op.create_table(
        "hr_appraisal_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("appraisal_id", sa.Uuid(), nullable=False),
        sa.Column("feedback_from_id", sa.Uuid(), nullable=True),
        sa.Column("feedback_type", sa.String(30), nullable=False),
        sa.Column("overall_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("strengths", sa.Text(), nullable=True),
        sa.Column("areas_for_improvement", sa.Text(), nullable=True),
        sa.Column("general_comments", sa.Text(), nullable=True),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False),
        sa.Column("submitted_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_feedback_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "appraisal_id"],
            ["hr_appraisals.tenant_id", "hr_appraisals.id"],
            ondelete="CASCADE",
            name="fk_hr_feedback_tenant_appraisal",
        ),
    )
    op.create_index("ix_hr_feedback_tenant_appraisal", "hr_appraisal_feedback", ["tenant_id", "appraisal_id"])
    op.create_table(
        "hr_appraisal_appeals",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("appraisal_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("filed_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_outcome", sa.Text(), nullable=True),
        sa.Column("mediator_id", sa.Uuid(), nullable=True),
        sa.Column("adjusted_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("resolution_date", sa.Date(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "appraisal_id"],
            ["hr_appraisals.tenant_id", "hr_appraisals.id"],
            ondelete="CASCADE",
            name="fk_hr_appeals_tenant_appraisal",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_appeals_tenant_employee",
        ),
        sa.CheckConstraint(
            "status IN ('filed','in_mediation','in_committee','resolved','rejected')", name="ck_hr_appeals_status"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_appraisal_appeals_tenant_id_id"),
    )
    op.create_index("ix_hr_appeals_tenant_appraisal", "hr_appraisal_appeals", ["tenant_id", "appraisal_id"])
    op.create_table(
        "hr_appraisal_outcome_actions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("appraisal_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(40), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("actioned_by_id", sa.Uuid(), nullable=True),
        sa.Column("actioned_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "appraisal_id"],
            ["hr_appraisals.tenant_id", "hr_appraisals.id"],
            ondelete="CASCADE",
            name="fk_hr_outcome_actions_tenant_appraisal",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_outcome_actions_tenant_id_id"),
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
