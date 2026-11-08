"""HR training events + employee credentials — ERP-unique slice 8.

Event training (programs/events/attendees) and credential records
(qualifications, certifications, competencies, skills). The legacy
in-ERP course LMS (training_course*/lessons/questions/exams) is NOT
rebuilt here: learning content belongs to Academy's dotmac-learning;
ERP keeps assignment/progress observations via the Academy boundary
(see importer pairings).
"""

from __future__ import annotations

import enum
from datetime import date, time
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from erp_v2.hr.models import _tenant_fk


class AttendeeStatus(str, enum.Enum):
    INVITED = "invited"
    CONFIRMED = "confirmed"
    ATTENDED = "attended"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"


_ATTENDEE_FLOW = {
    AttendeeStatus.INVITED: (AttendeeStatus.CONFIRMED, AttendeeStatus.CANCELLED),
    AttendeeStatus.CONFIRMED: (
        AttendeeStatus.ATTENDED,
        AttendeeStatus.NO_SHOW,
        AttendeeStatus.CANCELLED,
    ),
    AttendeeStatus.ATTENDED: (),
    AttendeeStatus.NO_SHOW: (),
    AttendeeStatus.CANCELLED: (),
}


class TrainingProgram(Base, TimestampMixin):
    __tablename__ = "hr_training_programs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_training_programs_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_training_programs_tenant_code"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    training_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    duration_hours: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    objectives: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class TrainingEvent(Base, TimestampMixin):
    __tablename__ = "hr_training_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_training_events_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "program_id"],
            ["hr_training_programs.tenant_id", "hr_training_programs.id"],
            ondelete="RESTRICT",
            name="fk_hr_training_events_tenant_program",
        ),
        CheckConstraint("end_date >= start_date", name="ck_hr_training_events_range"),
        CheckConstraint("status IN ('scheduled','completed','cancelled')", name="ck_hr_training_events_status"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    program_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    trainer_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    trainer_employee_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    max_attendees: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")


class TrainingAttendee(Base, TimestampMixin):
    __tablename__ = "hr_training_attendees"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_training_attendees_tenant_id_id"),
        UniqueConstraint("tenant_id", "event_id", "employee_id", name="uq_hr_attendee_event_employee"),
        ForeignKeyConstraint(
            ["tenant_id", "event_id"],
            ["hr_training_events.tenant_id", "hr_training_events.id"],
            ondelete="CASCADE",
            name="fk_hr_attendees_tenant_event",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_attendees_tenant_employee",
        ),
        CheckConstraint(
            "status IN ('invited','confirmed','attended','no_show','cancelled')",
            name="ck_hr_attendees_status",
        ),
        Index("ix_hr_attendees_tenant_event", "tenant_id", "event_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    event_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="invited")
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    certificate_issued: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    certificate_number: Mapped[str | None] = mapped_column(String(60), nullable=True)


class Qualification(Base, TimestampMixin):
    __tablename__ = "hr_qualifications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_qualifications_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_qualifications_tenant_employee",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    qualification_type: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    field_of_study: Mapped[str | None] = mapped_column(String(160), nullable=True)
    institution_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(60), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    document_file_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


class Certification(Base, TimestampMixin):
    __tablename__ = "hr_certifications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_certifications_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_certifications_tenant_employee",
        ),
        Index("ix_hr_certifications_expiry", "tenant_id", "expiry_date"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    issuing_authority: Mapped[str | None] = mapped_column(String(200), nullable=True)
    credential_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    does_not_expire: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())
    document_file_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


class Competency(Base, TimestampMixin):
    __tablename__ = "hr_competencies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_competencies_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_competencies_tenant_code"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class Skill(Base, TimestampMixin):
    __tablename__ = "hr_skills"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_skills_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_hr_skills_tenant_name"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())


class EmployeeSkill(Base, TimestampMixin):
    __tablename__ = "hr_employee_skills"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_employee_skills_tenant_id_id"),
        UniqueConstraint("tenant_id", "employee_id", "skill_id", name="uq_hr_employee_skill"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_employee_skills_tenant_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_id"],
            ["hr_skills.tenant_id", "hr_skills.id"],
            ondelete="RESTRICT",
            name="fk_hr_employee_skills_tenant_skill",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    skill_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    proficiency_level: Mapped[str] = mapped_column(String(30), nullable=False)
    years_experience: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    last_used_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    is_certified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())


TRAINING_TABLES = (
    "hr_training_programs",
    "hr_training_events",
    "hr_training_attendees",
    "hr_qualifications",
    "hr_certifications",
    "hr_competencies",
    "hr_skills",
    "hr_employee_skills",
)


class TrainingError(RuntimeError):
    """A training rule refusal."""


def invite_attendee(session: Session, *, tenant_id: UUID, event_id: UUID, employee_id: UUID) -> TrainingAttendee:
    """Invite if the event is scheduled and seats remain."""

    event = session.scalar(
        select(TrainingEvent).where(TrainingEvent.tenant_id == tenant_id, TrainingEvent.id == event_id)
    )
    if event is None:
        raise TrainingError("training event not found")
    if event.status != "scheduled":
        raise TrainingError(f"event is {event.status!r}, not scheduled")
    if event.max_attendees is not None:
        taken = (
            session.scalar(
                select(sa.func.count())
                .select_from(TrainingAttendee)
                .where(
                    TrainingAttendee.tenant_id == tenant_id,
                    TrainingAttendee.event_id == event_id,
                    TrainingAttendee.status != AttendeeStatus.CANCELLED.value,
                )
            )
            or 0
        )
        if taken >= event.max_attendees:
            raise TrainingError("event is full")
    row = TrainingAttendee(tenant_id=tenant_id, event_id=event_id, employee_id=employee_id)
    session.add(row)
    session.flush()
    return row


def transition_attendee(
    session: Session,
    *,
    tenant_id: UUID,
    attendee_id: UUID,
    to_status: AttendeeStatus,
    rating: Decimal | None = None,
    certificate_number: str | None = None,
) -> TrainingAttendee:
    row = session.scalar(
        select(TrainingAttendee).where(TrainingAttendee.tenant_id == tenant_id, TrainingAttendee.id == attendee_id)
    )
    if row is None:
        raise TrainingError("attendee not found")
    current = AttendeeStatus(row.status)
    if to_status not in _ATTENDEE_FLOW[current]:
        allowed = ", ".join(s.value for s in _ATTENDEE_FLOW[current]) or "none (terminal)"
        raise TrainingError(f"cannot move {current.value!r} -> {to_status.value!r}; allowed: {allowed}")
    if rating is not None:
        if to_status is not AttendeeStatus.ATTENDED:
            raise TrainingError("ratings attach only on attendance")
        if not (Decimal(0) <= rating <= Decimal(5)):
            raise TrainingError("rating outside 0..5")
        row.rating = rating
    if certificate_number is not None:
        row.certificate_issued = True
        row.certificate_number = certificate_number
    row.status = to_status.value
    session.flush()
    return row


def expiring_certifications(session: Session, *, tenant_id: UUID, before: date) -> list[Certification]:
    """Active, expiring (non-perpetual) certifications due before a date."""

    return list(
        session.scalars(
            select(Certification)
            .where(
                Certification.tenant_id == tenant_id,
                Certification.is_active.is_(True),
                Certification.does_not_expire.is_(False),
                Certification.expiry_date.is_not(None),
                Certification.expiry_date <= before,
            )
            .order_by(Certification.expiry_date)
        )
    )


__all__ = [
    "TRAINING_TABLES",
    "AttendeeStatus",
    "Certification",
    "Competency",
    "EmployeeSkill",
    "Qualification",
    "Skill",
    "TrainingAttendee",
    "TrainingError",
    "TrainingEvent",
    "TrainingProgram",
    "expiring_certifications",
    "invite_attendee",
    "transition_attendee",
]
