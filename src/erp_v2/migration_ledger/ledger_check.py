"""Recomputed v2 side and control ties (B-9 section 12.3c / 12.4): nothing here is self-attested.

* ``v2_posted``: the WHOLE destination ledger (``posted_ledger_lines``) as net (debit - credit) by account code x
  period. The period is ``YYYY-MM`` of the posting date, except journals owned by ``erp.opening`` / ``erp.ias8``,
  which sit in ``OPENING`` (the bridge's opening period).
* ``difference_rows``: recomputed v2 posted minus the expected v2 (the bridge's planned v2 plus every v2-native
  posting, i.e. every line whose owner is not the legacy import, the opening/IAS 8 owners or a rounding correction).
  It must be 0 for every row: no tolerance.
* ``control_state``: for each code in ``ledger_roles.GATE_*`` the GL net against the live v2 owner subledger, read from
  the owner tables. A subledger that does not exist yet REFUSES with a named reason; it is never skipped.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.models import Account, JournalEntry, PostedLedgerLine
from sqlalchemy.orm import Session

from erp_v2.ledger_roles import GATE_TIE_CODES, GATE_WHT_CODES, GATE_ZERO_CODES
from erp_v2.migration_ledger.common import OPENING_PERIOD, ZERO, canonical_sha, fmt
from erp_v2.migration_ledger.rounding_verify import ROUNDING_KIND

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
        if owner not in (LEGACY_OWNER, *OPENING_OWNERS) and not (owner == MANUAL_OWNER and kind == ROUNDING_KIND):
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
# NOT in ``_SUBLEDGERS`` because no owner subledger exists in v2 yet, so the gate REFUSES with a named reason (never
# skips): 1400 (ruling D-B9-1 pending), 2045 (expenses a2 not pinned), 2110 (the a6 WHT table is not pinned; GL(2110)
# must then equal the WHT ledger plus named exceptions).


def control_state(db: Session, tenant: UUID) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for code in (*GATE_TIE_CODES, *GATE_WHT_CODES):
        gl = _gl_net(db, tenant, code)
        if code not in _SUBLEDGERS:
            state[code] = {
                "gl": fmt(gl),
                "subledger": None,
                "ok": False,
                "reason": f"control_subledger_unavailable:{code}",
            }
            continue
        sub = _SUBLEDGERS[code](db, tenant)
        state[code] = {
            "gl": fmt(gl),
            "subledger": fmt(sub),
            "ok": gl == sub,
            "reason": None if gl == sub else f"control_not_tied:{code}",
        }
    for code in GATE_ZERO_CODES:
        gl = _gl_net(db, tenant, code)
        state[code] = {
            "gl": fmt(gl),
            "subledger": None,
            "ok": gl == 0,
            "reason": None if gl == 0 else f"zero_account_not_zero:{code}",
        }
    return state


def control_lists() -> dict[str, list[str]]:
    return {"tie": list(GATE_TIE_CODES), "zero": list(GATE_ZERO_CODES), "wht": list(GATE_WHT_CODES)}


def ledger_state(db: Session, tenant: UUID) -> dict[str, Any]:
    total, _native = v2_posted(db, tenant)
    controls = control_state(db, tenant)
    rows = [[a, p, fmt(v)] for (a, p), v in sorted(total.items())]
    return {"v2_posted_sha256": canonical_sha(rows), "controls": controls, "control_lists": control_lists()}
