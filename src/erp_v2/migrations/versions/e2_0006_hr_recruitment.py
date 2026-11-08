"""HR recruitment (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0006_hr_recruitment"
down_revision = "e2_0005_hr_discipline"
branch_labels = None
depends_on = None

_TABLES = (
    "hr_job_openings",
    "hr_job_applicants",
    "hr_interviews",
    "hr_job_offers",
)


def upgrade() -> None:
    op.create_table(
        "hr_job_openings",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("designation_id", sa.Uuid(), nullable=True),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("number_of_positions", sa.Integer(), nullable=False),
        sa.Column("positions_filled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("posted_on", sa.Date(), nullable=True),
        sa.Column("closes_on", sa.Date(), nullable=True),
        sa.Column("employment_type", sa.String(40), nullable=True),
        sa.Column("location", sa.String(120), nullable=True),
        sa.Column("is_remote", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('draft','open','closed','cancelled')", name="ck_hr_job_openings_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("positions_filled <= number_of_positions", name="ck_hr_job_openings_filled"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_job_openings_tenant_code"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_job_openings_tenant_id_id"),
    )
    op.create_table(
        "hr_job_applicants",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("application_number", sa.String(30), nullable=False),
        sa.Column("job_opening_id", sa.Uuid(), nullable=False),
        sa.Column("first_name", sa.String(80), nullable=False),
        sa.Column("last_name", sa.String(80), nullable=False),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("current_employer", sa.String(160), nullable=True),
        sa.Column("years_of_experience", sa.Numeric(5, 2), nullable=True),
        sa.Column("highest_qualification", sa.String(160), nullable=True),
        sa.Column("applied_on", sa.Date(), nullable=False),
        sa.Column("source", sa.String(60), nullable=True),
        sa.Column("resume_file_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("overall_rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_job_applicants_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "application_number", name="uq_hr_job_applicants_number"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_opening_id"],
            ["hr_job_openings.tenant_id", "hr_job_openings.id"],
            ondelete="CASCADE",
            name="fk_hr_applicants_tenant_opening",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('applied','screening','interview','offer','hired','rejected','withdrawn')",
            name="ck_hr_applicants_status",
        ),
    )
    op.create_index("ix_hr_applicants_tenant_opening", "hr_job_applicants", ["tenant_id", "job_opening_id"])
    op.create_table(
        "hr_interviews",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("interview_type", sa.String(40), nullable=False),
        sa.Column("scheduled_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("interviewer_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("rating", sa.Numeric(5, 2), nullable=True),
        sa.Column("recommendation", sa.String(30), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "applicant_id"],
            ["hr_job_applicants.tenant_id", "hr_job_applicants.id"],
            ondelete="CASCADE",
            name="fk_hr_interviews_tenant_applicant",
        ),
        sa.CheckConstraint("status IN ('scheduled','completed','cancelled','no_show')", name="ck_hr_interviews_status"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_interviews_tenant_id_id"),
    )
    op.create_table(
        "hr_job_offers",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(30), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("job_opening_id", sa.Uuid(), nullable=False),
        sa.Column("offer_date", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=False),
        sa.Column("expected_joining_date", sa.Date(), nullable=True),
        sa.Column("base_salary", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("responded_on", sa.Date(), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column("converted_to_employee_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_hr_job_offers_tenant_number"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_job_offers_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "applicant_id"],
            ["hr_job_applicants.tenant_id", "hr_job_applicants.id"],
            ondelete="CASCADE",
            name="fk_hr_offers_tenant_applicant",
        ),
        sa.CheckConstraint(
            "status IN ('draft','extended','accepted','declined','expired','withdrawn')", name="ck_hr_job_offers_status"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_opening_id"],
            ["hr_job_openings.tenant_id", "hr_job_openings.id"],
            ondelete="RESTRICT",
            name="fk_hr_offers_tenant_opening",
        ),
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
