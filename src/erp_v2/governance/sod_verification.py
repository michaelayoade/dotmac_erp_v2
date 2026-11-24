"""Saved check (D-9 safeguard 4): separation was relaxed on a row created while the policy was ON.

Read-only (the command runs in a READ ONLY transaction). The policy timeline comes from the append-only
``erp.governance.dual_control_set`` audit events (each carries the new value), so the question "was the policy ON
when this row was created" has an evidence-based answer even after the policy was switched off again. All times are
the database clock (audit ``created_at``); nothing compares against an application clock.

The check REFUSES (exit 2, ``policy_timeline_inconsistent``) rather than reporting clean when the evidence cannot
be trusted: the timeline's final state differs from the current policy row, or the last event is not within
``CLOCK_TOLERANCE`` of the row's ``set_at`` (the row was changed outside the audited operator CLI, or restored).

Findings (exit 3):

* ``supplier_payments.separation_enforced = false`` (dotmac-payables a7 stamp) whose ERP ``prepared``
  audit event falls inside an ON window, or has no such event (unjudged);
* ERP's payroll audit events (batch calculated, run approved, run finalized, payout line returned) inside an ON
  window whose recorded ``separation_enforced`` is false (``relaxed``) or absent (``unstamped``).

    MIGRATION_DATABASE_URL=... python -m erp_v2.governance.sod_verification --tenant-id <uuid>

The receipt also carries ``pa_0007_stamped_true``: a read-only count (not a finding) of payments stamped true whose
``prepared`` event is missing or outside every ON window, the shape pa_0007 gives rows that existed before a7.

exits 0 when clean, 3 when findings exist (the JSON lists entity kind and id, never secrets), 2 when refused.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import UUID

from dotmac_kernel.audit import AuditEvent
from dotmac_payables.models import SupplierPayment
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from erp_v2.governance.models import GovernancePolicy
from erp_v2.governance.policy import AUDIT_DUAL_CONTROL_SET
from erp_v2.payroll_payouts.service import AUDIT_LINE_RETURNED
from erp_v2.supplier_payments.service import AUDIT_PREPARED
from erp_v2.web.payroll_run_services import AUDIT_APPROVED, AUDIT_BATCH_CALCULATED, AUDIT_FINALIZED_POSTED

PAYROLL_ACTIONS = (AUDIT_BATCH_CALCULATED, AUDIT_APPROVED, AUDIT_FINALIZED_POSTED, AUDIT_LINE_RETURNED)

# The CLI stamps ``set_at`` from the application clock and the audit event from the database clock in one
# transaction; anything further apart than this means the row was changed outside the audited CLI.
CLOCK_TOLERANCE = timedelta(minutes=5)

KEY = "separation_enforced"


class TimelineInconsistent(RuntimeError):
    """The audit timeline cannot be reconciled with the current policy row: refuse, never report clean."""


@dataclass(frozen=True, slots=True)
class Finding:
    source: str  # "supplier_payment" | "payroll_event"
    entity_id: str
    action: str  # "relaxed_stamp" | "unjudged" | the payroll audit action
    kind: str  # "relaxed" | "unstamped" | "unjudged"
    at: datetime | None


@dataclass(frozen=True, slots=True)
class Report:
    findings: tuple[Finding, ...]
    payables_stamp_available: bool
    on_windows: int
    # Read-only count (never a finding): payments stamped true whose ERP ``prepared`` event is missing or outside
    # every ON window, i.e. the shape pa_0007 gives pre-a7 rows (stamp true, policy not ON at creation). While the
    # policy is OFF each of them writes a ``sod_stamp_flagged`` event per step and still needs two people.
    pa_0007_stamped_true: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    def receipt(self) -> dict[str, object]:
        return {
            "clean": self.clean,
            "payables_stamp_available": self.payables_stamp_available,
            "on_windows": self.on_windows,
            "pa_0007_stamped_true": self.pa_0007_stamped_true,
            "findings": [{**asdict(f), "at": f.at.isoformat() if f.at else None} for f in self.findings],
        }


Window = tuple[datetime, datetime | None]


def _stamp(event: AuditEvent) -> datetime | None:
    """The database-clock persistence time. ``occurred_at`` is caller-supplyable and never used."""
    return event.created_at


def policy_timeline(db: Session, tenant_id: UUID) -> tuple[list[Window], datetime | None]:
    """ON windows [start, end) from the audit trail (end None = still ON) and the time of the last event."""
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == AUDIT_DUAL_CONTROL_SET)
    ).all()
    ordered = sorted((e for e in events if _stamp(e) is not None), key=lambda e: _stamp(e))
    windows: list[Window] = []
    start: datetime | None = None
    for event in ordered:
        value = (event.details or {}).get("dual_control")
        if value is True and start is None:
            start = _stamp(event)
        elif value is False and start is not None:
            windows.append((start, _stamp(event)))
            start = None
    if start is not None:
        windows.append((start, None))
    return windows, (_stamp(ordered[-1]) if ordered else None)


def on_windows(db: Session, tenant_id: UUID) -> list[Window]:
    return policy_timeline(db, tenant_id)[0]


def _reconcile(db: Session, tenant_id: UUID, windows: list[Window], last_event: datetime | None) -> None:
    row = db.get(GovernancePolicy, tenant_id)
    current = bool(row.dual_control) if row is not None else False
    timeline_on = bool(windows) and windows[-1][1] is None
    if current != timeline_on:
        raise TimelineInconsistent("the policy row disagrees with the audit timeline")
    if last_event is not None and row is not None and abs(row.set_at - last_event) > CLOCK_TOLERANCE:
        raise TimelineInconsistent("the policy row was changed outside the audited operator command")
    if last_event is None and row is not None and current:
        raise TimelineInconsistent("the policy is ON with no audit event")


def _inside(moment: datetime | None, windows: list[Window]) -> bool:
    if moment is None:
        return False
    return any(moment >= start and (end is None or moment < end) for start, end in windows)


def verify(db: Session, tenant_id: UUID) -> Report:
    windows, last_event = policy_timeline(db, tenant_id)
    _reconcile(db, tenant_id, windows, last_event)
    findings: list[Finding] = []
    backfilled = 0
    column = SupplierPayment.__table__.c.get(KEY)
    if column is not None:
        prepared = {
            e.entity_id: _stamp(e)
            for e in db.scalars(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.action == AUDIT_PREPARED,
                    AuditEvent.is_success.is_(True),
                )
            )
        }
        for payment in db.scalars(
            select(SupplierPayment).where(SupplierPayment.tenant_id == tenant_id, column.is_(True))
        ):
            if not _inside(prepared.get(str(payment.id)), windows):
                backfilled += 1
        for payment in db.scalars(
            select(SupplierPayment).where(SupplierPayment.tenant_id == tenant_id, column.is_(False))
        ):
            created = prepared.get(str(payment.id))
            if created is None:
                if windows:
                    findings.append(Finding("supplier_payment", str(payment.id), "unjudged", "unjudged", None))
            elif _inside(created, windows):
                findings.append(Finding("supplier_payment", str(payment.id), "relaxed_stamp", "relaxed", created))
    if windows:
        for event in db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.action.in_(PAYROLL_ACTIONS),
                AuditEvent.is_success.is_(True),
            )
        ):
            if not _inside(_stamp(event), windows):
                continue
            recorded = (event.details or {}).get(KEY)
            if recorded is False:
                findings.append(Finding("payroll_event", str(event.entity_id), event.action, "relaxed", _stamp(event)))
            elif recorded is None:
                findings.append(
                    Finding("payroll_event", str(event.entity_id), event.action, "unstamped", _stamp(event))
                )
    findings.sort(key=lambda f: (f.at is None, f.at or datetime.min, f.source, f.entity_id))
    return Report(tuple(findings), column is not None, len(windows), backfilled)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.governance.sod_verification")
    parser.add_argument("--tenant-id", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    refusal = "sod_verification_refused"
    try:
        tenant_id = UUID(args.tenant_id)
        url = os.environ.pop("MIGRATION_DATABASE_URL", "")
        if not url or "DATABASE_URL" in os.environ:
            raise ValueError
        import sqlalchemy as sa

        engine = sa.create_engine(url, echo=False, hide_parameters=True)
        try:
            if engine.dialect.name != "postgresql":
                raise ValueError
            with Session(engine) as db, db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                report = verify(db, tenant_id)
        finally:
            engine.dispose()
    except TimelineInconsistent:
        print(json.dumps({"refusal": "policy_timeline_inconsistent"}))
        return 2
    except Exception:
        print(json.dumps({"refusal": refusal}))
        return 2
    print(json.dumps(report.receipt(), indent=2))
    return 0 if report.clean else 3


if __name__ == "__main__":
    raise SystemExit(main())
