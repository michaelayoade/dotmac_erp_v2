"""Fleet operations (ERP-unique e2 lineage).

Shapes frozen at creation from the hr models; later model changes
need new revisions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2_0009_fleet"
down_revision = "e2_0008_hr_training"
branch_labels = None
depends_on = None

_TABLES = (
    "fleet_vehicle_profiles",
    "fleet_fuel_logs",
    "fleet_reservations",
    "fleet_incidents",
    "fleet_maintenance_records",
    "fleet_vehicle_documents",
)


def upgrade() -> None:
    op.create_table(
        "fleet_vehicle_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("registration_number", sa.String(30), nullable=False),
        sa.Column("vin", sa.String(40), nullable=True),
        sa.Column("make", sa.String(60), nullable=True),
        sa.Column("model", sa.String(60), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("vehicle_type", sa.String(40), nullable=True),
        sa.Column("fuel_type", sa.String(30), nullable=True),
        sa.Column("fuel_tank_capacity_liters", sa.Numeric(8, 2), nullable=True),
        sa.Column("expected_fuel_efficiency", sa.Numeric(8, 2), nullable=True),
        sa.Column("current_odometer", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("license_expiry_date", sa.Date(), nullable=True),
        sa.Column("gps_device_ref", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "registration_number", name="uq_fleet_profiles_registration"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_fleet_profiles_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "asset_id", name="uq_fleet_profiles_tenant_asset"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "asset_id"],
            ["mod_assets.assets.tenant_id", "mod_assets.assets.id"],
            ondelete="CASCADE",
            name="fk_fleet_profiles_tenant_asset",
        ),
    )
    op.create_table(
        "fleet_fuel_logs",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=True),
        sa.Column("log_date", sa.Date(), nullable=False),
        sa.Column("fuel_type", sa.String(30), nullable=True),
        sa.Column("quantity_liters", sa.Numeric(10, 3), nullable=False),
        sa.Column("price_per_liter", sa.Numeric(12, 4), nullable=False),
        sa.Column("total_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("odometer_reading", sa.Numeric(14, 2), nullable=True),
        sa.Column("station_name", sa.String(160), nullable=True),
        sa.Column("receipt_number", sa.String(60), nullable=True),
        sa.Column("expense_claim_ref", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_fuel_tenant_profile",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_fleet_fuel_logs_tenant_id_id"),
    )
    op.create_index("ix_fleet_fuel_tenant_profile_date", "fleet_fuel_logs", ["tenant_id", "profile_id", "log_date"])
    op.create_table(
        "fleet_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("start_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("destination", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("start_odometer", sa.Numeric(14, 2), nullable=True),
        sa.Column("end_odometer", sa.Numeric(14, 2), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_reservations_tenant_profile",
        ),
        sa.CheckConstraint(
            "status IN ('requested','approved','rejected','in_use','completed','cancelled')",
            name="ck_fleet_reservations_status",
        ),
        sa.CheckConstraint("end_datetime > start_datetime", name="ck_fleet_reservations_range"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_fleet_reservations_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_fleet_reservations_tenant_employee",
        ),
    )
    op.create_index(
        "ix_fleet_reservations_tenant_profile_window",
        "fleet_reservations",
        ["tenant_id", "profile_id", "start_datetime"],
    )
    op.create_table(
        "fleet_incidents",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("reported_by_id", sa.Uuid(), nullable=True),
        sa.Column("driver_id", sa.Uuid(), nullable=True),
        sa.Column("incident_type", sa.String(60), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("incident_date", sa.Date(), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("police_report_number", sa.String(60), nullable=True),
        sa.Column("insurance_claim_number", sa.String(60), nullable=True),
        sa.Column("estimated_repair_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("actual_repair_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("resolution_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_incidents_tenant_profile",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_fleet_incidents_tenant_id_id"),
        sa.CheckConstraint(
            "status IN ('reported','under_review','resolved','closed')", name="ck_fleet_incidents_status"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "fleet_maintenance_records",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("maintenance_type", sa.String(60), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("completed_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("odometer_at_service", sa.Numeric(14, 2), nullable=True),
        sa.Column("next_service_date", sa.Date(), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("actual_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("supplier_ref", sa.String(80), nullable=True),
        sa.Column("work_performed", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_fleet_maintenance_tenant_id_id"),
        sa.CheckConstraint(
            "status IN ('scheduled','in_progress','completed','cancelled')", name="ck_fleet_maintenance_status"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_maintenance_tenant_profile",
        ),
    )
    op.create_table(
        "fleet_vehicle_documents",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(60), nullable=False),
        sa.Column("document_number", sa.String(80), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("provider_name", sa.String(160), nullable=True),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_documents_tenant_profile",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_fleet_documents_tenant_id_id"),
    )
    op.create_index("ix_fleet_documents_expiry", "fleet_vehicle_documents", ["tenant_id", "expiry_date"])
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
