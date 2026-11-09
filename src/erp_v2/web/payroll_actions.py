"""Draft payroll orchestration over the released flush-only payroll owner."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.money import Currency, Money
from dotmac_payroll import service
from dotmac_payroll.contracts import EmployeeComponentInput
from dotmac_payroll.models import (
    EmployeePayAssignment,
    PayComponent,
    PayrollCalculation,
    PayrollRun,
    PayStructureRule,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


class PayrollActionError(ValueError):
    """A payroll form was refused without committing application state."""


def _text(value: str, label: str, maximum: int) -> str:
    value = value.strip()
    if not value or len(value) > maximum:
        raise PayrollActionError(f"{label} is required (maximum {maximum} characters)")
    return value


def exact_decimal(value: str, *, label: str, places: int, maximum: Decimal) -> Decimal:
    """Reject non-finite, exponent, negative, rounded and oversized input."""
    value = value.strip()
    if len(value) > 40 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise PayrollActionError(f"{label} must be a non-negative decimal number")
    result = Decimal(value)
    if -result.as_tuple().exponent > places or result > maximum:
        raise PayrollActionError(f"{label} exceeds the allowed precision or range")
    return result


def create_draft(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    run_ref: str,
    period_start: str,
    period_end: str,
    currency_code: str,
    minor_units: str,
) -> PayrollRun:
    run_ref = _text(run_ref, "Run reference", 160)
    try:
        start, end = date.fromisoformat(period_start), date.fromisoformat(period_end)
    except ValueError as exc:
        raise PayrollActionError("Enter both period dates as YYYY-MM-DD") from exc
    if not re.fullmatch(r"[0-6]", minor_units):
        raise PayrollActionError("Currency decimal places must be between 0 and 6")
    code = currency_code.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise PayrollActionError("Enter a three-letter currency code")
    try:
        with db.begin_nested():
            return service.create_payroll_run(
                db,
                tenant_id=tenant.id,
                run_ref=run_ref,
                period_start=start,
                period_end=end,
                currency=Currency(code, int(minor_units)),
                created_by_id=party.id,
                created_at=datetime.now(UTC),
            )
    except (service.PayrollConflict, service.PayrollRuleViolation) as exc:
        raise PayrollActionError(str(exc)) from exc
    except IntegrityError as exc:
        raise PayrollActionError("Run reference already exists or conflicts with payroll data") from exc


def calculate_employee(
    db: Session,
    *,
    tenant: Tenant,
    run_id: UUID,
    employee_ref: str,
    proration_factor: str,
    component_inputs: str,
) -> PayrollCalculation:
    employee_ref = _text(employee_ref, "Employee reference", 240)
    factor = exact_decimal(proration_factor, label="Proration", places=8, maximum=Decimal(1))
    if len(component_inputs) > 64000:
        raise PayrollActionError("Component inputs are too long")
    run = db.scalar(select(PayrollRun).where(PayrollRun.tenant_id == tenant.id, PayrollRun.id == run_id))
    if run is None:
        raise PayrollActionError("Payroll run not found")
    currency = Currency(run.currency_code, run.minor_units)
    # An explicit input must belong to this employee's effective input rules.
    # The owner still resolves assignment ambiguity and all calculation policy.
    components = dict(
        db.execute(
            select(PayComponent.component_code, PayComponent.id)
            .join(
                PayStructureRule,
                (PayStructureRule.tenant_id == PayComponent.tenant_id)
                & (PayStructureRule.component_id == PayComponent.id),
            )
            .join(
                EmployeePayAssignment,
                (EmployeePayAssignment.tenant_id == PayStructureRule.tenant_id)
                & (EmployeePayAssignment.revision_id == PayStructureRule.revision_id),
            )
            .where(
                PayComponent.tenant_id == tenant.id,
                EmployeePayAssignment.tenant_id == tenant.id,
                EmployeePayAssignment.employee_ref == employee_ref,
                EmployeePayAssignment.status == "active",
                EmployeePayAssignment.effective_from <= run.period_end,
                (EmployeePayAssignment.effective_to.is_(None)) | (EmployeePayAssignment.effective_to >= run.period_end),
                PayStructureRule.calculation_method == "input",
            )
        ).all()
    )
    inputs = []
    seen = set()
    for line in component_inputs.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise PayrollActionError("Use one component code | amount | evidence reference per line")
        code, amount, evidence = (part.strip() for part in parts)
        if code not in components:
            raise PayrollActionError("Component is not an input rule for this employee in this tenant")
        if code in seen:
            raise PayrollActionError("Each input component must appear only once")
        seen.add(code)
        value = exact_decimal(amount, label="Amount", places=run.minor_units, maximum=Decimal("99999999999999"))
        inputs.append(
            EmployeeComponentInput(
                component_id=components[code],
                amount=Money.of(value, currency),
                evidence_ref=_text(evidence, "Evidence reference", 240),
            )
        )
    try:
        with db.begin_nested():
            return service.calculate_employee_payroll(
                db,
                tenant_id=tenant.id,
                run_id=run.id,
                employee_ref=employee_ref,
                proration_factor=factor,
                inputs=tuple(inputs),
                calculated_at=datetime.now(UTC),
            )
    except (service.PayrollNotFound, service.PayrollConflict, service.PayrollRuleViolation) as exc:
        raise PayrollActionError(str(exc)) from exc
    except IntegrityError as exc:
        raise PayrollActionError("Calculation conflicts with existing payroll evidence") from exc
