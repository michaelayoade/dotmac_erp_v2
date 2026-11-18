"""Seed helper: a RECORDED staff-loan opening row, written directly (no GL, no headroom check).

Other workflow tests (B4 internal recovery, B5 payouts) need a loan balance to exist before a payroll run may
recover against it (``loan_recovery_exceeds_balance``). The governed flow is covered by test_staff_loans_pg.py.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from erp_v2.staff_loans.models import StaffLoanEntry


def seed_recorded_opening(engine, tenant_id, employee_ref, amount, *, value_date=date(2025, 12, 31)):
    when = datetime(2026, 1, 1, tzinfo=UTC)
    actor = uuid4()
    with Session(engine) as db:
        row = StaffLoanEntry(
            tenant_id=tenant_id,
            request_id=uuid4(),
            employee_ref=employee_ref,
            kind="opening",
            amount=Decimal(amount),
            evidence_ref="test schedule",
            value_date=value_date,
            state="recorded",
            prepared_by_party_id=actor,
            prepared_at=when,
            approved_by_party_id=actor,
            approved_at=when,
            recorded_by_party_id=actor,
            recorded_at=when,
        )
        db.add(row)
        db.commit()
        return row.id
