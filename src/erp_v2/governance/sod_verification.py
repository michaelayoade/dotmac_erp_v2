"""Saved check (D-9 safeguard 4): separation was relaxed on a row created while the policy was ON.

Read-only. The policy timeline comes from the append-only ``erp.governance.dual_control_set`` audit events (each
carries the new value), so the question "was the policy ON when this row was created" has an evidence-based answer
even after the policy was switched off again. Two sources are checked:

* ``supplier_payments.separation_enforced = false`` (dotmac-payables a7 stamp; skipped, and reported as
  ``payables_stamp_available: false``, until a7 is pinned) created inside an ON window;
* ERP's payroll audit events (run approved, run finalized, payout line returned) whose recorded
  ``separation_enforced`` is false inside an ON window. ERP records the value it passed; dotmac-payroll has no
  stamp column of its own.

Rows or events from before ERP recorded the stamp carry no ``separation_enforced`` key and are not judged.

    MIGRATION_DATABASE_URL=... python -m erp_v2.governance.sod_verification --tenant-id <uuid>

exits 0 when clean, 3 when findings exist (the JSON lists entity kind and id, never secrets), 2 when refused.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from dotmac_kernel.audit import AuditEvent
from dotmac_payables.models import SupplierPayment
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.governance.policy import AUDIT_DUAL_CONTROL_SET
from erp_v2.payroll_payouts.service import AUDIT_LINE_RETURNED
from erp_v2.web.payroll_run_services import AUDIT_APPROVED, AUDIT_FINALIZED_POSTED

PAYROLL_ACTIONS = (AUDIT_APPROVED, AUDIT_FINALIZED_POSTED, AUDIT_LINE_RETURNED)


@dataclass(frozen=True, slots=True)
class Finding:
    source: str  # "supplier_payment" | "payroll_event"
    entity_id: str
    action: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Report:
    findings: tuple[Finding, ...]
    payables_stamp_available: bool
    on_windows: int

    @property
    def clean(self) -> bool:
        return not self.findings

    def receipt(self) -> dict[str, object]:
        return {
            "clean": self.clean,
            "payables_stamp_available": self.payables_stamp_available,
            "on_windows": self.on_windows,
            "findings": [{**asdict(f), "at": f.at.isoformat()} for f in self.findings],
        }


Window = tuple[datetime, datetime | None]


def _stamp(event: AuditEvent) -> datetime | None:
    return event.occurred_at or event.created_at


def on_windows(db: Session, tenant_id: UUID) -> list[Window]:
    """Half-open [start, end) intervals when the policy was ON, from the audit trail (end None = still ON)."""
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
    return windows


def _inside(moment: datetime | None, windows: list[Window]) -> bool:
    if moment is None:
        return False
    return any(moment >= start and (end is None or moment < end) for start, end in windows)


def verify(db: Session, tenant_id: UUID) -> Report:
    windows = on_windows(db, tenant_id)
    findings: list[Finding] = []
    column = SupplierPayment.__table__.c.get("separation_enforced")
    if column is not None and windows:
        for payment in db.scalars(
            select(SupplierPayment).where(SupplierPayment.tenant_id == tenant_id, column.is_(False))
        ):
            if _inside(payment.created_at, windows):
                findings.append(Finding("supplier_payment", str(payment.id), "relaxed_stamp", payment.created_at))
    if windows:
        for event in db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.action.in_(PAYROLL_ACTIONS),
                AuditEvent.is_success.is_(True),
            )
        ):
            if (event.details or {}).get("separation_enforced") is False and _inside(_stamp(event), windows):
                findings.append(Finding("payroll_event", str(event.entity_id), event.action, _stamp(event)))
    findings.sort(key=lambda f: (f.at, f.source, f.entity_id))
    return Report(tuple(findings), column is not None, len(windows))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.governance.sod_verification")
    parser.add_argument("--tenant-id", required=True)
    args = parser.parse_args(argv)
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
                report = verify(db, tenant_id)
                db.rollback()
        finally:
            engine.dispose()
    except Exception:
        print("sod verification refused: tenant id or operator database materials")
        return 2
    print(json.dumps(report.receipt(), indent=2))
    return 0 if report.clean else 3


if __name__ == "__main__":
    raise SystemExit(main())
