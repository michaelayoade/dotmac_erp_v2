"""Destination receiver: atomic dedupe + completion consequence.

One `receive` call, one caller-owned transaction (the route's
`tenant_session`): the receipt row, the observation row, and the
certification upsert land or roll back together. The receiver never
commits. A duplicate key with the same fingerprint answers
ALREADY_APPLIED with the original destination ref; the same key with a
different fingerprint is a REJECTED conflict. An unmatched employee is
REJECTED without a receipt row, so the Academy checkpoint stays
unstamped and the backlog remains visible and retryable — the same
posture as the legacy v2 webhook's `ignored` outcome.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from dotmac_app_sync import SyncAcceptance, SyncEnvelope, SyncReceipt
from dotmac_people.models import Employee
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.academy_sync.models import AcademyLearningCompletion, AcademySyncReceipt
from erp_v2.hr.training import Certification

ERROR_FINGERPRINT_CONFLICT = "fingerprint_conflict"
ERROR_EMPLOYEE_UNMATCHED = "employee_unmatched"


def _destination_ref(completion_id: UUID) -> str:
    return f"academy_learning_completion:{completion_id}"


class CompletionSyncReceiver:
    """`SyncReceiver` for `learning.completion.observation.v1`."""

    def __init__(self, db: Session, *, tenant_id: UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    def _existing(self, idempotency_key: str) -> AcademySyncReceipt | None:
        return self._db.scalars(
            select(AcademySyncReceipt).where(
                AcademySyncReceipt.tenant_id == self._tenant_id,
                AcademySyncReceipt.idempotency_key == idempotency_key,
            )
        ).one_or_none()

    @staticmethod
    def _verdict(existing: AcademySyncReceipt, fingerprint: str) -> SyncReceipt:
        if existing.fingerprint != fingerprint:
            return SyncReceipt(acceptance=SyncAcceptance.REJECTED, error_code=ERROR_FINGERPRINT_CONFLICT)
        return SyncReceipt(acceptance=SyncAcceptance.ALREADY_APPLIED, destination_ref=existing.destination_ref)

    def receive(self, *, envelope: SyncEnvelope, idempotency_key: str, fingerprint: str) -> SyncReceipt:
        existing = self._existing(idempotency_key)
        if existing is not None:
            return self._verdict(existing, fingerprint)

        payload = envelope.payload
        employee = self._db.scalars(
            select(Employee).where(
                Employee.tenant_id == self._tenant_id,
                Employee.employee_code == payload["employee_ref"],
            )
        ).one_or_none()
        if employee is None:
            return SyncReceipt(acceptance=SyncAcceptance.REJECTED, error_code=ERROR_EMPLOYEE_UNMATCHED)

        completed_on_raw = payload.get("completed_on")
        completion = AcademyLearningCompletion(
            tenant_id=self._tenant_id,
            employee_id=employee.id,
            employee_ref=str(payload["employee_ref"]),
            academy_course_ref=str(payload["academy_course_ref"]),
            academy_enrollment_ref=payload.get("academy_enrollment_ref"),
            course_title=str(payload["course_title"]),
            course_version=payload.get("course_version"),
            progress_pct=Decimal(str(payload["progress_pct"])),
            passed=bool(payload["passed"]),
            completed_on=date.fromisoformat(completed_on_raw) if completed_on_raw else None,
            certificate_ref=payload.get("certificate_ref"),
            source_event_id=envelope.source_event_id,
        )
        try:
            with self._db.begin_nested():
                self._db.add(completion)
                self._db.flush()
                certification = self._upsert_certification(completion)
                completion.certification_id = certification.id if certification is not None else None
                self._db.add(
                    AcademySyncReceipt(
                        tenant_id=self._tenant_id,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        capability_id=envelope.capability_id,
                        source_application=envelope.source_application,
                        source_event_id=envelope.source_event_id,
                        destination_ref=_destination_ref(completion.id),
                    )
                )
                self._db.flush()
        except IntegrityError:
            # A concurrent delivery won the key (or the event id). The
            # savepoint rollback left the winner's rows intact; answer
            # from its receipt instead of failing the sender's retry.
            winner = self._existing(idempotency_key)
            if winner is None:
                raise
            return self._verdict(winner, fingerprint)
        return SyncReceipt(acceptance=SyncAcceptance.ACCEPTED, destination_ref=_destination_ref(completion.id))

    def _upsert_certification(self, completion: AcademyLearningCompletion) -> Certification | None:
        """Mirror the legacy completion consequence: an employee credential.

        Keyed by the Academy certificate reference when present (stable
        across redelivery), else by (employee, course title, issue date).
        """
        query = select(Certification).where(
            Certification.tenant_id == self._tenant_id,
            Certification.employee_id == completion.employee_id,
        )
        if completion.certificate_ref:
            query = query.where(Certification.credential_id == completion.certificate_ref)
        else:
            query = query.where(
                Certification.name == completion.course_title,
                Certification.issue_date == completion.completed_on,
            )
        certification = self._db.scalars(query).one_or_none()
        if certification is None:
            certification = Certification(tenant_id=self._tenant_id, employee_id=completion.employee_id)
            self._db.add(certification)
        certification.name = completion.course_title
        certification.credential_id = completion.certificate_ref
        certification.issue_date = completion.completed_on
        # The observation comes from the authoritative source system over
        # an authenticated channel; the credential is verified at source.
        certification.is_verified = True
        self._db.flush()
        return certification


__all__ = [
    "ERROR_EMPLOYEE_UNMATCHED",
    "ERROR_FINGERPRINT_CONFLICT",
    "CompletionSyncReceiver",
]
