"""Tier C: the reviewed FY2025 opening journal and the IAS 8 correction journals (MIGRATION-REVIEWED-CONVERSION s2).

* ONE system-built opening journal, owner ``erp.opening``, one line per account, from the reviewed trial balance at
  the FY2025 close (as reported). Legacy history stays read-only and is never re-posted.
* IAS 8 prior-period corrections are SEPARATE journals, owner ``erp.ias8``, one per itemised item, each restating
  one account against retained earnings 3100, dated at the first OPEN period after the opening (D-3).

Sign convention everywhere: net = debit - credit (an asset is positive, a liability negative).

Flip-gate profile ``as_is_v1`` (AS-IS-MIGRATION-REPLAN sections 1 and 4):

* The trial balance is GENERATED from the legacy ledger snapshot at the opening date (``tb_from_legacy``, the same
  ``legacy_ledger.recompute`` the bridge uses). A declared TB file is still sha-bound in the spec but is REFUSED if it
  differs from the recomputed one (``opening_tb_differs_from_legacy``).
* The CLI refuses only a trial balance that does not sum to zero. The opening control difference
  (``tb_net[code] + ias8 movement - subledger_net[code]``, accounts 2000, 2040, 1410, 1400) is REPORT-ONLY: it is listed
  per account as an E37 info item (``control_differences``); the flip holding reclass covers it. It is never a refusal.
* IAS 8 is empty before the flip: the IAS 8 journal kind stays in ``apply_opening`` as a post-flip Finance tool, but
  the operator CLI (the flip runbook door) calls ``require_no_ias8_before_flip`` and refuses any item.

Both journals are posted through accounting a2 directly (create + post) under their own source owners; the
manual-journal control guard is for typed lines and is not the door here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.ledger_roles import LEDGER_FAULT_CONTROL_CODES
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger.common import (
    OPENING_PERIOD,
    ZERO,
    canonical_sha,
    day,
    fmt,
    keys_exactly,
    money,
    refuse,
    sha_text,
    text,
)

OPENING_OWNER: Final = "erp.opening"
OPENING_KIND: Final = "opening_balance"
IAS8_OWNER: Final = "erp.ias8"
IAS8_KIND: Final = "prior_period_correction"
RETAINED_EARNINGS: Final = "3100"
# The four control accounts whose opening must equal their subledger (AR 1400 joins the ledger-fault three). Kept as
# the original four on purpose: S9b widened ``CONTROL_ACCOUNT_CODES`` (guard) with 1400/2045/prepayment, and that must
# not silently add 2045 to what an opening proof demands (the flip gate ties 2045 through ``GATE_TIE_CODES``).
CONTROL_CODES: Final = frozenset(LEDGER_FAULT_CONTROL_CODES | {"1400"})
_NAMESPACE: Final = UUID("8d3c4f0a-6b7e-4d19-a2c5-0e1f7b9a3c64")
MAX_ROWS: Final = 5000


@dataclass(frozen=True)
class Ias8Item:
    item_id: str
    account_code: str
    amount: Decimal  # net movement the IAS 8 journal posts on account_code; 3100 takes the opposite side
    evidence_sha256: str
    description: str


@dataclass(frozen=True)
class OpeningInputs:
    as_of: date
    tb: dict[str, Decimal]  # account code -> net
    tb_sha256: str
    subledger: dict[str, Decimal]  # control code -> net (recomputed from the legacy snapshot, legacy_subledger.py)
    subledger_sha256: str
    ias8: tuple[Ias8Item, ...]
    ias8_sha256: str
    recomputed_tb_sha256: str  # canonical sha of the TB generated from the legacy snapshot (mandatory binding)
    import_receipt_sha256: str  # sha of the importer execute receipt whose history cutoff equals ``as_of`` (B-13)


# --- parsing ---------------------------------------------------------------------------------------------------


def parse_tb(raw: object, raw_sha: str) -> tuple[date, dict[str, Decimal]]:
    """{"version":1,"as_of":"YYYY-MM-DD","rows":[{"account_code","debit","credit"}]}: one row per account."""
    body = keys_exactly(raw, {"version", "as_of", "rows"}, "invalid_tb_schema")
    if body["version"] != 1 or not isinstance(body["rows"], list) or not 1 <= len(body["rows"]) <= MAX_ROWS:
        refuse("invalid_tb_schema")
    as_of = day(body["as_of"], "invalid_tb_as_of")
    rows: dict[str, Decimal] = {}
    for row in body["rows"]:
        entry = keys_exactly(row, {"account_code", "debit", "credit"}, "invalid_tb_row")
        code = text(entry["account_code"], "invalid_tb_row", 30)
        debit, credit = money(entry["debit"], "invalid_tb_row"), money(entry["credit"], "invalid_tb_row")
        if debit < 0 or credit < 0 or (debit > 0 and credit > 0):
            refuse("invalid_tb_row", "an account carries one non-negative side")
        if code in rows:
            refuse("tb_duplicate_account")
        rows[code] = debit - credit
    return as_of, rows


def parse_ias8(raw: object) -> tuple[Ias8Item, ...]:
    """{"version":1,"items":[{"item_id","account_code","amount","evidence_sha256","description"}]}."""
    body = keys_exactly(raw, {"version", "items"}, "invalid_ias8_schema")
    if body["version"] != 1 or not isinstance(body["items"], list) or len(body["items"]) > MAX_ROWS:
        refuse("invalid_ias8_schema")
    seen: set[str] = set()
    items: list[Ias8Item] = []
    for row in body["items"]:
        entry = keys_exactly(
            row, {"item_id", "account_code", "amount", "evidence_sha256", "description"}, "invalid_ias8_item"
        )
        item_id = text(entry["item_id"], "invalid_ias8_item")
        account = text(entry["account_code"], "invalid_ias8_item", 30)
        amount = money(entry["amount"], "invalid_ias8_item")
        if item_id in seen:
            refuse("ias8_duplicate_item")
        seen.add(item_id)
        if amount == 0:
            refuse("ias8_zero_item")
        if account == RETAINED_EARNINGS:
            refuse("ias8_account_is_retained_earnings")
        items.append(
            Ias8Item(
                item_id,
                account,
                amount,
                sha_text(entry["evidence_sha256"], "invalid_ias8_item"),
                text(entry["description"], "invalid_ias8_item", 500),
            )
        )
    return tuple(sorted(items, key=lambda i: i.item_id))


# --- the generated TB (profile as_is_v1) ---------------------------------------------------------------------


def tb_from_legacy(legacy_net: dict[tuple[str, str], Decimal]) -> dict[str, Decimal]:
    """The opening TB: the OPENING-period nets of ``legacy_ledger.recompute(..., opening_as_of=D)`` per account code
    (P&L already rolled into 3100 there). Zero nets are omitted."""
    return {code: net for (code, period), net in sorted(legacy_net.items()) if period == OPENING_PERIOD and net != 0}


def tb_canonical_sha(tb: dict[str, Decimal]) -> str:
    return canonical_sha([[code, fmt(net)] for code, net in sorted(tb.items()) if net != 0])


def tb_file_body(as_of: date, tb: dict[str, Decimal]) -> dict[str, Any]:
    """The generated TB in the ``parse_tb`` schema (one non-negative side per account)."""
    rows = [
        {"account_code": code, "debit": fmt(net if net > 0 else ZERO), "credit": fmt(-net if net < 0 else ZERO)}
        for code, net in sorted(tb.items())
        if net != 0
    ]
    return {"version": 1, "as_of": as_of.isoformat(), "rows": rows}


def require_declared_tb_matches(declared: dict[str, Decimal], generated: dict[str, Decimal]) -> None:
    """A declared TB is refused if it differs from the one recomputed from the legacy snapshot."""
    if {c: n for c, n in declared.items() if n != 0} != {c: n for c, n in generated.items() if n != 0}:
        refuse("opening_tb_differs_from_legacy", "regenerate the TB with gen-tb; never declare one that differs")


def parse_import_receipt(raw: object) -> date:
    """The history cutoff of the B-13 importer EXECUTE receipt (applied and complete). ``history_cutoff`` is read as an
    ISO date string or as the receipt section ``{"history_cutoff": "<date>", ...}`` (the shape #74 emits today)."""
    if not isinstance(raw, dict) or raw.get("kind") != "execute" or raw.get("dry_run") is not False:
        refuse("invalid_import_receipt", "an applied importer execute receipt is required")
    if raw.get("complete") is not True:
        refuse("import_receipt_incomplete")
    cutoff = raw.get("history_cutoff")
    if isinstance(cutoff, dict):
        cutoff = cutoff.get("history_cutoff")
    if not isinstance(cutoff, str):
        refuse("import_receipt_history_cutoff_missing")
    return day(cutoff, "import_receipt_history_cutoff_invalid")


def require_as_of_matches_cutoff(as_of: date, cutoff: date) -> None:
    """D must be the import's history cutoff: otherwise the opening and the imported ledger overlap or leave a gap, and
    the bridge would only show it AFTER the irreversible opening post."""
    if as_of != cutoff:
        refuse("opening_as_of_differs_from_history_cutoff")


def require_no_ias8_before_flip(inputs: OpeningInputs) -> None:
    if inputs.ias8:
        refuse("ias8_not_allowed_before_flip", "IAS 8 corrections are a post-flip Finance tool (profile as_is_v1)")


# --- the pure proof -------------------------------------------------------------------------------------------


def opening_proof(inputs: OpeningInputs) -> dict[str, Any]:
    """The refusal conditions as data. ``ok`` only when Σ = 0 and every control account ties."""
    if set(inputs.subledger) != CONTROL_CODES:
        refuse("subledger_incomplete", "every control account needs a recomputed subledger opening")
    total = sum(inputs.tb.values(), ZERO)
    ias8_by_account: dict[str, Decimal] = {}
    for item in inputs.ias8:
        ias8_by_account[item.account_code] = ias8_by_account.get(item.account_code, ZERO) + item.amount
    controls = {}
    for code in sorted(CONTROL_CODES):
        tb_net = inputs.tb.get(code, ZERO)
        corrected = tb_net + ias8_by_account.get(code, ZERO)
        controls[code] = {
            "tb_net": fmt(tb_net),
            "ias8_movement": fmt(ias8_by_account.get(code, ZERO)),
            "subledger_net": fmt(inputs.subledger[code]),
            "difference": fmt(corrected - inputs.subledger[code]),
            "ties": corrected == inputs.subledger[code],
        }
    zero_sum = total == 0
    # E37 info items: D-level opening control differences, listed and never refused (the flip holding covers them)
    differences = [
        {"account": code, "difference": c["difference"], "class": "E37"}
        for code, c in controls.items()
        if not c["ties"]
    ]
    return {
        "tb_accounts": len(inputs.tb),
        "tb_net_total": fmt(total),
        "zero_sum": zero_sum,
        "control_accounts": controls,
        "ias8_items": len(inputs.ias8),
        "ias8_net_total": fmt(sum((i.amount for i in inputs.ias8), ZERO)),
        "control_differences": differences,
        "control_differences_report_only": True,
        "ok": zero_sum,
    }


def require_proof(proof: dict[str, Any]) -> None:
    """Only the zero-sum TB is a refusal; the control differences are report-only (see ``control_differences``)."""
    if not proof["zero_sum"]:
        refuse("opening_tb_not_zero_sum")


def plan_sha256(inputs: OpeningInputs, proof: dict[str, Any]) -> str:
    return canonical_sha(
        {
            "as_of": inputs.as_of.isoformat(),
            "tb_sha256": inputs.tb_sha256,
            "subledger_sha256": inputs.subledger_sha256,
            "ias8_sha256": inputs.ias8_sha256,
            "recomputed_tb_sha256": inputs.recomputed_tb_sha256,
            "import_receipt_sha256": inputs.import_receipt_sha256,
            "proof": proof,
        }
    )


# --- database --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyInputs:
    spec_sha256: str
    plan_sha256: str
    planned_by: str
    applied_by: str
    requester_party_id: UUID
    approver_party_id: UUID
    dual_control: bool
    evidence_ref: str


def _accounts(db: Session, tenant: UUID, codes: set[str]) -> dict[str, UUID]:
    from dotmac_accounting.contracts import AccountKind
    from dotmac_accounting.models import Account

    found = {
        a.code: a for a in db.scalars(sa.select(Account).where(Account.tenant_id == tenant, Account.code.in_(codes)))
    }
    for code in codes:
        a = found.get(code)
        if a is None or not a.is_active or not a.posting_allowed or a.kind != AccountKind.POSTING:
            refuse("account_missing_or_unpostable")
    return {code: a.id for code, a in found.items()}


def _period_for(db: Session, tenant: UUID, on: date):
    from dotmac_accounting.models import FiscalPeriod

    periods = list(
        db.scalars(
            sa.select(FiscalPeriod).where(
                FiscalPeriod.tenant_id == tenant, FiscalPeriod.start_date <= on, FiscalPeriod.end_date >= on
            )
        )
    )
    if len(periods) != 1:
        refuse("period_missing")
    return periods[0]


def ias8_date(db: Session, tenant: UUID, as_of: date) -> date:
    """D-3: exactly the first day of the FIRST period of FY2026 (the first fiscal year starting after the opening).
    It must be OPEN and not an adjustment period; otherwise refused (never a later period)."""
    from dotmac_accounting.contracts import PeriodStatus
    from dotmac_accounting.models import FiscalPeriod, FiscalYear

    year = db.scalar(
        sa.select(FiscalYear)
        .where(FiscalYear.tenant_id == tenant, FiscalYear.start_date > as_of)
        .order_by(FiscalYear.start_date)
        .limit(1)
    )
    if year is None:
        refuse("ias8_fiscal_year_missing")
    first = db.scalar(
        sa.select(FiscalPeriod)
        .where(
            FiscalPeriod.tenant_id == tenant,
            FiscalPeriod.fiscal_year_id == year.id,
            FiscalPeriod.is_adjustment.is_(False),
        )
        .order_by(FiscalPeriod.start_date)
        .limit(1)
    )
    if first is None:
        refuse("ias8_first_period_missing")
    if first.status != PeriodStatus.OPEN:
        refuse("ias8_first_period_not_open")
    return first.start_date


def existing(db: Session, tenant: UUID) -> dict[tuple[str, str], m.OpeningJournal]:
    return {
        (r.kind, r.item_ref): r
        for r in db.scalars(sa.select(m.OpeningJournal).where(m.OpeningJournal.tenant_id == tenant))
    }


def check_state(db: Session, tenant: UUID, inputs: OpeningInputs) -> dict[str, Any]:
    """Read-only: what already exists, and the refusals that would stop an apply (drift, other opening)."""
    have = existing(db, tenant)
    opening = have.get((m.KIND_OPENING, "opening"))
    if opening is not None and opening.source_sha256 != inputs.tb_sha256:
        refuse("opening_already_posted_with_other_trial_balance")
    for item in inputs.ias8:
        row = have.get((m.KIND_IAS8, item.item_id))
        if row is not None and (row.amount != item.amount or row.account_code != item.account_code):
            refuse("ias8_item_changed_after_posting")
    return {
        "opening_posted": opening is not None,
        "ias8_posted": sum(1 for item in inputs.ias8 if (m.KIND_IAS8, item.item_id) in have),
        "ias8_pending": sum(1 for item in inputs.ias8 if (m.KIND_IAS8, item.item_id) not in have),
    }


def _post(
    db: Session,
    tenant: UUID,
    apply: ApplyInputs,
    *,
    number: str,
    owner: str,
    kind: str,
    document_id: str,
    posting_date: date,
    description: str,
    reference: str,
    lines: list[tuple[UUID, Decimal, Decimal]],
    now: datetime,
) -> UUID:
    from dotmac_accounting import service as accounting
    from dotmac_accounting.contracts import CreateJournal, JournalKind, JournalLineInput, JournalStatus, SourceIdentity
    from dotmac_kernel.cache import TenantScope

    from erp_v2.journals.policy import functional_currency

    currency = functional_currency(db, tenant)
    fingerprint = canonical_sha(
        {
            "owner": owner,
            "kind": kind,
            "document_id": document_id,
            "date": posting_date.isoformat(),
            "lines": [[str(a), fmt(d), fmt(c)] for a, d, c in lines],
        }
    )
    key = f"{owner}:{document_id}"
    journal = accounting.create_journal(
        db,
        scope=TenantScope(tenant),
        command=CreateJournal(
            number=number,
            kind=JournalKind.STANDARD,
            entry_date=posting_date,
            posting_date=posting_date,
            currency_code=currency,
            exchange_rate=Decimal(1),
            description=description,
            reference=reference,
            source=SourceIdentity(
                owner=owner, document_kind=kind, document_id=document_id, version="1", fingerprint=fingerprint
            ),
            lines=tuple(JournalLineInput(a, d, c, None) for a, d, c in lines),
        ),
        idempotency_key=f"{key}:create",
        idempotency_expires_at=None,
        recorded_at=now,
    )
    if journal.status == JournalStatus.DRAFT:
        journal = accounting.post_journal(
            db,
            scope=TenantScope(tenant),
            journal_id=journal.id,
            approval_reference=f"{key}:{apply.plan_sha256[:16]}",
            posted_by=str(apply.approver_party_id),
            idempotency_key=f"{key}:post",
            idempotency_expires_at=None,
            posted_at=now,
        )
    if journal.status != JournalStatus.POSTED:
        refuse("journal_not_posted")
    return journal.id


def _party(db: Session, tenant: UUID, party_id: UUID) -> None:
    from dotmac_kernel.models import Party

    if not db.scalar(
        sa.select(Party.id).where(Party.id == party_id, Party.tenant_id == tenant, Party.is_active.is_(True))
    ):
        refuse("actor_invalid")


def _record(
    db: Session,
    tenant: UUID,
    apply: ApplyInputs,
    *,
    kind: str,
    item_ref: str,
    journal_id: UUID,
    source_sha: str,
    account_code: str | None,
    amount: Decimal,
    evidence: str,
    now: datetime,
) -> None:
    db.add(
        m.OpeningJournal(
            tenant_id=tenant,
            kind=kind,
            item_ref=item_ref,
            journal_id=journal_id,
            source_sha256=source_sha,
            account_code=account_code,
            amount=amount,
            evidence_sha256=evidence,
            spec_sha256=apply.spec_sha256,
            plan_sha256=apply.plan_sha256,
            planned_by=apply.planned_by,
            applied_by=apply.applied_by,
            dual_control=apply.dual_control,
            created_at=now,
        )
    )
    db.flush()


# P-7 (pending, before B-14): the opening posts functional currency only, so foreign-currency control positions at D are
# not in the opening journal; the per-currency ties (``control_ties_per_currency``) and the B-14 holding depend on it.


def apply_opening(db: Session, tenant: UUID, inputs: OpeningInputs, apply: ApplyInputs) -> dict[str, Any]:
    """Post the opening journal, then each IAS 8 journal. Everything is refused unless the proof holds; a repeat is a
    replay. Runs in one SAVEPOINT: a refusal leaves no row and no journal."""
    require_proof(opening_proof(inputs))
    if apply.dual_control and (
        apply.planned_by == apply.applied_by or apply.requester_party_id == apply.approver_party_id
    ):
        refuse("dual_control_same_actor")
    _party(db, tenant, apply.requester_party_id)
    _party(db, tenant, apply.approver_party_id)
    now = datetime.now(UTC)
    posted = {"opening": 0, "ias8": 0, "replayed": 0}
    with db.begin_nested():
        state = check_state(db, tenant, inputs)
        codes = set(inputs.tb) | {RETAINED_EARNINGS} | {i.account_code for i in inputs.ias8}
        ids = _accounts(db, tenant, codes)
        from dotmac_accounting.contracts import PeriodStatus

        period = _period_for(db, tenant, inputs.as_of)
        if period.is_adjustment:
            refuse("opening_period_is_adjustment")
        if period.status != PeriodStatus.OPEN and not state["opening_posted"]:
            refuse("opening_period_not_open", "open the period, post, then soft-close and lock it")
        have = existing(db, tenant)
        if not state["opening_posted"]:
            lines = [
                (ids[code], net if net > 0 else ZERO, -net if net < 0 else ZERO)
                for code, net in sorted(inputs.tb.items())
                if net != 0
            ]
            journal_id = _post(
                db,
                tenant,
                apply,
                number="OB-" + inputs.tb_sha256[:24].upper(),
                owner=OPENING_OWNER,
                kind=OPENING_KIND,
                document_id=inputs.tb_sha256,
                posting_date=inputs.as_of,
                description=f"Reviewed opening balances at {inputs.as_of.isoformat()} [{apply.evidence_ref}]",
                reference=apply.evidence_ref[:255],
                lines=lines,
                now=now,
            )
            _record(
                db,
                tenant,
                apply,
                kind=m.KIND_OPENING,
                item_ref="opening",
                journal_id=journal_id,
                source_sha=inputs.tb_sha256,
                account_code=None,
                amount=sum((d for _a, d, _c in lines), ZERO),
                evidence=inputs.tb_sha256,
                now=now,
            )
            posted["opening"] = 1
        else:
            posted["replayed"] += 1
        for item in inputs.ias8:
            if (m.KIND_IAS8, item.item_id) in have:
                posted["replayed"] += 1
                continue
            when = ias8_date(db, tenant, inputs.as_of)
            move = item.amount
            lines = [
                (ids[item.account_code], move if move > 0 else ZERO, -move if move < 0 else ZERO),
                (ids[RETAINED_EARNINGS], -move if move < 0 else ZERO, move if move > 0 else ZERO),
            ]
            journal_id = _post(
                db,
                tenant,
                apply,
                number="IA-" + uuid5(_NAMESPACE, item.item_id).hex[:24].upper(),
                owner=IAS8_OWNER,
                kind=IAS8_KIND,
                document_id=item.item_id,
                posting_date=when,
                description=f"IAS 8 prior-period correction {item.item_id}: {item.description}",
                reference=f"ias8:{item.evidence_sha256[:32]}",
                lines=lines,
                now=now,
            )
            _record(
                db,
                tenant,
                apply,
                kind=m.KIND_IAS8,
                item_ref=item.item_id,
                journal_id=journal_id,
                source_sha=inputs.ias8_sha256,
                account_code=item.account_code,
                amount=item.amount,
                evidence=item.evidence_sha256,
                now=now,
            )
            posted["ias8"] += 1
    return posted


def ledger_net_by_account(db: Session, tenant: UUID, journal_id: UUID) -> dict[str, Decimal]:
    """The posted ledger net (debit - credit) of one journal by account code (for the bridge's carry column)."""
    from dotmac_accounting.models import Account, PostedLedgerLine

    rows = db.execute(
        sa.select(Account.code, sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit))
        .join(Account, (Account.tenant_id == PostedLedgerLine.tenant_id) & (Account.id == PostedLedgerLine.account_id))
        .where(PostedLedgerLine.tenant_id == tenant, PostedLedgerLine.journal_id == journal_id)
        .group_by(Account.code)
    )
    return {code: Decimal(net) for code, net in rows}
