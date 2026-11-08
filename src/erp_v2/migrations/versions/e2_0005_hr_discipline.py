"""HR discipline + grievances (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0005_hr_discipline"
down_revision = "e2_0004_hr_feedback"
branch_labels = None
depends_on = None

_TABLES = (
    "hr_disciplinary_cases",
    "hr_case_responses",
    "hr_case_witnesses",
    "hr_case_actions",
    "hr_case_documents",
    "hr_grievances",
)


def upgrade() -> None:
    op.create_table(
        "hr_disciplinary_cases",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(30), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("violation_type", sa.String(60), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("incident_date", sa.Date(), nullable=True),
        sa.Column("reported_date", sa.Date(), nullable=False),
        sa.Column("hearing_date", sa.Date(), nullable=True),
        sa.Column("decision_date", sa.Date(), nullable=True),
        sa.Column("appeal_deadline", sa.Date(), nullable=True),
        sa.Column("closed_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("decision_summary", sa.Text(), nullable=True),
        sa.Column("appeal_reason", sa.Text(), nullable=True),
        sa.Column("appeal_decision", sa.Text(), nullable=True),
        sa.Column("reported_by_id", sa.Uuid(), nullable=True),
        sa.Column("investigating_officer_id", sa.Uuid(), nullable=True),
        sa.Column("panel_chair_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("severity IN ('minor','major','gross')", name="ck_hr_cases_severity"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_cases_tenant_employee",
        ),
        sa.CheckConstraint(
            "status IN ('reported','queried','hearing','decided','appealed','closed','withdrawn')",
            name="ck_hr_cases_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_hr_disciplinary_cases_tenant_number"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_disciplinary_cases_tenant_id_id"),
    )
    op.create_index("ix_hr_cases_tenant_employee", "hr_disciplinary_cases", ["tenant_id", "employee_id"])
    op.create_table(
        "hr_case_responses",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("is_appeal_response", sa.Boolean(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["hr_disciplinary_cases.tenant_id", "hr_disciplinary_cases.id"],
            ondelete="CASCADE",
            name="fk_hr_responses_tenant_case",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_case_responses_tenant_id_id"),
    )
    op.create_table(
        "hr_case_witnesses",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=True),
        sa.Column("external_name", sa.String(160), nullable=True),
        sa.Column("statement", sa.Text(), nullable=True),
        sa.Column("statement_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_case_witnesses_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["hr_disciplinary_cases.tenant_id", "hr_disciplinary_cases.id"],
            ondelete="CASCADE",
            name="fk_hr_witnesses_tenant_case",
        ),
    )
    op.create_table(
        "hr_case_actions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(40), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("warning_expiry_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("issued_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_case_actions_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["hr_disciplinary_cases.tenant_id", "hr_disciplinary_cases.id"],
            ondelete="CASCADE",
            name="fk_hr_actions_tenant_case",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "hr_case_documents",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_case_documents_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["hr_disciplinary_cases.tenant_id", "hr_disciplinary_cases.id"],
            ondelete="CASCADE",
            name="fk_hr_documents_tenant_case",
        ),
    )
    op.create_table(
        "hr_grievances",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(30), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(60), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("assigned_to_id", sa.Uuid(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolution_date", sa.Date(), nullable=True),
        sa.Column("sla_due_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_grievances_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_grievances_tenant_employee",
        ),
        sa.UniqueConstraint("tenant_id", "number", name="uq_hr_grievances_tenant_number"),
        sa.CheckConstraint(
            "status IN ('open','under_review','resolved','closed','escalated')", name="ck_hr_grievances_status"
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
