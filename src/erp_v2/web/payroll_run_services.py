"""Payroll run batch calculate, approve and finalize-and-post (B4).

Four governed commands over the released Starter payroll owner (0.1.0a4); nothing here
changes an amount, creates an account or a mapping, or moves money.

* ``batch_calculate``: calculate EVERY active assignment effective at the run's period end
  in one savepoint (all or nothing), using the released ``calculate_employee_payroll``.
  Employees whose structure has input rules need rows (with mandatory evidence references)
  in the uploaded input file; a missing, surplus or malformed row refuses the whole batch.
* ``approve``: payroll_approver; creator != approver.
* ``finalize``: payroll_finalizer; creator != approver != finalizer. The payroll owner's
  ``finalize_payroll_run`` (liabilities), the run's accounting consequence and the posting
  bridge all happen in ONE savepoint: no liability without a ledger entry and no ledger
  entry without liabilities. Posted journal: Dr earnings and employer-liability expense,
  Cr deduction/employer liability accounts, Cr 2040 Payroll Payable = total net pay.

Guards that apply at batch, approve and finalize (each fails closed with a stable code):
the run currency must equal the tenant functional currency (a typed setting, never a literal);
the period must not overlap ANY other non-cancelled run (this covers imported legacy runs, which
carry no marker); every component line must have a Finance-supplied GL account mapping that
resolves to exactly one active postable tenant account. Account setup and mappings are Finance
input and are never created here (see docs/payroll-account-setup.md on PR #42 for the 2133 NHIS
Payable account and the 1410 Staff Loan reuse; new revisions consume its validated UUIDs).

Loan recovery (payroll a4 ``settlement_mode``): a deduction with ``internal_recovery`` is recovered
against the employee's own balance and must map to an ASSET-class postable account (for example
1410 Staff Loan); the owner books it as already recovered, never as an outstanding payable. An
``external`` deduction (the default) must map to a LIABILITY-class account. A mismatch either way is
refused, so an internal recovery can never become a payable and an external payable can never
credit an asset. Payouts independently refuse internal recoveries (B5).

Leavers: an active assignment ending inside the period (no assignment at the period end) is part of
the roster. The owner calculates it at min(period end, assignment end); the batch never prorates, so
an uncalculated leaver refuses the batch (``proration_required``) and blocks approval until it is
calculated with the single-employee form and its proration factor.

Cancel: ``cancel_run`` calls the owner ``cancel_payroll_run`` (draft or approved only, mandatory
reason, stored on the run) and writes an audit event. It never touches a finalized run.

Replay: finalizing an already finalized run that has its journal returns it unchanged
(no write, no audit). Finalized without a journal is refused and referred to the exceptions
review; nothing is repaired here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from dotmac_accounting.contracts import AccountClass, AccountKind, JournalStatus
from dotmac_accounting.models import Account, AccountCategory, JournalEntry
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency import IdempotencyConflict
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.money import Currency, Money
from dotmac_payroll import service as payroll
from dotmac_payroll.consequences import (
    CONSEQUENCE_OWNER,
    CONSEQUENCE_SOURCE_VERSION,
    RUN_DOCUMENT_KIND,
    build_run_accounting_consequence,
)
from dotmac_payroll.contracts import EmployeeComponentInput
from dotmac_payroll.models import (
    EmployeePayAssignment,
    PayrollCalculation,
    PayrollCalculationLine,
    PayrollRun,
    PayStructureRevision,
    PayStructureRule,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.journals.policy import FunctionalCurrencyError, functional_currency
from erp_v2.posting import rules
from erp_v2.posting.payroll import post_payroll_consequence
from erp_v2.posting.rules import PostingRefused
from erp_v2.staff_loans import queries as staff_loans
from erp_v2.web import payroll_readiness
from erp_v2.web.payroll_run_input import EMPTY, InputFileRefused, ParsedInput, parse_input_file

PAYROLL_PAYABLE_CODE = "2040"  # Payroll Payable, confirmed by Michael; also the bridge's declared code ref
AUDIT_BATCH_CALCULATED = "erp.payroll.run.batch_calculated"
AUDIT_APPROVED = "erp.payroll.run.approved"
AUDIT_FINALIZED_POSTED = "erp.payroll.run.finalized_posted"
AUDIT_CANCELLED = "erp.payroll.run.cancelled"
AUDIT_REFUSED = "erp.payroll.run.command_refused"
AUDIT_ACTIONS = (AUDIT_BATCH_CALCULATED, AUDIT_APPROVED, AUDIT_FINALIZED_POSTED, AUDIT_CANCELLED, AUDIT_REFUSED)
MAX_REASON_CHARS = 500
_KINDS_WITH_EXPENSE = ("earning", "employer_liability")
_KINDS_WITH_LIABILITY = ("deduction", "employer_liability")
_LISTED = 10
# Internal recoveries may credit only these account codes (Finance-declared, like 2040): a bank cash GL or a
# receivables control account must never be credited with no cash moving.
INTERNAL_RECOVERY_CODES = frozenset({"1410"})


class RunRefused(ValueError):
    """A safe, user-facing refusal with a stable ``code``; nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    run_id: UUID
    calculated: int
    skipped_existing: int
    input_sha256: str | None
    input_rows: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ApproveOutcome:
    run_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    run_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class FinalizeOutcome:
    run_id: UUID
    journal_id: UUID
    journal_number: str
    net_total: Decimal
    replayed: bool


def _listed(items: list[str]) -> str:
    shown = ", ".join(sorted(items)[:_LISTED])
    return shown + (f" and {len(items) - _LISTED} more" if len(items) > _LISTED else "")


def _audit(db: Session, *, tenant: Tenant, party: Party, run_id: UUID, action: str, success: bool, details) -> None:
    write_audit_event(
        db,
        tenant_id=tenant.id,
        actor_party_id=party.id,
        actor_type="user",
        actor_id=str(party.id),
        actor_label=getattr(party, "email", None) or None,
        source_application="dotmac_erp",
        action=action,
        entity_type="payroll_run",
        entity_id=str(run_id),
        is_success=success,
        details=details,
    )


# --- guards ------------------------------------------------------------------------------------


def _locked_run(db: Session, tenant_id: UUID, run_id: UUID) -> PayrollRun:
    run = db.scalar(
        select(PayrollRun).where(PayrollRun.tenant_id == tenant_id, PayrollRun.id == run_id).with_for_update()
    )
    if run is None:
        raise RunRefused("not_found", "Payroll run was not found")
    return run


def check_currency(db: Session, run: PayrollRun) -> str:
    """The run currency must equal the tenant functional currency (typed setting)."""
    try:
        functional = functional_currency(db, run.tenant_id)
    except FunctionalCurrencyError as exc:
        raise RunRefused(
            exc.code, "The tenant has no functional currency setting, so payroll cannot be posted"
        ) from exc
    if run.currency_code != functional:
        raise RunRefused(
            "non_functional_currency",
            f"Run currency {run.currency_code} differs from the functional currency {functional}",
        )
    return functional


def check_no_overlap(db: Session, run: PayrollRun) -> None:
    """Refuse a period overlapping any other non-cancelled run, imported legacy runs included."""
    others = db.scalars(
        select(PayrollRun.run_ref).where(
            PayrollRun.tenant_id == run.tenant_id,
            PayrollRun.id != run.id,
            PayrollRun.status != "cancelled",
            PayrollRun.period_start <= run.period_end,
            PayrollRun.period_end >= run.period_start,
        )
    ).all()
    if others:
        raise RunRefused(
            "period_overlaps_existing_run",
            f"The period overlaps existing payroll run(s): {_listed(list(others))}. "
            "Imported legacy runs and earlier runs are never recalculated or duplicated",
        )


def _active_assignments(db: Session, run: PayrollRun) -> list[EmployeePayAssignment]:
    return list(
        db.scalars(
            select(EmployeePayAssignment)
            .where(
                EmployeePayAssignment.tenant_id == run.tenant_id,
                EmployeePayAssignment.status == "active",
                EmployeePayAssignment.effective_from <= run.period_end,
                (EmployeePayAssignment.effective_to.is_(None)) | (EmployeePayAssignment.effective_to >= run.period_end),
            )
            .order_by(EmployeePayAssignment.employee_ref)
        )
    )


def _leavers(db: Session, run: PayrollRun) -> list[EmployeePayAssignment]:
    """Active assignments ending inside the period for employees with none at the period end.

    Payroll a4 calculates these at min(period end, assignment end); the roster must include them so a
    leaver is neither silently unpaid nor refused as a calculation without an assignment.
    """
    stayers = {a.employee_ref for a in _active_assignments(db, run)}
    rows = db.scalars(
        select(EmployeePayAssignment)
        .where(
            EmployeePayAssignment.tenant_id == run.tenant_id,
            EmployeePayAssignment.status == "active",
            EmployeePayAssignment.effective_from <= run.period_end,
            EmployeePayAssignment.effective_to.is_not(None),
            EmployeePayAssignment.effective_to < run.period_end,
            EmployeePayAssignment.effective_to >= run.period_start,
        )
        .order_by(EmployeePayAssignment.employee_ref)
    )
    return [a for a in rows if a.employee_ref not in stayers]


def check_roster_complete(db: Session, run: PayrollRun) -> None:
    expected = {a.employee_ref for a in _active_assignments(db, run)}
    expected |= {a.employee_ref for a in _leavers(db, run)}
    calculated = set(
        db.scalars(
            select(PayrollCalculation.employee_ref).where(
                PayrollCalculation.tenant_id == run.tenant_id, PayrollCalculation.run_id == run.id
            )
        )
    )
    if not expected:
        raise RunRefused("no_active_assignments", "There are no active pay assignments for this period")
    missing = sorted(expected - calculated)
    if missing:
        raise RunRefused(
            "roster_incomplete",
            f"{len(missing)} active employee(s) have no calculation in this run: {_listed(missing)}",
        )
    extra = sorted(calculated - expected)
    if extra:
        raise RunRefused(
            "calculation_without_active_assignment",
            f"{len(extra)} calculated employee(s) have no active assignment at the period end: {_listed(extra)}",
        )


def check_evidence(db: Session, run: PayrollRun) -> dict[str, object]:
    packet = payroll_readiness.approval_readiness(db, tenant_id=run.tenant_id, run_id=run.id)
    if packet is None:  # pragma: no cover - the run was just loaded
        raise RunRefused("not_found", "Payroll run was not found")
    blockers = packet["evidence_blockers"]
    if blockers:
        raise RunRefused("evidence_incomplete", " ".join(blockers))  # type: ignore[arg-type]
    return packet


def _resolve(db: Session, tenant_id: UUID, ref: str, *, label: str) -> Account:
    policy = rules.policy_for(CONSEQUENCE_OWNER, RUN_DOCUMENT_KIND)
    try:
        form, value = rules.classify_ref(policy, ref)
    except PostingRefused as exc:
        raise RunRefused(
            "mapping_ref_form_refused", f"{label}: the account reference is not in a declared form"
        ) from exc
    column = Account.id == UUID(value) if form == "uuid" else Account.code == value
    rows = list(db.scalars(select(Account).where(Account.tenant_id == tenant_id, column)))
    if len(rows) != 1:
        raise RunRefused("mapping_account_unknown", f"{label}: the mapped account does not exist")
    account = rows[0]
    if not account.is_active or not account.posting_allowed or account.kind != AccountKind.POSTING:
        raise RunRefused(
            "mapping_account_unusable", f"{label}: the mapped account {account.code} is not open for posting"
        )
    return account


def _account_class(db: Session, account: Account) -> AccountClass | None:
    return db.scalar(
        select(AccountCategory.account_class).where(
            AccountCategory.tenant_id == account.tenant_id, AccountCategory.id == account.category_id
        )
    )


def check_mappings(db: Session, run: PayrollRun) -> UUID:
    """Every component line and revision has a Finance mapping that resolves; returns the 2040 account id.

    Refuses on the FIRST class of problem it finds, but lists every unmapped component of that
    class. Never creates or edits an account or a mapping.
    """
    tenant_id = run.tenant_id
    lines = list(
        db.execute(
            select(PayrollCalculationLine, PayrollCalculation.revision_id)
            .join(
                PayrollCalculation,
                (PayrollCalculation.tenant_id == PayrollCalculationLine.tenant_id)
                & (PayrollCalculation.id == PayrollCalculationLine.calculation_id),
            )
            .where(PayrollCalculation.tenant_id == tenant_id, PayrollCalculation.run_id == run.id)
        ).all()
    )
    if not lines:
        raise RunRefused("no_calculations", "The run has no calculations")
    unmapped: dict[str, set[str]] = {}
    liability_refs: dict[str, set[str]] = {}
    internal_refs: dict[str, set[str]] = {}
    expense_refs: dict[str, set[str]] = {}
    for line, _revision in lines:
        code = line.component_code
        if line.component_kind == "information":
            continue
        if line.component_kind not in (*_KINDS_WITH_EXPENSE, *_KINDS_WITH_LIABILITY):
            raise RunRefused("unknown_component_kind", f"Component {code} has an unknown kind")
        if line.component_kind in _KINDS_WITH_EXPENSE:
            if not (line.expense_account_ref or "").strip():
                unmapped.setdefault("expense account", set()).add(code)
            else:
                expense_refs.setdefault(line.expense_account_ref, set()).add(code)
        if line.component_kind in _KINDS_WITH_LIABILITY:
            if not (line.liability_account_ref or "").strip():
                unmapped.setdefault("liability account", set()).add(code)
            elif line.settlement_mode == "internal_recovery":
                if line.component_kind != "deduction":
                    raise RunRefused(
                        "internal_recovery_not_deduction",
                        f"Component {code}: only a deduction can be an internal recovery",
                    )
                internal_refs.setdefault(line.liability_account_ref, set()).add(code)
            elif line.settlement_mode == "external":
                liability_refs.setdefault(line.liability_account_ref, set()).add(code)
            else:
                raise RunRefused("unknown_settlement_mode", f"Component {code} has an unknown settlement mode")
            if (
                line.settlement_mode == "external"
                and line.amount > 0
                and not (line.liability_destination_ref or "").strip()
            ):
                unmapped.setdefault("remittance destination", set()).add(code)
    revision_ids = {revision for _line, revision in lines}
    net_refs: dict[str, set[str]] = {}
    for revision in db.scalars(
        select(PayStructureRevision).where(
            PayStructureRevision.tenant_id == tenant_id, PayStructureRevision.id.in_(revision_ids)
        )
    ):
        if not (revision.net_payable_account_ref or "").strip():
            unmapped.setdefault("net payable account", set()).add(f"revision {revision.id}")
        else:
            net_refs.setdefault(revision.net_payable_account_ref, set()).add(f"revision {revision.id}")
    if unmapped:
        detail = "; ".join(f"{what}: {_listed(sorted(who))}" for what, who in sorted(unmapped.items()))
        raise RunRefused(
            "missing_account_mapping",
            "Finance has not mapped every component to a ledger account (" + detail + "). "
            "Mappings are Finance input and are not created here",
        )
    payable = _resolve(db, tenant_id, PAYROLL_PAYABLE_CODE, label="Payroll Payable")
    for ref, who in net_refs.items():
        if _resolve(db, tenant_id, ref, label=f"Net payable ({_listed(sorted(who))})").id != payable.id:
            raise RunRefused(
                "net_payable_not_2040",
                f"The net payable account of {_listed(sorted(who))} is not {PAYROLL_PAYABLE_CODE} Payroll Payable",
            )
    for ref, who in expense_refs.items():
        account = _resolve(db, tenant_id, ref, label=f"Expense account of {_listed(sorted(who))}")
        if _account_class(db, account) != AccountClass.EXPENSE:
            raise RunRefused(
                "mapping_account_class_refused",
                f"{_listed(sorted(who))}: expense mapping {account.code} must be an EXPENSE-class account",
            )
    for ref, who in liability_refs.items():
        account = _resolve(db, tenant_id, ref, label=f"Liability account of {_listed(sorted(who))}")
        klass = _account_class(db, account)
        if klass != AccountClass.LIABILITY:
            raise RunRefused(
                "mapping_account_class_refused",
                f"{_listed(sorted(who))}: an external deduction or employer liability maps to {account.code}, "
                "which must be a LIABILITY-class account (an asset recovery must be marked internal_recovery "
                "on the pay structure rule)",
            )
        if account.id == payable.id:
            raise RunRefused(
                "deduction_mapped_to_net_payable",
                f"{_listed(sorted(who))} is mapped to {PAYROLL_PAYABLE_CODE}, which holds net pay only",
            )
    for ref, who in internal_refs.items():
        account = _resolve(db, tenant_id, ref, label=f"Recovery account of {_listed(sorted(who))}")
        if account.code not in INTERNAL_RECOVERY_CODES:
            raise RunRefused(
                "internal_recovery_account_not_allowed",
                f"{_listed(sorted(who))}: internal recoveries may credit only "
                f"{', '.join(sorted(INTERNAL_RECOVERY_CODES))}, not {account.code}",
            )
        if _account_class(db, account) != AccountClass.ASSET:
            raise RunRefused(
                "internal_recovery_account_not_asset",
                f"{_listed(sorted(who))}: an internal recovery maps to {account.code}, which must be an "
                "ASSET-class account (for example 1410 Staff Loan); it is never a payable",
            )
    return payable.id


def check_loan_recovery(db: Session, run: PayrollRun) -> None:
    """B4 loan check: an employee's internal recovery may not exceed that employee's staff-loan balance.

    The balance is the staff-loan subledger balance (``erp_v2.staff_loans``) before this run, so a run that
    recovers more than the employee owes is refused and nothing is posted. Takes the tenant's staff-loan lock
    so a concurrent loan entry cannot change the balance between the check and the finalize.
    """
    rows = db.execute(
        select(PayrollCalculation.employee_ref, PayrollCalculationLine.amount)
        .join(
            PayrollCalculation,
            (PayrollCalculation.tenant_id == PayrollCalculationLine.tenant_id)
            & (PayrollCalculation.id == PayrollCalculationLine.calculation_id),
        )
        .where(
            PayrollCalculation.tenant_id == run.tenant_id,
            PayrollCalculation.run_id == run.id,
            PayrollCalculationLine.component_kind == "deduction",
            PayrollCalculationLine.settlement_mode == "internal_recovery",
            PayrollCalculationLine.amount > 0,
        )
    ).all()
    recovery: dict[str, Decimal] = {}
    for employee_ref, amount in rows:
        recovery[employee_ref] = recovery.get(employee_ref, Decimal(0)) + amount
    if not recovery:
        return
    staff_loans.lock_book(db, run.tenant_id)
    over = [
        ref
        for ref, amount in sorted(recovery.items())
        if amount
        > staff_loans.employee_balance(db, tenant_id=run.tenant_id, employee_ref=ref, exclude_run_id=run.id).balance
    ]
    if over:
        raise RunRefused(
            "loan_recovery_exceeds_balance",
            f"The loan recovery exceeds the staff-loan balance of: {_listed(over)}. "
            "Record the employee's opening or advance first, or correct the recovery input",
        )


# --- batch calculate -----------------------------------------------------------------------------


def _input_rules(db: Session, run: PayrollRun, revision_ids: set[UUID]) -> dict[UUID, dict[str, UUID]]:
    result: dict[UUID, dict[str, UUID]] = {r: {} for r in revision_ids}
    for rule in db.scalars(
        select(PayStructureRule).where(
            PayStructureRule.tenant_id == run.tenant_id,
            PayStructureRule.revision_id.in_(revision_ids),
            PayStructureRule.calculation_method == "input",
        )
    ):
        result[rule.revision_id][rule.component_code] = rule.component_id
    return result


def _guarded_batch(
    db: Session, *, tenant: Tenant, party: Party, run_id: UUID, parsed: ParsedInput, now: datetime
) -> BatchOutcome:
    run = _locked_run(db, tenant.id, run_id)
    if run.status != "draft":
        raise RunRefused("not_draft", "Only a draft payroll run can be calculated")
    if run.created_by_id != party.id:
        raise RunRefused(
            "sod_not_run_creator",
            "Only the person who created this run can enter its calculations; they cannot also approve it",
        )
    check_currency(db, run)
    check_no_overlap(db, run)
    assignments = _active_assignments(db, run)
    if not assignments:
        raise RunRefused("no_active_assignments", "There are no active pay assignments for this period")
    by_employee: dict[str, list[EmployeePayAssignment]] = {}
    for assignment in assignments:
        by_employee.setdefault(assignment.employee_ref, []).append(assignment)
    ambiguous = sorted(e for e, rows in by_employee.items() if len(rows) > 1)
    if ambiguous:
        raise RunRefused("ambiguous_assignment", f"Employees with several active assignments: {_listed(ambiguous)}")
    calculated = set(
        db.scalars(
            select(PayrollCalculation.employee_ref).where(
                PayrollCalculation.tenant_id == tenant.id, PayrollCalculation.run_id == run.id
            )
        )
    )
    todo = [a for a in assignments if a.employee_ref not in calculated]
    partial = sorted(a.employee_ref for a in todo if a.effective_from > run.period_start)
    if partial:
        raise RunRefused(
            "proration_required",
            f"{len(partial)} employee(s) joined after the period start and need a proration factor: "
            f"{_listed(partial)}. Calculate them with the single-employee form, then run the batch again",
        )
    leavers = sorted({a.employee_ref for a in _leavers(db, run) if a.employee_ref not in calculated})
    if leavers:
        raise RunRefused(
            "proration_required",
            f"{len(leavers)} employee(s) left before the period end and need a proration factor: "
            f"{_listed(leavers)}. Calculate them with the single-employee form, then run the batch again",
        )
    stray = sorted(set(parsed.by_employee) - {a.employee_ref for a in todo})
    if stray:
        raise RunRefused(
            "input_for_unknown_or_calculated_employee",
            f"The input file names {len(stray)} employee(s) that are not awaiting calculation: {_listed(stray)}",
        )
    rules_by_revision = _input_rules(db, run, {a.revision_id for a in todo})
    problems: list[str] = []
    missing_for: list[str] = []
    prepared: list[tuple[str, tuple[EmployeeComponentInput, ...]]] = []
    currency = Currency(run.currency_code, run.minor_units)
    for assignment in todo:
        employee = assignment.employee_ref
        wanted = rules_by_revision[assignment.revision_id]
        supplied = parsed.by_employee.get(employee, {})
        surplus = sorted(set(supplied) - set(wanted))
        lacking = sorted(set(wanted) - set(supplied))
        if surplus:
            problems.append(f"{employee}: not an input component of the assigned structure: {_listed(surplus)}")
        if lacking:
            missing_for.append(employee)
        prepared.append(
            (
                employee,
                tuple(
                    EmployeeComponentInput(
                        component_id=wanted[code],
                        amount=Money.of(row.amount, currency),
                        evidence_ref=row.evidence_ref,
                    )
                    for code, row in sorted(supplied.items())
                    if code in wanted
                ),
            )
        )
    if missing_for:
        raise RunRefused(
            "input_file_missing_rows",
            f"{len(missing_for)} employee(s) have input rules but no complete rows with evidence references "
            f"in the uploaded file: {_listed(missing_for)}",
        )
    if problems:
        raise RunRefused("input_file_surplus_rows", "; ".join(problems[:_LISTED]))
    for employee, inputs in prepared:
        try:
            payroll.calculate_employee_payroll(
                db,
                tenant_id=tenant.id,
                run_id=run.id,
                employee_ref=employee,
                proration_factor=Decimal(1),
                inputs=inputs,
                calculated_at=now,
            )
        except (payroll.PayrollNotFound, payroll.PayrollConflict, payroll.PayrollRuleViolation) as exc:
            raise RunRefused("calculation_refused", f"{employee}: {exc}") from exc
    return BatchOutcome(
        run.id,
        calculated=len(prepared),
        skipped_existing=len(calculated),
        input_sha256=parsed.sha256 if parsed.row_count else None,
        input_rows=parsed.row_count,
        replayed=not prepared,
    )


def batch_calculate(
    db: Session,
    *,
    tenant: Tenant,
    party: Party,
    run_id: UUID,
    input_bytes: bytes | None,
    now: datetime | None = None,
) -> BatchOutcome:
    """Calculate every active assignment not yet calculated, all or nothing."""
    if party.tenant_id != tenant.id:
        raise RunRefused("actor_tenant", "The acting party must belong to this tenant")
    moment = now or datetime.now(UTC)
    try:
        run_scale = db.scalar(
            select(PayrollRun.minor_units).where(PayrollRun.tenant_id == tenant.id, PayrollRun.id == run_id)
        )
        if run_scale is None:
            raise RunRefused("not_found", "Payroll run was not found")
        try:
            parsed = parse_input_file(input_bytes, minor_units=run_scale) if input_bytes else EMPTY
        except InputFileRefused as exc:
            raise RunRefused("input_file_refused", "; ".join(exc.problems[:_LISTED])) from exc
        with db.begin_nested():
            outcome = _guarded_batch(db, tenant=tenant, party=party, run_id=run_id, parsed=parsed, now=moment)
            if outcome.calculated:
                _audit(
                    db,
                    tenant=tenant,
                    party=party,
                    run_id=run_id,
                    action=AUDIT_BATCH_CALCULATED,
                    success=True,
                    details={
                        "calculated": outcome.calculated,
                        "skipped_existing": outcome.skipped_existing,
                        "input_sha256": outcome.input_sha256,
                        "input_rows": outcome.input_rows,
                    },
                )
            return outcome
    except RunRefused as exc:
        refusal = exc
    except (payroll.PayrollNotFound, payroll.PayrollConflict, payroll.PayrollRuleViolation) as exc:
        refusal = RunRefused("payroll_refused", str(exc))
    except IntegrityError:
        refusal = RunRefused("conflict", "A concurrent change prevented the calculation; reload and retry")
    _refuse(db, tenant=tenant, party=party, run_id=run_id, command="batch_calculate", refusal=refusal)


def _refuse(db: Session, *, tenant: Tenant, party: Party, run_id: UUID, command: str, refusal: RunRefused):
    if refusal.code != "not_found":
        # The savepoint rolled back; the refusal is recorded in the caller's transaction.
        _audit(
            db,
            tenant=tenant,
            party=party,
            run_id=run_id,
            action=AUDIT_REFUSED,
            success=False,
            details={"command": command, "code": refusal.code},
        )
    raise refusal


# --- approve -------------------------------------------------------------------------------------


def _guarded_approve(db: Session, *, tenant: Tenant, party: Party, run_id: UUID, now: datetime) -> ApproveOutcome:
    run = _locked_run(db, tenant.id, run_id)
    if run.status in ("approved", "finalized") and run.approved_by_id is not None:
        return ApproveOutcome(run.id, True)
    if run.status != "draft":
        raise RunRefused("not_draft", "Only a draft payroll run can be approved")
    if run.created_by_id == party.id:
        raise RunRefused("sod_creator_is_approver", "You created this run; another person must approve it")
    check_currency(db, run)
    check_no_overlap(db, run)
    check_evidence(db, run)
    check_roster_complete(db, run)
    check_mappings(db, run)
    check_loan_recovery(db, run)
    payroll.approve_payroll_run(db, tenant_id=tenant.id, run_id=run.id, approved_by_id=party.id, approved_at=now)
    _audit(
        db,
        tenant=tenant,
        party=party,
        run_id=run.id,
        action=AUDIT_APPROVED,
        success=True,
        details={"created_by": str(run.created_by_id)},
    )
    return ApproveOutcome(run.id, False)


def approve(db: Session, *, tenant: Tenant, party: Party, run_id: UUID, now: datetime | None = None) -> ApproveOutcome:
    if party.tenant_id != tenant.id:
        raise RunRefused("actor_tenant", "The acting party must belong to this tenant")
    moment = now or datetime.now(UTC)
    try:
        with db.begin_nested():
            return _guarded_approve(db, tenant=tenant, party=party, run_id=run_id, now=moment)
    except RunRefused as exc:
        refusal = exc
    except (payroll.PayrollNotFound, payroll.PayrollConflict, payroll.PayrollRuleViolation) as exc:
        refusal = RunRefused("payroll_refused", str(exc))
    _refuse(db, tenant=tenant, party=party, run_id=run_id, command="approve", refusal=refusal)


# --- cancel --------------------------------------------------------------------------------------


def _guarded_cancel(
    db: Session, *, tenant: Tenant, party: Party, run_id: UUID, reason: str, now: datetime
) -> CancelOutcome:
    cleaned = (reason or "").strip()
    if not cleaned:
        raise RunRefused("reason_required", "A reason is required to cancel a payroll run")
    if len(cleaned) > MAX_REASON_CHARS:
        raise RunRefused("reason_too_long", f"The reason must be at most {MAX_REASON_CHARS} characters")
    run = _locked_run(db, tenant.id, run_id)
    if run.status == "cancelled" and run.cancelled_by_id == party.id and run.cancel_reason == cleaned:
        return CancelOutcome(run.id, True)
    if run.status not in ("draft", "approved"):
        raise RunRefused("not_cancellable", f"A {run.status} payroll run cannot be cancelled")
    previous = run.status
    payroll.cancel_payroll_run(
        db, tenant_id=tenant.id, run_id=run.id, cancelled_by_id=party.id, reason=cleaned, cancelled_at=now
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        run_id=run.id,
        action=AUDIT_CANCELLED,
        success=True,
        details={"previous_status": previous, "reason": cleaned, "created_by": str(run.created_by_id)},
    )
    return CancelOutcome(run.id, False)


def cancel_run(
    db: Session, *, tenant: Tenant, party: Party, run_id: UUID, reason: str, now: datetime | None = None
) -> CancelOutcome:
    """Cancel a draft or approved run through the owner command; a finalized run is never cancellable."""
    if party.tenant_id != tenant.id:
        raise RunRefused("actor_tenant", "The acting party must belong to this tenant")
    moment = now or datetime.now(UTC)
    try:
        with db.begin_nested():
            return _guarded_cancel(db, tenant=tenant, party=party, run_id=run_id, reason=reason, now=moment)
    except RunRefused as exc:
        refusal = exc
    except (payroll.PayrollNotFound, payroll.PayrollConflict, payroll.PayrollRuleViolation) as exc:
        refusal = RunRefused("payroll_refused", str(exc))
    _refuse(db, tenant=tenant, party=party, run_id=run_id, command="cancel", refusal=refusal)


# --- finalize + post -----------------------------------------------------------------------------


def _existing_journal(db: Session, tenant_id: UUID, run_id: UUID) -> JournalEntry | None:
    return db.scalar(
        select(JournalEntry).where(
            JournalEntry.tenant_id == tenant_id,
            JournalEntry.source_owner == CONSEQUENCE_OWNER,
            JournalEntry.source_document_kind == RUN_DOCUMENT_KIND,
            JournalEntry.source_document_id == str(run_id),
            JournalEntry.source_version == CONSEQUENCE_SOURCE_VERSION,
        )
    )


def _net_total(db: Session, run: PayrollRun) -> Decimal:
    return sum(
        db.scalars(
            select(PayrollCalculation.net_amount).where(
                PayrollCalculation.tenant_id == run.tenant_id, PayrollCalculation.run_id == run.id
            )
        ),
        Decimal(0),
    )


def _guarded_finalize(db: Session, *, tenant: Tenant, party: Party, run_id: UUID, now: datetime) -> FinalizeOutcome:
    scope = TenantScope(tenant.id)
    run = _locked_run(db, tenant.id, run_id)
    if run.status == "finalized":
        journal = _existing_journal(db, tenant.id, run.id)
        if journal is None or journal.status != JournalStatus.POSTED:
            raise RunRefused(
                "finalized_without_ledger_entry",
                "The run is finalized but has no posted ledger entry; refer it to the exceptions review",
            )
        return FinalizeOutcome(run.id, journal.id, journal.number, _net_total(db, run), True)
    if run.status != "approved" or run.approved_by_id is None:
        raise RunRefused("not_approved", "Only an approved payroll run can be finalized")
    if party.id == run.approved_by_id:
        raise RunRefused("sod_approver_is_finalizer", "You approved this run; another person must finalize it")
    if party.id == run.created_by_id:
        raise RunRefused("sod_creator_is_finalizer", "You created this run; another person must finalize it")
    functional = check_currency(db, run)
    check_no_overlap(db, run)
    check_evidence(db, run)
    check_roster_complete(db, run)
    payable_id = check_mappings(db, run)
    check_loan_recovery(db, run)
    payroll.finalize_payroll_run(db, tenant_id=tenant.id, run_id=run.id, finalized_by_id=party.id, finalized_at=now)
    consequence = build_run_accounting_consequence(db, tenant_id=tenant.id, run_id=run.id)
    net_total = _net_total(db, run)
    refs_2040 = {PAYROLL_PAYABLE_CODE, str(payable_id)}
    credited_2040 = sum((e.credit for e in consequence.entries if e.account_ref in refs_2040), Decimal(0))
    if credited_2040 != net_total or any(e.debit for e in consequence.entries if e.account_ref in refs_2040):
        raise RunRefused("payable_2040_not_equal_net_pay", "The credit to Payroll Payable does not equal total net pay")
    approval_ref = f"erp.b4:payroll_run:{run.id}:approver:{run.approved_by_id}:finalizer:{party.id}"
    posted = post_payroll_consequence(
        db,
        scope=scope,
        consequence=consequence,
        functional_currency=functional,
        approval_ref=approval_ref,
        actor=str(party.id),
    )
    _audit(
        db,
        tenant=tenant,
        party=party,
        run_id=run.id,
        action=AUDIT_FINALIZED_POSTED,
        success=True,
        details={
            "journal_id": str(posted.journal_id),
            "journal_number": posted.journal_number,
            "consequence_fingerprint": consequence.fingerprint,
            "approved_by": str(run.approved_by_id),
            "created_by": str(run.created_by_id),
        },
    )
    return FinalizeOutcome(run.id, posted.journal_id, posted.journal_number, net_total, posted.replayed)


def finalize(
    db: Session, *, tenant: Tenant, party: Party, run_id: UUID, now: datetime | None = None
) -> FinalizeOutcome:
    """Finalize + post in one savepoint, or refuse leaving no payroll or ledger change."""
    if party.tenant_id != tenant.id:
        raise RunRefused("actor_tenant", "The acting party must belong to this tenant")
    moment = now or datetime.now(UTC)
    try:
        with db.begin_nested():
            return _guarded_finalize(db, tenant=tenant, party=party, run_id=run_id, now=moment)
    except RunRefused as exc:
        refusal = exc
    except PostingRefused as exc:
        refusal = RunRefused(f"posting_{exc.code}", f"The ledger refused the posting ({exc.code})")
    except (payroll.PayrollNotFound, payroll.PayrollConflict, payroll.PayrollRuleViolation) as exc:
        refusal = RunRefused("payroll_refused", str(exc))
    except IdempotencyConflict as exc:
        refusal = RunRefused("idempotency_conflict", str(exc))
    except IntegrityError:
        refusal = RunRefused("conflict", "A concurrent change prevented the finalization; reload and retry")
    _refuse(db, tenant=tenant, party=party, run_id=run_id, command="finalize", refusal=refusal)
