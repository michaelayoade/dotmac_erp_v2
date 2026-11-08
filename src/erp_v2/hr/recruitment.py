"""HR recruitment — ERP-unique slice 6 (legacy: job_opening,
job_applicant, interview, job_offer).

Guarded pipeline: applied -> screening -> interview -> offer ->
hired, with rejected/withdrawn exits. Accepting an offer is the only
transition that consumes a position slot, and it refuses to overfill.
Candidate self-service tokens stay hashes here; the public careers
surface is a later web slice.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
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


class ApplicantStatus(str, enum.Enum):
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    HIRED = "hired"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


_FLOW: dict[ApplicantStatus, tuple[ApplicantStatus, ...]] = {
    ApplicantStatus.APPLIED: (ApplicantStatus.SCREENING, ApplicantStatus.REJECTED, ApplicantStatus.WITHDRAWN),
    ApplicantStatus.SCREENING: (ApplicantStatus.INTERVIEW, ApplicantStatus.REJECTED, ApplicantStatus.WITHDRAWN),
    ApplicantStatus.INTERVIEW: (ApplicantStatus.OFFER, ApplicantStatus.REJECTED, ApplicantStatus.WITHDRAWN),
    ApplicantStatus.OFFER: (ApplicantStatus.HIRED, ApplicantStatus.REJECTED, ApplicantStatus.WITHDRAWN),
    ApplicantStatus.HIRED: (),
    ApplicantStatus.REJECTED: (),
    ApplicantStatus.WITHDRAWN: (),
}


class JobOpening(Base, TimestampMixin):
    __tablename__ = "hr_job_openings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_job_openings_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_hr_job_openings_tenant_code"),
        CheckConstraint("status IN ('draft','open','closed','cancelled')", name="ck_hr_job_openings_status"),
        CheckConstraint("positions_filled <= number_of_positions", name="ck_hr_job_openings_filled"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    department_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    designation_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    position_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    number_of_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    positions_filled: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    posted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    closes_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_remote: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")


class JobApplicant(Base, TimestampMixin):
    __tablename__ = "hr_job_applicants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_job_applicants_tenant_id_id"),
        UniqueConstraint("tenant_id", "application_number", name="uq_hr_job_applicants_number"),
        ForeignKeyConstraint(
            ["tenant_id", "job_opening_id"],
            ["hr_job_openings.tenant_id", "hr_job_openings.id"],
            ondelete="CASCADE",
            name="fk_hr_applicants_tenant_opening",
        ),
        CheckConstraint(
            "status IN ('applied','screening','interview','offer','hired','rejected','withdrawn')",
            name="ck_hr_applicants_status",
        ),
        Index("ix_hr_applicants_tenant_opening", "tenant_id", "job_opening_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    application_number: Mapped[str] = mapped_column(String(30), nullable=False)
    job_opening_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    current_employer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    years_of_experience: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    highest_qualification: Mapped[str | None] = mapped_column(String(160), nullable=True)
    applied_on: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    resume_file_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="applied")
    overall_rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Interview(Base, TimestampMixin):
    __tablename__ = "hr_interviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_interviews_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "applicant_id"],
            ["hr_job_applicants.tenant_id", "hr_job_applicants.id"],
            ondelete="CASCADE",
            name="fk_hr_interviews_tenant_applicant",
        ),
        CheckConstraint("status IN ('scheduled','completed','cancelled','no_show')", name="ck_hr_interviews_status"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    applicant_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    interview_type: Mapped[str] = mapped_column(String(40), nullable=False, default="panel")
    scheduled_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    interviewer_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(30), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobOffer(Base, TimestampMixin):
    __tablename__ = "hr_job_offers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_job_offers_tenant_id_id"),
        UniqueConstraint("tenant_id", "number", name="uq_hr_job_offers_tenant_number"),
        ForeignKeyConstraint(
            ["tenant_id", "applicant_id"],
            ["hr_job_applicants.tenant_id", "hr_job_applicants.id"],
            ondelete="CASCADE",
            name="fk_hr_offers_tenant_applicant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "job_opening_id"],
            ["hr_job_openings.tenant_id", "hr_job_openings.id"],
            ondelete="RESTRICT",
            name="fk_hr_offers_tenant_opening",
        ),
        CheckConstraint(
            "status IN ('draft','extended','accepted','declined','expired','withdrawn')",
            name="ck_hr_job_offers_status",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    applicant_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    job_opening_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    offer_date: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date] = mapped_column(Date, nullable=False)
    expected_joining_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    base_salary: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    responded_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    converted_to_employee_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


RECRUITMENT_TABLES = ("hr_job_openings", "hr_job_applicants", "hr_interviews", "hr_job_offers")


class RecruitmentError(RuntimeError):
    """A recruitment rule refusal."""


def _applicant(session: Session, tenant_id: UUID, applicant_id: UUID) -> JobApplicant:
    row = session.scalar(
        select(JobApplicant).where(JobApplicant.tenant_id == tenant_id, JobApplicant.id == applicant_id)
    )
    if row is None:
        raise RecruitmentError("applicant not found")
    return row


def advance_applicant(
    session: Session, *, tenant_id: UUID, applicant_id: UUID, to_status: ApplicantStatus
) -> JobApplicant:
    applicant = _applicant(session, tenant_id, applicant_id)
    current = ApplicantStatus(applicant.status)
    if to_status is ApplicantStatus.HIRED:
        raise RecruitmentError("hiring happens by accepting an offer, not by advancing")
    if to_status not in _FLOW[current]:
        allowed = ", ".join(s.value for s in _FLOW[current]) or "none (terminal)"
        raise RecruitmentError(f"cannot move {current.value!r} -> {to_status.value!r}; allowed: {allowed}")
    applicant.status = to_status.value
    session.flush()
    return applicant


def record_interview_outcome(
    session: Session,
    *,
    tenant_id: UUID,
    interview_id: UUID,
    rating: Decimal,
    recommendation: str,
    feedback: str | None = None,
    rating_scale_max: Decimal = Decimal(5),
) -> Interview:
    interview = session.scalar(select(Interview).where(Interview.tenant_id == tenant_id, Interview.id == interview_id))
    if interview is None:
        raise RecruitmentError("interview not found")
    if interview.status != "scheduled":
        raise RecruitmentError(f"interview already {interview.status!r}")
    if not (Decimal(0) <= rating <= rating_scale_max):
        raise RecruitmentError(f"rating outside 0..{rating_scale_max}")
    interview.rating = rating
    interview.recommendation = recommendation
    interview.feedback = feedback
    interview.status = "completed"
    session.flush()
    return interview


def respond_offer(
    session: Session,
    *,
    tenant_id: UUID,
    offer_id: UUID,
    accept: bool,
    on: date,
    decline_reason: str | None = None,
) -> JobOffer:
    """Accept/decline an extended offer; acceptance consumes one slot."""

    offer = session.scalar(select(JobOffer).where(JobOffer.tenant_id == tenant_id, JobOffer.id == offer_id))
    if offer is None:
        raise RecruitmentError("offer not found")
    if offer.status != "extended":
        raise RecruitmentError(f"offer is {offer.status!r}, not extended")
    if on > offer.valid_until:
        offer.status = "expired"
        session.flush()
        raise RecruitmentError("offer validity has expired")
    applicant = _applicant(session, tenant_id, offer.applicant_id)
    offer.responded_on = on
    if not accept:
        offer.status = "declined"
        offer.decline_reason = decline_reason
        applicant.status = ApplicantStatus.WITHDRAWN.value
        session.flush()
        return offer
    opening = session.scalar(
        select(JobOpening).where(JobOpening.tenant_id == tenant_id, JobOpening.id == offer.job_opening_id)
    )
    if opening is None or opening.positions_filled >= opening.number_of_positions:
        raise RecruitmentError("all positions for this opening are already filled")
    opening.positions_filled += 1
    if opening.positions_filled >= opening.number_of_positions:
        opening.status = "closed"
    offer.status = "accepted"
    applicant.status = ApplicantStatus.HIRED.value
    session.flush()
    return offer


__all__ = [
    "RECRUITMENT_TABLES",
    "ApplicantStatus",
    "Interview",
    "JobApplicant",
    "JobOffer",
    "JobOpening",
    "RecruitmentError",
    "advance_applicant",
    "record_interview_outcome",
    "respond_offer",
]
