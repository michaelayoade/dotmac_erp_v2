"""Rounding-residue correction: the bridge-anchored amount and its unconditional re-verification (ruling v1640).

No reconciliation tolerance: each T-ROUND-2DP header delta is an itemised bridge line, and the per-period sum is
cleared by ONE system-built correcting journal, Dr/Cr 2000 against the Finance-named rounding account. The journal's
source identity is anchored to the sha-bound bridge version (``<bridge sha256>:<YYYY-MM>``), so it posts at most
once. ``journals.service`` admits control lines for this kind (as for the section 3c ledger fault) only after
``verify`` re-proves, at every request and every approval, that the journal still equals what the LATEST bridge
version says is due. Imports no journal command, so ``journals.service`` may import this module.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.contracts import JournalStatus
from dotmac_accounting.models import JournalEntry, JournalLine
from sqlalchemy.orm import Session

from erp_v2.ledger_roles import account_id_for_code
from erp_v2.migration_ledger.common import PERIOD_RE, SHA_RE, ZERO, LedgerRefused, refuse

ROUNDING_KIND: Final = "rounding_residue_correction"
MANUAL_OWNER: Final = "erp.manual"
AP_CONTROL: Final = "2000"
_KEY = re.compile(r"^([0-9a-f]{64}):(\d{4}-(?:0[1-9]|1[0-2]))$")


def key_for(bridge_sha: str, period: str) -> str:
    if not SHA_RE.match(bridge_sha) or not PERIOD_RE.match(period):
        refuse("rounding_source_invalid")
    return f"{bridge_sha}:{period}"


def parse_key(document_id: str | None) -> tuple[str, str]:
    found = _KEY.match(document_id or "")
    if not found:
        raise LedgerRefused("rounding_source_invalid", "the correction's source identity is malformed")
    return found.group(1), found.group(2)


def _net(db: Session, tenant: UUID, journal_id: UUID, account_id: UUID) -> Decimal:
    return Decimal(
        db.scalar(
            sa.select(
                sa.func.coalesce(sa.func.sum(JournalLine.debit_functional - JournalLine.credit_functional), 0)
            ).where(
                JournalLine.tenant_id == tenant,
                JournalLine.journal_id == journal_id,
                JournalLine.account_id == account_id,
            )
        )
    )


def posted_corrections(db: Session, tenant: UUID, period: str, exclude: UUID | None = None) -> list[UUID]:
    query = sa.select(JournalEntry.id).where(
        JournalEntry.tenant_id == tenant,
        JournalEntry.source_owner == MANUAL_OWNER,
        JournalEntry.source_document_kind == ROUNDING_KIND,
        JournalEntry.source_document_id.like(f"%:{period}"),
        JournalEntry.status == JournalStatus.POSTED,
    )
    if exclude is not None:
        query = query.where(JournalEntry.id != exclude)
    return list(db.scalars(query))


def due(db: Session, tenant: UUID, body: dict[str, Any], period: str, exclude: UUID | None = None) -> Decimal:
    """The 2000 net (debit - credit) the correction for this period must carry: the residue (v2 subledger - legacy GL)
    minus what is already posted, so that GL(2000) equals the subledger."""
    residue = Decimal(body["rounding_residue_by_period"].get(period, "0"))
    ap = account_id_for_code(db, tenant, AP_CONTROL)
    if ap is None:
        refuse("rounding_ap_control_missing")
    posted = sum((_net(db, tenant, j, ap) for j in posted_corrections(db, tenant, period, exclude)), ZERO)
    return residue - posted


def pending_periods(db: Session, tenant: UUID, body: dict[str, Any]) -> list[str]:
    """Periods whose planned correction is not (fully) posted yet."""
    return [p for p in sorted(body["rounding_residue_by_period"]) if due(db, tenant, body, p) != 0]


def load_bridge(db: Session, tenant: UUID, sha: str) -> dict[str, Any]:
    from erp_v2.migration_ledger import bridge

    row = bridge.by_sha(db, tenant, sha)
    if row is None:
        refuse("rounding_bridge_unknown")
    body = bridge.verified_body(row)
    if not row.ready or not body.get("ready"):
        refuse("rounding_bridge_not_ready", "only a ready bridge version anchors a correction")
    latest = bridge.latest(db, tenant)
    if latest is None or latest.sha256 != sha:
        refuse("rounding_bridge_superseded", "only the latest bridge version anchors a correction")
    return body


def verify(db: Session, tenant: UUID, journal: JournalEntry) -> None:
    """Re-prove that this journal still clears exactly what the latest bridge says is due for its period."""
    sha, period = parse_key(journal.source_document_id)
    body = load_bridge(db, tenant, sha)
    code = body.get("rounding_account_code")
    if not code:
        refuse("rounding_account_required", "the bridge names no rounding account")
    account = account_id_for_code(db, tenant, code)
    ap = account_id_for_code(db, tenant, AP_CONTROL)
    if account is None or ap is None:
        refuse("rounding_account_required", "the rounding account does not exist for this tenant")
    expected = due(db, tenant, body, period, exclude=journal.id)
    if expected == 0:
        refuse("rounding_nothing_due")
    lines = list(
        db.scalars(
            sa.select(JournalLine.account_id).where(
                JournalLine.tenant_id == tenant, JournalLine.journal_id == journal.id
            )
        )
    )
    if sorted(map(str, lines)) != sorted(map(str, (ap, account))):
        refuse("rounding_lines_invalid", "exactly one line on 2000 and one on the rounding account")
    if _net(db, tenant, journal.id, ap) != expected or _net(db, tenant, journal.id, account) != -expected:
        refuse("rounding_amount_changed", "the correction no longer equals the bridge residue")
