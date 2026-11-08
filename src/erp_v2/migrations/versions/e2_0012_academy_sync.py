"""Academy app-sync receiver tables (ERP-unique e2 lineage).

Shapes frozen at creation from erp_v2.academy_sync.models; later model
changes need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0012_academy_sync"
down_revision = "e2_0011_leases"
branch_labels = None
depends_on = None

_TABLES = (
    "academy_sync_receipts",
    "academy_learning_completions",
)


def upgrade() -> None:
    op.create_table(
        "academy_sync_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("capability_id", sa.String(120), nullable=False),
        sa.Column("source_application", sa.String(120), nullable=False),
        sa.Column("source_event_id", sa.String(240), nullable=False),
        sa.Column("destination_ref", sa.String(240), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_academy_sync_receipts_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_academy_sync_receipts_tenant_key"),
    )
    op.create_table(
        "academy_learning_completions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("employee_ref", sa.String(30), nullable=False),
        sa.Column("academy_course_ref", sa.String(120), nullable=False),
        sa.Column("academy_enrollment_ref", sa.String(64), nullable=True),
        sa.Column("course_title", sa.String(200), nullable=False),
        sa.Column("course_version", sa.Integer(), nullable=True),
        sa.Column("progress_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("completed_on", sa.Date(), nullable=True),
        sa.Column("certificate_ref", sa.String(100), nullable=True),
        sa.Column("certification_id", sa.Uuid(), nullable=True),
        sa.Column("source_event_id", sa.String(240), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_academy_learning_completions_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "source_event_id", name="uq_academy_learning_completions_tenant_event"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_academy_learning_completions_tenant_employee",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "certification_id"],
            ["hr_certifications.tenant_id", "hr_certifications.id"],
            ondelete="SET NULL",
            name="fk_academy_learning_completions_tenant_certification",
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
