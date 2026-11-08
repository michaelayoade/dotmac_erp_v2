"""Fleet operations — ERP-unique slice 9.

Vehicle identity/custody/lifecycle belong to dotmac-assets; this
slice owns only the fleet-operational layer legacy ERP kept on the
vehicle tables: profiles keyed to an assets-module asset, fuel,
reservations (overlap-guarded), incidents, maintenance and expiry
documents. GPS positions arrive via the Traccar connector boundary,
never written by product code paths here.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from erp_v2.hr.models import _tenant_fk


class ReservationStatus(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    IN_USE = "in_use"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


_ACTIVE_RESERVATIONS = ("approved", "in_use")


class VehicleProfile(Base, TimestampMixin):
    __tablename__ = "fleet_vehicle_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_fleet_profiles_tenant_id_id"),
        UniqueConstraint("tenant_id", "asset_id", name="uq_fleet_profiles_tenant_asset"),
        UniqueConstraint("tenant_id", "registration_number", name="uq_fleet_profiles_registration"),
        ForeignKeyConstraint(
            ["tenant_id", "asset_id"],
            ["mod_assets.assets.tenant_id", "mod_assets.assets.id"],
            ondelete="CASCADE",
            name="fk_fleet_profiles_tenant_asset",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    asset_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    registration_number: Mapped[str] = mapped_column(String(30), nullable=False)
    vin: Mapped[str | None] = mapped_column(String(40), nullable=True)
    make: Mapped[str | None] = mapped_column(String(60), nullable=True)
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vehicle_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fuel_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    fuel_tank_capacity_liters: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    expected_fuel_efficiency: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    current_odometer: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal(0), server_default="0"
    )
    license_expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    gps_device_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)


class FuelLogEntry(Base, TimestampMixin):
    __tablename__ = "fleet_fuel_logs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_fleet_fuel_logs_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_fuel_tenant_profile",
        ),
        Index("ix_fleet_fuel_tenant_profile_date", "tenant_id", "profile_id", "log_date"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    profile_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    employee_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    log_date: Mapped[date] = mapped_column(Date, nullable=False)
    fuel_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    quantity_liters: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    price_per_liter: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    odometer_reading: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    station_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    receipt_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    expense_claim_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)


class VehicleReservation(Base, TimestampMixin):
    __tablename__ = "fleet_reservations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_fleet_reservations_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_reservations_tenant_profile",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_fleet_reservations_tenant_employee",
        ),
        CheckConstraint("end_datetime > start_datetime", name="ck_fleet_reservations_range"),
        CheckConstraint(
            "status IN ('requested','approved','rejected','in_use','completed','cancelled')",
            name="ck_fleet_reservations_status",
        ),
        Index("ix_fleet_reservations_tenant_profile_window", "tenant_id", "profile_id", "start_datetime"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    profile_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    start_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    destination: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="requested")
    start_odometer: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    end_odometer: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class VehicleIncident(Base, TimestampMixin):
    __tablename__ = "fleet_incidents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_fleet_incidents_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_incidents_tenant_profile",
        ),
        CheckConstraint("status IN ('reported','under_review','resolved','closed')", name="ck_fleet_incidents_status"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    profile_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    reported_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    driver_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    incident_type: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="minor")
    incident_date: Mapped[date] = mapped_column(Date, nullable=False)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="reported")
    police_report_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    insurance_claim_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    estimated_repair_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    actual_repair_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    resolution_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class MaintenanceRecord(Base, TimestampMixin):
    __tablename__ = "fleet_maintenance_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_fleet_maintenance_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_maintenance_tenant_profile",
        ),
        CheckConstraint(
            "status IN ('scheduled','in_progress','completed','cancelled')",
            name="ck_fleet_maintenance_status",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    profile_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    maintenance_type: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    odometer_at_service: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    next_service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    supplier_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    work_performed: Mapped[str | None] = mapped_column(Text, nullable=True)


class VehicleDocument(Base, TimestampMixin):
    __tablename__ = "fleet_vehicle_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_fleet_documents_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["fleet_vehicle_profiles.tenant_id", "fleet_vehicle_profiles.id"],
            ondelete="CASCADE",
            name="fk_fleet_documents_tenant_profile",
        ),
        Index("ix_fleet_documents_expiry", "tenant_id", "expiry_date"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    profile_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    document_type: Mapped[str] = mapped_column(String(60), nullable=False)
    document_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    file_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)


FLEET_TABLES = (
    "fleet_vehicle_profiles",
    "fleet_fuel_logs",
    "fleet_reservations",
    "fleet_incidents",
    "fleet_maintenance_records",
    "fleet_vehicle_documents",
)


class FleetError(RuntimeError):
    """A fleet rule refusal."""


def log_fuel(
    session: Session,
    *,
    tenant_id: UUID,
    profile_id: UUID,
    log_date: date,
    quantity_liters: Decimal,
    price_per_liter: Decimal,
    odometer_reading: Decimal | None = None,
    employee_id: UUID | None = None,
    station_name: str | None = None,
    receipt_number: str | None = None,
) -> FuelLogEntry:
    """Record fuel; total is computed, odometer only moves forward."""

    if quantity_liters <= 0 or price_per_liter < 0:
        raise FleetError("quantity must be positive and price non-negative")
    profile = session.scalar(
        select(VehicleProfile).where(VehicleProfile.tenant_id == tenant_id, VehicleProfile.id == profile_id)
    )
    if profile is None:
        raise FleetError("vehicle profile not found")
    if odometer_reading is not None:
        if odometer_reading < profile.current_odometer:
            raise FleetError("odometer reading is below the current odometer")
        profile.current_odometer = odometer_reading
    row = FuelLogEntry(
        tenant_id=tenant_id,
        profile_id=profile_id,
        employee_id=employee_id,
        log_date=log_date,
        quantity_liters=quantity_liters,
        price_per_liter=price_per_liter,
        total_cost=(quantity_liters * price_per_liter).quantize(Decimal("0.01")),
        odometer_reading=odometer_reading,
        station_name=station_name,
        receipt_number=receipt_number,
    )
    session.add(row)
    session.flush()
    return row


def approve_reservation(session: Session, *, tenant_id: UUID, reservation_id: UUID) -> VehicleReservation:
    """Approve only if no active reservation overlaps the window."""

    reservation = session.scalar(
        select(VehicleReservation).where(
            VehicleReservation.tenant_id == tenant_id,
            VehicleReservation.id == reservation_id,
        )
    )
    if reservation is None:
        raise FleetError("reservation not found")
    if reservation.status != ReservationStatus.REQUESTED.value:
        raise FleetError(f"reservation is {reservation.status!r}, not requested")
    clash = session.scalar(
        select(VehicleReservation).where(
            VehicleReservation.tenant_id == tenant_id,
            VehicleReservation.profile_id == reservation.profile_id,
            VehicleReservation.id != reservation.id,
            VehicleReservation.status.in_(_ACTIVE_RESERVATIONS),
            VehicleReservation.start_datetime < reservation.end_datetime,
            VehicleReservation.end_datetime > reservation.start_datetime,
        )
    )
    if clash is not None:
        raise FleetError("vehicle is already reserved for part of that window")
    reservation.status = ReservationStatus.APPROVED.value
    session.flush()
    return reservation


def complete_reservation(
    session: Session,
    *,
    tenant_id: UUID,
    reservation_id: UUID,
    end_odometer: Decimal,
) -> VehicleReservation:
    reservation = session.scalar(
        select(VehicleReservation).where(
            VehicleReservation.tenant_id == tenant_id,
            VehicleReservation.id == reservation_id,
        )
    )
    if reservation is None:
        raise FleetError("reservation not found")
    if reservation.status not in (ReservationStatus.APPROVED.value, ReservationStatus.IN_USE.value):
        raise FleetError(f"reservation is {reservation.status!r}, not active")
    profile = session.scalar(
        select(VehicleProfile).where(
            VehicleProfile.tenant_id == tenant_id,
            VehicleProfile.id == reservation.profile_id,
        )
    )
    if profile is not None and end_odometer < profile.current_odometer:
        raise FleetError("end odometer is below the current odometer")
    if profile is not None:
        profile.current_odometer = end_odometer
    reservation.end_odometer = end_odometer
    reservation.status = ReservationStatus.COMPLETED.value
    session.flush()
    return reservation


__all__ = [
    "FLEET_TABLES",
    "FleetError",
    "FuelLogEntry",
    "MaintenanceRecord",
    "ReservationStatus",
    "VehicleDocument",
    "VehicleIncident",
    "VehicleProfile",
    "VehicleReservation",
    "approve_reservation",
    "complete_reservation",
    "log_fuel",
]
