"""HR discipline + grievances — ERP-unique slice 5.

Case lifecycle is a guarded status machine (reported -> queried ->
hearing -> decided -> [appealed ->] closed). Documents are
files-module references (opaque file ids), never stored bytes.
Responses and witness statements are append-only rows; case actions
(warnings, suspensions) carry effective windows for payroll/reviews.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from uuid import UUID

from dotmac_kernel.models import Base, TimestampMixin, uuid_pk
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from erp_v2.hr.models import _tenant_fk


class CaseStatus(str, enum.Enum):
    REPORTED = "reported"
    QUERIED = "queried"
    HEARING = "hearing"
    DECIDED = "decided"
    APPEALED = "appealed"
    CLOSED = "closed"
    WITHDRAWN = "withdrawn"


_FLOW = {
    CaseStatus.REPORTED: (CaseStatus.QUERIED, CaseStatus.WITHDRAWN),
    CaseStatus.QUERIED: (CaseStatus.HEARING, CaseStatus.DECIDED, CaseStatus.WITHDRAWN),
    CaseStatus.HEARING: (CaseStatus.DECIDED,),
    CaseStatus.DECIDED: (CaseStatus.APPEALED, CaseStatus.CLOSED),
    CaseStatus.APPEALED: (CaseStatus.CLOSED,),
    CaseStatus.CLOSED: (),
    CaseStatus.WITHDRAWN: (),
}


class DisciplinaryCase(Base, TimestampMixin):
    __tablename__ = "hr_disciplinary_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_disciplinary_cases_tenant_id_id"),
        UniqueConstraint("tenant_id", "number", name="uq_hr_disciplinary_cases_tenant_number"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_cases_tenant_employee",
        ),
        CheckConstraint(
            "status IN ('reported','queried','hearing','decided','appealed','closed','withdrawn')",
            name="ck_hr_cases_status",
        ),
        CheckConstraint("severity IN ('minor','major','gross')", name="ck_hr_cases_severity"),
        Index("ix_hr_cases_tenant_employee", "tenant_id", "employee_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    employee_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    violation_type: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    incident_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reported_date: Mapped[date] = mapped_column(Date, nullable=False)
    hearing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    decision_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    appeal_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="reported")
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    investigating_officer_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    panel_chair_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


class CaseResponse(Base, TimestampMixin):
    __tablename__ = "hr_case_responses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_case_responses_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["hr_disciplinary_cases.tenant_id", "hr_disciplinary_cases.id"],
            ondelete="CASCADE",
            name="fk_hr_responses_tenant_case",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    case_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_appeal_response: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseWitness(Base, TimestampMixin):
    __tablename__ = "hr_case_witnesses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_case_witnesses_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["hr_disciplinary_cases.tenant_id", "hr_disciplinary_cases.id"],
            ondelete="CASCADE",
            name="fk_hr_witnesses_tenant_case",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    case_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    employee_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    external_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class CaseAction(Base, TimestampMixin):
    __tablename__ = "hr_case_actions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_case_actions_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["hr_disciplinary_cases.tenant_id", "hr_disciplinary_cases.id"],
            ondelete="CASCADE",
            name="fk_hr_actions_tenant_case",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    case_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    warning_expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    issued_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


class CaseDocument(Base, TimestampMixin):
    __tablename__ = "hr_case_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_case_documents_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["hr_disciplinary_cases.tenant_id", "hr_disciplinary_cases.id"],
            ondelete="CASCADE",
            name="fk_hr_documents_tenant_case",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    case_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    document_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # Opaque dotmac-files stored-file id; bytes never live here.
    file_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    uploaded_by_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


class Grievance(Base, TimestampMixin):
    __tablename__ = "hr_grievances"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_hr_grievances_tenant_id_id"),
        UniqueConstraint("tenant_id", "number", name="uq_hr_grievances_tenant_number"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["mod_people.employees.tenant_id", "mod_people.employees.id"],
            ondelete="CASCADE",
            name="fk_hr_grievances_tenant_employee",
        ),
        CheckConstraint(
            "status IN ('open','under_review','resolved','closed','escalated')",
            name="ck_hr_grievances_status",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    employee_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    assigned_to_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sla_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)


DISCIPLINE_TABLES = (
    "hr_disciplinary_cases",
    "hr_case_responses",
    "hr_case_witnesses",
    "hr_case_actions",
    "hr_case_documents",
    "hr_grievances",
)


class DisciplineError(RuntimeError):
    """A discipline/grievance rule refusal."""


def _case(session: Session, tenant_id: UUID, case_id: UUID) -> DisciplinaryCase:
    case = session.scalar(
        select(DisciplinaryCase).where(DisciplinaryCase.tenant_id == tenant_id, DisciplinaryCase.id == case_id)
    )
    if case is None:
        raise DisciplineError("disciplinary case not found")
    return case


def transition_case(
    session: Session,
    *,
    tenant_id: UUID,
    case_id: UUID,
    to_status: CaseStatus,
    on: date | None = None,
    decision_summary: str | None = None,
    appeal_reason: str | None = None,
    appeal_decision: str | None = None,
) -> DisciplinaryCase:
    case = _case(session, tenant_id, case_id)
    current = CaseStatus(case.status)
    if to_status not in _FLOW[current]:
        allowed = ", ".join(s.value for s in _FLOW[current]) or "none (terminal)"
        raise DisciplineError(f"cannot move {current.value!r} -> {to_status.value!r}; allowed: {allowed}")
    if to_status is CaseStatus.APPEALED:
        if case.appeal_deadline is not None and on is not None and on > case.appeal_deadline:
            raise DisciplineError("appeal filed after the appeal deadline")
        case.appeal_reason = appeal_reason
    if to_status is CaseStatus.DECIDED:
        case.decision_date = on
        if decision_summary is not None:
            case.decision_summary = decision_summary
    if to_status is CaseStatus.CLOSED:
        case.closed_date = on
        if current is CaseStatus.APPEALED:
            case.appeal_decision = appeal_decision
    case.status = to_status.value
    session.flush()
    return case


def respond_to_case(
    session: Session,
    *,
    tenant_id: UUID,
    case_id: UUID,
    response_text: str,
    is_appeal_response: bool = False,
) -> CaseResponse:
    case = _case(session, tenant_id, case_id)
    if CaseStatus(case.status) in (CaseStatus.CLOSED, CaseStatus.WITHDRAWN):
        raise DisciplineError("cannot respond to a closed case")
    if is_appeal_response and case.status != CaseStatus.APPEALED.value:
        raise DisciplineError("appeal responses require an appealed case")
    row = CaseResponse(
        tenant_id=tenant_id,
        case_id=case_id,
        response_text=response_text,
        is_appeal_response=is_appeal_response,
        submitted_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def resolve_grievance(
    session: Session,
    *,
    tenant_id: UUID,
    grievance_id: UUID,
    resolution: str,
    resolution_date: date,
) -> Grievance:
    grievance = session.scalar(select(Grievance).where(Grievance.tenant_id == tenant_id, Grievance.id == grievance_id))
    if grievance is None:
        raise DisciplineError("grievance not found")
    if grievance.status in ("resolved", "closed"):
        raise DisciplineError(f"grievance already {grievance.status!r}")
    grievance.resolution = resolution
    grievance.resolution_date = resolution_date
    grievance.status = "resolved"
    session.flush()
    return grievance


__all__ = [
    "DISCIPLINE_TABLES",
    "CaseAction",
    "CaseDocument",
    "CaseResponse",
    "CaseStatus",
    "CaseWitness",
    "DisciplinaryCase",
    "DisciplineError",
    "Grievance",
    "resolve_grievance",
    "respond_to_case",
    "transition_case",
]
