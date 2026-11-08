"""Payroll workspace reads over dotmac-payroll models.

Read-only presentation; run math (calculate/recalculate) stays in
the payroll service when the write workspace lands. Run totals are
summed from calculation rows, the module's evidence.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from dotmac_payroll.models import (
    EmployeePayAssignment,
    PayComponent,
    PayrollCalculation,
    PayrollRun,
    PayStructure,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def pay_components(db: Session, *, tenant_id: UUID) -> list[dict[str, object]]:
    rows = db.scalars(
        select(PayComponent).where(PayComponent.tenant_id == tenant_id).order_by(PayComponent.component_code)
    )
    return [{"code": c.component_code, "name": c.name, "kind": c.kind} for c in rows]


def pay_structures(db: Session, *, tenant_id: UUID) -> list[dict[str, object]]:
    rows = db.scalars(select(PayStructure).where(PayStructure.tenant_id == tenant_id).order_by(PayStructure.code))
    return [
        {
            "code": s.code,
            "name": s.name,
            "currency_code": s.currency_code,
            "status": s.status,
        }
        for s in rows
    ]


def assignment_count(db: Session, *, tenant_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(EmployeePayAssignment)
            .where(
                EmployeePayAssignment.tenant_id == tenant_id,
                EmployeePayAssignment.status == "active",
            )
        )
        or 0
    )


def payroll_runs(db: Session, *, tenant_id: UUID, limit: int = 20) -> list[dict[str, object]]:
    rows = db.execute(
        select(
            PayrollRun.run_ref,
            PayrollRun.period_start,
            PayrollRun.period_end,
            PayrollRun.status,
            PayrollRun.currency_code,
            func.count(PayrollCalculation.id).label("calculations"),
            func.coalesce(func.sum(PayrollCalculation.gross_amount), 0).label("gross"),
            func.coalesce(func.sum(PayrollCalculation.net_amount), 0).label("net"),
        )
        .outerjoin(
            PayrollCalculation,
            (PayrollCalculation.tenant_id == PayrollRun.tenant_id) & (PayrollCalculation.run_id == PayrollRun.id),
        )
        .where(PayrollRun.tenant_id == tenant_id)
        .group_by(
            PayrollRun.run_ref,
            PayrollRun.period_start,
            PayrollRun.period_end,
            PayrollRun.status,
            PayrollRun.currency_code,
        )
        .order_by(PayrollRun.period_start.desc())
        .limit(limit)
    ).mappings()
    return [
        {
            "run_ref": r["run_ref"],
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "status": r["status"],
            "currency_code": r["currency_code"],
            "calculations": int(r["calculations"]),
            "gross": Decimal(r["gross"]),
            "net": Decimal(r["net"]),
        }
        for r in rows
    ]


__all__ = ["assignment_count", "pay_components", "pay_structures", "payroll_runs"]
