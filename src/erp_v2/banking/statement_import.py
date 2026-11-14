"""Bank statement upload: read-only preview and the governed import command.

No GL posting here (reconciliation is B7). The import calls the released banking
owner ``import_bank_statement`` and nothing else; the caller owns commit/rollback.

Guarantees (all fail closed, any one refuses the whole import):

* tenant scoping: the bank account is resolved inside the caller's tenant;
* idempotency: the file's sha256 is the statement ``source_ref`` (the owner refuses a
  second import of the same file), and every incoming line is compared with the
  account's existing lines (date, direction, amount, reference), so an overlapping
  or re-keyed export cannot import the same transaction twice;
* provenance: ``evidence_ref`` and ``source_ref`` carry the sha256, the audit event
  records who imported which file hash; the file bytes themselves are not retained;
* range: dates, spans, amounts, row counts and decimal places are bounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from uuid import UUID

from dotmac_banking.contracts import BankStatementInput, StatementLineDirection, StatementLineInput
from dotmac_banking.models import BankAccount, BankStatement, BankStatementLine
from dotmac_banking.service import BankingConflict, BankingNotFound, MatchRuleViolation, import_bank_statement
from dotmac_kernel.money import Currency, Money
from dotmac_kernel.transactions import conflict_savepoint
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp_v2.banking.statement_file import ParsedStatement, StatementFileError, parse_statement

AUDIT_STATEMENT_IMPORTED: Final = "erp.banking.statement_imported"
EARLIEST_DATE: Final = date(2000, 1, 1)
MAX_SPAN_DAYS: Final = 370
SOURCE_REF_PREFIX: Final = "erp:statement-upload:sha256:"
EVIDENCE_PREFIX: Final = "upload:sha256:"
_STATEMENT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/\-]{0,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StatementImportRefused(ValueError):
    """The import was refused; ``reasons`` are operator-safe (no file content echoed)."""

    def __init__(self, reasons: list[str] | tuple[str, ...]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class ImportOptions:
    account_id: UUID
    date_format: str = "iso"
    statement_ref: str | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None


@dataclass(frozen=True)
class DuplicateLine:
    source_row: int
    transaction_date: date
    direction: str
    amount: Decimal
    existing_statement_ref: str


@dataclass(frozen=True)
class ImportPlan:
    parsed: ParsedStatement
    account_code: str
    currency_code: str
    minor_units: int
    statement_ref: str
    source_ref: str
    evidence_ref: str
    period_start: date | None
    period_end: date | None
    total_credits: Decimal
    total_debits: Decimal
    duplicates: tuple[DuplicateLine, ...]
    blockers: tuple[str, ...]

    @property
    def importable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class ImportResult:
    statement_id: UUID
    statement_ref: str
    sha256: str
    line_count: int
    total_credits: Decimal
    total_debits: Decimal


def _account(db: Session, tenant_id: UUID, account_id: UUID, *, lock: bool) -> BankAccount:
    query = select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.id == account_id)
    account = db.scalar(query.with_for_update() if lock else query)
    if account is None:
        raise StatementImportRefused(["Bank account not found"])
    if account.status != "active":
        raise StatementImportRefused(["Bank account is not active"])
    return account


def _balance(value: Decimal | None, account: BankAccount, label: str, blockers: list[str]) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite() or abs(value) >= Decimal("100000000000000"):
        blockers.append(f"{label} is out of range")
        return None
    quantum = Decimal(1).scaleb(-account.minor_units)
    if value != value.quantize(quantum, rounding=ROUND_HALF_UP):
        blockers.append(f"{label} has more decimal places than the account currency allows")
        return None
    return value


def build_plan(
    db: Session,
    *,
    tenant_id: UUID,
    data: bytes,
    file_format: str,
    options: ImportOptions,
    now: datetime,
    lock_account: bool = False,
) -> ImportPlan:
    """Parse, validate and compare with existing data. Writes nothing."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise StatementImportRefused(["Clock must be timezone aware"])
    account = _account(db, tenant_id, options.account_id, lock=lock_account)
    try:
        parsed = parse_statement(data, file_format, date_format=options.date_format)
    except StatementFileError as exc:
        raise StatementImportRefused([str(exc)]) from None
    blockers: list[str] = list(parsed.errors)
    quantum = Decimal(1).scaleb(-account.minor_units)
    lines = parsed.lines
    for line in lines:
        if line.amount != line.amount.quantize(quantum, rounding=ROUND_HALF_UP):
            blockers.append(f"Row {line.source_row}: amount has more decimal places than the account currency allows")
    latest = now.astimezone(UTC).date() + timedelta(days=1)
    dates = [d for line in lines for d in (line.transaction_date, line.value_date) if d is not None]
    if dates and (min(dates) < EARLIEST_DATE or max(dates) > latest):
        blockers.append(f"Dates must be between {EARLIEST_DATE.isoformat()} and tomorrow")
    period_start = min((line.transaction_date for line in lines), default=None)
    period_end = max((line.transaction_date for line in lines), default=None)
    if period_start and period_end and (period_end - period_start).days > MAX_SPAN_DAYS:
        blockers.append(f"Statement spans more than {MAX_SPAN_DAYS} days; split it")
    opening = _balance(options.opening_balance, account, "Opening balance", blockers)
    closing = _balance(options.closing_balance, account, "Closing balance", blockers)
    if (options.opening_balance is None) != (options.closing_balance is None):
        blockers.append("Provide both opening and closing balance, or neither")
    credits = sum((line.amount for line in lines if line.direction == "credit"), Decimal(0))
    debits = sum((line.amount for line in lines if line.direction == "debit"), Decimal(0))
    if opening is not None and closing is not None and opening + credits - debits != closing:
        blockers.append("Opening balance plus credits minus debits does not equal the closing balance")

    source_ref = SOURCE_REF_PREFIX + parsed.sha256
    ref = (options.statement_ref or "").strip() or (
        f"{account.account_code}-{period_start or 'empty'}-{period_end or 'empty'}-{parsed.sha256[:8]}"
    )
    if len(ref) > 120 or not _STATEMENT_REF.match(ref):
        blockers.append("Statement reference must be 1-120 letters, digits or . _ / - and spaces")
    with db.no_autoflush:
        same_file = db.scalar(
            select(BankStatement.statement_ref).where(
                BankStatement.tenant_id == tenant_id, BankStatement.source_ref == source_ref
            )
        )
        same_ref = db.scalar(
            select(BankStatement.id).where(
                BankStatement.tenant_id == tenant_id,
                BankStatement.account_id == account.id,
                BankStatement.statement_ref == ref,
            )
        )
        overlap = (
            db.scalar(
                select(BankStatement.statement_ref)
                .where(
                    BankStatement.tenant_id == tenant_id,
                    BankStatement.account_id == account.id,
                    BankStatement.period_start <= period_end,
                    BankStatement.period_end >= period_start,
                )
                .limit(1)
            )
            if period_start and period_end
            else None
        )
        duplicates = _duplicates(db, tenant_id, account.id, lines, period_start, period_end)
    if same_file is not None:
        blockers.append("This exact file has already been imported")
    if overlap is not None:
        blockers.append(
            f"The statement period overlaps existing statement '{overlap}' on this account "
            "(including statements imported from the legacy system)"
        )
    if same_ref is not None:
        blockers.append("A statement with this reference already exists for the account")
    if duplicates:
        blockers.append(f"{len(duplicates)} line(s) already exist on this account (overlapping statement)")
    return ImportPlan(
        parsed=parsed,
        account_code=account.account_code,
        currency_code=account.currency_code,
        minor_units=account.minor_units,
        statement_ref=ref,
        source_ref=source_ref,
        evidence_ref=EVIDENCE_PREFIX + parsed.sha256,
        period_start=period_start,
        period_end=period_end,
        total_credits=credits,
        total_debits=debits,
        duplicates=tuple(duplicates),
        blockers=tuple(blockers),
    )


def _duplicates(db, tenant_id, account_id, lines, start, end) -> list[DuplicateLine]:
    if not lines or start is None or end is None:
        return []
    rows = db.execute(
        select(
            BankStatementLine.transaction_date,
            BankStatementLine.direction,
            BankStatementLine.amount,
            BankStatementLine.external_ref,
            BankStatement.statement_ref,
        )
        .join(BankStatement, BankStatement.id == BankStatementLine.statement_id)
        .where(
            BankStatementLine.tenant_id == tenant_id,
            BankStatement.tenant_id == tenant_id,
            BankStatement.account_id == account_id,
            BankStatementLine.transaction_date >= start,
            BankStatementLine.transaction_date <= end,
        )
    ).all()
    existing = {(r[0], r[1], Decimal(r[2]).normalize(), r[3]): r[4] for r in rows}
    found: list[DuplicateLine] = []
    for line in lines:
        hit = existing.get((line.transaction_date, line.direction, line.amount.normalize(), line.external_ref))
        if hit is not None:
            found.append(DuplicateLine(line.source_row, line.transaction_date, line.direction, line.amount, hit))
    return found


def preview(
    db: Session, *, tenant_id: UUID, data: bytes, file_format: str, options: ImportOptions, now: datetime
) -> ImportPlan:
    return build_plan(db, tenant_id=tenant_id, data=data, file_format=file_format, options=options, now=now)


def import_statement(
    db: Session,
    *,
    tenant_id: UUID,
    actor_party_id: UUID,
    data: bytes,
    file_format: str,
    expected_sha256: str,
    options: ImportOptions,
    now: datetime,
    audit=None,
) -> ImportResult:
    """Atomic within the caller's transaction; raises StatementImportRefused, writing nothing."""

    if not _SHA256.match(expected_sha256 or ""):
        raise StatementImportRefused(["A previewed file hash is required"])
    # Lock the account row first: concurrent imports to one account serialise, so the
    # overlap check below sees every committed statement.
    plan = build_plan(
        db, tenant_id=tenant_id, data=data, file_format=file_format, options=options, now=now, lock_account=True
    )
    if plan.parsed.sha256 != expected_sha256:
        raise StatementImportRefused(["Uploaded file differs from the previewed file"])
    if not plan.importable:
        raise StatementImportRefused(plan.blockers)
    account = _account(db, tenant_id, options.account_id, lock=False)
    currency = Currency(account.currency_code, account.minor_units)

    def money(value: Decimal | None) -> Money | None:
        return None if value is None else Money.of(value, currency)

    command = BankStatementInput(
        account_id=account.id,
        statement_ref=plan.statement_ref,
        period_start=plan.period_start,  # type: ignore[arg-type]  # importable => lines exist
        period_end=plan.period_end,  # type: ignore[arg-type]
        opening_balance=money(options.opening_balance),
        closing_balance=money(options.closing_balance),
        source_ref=plan.source_ref,
        source_version="1",
        evidence_ref=plan.evidence_ref,
        lines=tuple(
            StatementLineInput(
                line_number=line.line_number,
                transaction_date=line.transaction_date,
                direction=StatementLineDirection(line.direction),
                amount=Money.of(line.amount, currency),
                external_ref=line.external_ref,
                description=line.description,
                value_date=line.value_date,
                reference=line.reference,
                counterparty=line.counterparty,
                bank_transaction_code=line.bank_code,
            )
            for line in plan.parsed.lines
        ),
    )
    try:
        with conflict_savepoint(db):
            statement = import_bank_statement(
                db, tenant_id=tenant_id, command=command, imported_at=now, imported_by_id=actor_party_id
            )
            (audit or _write_audit)(
                db,
                tenant_id=tenant_id,
                actor_party_id=actor_party_id,
                statement_id=statement.id,
                sha256=plan.parsed.sha256,
                line_count=len(plan.parsed.lines),
            )
    except IntegrityError:
        raise StatementImportRefused(["A concurrent import of this statement won; nothing was imported"]) from None
    except (BankingConflict, BankingNotFound, MatchRuleViolation) as exc:
        raise StatementImportRefused([f"Banking owner refused the statement: {exc}"]) from None
    return ImportResult(
        statement_id=statement.id,
        statement_ref=plan.statement_ref,
        sha256=plan.parsed.sha256,
        line_count=len(plan.parsed.lines),
        total_credits=plan.total_credits,
        total_debits=plan.total_debits,
    )


def _write_audit(db, *, tenant_id, actor_party_id, statement_id, sha256, line_count) -> None:
    from dotmac_kernel.audit import write_audit_event

    write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_party_id=actor_party_id,
        actor_type="user",
        actor_id=str(actor_party_id),
        source_application="dotmac_erp",
        action=AUDIT_STATEMENT_IMPORTED,
        entity_type="bank_statement",
        entity_id=str(statement_id),
        is_success=True,
        details={"sha256": sha256, "lines": line_count},
    )
