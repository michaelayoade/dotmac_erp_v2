"""Recomputed v2 side and control ties (B-9 section 12.3c / 12.4): nothing here is self-attested.

* ``v2_posted``: the WHOLE destination ledger (``posted_ledger_lines``) as net (debit - credit) by account code x
  period. The period is ``YYYY-MM`` of the posting date, except journals owned by ``erp.opening`` / ``erp.ias8``,
  which sit in ``OPENING`` (the bridge's opening period).
* ``difference_rows``: recomputed v2 posted minus the expected v2 (the bridge's planned v2 plus every v2-native
  posting, i.e. every line whose owner is not the legacy import, the opening/IAS 8 owners or a rounding correction).
  It must be 0 for every row: no tolerance.
* ``control_state``: for each code in ``ledger_roles.GATE_TIE_CODES`` (and the resolved prepayment account) the GL net
  against the live v2 owner subledger, read from the owner tables (1400 and prepayment from billing R1; 2045 still
  refuses until the claims-opening PR reads expenses a2). A subledger that cannot answer REFUSES with a named reason;
  it is never skipped.
* ``report_state``: the codes in ``REPORT_ZERO_CODES`` (2030) and ``REPORT_WHT_CODES`` (2110) are REPORTED, never
  gated (flip-gate profile ``as_is_v1``, AS-IS-MIGRATION-REPLAN section 4).

The profile id and every list are code constants: no runtime setting can relax the gate.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.models import Account, JournalEntry, PostedLedgerLine
from sqlalchemy.orm import Session

from erp_v2.holdings.models import KIND_STRADDLE_POST as STRADDLE_KIND
from erp_v2.holdings.models import KIND_SUBKOBO_POST as SUBKOBO_KIND
from erp_v2.holdings.models import OWNER as STRADDLE_OWNER
from erp_v2.ledger_roles import GATE_TIE_CODES, GATE_TIE_RESOLVED, REPORT_WHT_CODES, REPORT_ZERO_CODES
from erp_v2.migration_ledger.common import OPENING_PERIOD, ZERO, canonical_sha, fmt
from erp_v2.migration_ledger.rounding_verify import ROUNDING_KIND

GATE_PROFILE: Final = "as_is_v1"  # recorded in the bridge body, so Michael's signature covers it
LEGACY_OWNER: Final = "legacy-erp"
OPENING_OWNERS: Final = ("erp.opening", "erp.ias8")
MANUAL_OWNER: Final = "erp.manual"
_OPEN_OBLIGATION = ("OPEN", "PARTIALLY_SETTLED")
_OPEN_LIABILITY = ("outstanding", "partially_settled")


def _lines(db: Session, tenant: UUID):
    return db.execute(
        sa.select(
            Account.code,
            PostedLedgerLine.posting_date,
            JournalEntry.source_owner,
            JournalEntry.source_document_kind,
            PostedLedgerLine.debit - PostedLedgerLine.credit,
        )
        .join(Account, (Account.tenant_id == PostedLedgerLine.tenant_id) & (Account.id == PostedLedgerLine.account_id))
        .join(
            JournalEntry,
            (JournalEntry.tenant_id == PostedLedgerLine.tenant_id) & (JournalEntry.id == PostedLedgerLine.journal_id),
        )
        .where(PostedLedgerLine.tenant_id == tenant)
    )


def v2_posted(db: Session, tenant: UUID) -> tuple[dict[tuple[str, str], Decimal], dict[tuple[str, str], Decimal]]:
    """(all posted, v2-native posted) by (account code, period)."""
    total: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    native: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for code, day, owner, kind, net in _lines(db, tenant):
        period = OPENING_PERIOD if owner in OPENING_OWNERS else day.strftime("%Y-%m")
        total[(code, period)] += Decimal(net)
        if (
            owner not in (LEGACY_OWNER, *OPENING_OWNERS)
            and not (owner == MANUAL_OWNER and kind == ROUNDING_KIND)
            and not (owner == STRADDLE_OWNER and kind in (STRADDLE_KIND, SUBKOBO_KIND))
        ):
            native[(code, period)] += Decimal(net)
    return dict(total), dict(native)


def difference_rows(db: Session, tenant: UUID, body: dict[str, Any]) -> list[dict[str, str]]:
    """Rows where the recomputed v2 posted side differs from the bridge's expectation (must be empty)."""
    total, native = v2_posted(db, tenant)
    planned = {(r["account"], r["period"]): Decimal(r["v2_planned"]) for r in body["rows"]}
    out = []
    for key in sorted(set(total) | set(planned)):
        expected = planned.get(key, ZERO) + native.get(key, ZERO)
        actual = total.get(key, ZERO)
        if actual != expected:
            out.append({"account": key[0], "period": key[1], "actual": fmt(actual), "expected": fmt(expected)})
    return out


def _gl_net(db: Session, tenant: UUID, code: str) -> Decimal:
    return Decimal(
        db.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0))
            .join(
                Account, (Account.tenant_id == PostedLedgerLine.tenant_id) & (Account.id == PostedLedgerLine.account_id)
            )
            .where(PostedLedgerLine.tenant_id == tenant, Account.code == code)
        )
    )


def _sub_2000(db: Session, tenant: UUID) -> Decimal:
    t = {"t": tenant, "o": list(_OPEN_OBLIGATION)}
    out = db.scalar(
        sa.text(
            "SELECT coalesce(sum(outstanding_amount), 0) FROM mod_payables.payment_obligations "
            "WHERE tenant_id = :t AND status::text = ANY(:o)"
        ),
        t,
    )
    credit = db.scalar(
        sa.text(
            "SELECT coalesce(sum(available_amount), 0) FROM mod_payables.credit_notes "
            "WHERE tenant_id = :t AND status::text = 'APPROVED'"
        ),
        t,
    )
    return -(Decimal(out) - Decimal(credit))


def _sub_2040(db: Session, tenant: UUID) -> Decimal:
    ids = {
        str(i): c
        for i, c in db.execute(
            sa.select(Account.id, Account.code).where(Account.tenant_id == tenant, Account.code == "2040")
        )
    }
    refs = [*ids, "2040"]
    total = db.scalar(
        sa.text(
            "SELECT coalesce(sum(amount - amount_settled), 0) FROM mod_payroll.payroll_liabilities "
            "WHERE tenant_id = :t AND status = ANY(:s) AND liability_account_ref = ANY(:r)"
        ),
        {"t": tenant, "s": list(_OPEN_LIABILITY), "r": refs},
    )
    return -Decimal(total)


def _sub_1410(db: Session, tenant: UUID) -> Decimal:
    from erp_v2.staff_loans import queries

    return Decimal(queries.book_position(db, tenant_id=tenant).subledger_total)


_SUBLEDGERS = {"2000": _sub_2000, "2040": _sub_2040, "1410": _sub_1410}
# 1400 and the prepayment account are tied by the dedicated readers below (billing R1); 2045 is NOT built here. 2110 is
# report-only (``report_state``). These ties net ALL currencies together except the billing ones, which are the
# functional currency only (any other currency with a balance refuses: no FX tie exists).


class _Unavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _gl_net_ids(db: Session, tenant: UUID, ids) -> Decimal:
    return Decimal(
        db.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0)).where(
                PostedLedgerLine.tenant_id == tenant, PostedLedgerLine.account_id.in_(list(ids))
            )
        )
    )


def _billing_position(db: Session, tenant: UUID, tag: str, read_gl):
    """Billing R1 for the functional currency AND the GL it is tied to, as ``(position, gl)``. R1 is read in its OWN
    REPEATABLE READ READ ONLY session (R1 needs that posture and the bridge signs inside a write transaction). The
    order is: ledger sha, then the GL (``read_gl()``), then R1, then the ledger sha again: a ledger that moved at any
    point in between is ``moved_during_gate``. Any other currency with a balance has no FX tie and refuses."""
    from erp_v2.holdings import positions
    from erp_v2.holdings.apply import read_only_session
    from erp_v2.holdings.billing import A13Billing, Position
    from erp_v2.holdings.errors import HoldingRefused
    from erp_v2.journals.policy import functional_currency

    functional = functional_currency(db, tenant)
    before = positions.v2_posted_sha(db, tenant)
    gl = read_gl()
    try:
        with read_only_session(db.get_bind().engine) as billing_db:
            try:
                r1 = A13Billing(tenant).r1(billing_db)
            finally:
                billing_db.rollback()
    except HoldingRefused as exc:
        raise _Unavailable(f"control_subledger_unavailable:{tag}:{exc.code}") from exc
    if positions.v2_posted_sha(db, tenant) != before:
        raise _Unavailable(f"moved_during_gate:{tag}")
    for currency, position in r1.currencies.items():
        if currency != functional and (
            position.collectible_minor
            or position.non_cash_minor
            or position.available_credit_minor
            or position.prepaid_funding_minor
        ):
            raise _Unavailable(f"control_subledger_fx:{tag}")
    return r1.currencies.get(functional, Position(0, 0, 0, 0)), gl


def _billing_tie(db: Session, tenant: UUID, tag: str, read_gl, subledger) -> dict[str, Any]:
    """One billing-backed tie entry; the GL is read inside the ``moved_during_gate`` bracket."""
    box: dict[str, Decimal] = {}

    def gl_once() -> Decimal:
        box["gl"] = read_gl()
        return box["gl"]

    try:
        position, gl = _billing_position(db, tenant, tag, gl_once)
    except _Unavailable as exc:
        gl = box.get("gl")
        return {"gl": None if gl is None else fmt(gl), "subledger": None, "ok": False, "reason": exc.reason}
    sub = subledger(position)
    return {
        "gl": fmt(gl),
        "subledger": fmt(sub),
        "ok": gl == sub,
        "reason": None if gl == sub else f"control_not_tied:{tag}",
    }


def control_state(db: Session, tenant: UUID) -> dict[str, dict[str, Any]]:
    """The GATED ties (exact, GL against the owner subledger). A reader that cannot answer (billing refuses, the
    ledger moved while it read, a foreign-currency balance) is ``ok: False`` with a named reason, never skipped."""
    state: dict[str, dict[str, Any]] = {}
    for code in GATE_TIE_CODES:
        if code == "1400":
            state[code] = _billing_tie(
                db,
                tenant,
                "1400",
                lambda: _gl_net(db, tenant, "1400"),
                lambda position: Decimal(position.receivable()).scaleb(-2),
            )
            continue
        gl = _gl_net(db, tenant, code)
        reader = _SUBLEDGERS.get(code)
        if reader is None:  # 2045: the expenses a2 subledger is read by the claims-opening PR; until then it REFUSES
            state[code] = {
                "gl": fmt(gl),
                "subledger": None,
                "ok": False,
                "reason": f"control_subledger_unavailable:{code}",
            }
            continue
        try:
            sub = reader(db, tenant)
        except _Unavailable as exc:
            state[code] = {"gl": fmt(gl), "subledger": None, "ok": False, "reason": exc.reason}
            continue
        state[code] = {
            "gl": fmt(gl),
            "subledger": fmt(sub),
            "ok": gl == sub,
            "reason": None if gl == sub else f"control_not_tied:{code}",
        }
    state["prepayment"] = _prepayment_tie(db, tenant)
    return state


def _prepayment_tie(db: Session, tenant: UUID) -> dict[str, Any]:
    """GL(the resolved ``ar.prepayment_liability_account`` plus a separate ``ar.prepaid_funding_account`` when it
    resolves) == -(R1 available credit + prepaid funding)."""
    from erp_v2.holdings.config import prepaid_funding_account, prepayment_account
    from erp_v2.holdings.errors import HoldingRefused

    try:
        account = prepayment_account(db, tenant)
        funding = prepaid_funding_account(db, tenant)
    except HoldingRefused as exc:
        return {
            "gl": None,
            "subledger": None,
            "ok": False,
            "reason": f"control_subledger_unavailable:prepayment:{exc.code}",
        }
    ids = {account.id} | ({funding.id} if funding is not None else set())
    return _billing_tie(
        db,
        tenant,
        "prepayment",
        lambda: _gl_net_ids(db, tenant, ids),
        lambda position: -Decimal(position.credit()).scaleb(-2),
    )


def report_state(db: Session, tenant: UUID) -> dict[str, dict[str, Any]]:
    """Report-only balances (never a gate reason): 2030 (retired) and 2110 (real FIRS liability; the v2 WHT ledger
    starts empty, so its balance is for statutory review)."""
    out: dict[str, dict[str, Any]] = {}
    for code in REPORT_ZERO_CODES:
        gl = _gl_net(db, tenant, code)
        out[code] = {"gl": fmt(gl), "kind": "retired_account_balance", "zero": gl == 0}
    for code in REPORT_WHT_CODES:
        out[code] = {"gl": fmt(_gl_net(db, tenant, code)), "kind": "wht_balance_for_statutory_review"}
    return out


def control_lists() -> dict[str, list[str]]:
    return {
        "tie": list(GATE_TIE_CODES),
        "tie_resolved": list(GATE_TIE_RESOLVED),
        "report_zero": list(REPORT_ZERO_CODES),
        "report_wht": list(REPORT_WHT_CODES),
    }


def ledger_state(db: Session, tenant: UUID) -> dict[str, Any]:
    total, _native = v2_posted(db, tenant)
    controls = control_state(db, tenant)
    rows = [[a, p, fmt(v)] for (a, p), v in sorted(total.items())]
    return {
        "v2_posted_sha256": canonical_sha(rows),
        "controls": controls,
        "control_lists": control_lists(),
        "reports": report_state(db, tenant),  # not part of the moved-since-signature comparison, never a gate
    }
