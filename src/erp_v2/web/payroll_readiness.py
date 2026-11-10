"""Read-only calculation evidence for one explicitly selected payroll run.

Evidence checks do not establish roster completeness or approval authority.
The external approver policy has not been supplied; authorization stays blocked.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from dotmac_payroll.models import PayrollCalculation, PayrollCalculationLine, PayrollRun
from sqlalchemy import select
from sqlalchemy.orm import Session


def approval_readiness(db: Session, *, tenant_id: UUID, run_id: UUID) -> dict[str, object] | None:
    """Return private presentation evidence without flushing or changing state."""
    with db.no_autoflush:
        run = db.scalar(select(PayrollRun).where(PayrollRun.tenant_id == tenant_id, PayrollRun.id == run_id))
        if run is None:
            return None
        calculations = db.scalars(
            select(PayrollCalculation)
            .where(PayrollCalculation.tenant_id == tenant_id, PayrollCalculation.run_id == run_id)
            .order_by(PayrollCalculation.id)
        ).all()
        lines = db.scalars(
            select(PayrollCalculationLine)
            .join(
                PayrollCalculation,
                (PayrollCalculation.tenant_id == PayrollCalculationLine.tenant_id)
                & (PayrollCalculation.id == PayrollCalculationLine.calculation_id),
            )
            .where(
                PayrollCalculationLine.tenant_id == tenant_id,
                PayrollCalculation.tenant_id == tenant_id,
                PayrollCalculation.run_id == run_id,
            )
            .order_by(PayrollCalculationLine.calculation_id, PayrollCalculationLine.sequence)
        ).all()

        totals = {
            field: sum((getattr(c, field) for c in calculations), Decimal(0))
            for field in ("gross_amount", "deduction_amount", "net_amount", "employer_liability_amount")
        }
        by_calculation: dict[UUID, list[PayrollCalculationLine]] = {}
        for line in lines:
            by_calculation.setdefault(line.calculation_id, []).append(line)
        missing_lines = sum(not by_calculation.get(c.id) for c in calculations)
        missing_references = sum(not (line.evidence_ref or "").strip() for line in lines)
        inconsistent = sum(c.gross_amount - c.deduction_amount != c.net_amount for c in calculations)
        line_mismatches = 0
        for calculation in calculations:
            recorded = by_calculation.get(calculation.id, [])
            if any(
                sum((line.amount for line in recorded if line.component_kind == kind), Decimal(0))
                != getattr(calculation, field)
                for kind, field in (
                    ("earning", "gross_amount"),
                    ("deduction", "deduction_amount"),
                    ("employer_liability", "employer_liability_amount"),
                )
            ):
                line_mismatches += 1
        evidence_blockers = []
        if not calculations:
            evidence_blockers.append("No employee calculations exist for this run.")
        if missing_lines:
            evidence_blockers.append(f"{missing_lines} calculation(s) have no component lines.")
        if missing_references:
            evidence_blockers.append(f"{missing_references} component line(s) lack an evidence reference.")
        if inconsistent:
            evidence_blockers.append(f"{inconsistent} calculation(s) have inconsistent gross, deductions and net.")
        if line_mismatches:
            evidence_blockers.append(f"{line_mismatches} calculation(s) disagree with their recorded component totals.")
        return {
            "schema_version": 1,
            "run": run,
            "calculation_count": len(calculations),
            "line_count": len(lines),
            "totals": totals,
            "evidence_blockers": evidence_blockers,
            "calculation_evidence_ready": not evidence_blockers,
            "approval_authorized": False,
            "approval_blockers": [
                "External approver policy and authorized approver roster have not been supplied.",
                "Expected employee roster completeness has not been verified.",
            ],
        }
