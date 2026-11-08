"""HR training events + credentials (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0008_hr_training"
down_revision = "e2_0007_hr_lifecycle"
branch_labels = None
depends_on = None

_TABLES = (
    "hr_training_programs",
    "hr_training_events",
    "hr_training_attendees",
    "hr_qualifications",
    "hr_certifications",
    "hr_competencies",
    "hr_skills",
    "hr_employee_skills",
)


def upgrade() -> None:
    op.create_table(
        "hr_training_programs",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("training_type", sa.String(40), nullable=True),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("duration_hours", sa.Numeric(7, 2), nullable=True),
        sa.Column("objectives", sa.Text(), nullable=True),
        sa.Column("provider_name", sa.String(160), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_training_programs_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_training_programs_tenant_code"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "hr_training_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("trainer_name", sa.String(120), nullable=True),
        sa.Column("trainer_employee_id", sa.Uuid(), nullable=True),
        sa.Column("max_attendees", sa.Integer(), nullable=True),
        sa.Column("total_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency_code", sa.String(3), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('scheduled','completed','cancelled')", name="ck_hr_training_events_status"),
        sa.CheckConstraint("end_date >= start_date", name="ck_hr_training_events_range"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "program_id"],
            ["hr_training_programs.tenant_id", "hr_training_programs.id"],
            ondelete="RESTRICT",
            name="fk_hr_training_events_tenant_program",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_training_events_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "hr_training_attendees",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("rating", sa.Numeric(3, 2), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("certificate_issued", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("certificate_number", sa.String(60), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "event_id", "employee_id", name="uq_hr_attendee_event_employee"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_training_attendees_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "event_id"],
            ["hr_training_events.tenant_id", "hr_training_events.id"],
            ondelete="CASCADE",
            name="fk_hr_attendees_tenant_event",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_attendees_tenant_employee",
        ),
        sa.CheckConstraint(
            "status IN ('invited','confirmed','attended','no_show','cancelled')", name="ck_hr_attendees_status"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_hr_attendees_tenant_event", "hr_training_attendees", ["tenant_id", "event_id"])
    op.create_table(
        "hr_qualifications",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("qualification_type", sa.String(60), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("field_of_study", sa.String(160), nullable=True),
        sa.Column("institution_name", sa.String(200), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("grade", sa.String(60), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("document_file_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_qualifications_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_qualifications_tenant_employee",
        ),
    )
    op.create_table(
        "hr_certifications",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("issuing_authority", sa.String(200), nullable=True),
        sa.Column("credential_id", sa.String(100), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("does_not_expire", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("document_file_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_certifications_tenant_employee",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_certifications_tenant_id_id"),
    )
    op.create_index("ix_hr_certifications_expiry", "hr_certifications", ["tenant_id", "expiry_date"])
    op.create_table(
        "hr_competencies",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_hr_competencies_tenant_code"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_competencies_tenant_id_id"),
    )
    op.create_table(
        "hr_skills",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_skills_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_hr_skills_tenant_name"),
    )
    op.create_table(
        "hr_employee_skills",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("proficiency_level", sa.String(30), nullable=False),
        sa.Column("years_experience", sa.Numeric(5, 2), nullable=True),
        sa.Column("last_used_date", sa.Date(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_certified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_employee_skills_tenant_employee",
        ),
        sa.UniqueConstraint("tenant_id", "employee_id", "skill_id", name="uq_hr_employee_skill"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_hr_employee_skills_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_id"],
            ["hr_skills.tenant_id", "hr_skills.id"],
            ondelete="RESTRICT",
            name="fk_hr_employee_skills_tenant_skill",
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
