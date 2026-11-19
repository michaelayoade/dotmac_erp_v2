"""Straddle reproduction (STRADDLE-SPEC W8, simplified by the architect): legacy journals dated past the cutoff.

The importer (B-13) excludes each such journal as a UNIT and lists it, with its ledger lines, in a mode-600 side file
(E39). Its legacy GL is reproduced here by one system journal per (legacy journal, ledger-line day), kind
``migration_straddle``: the legacy lines at the legacy account, amount and date, balanced on their own. It never touches
the suspense account and never re-dates, re-amounts or re-accounts.

The step REFUSES (fail-closed, nothing written) any journal that:

* has a ledger line on or before the cutoff (it genuinely splits across D: ``straddle_splits_cutoff``);
* has a non-zero net, or a non-zero net on any one day (``straddle_nonzero_net``);
* touches a control account (``straddle_control_line``: 2000, 1400, 2040, 2045, 1410, the AR prepayment account or a
  holding account), so the W8 control-account door is not used;
* is in a non-functional currency, or on an inactive or non-postable account, or on a day whose period is not OPEN.

Side file (sealed, sha-bound in the run-spec ``receipts.straddle``)::

    {"version": 1, "cutoff": "YYYY-MM-DD", "currency": "NGN",
     "journals": [{"journal_id": "...", "legacy_status": "POSTED", "source_module": "...",
                   "lines": [{"account_id": "<uuid>", "date": "YYYY-MM-DD", "debit": "0.00", "credit": "0.00"}]}]}

Each journal also becomes one OPEN ``E39`` register item (source ``migration_straddle``): a review item, not a balance,
carrying the reproduction journal ids and the evidence sha of its legacy lines.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.contracts import AccountKind, JournalStatus, PeriodStatus
from dotmac_accounting.models import Account, FiscalPeriod, JournalEntry, PostedLedgerLine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from erp_v2.governance import policy as governance
from erp_v2.holdings import config as cfg
from erp_v2.holdings import plan as planning
from erp_v2.holdings import posting
from erp_v2.holdings.apply import read_only_session
from erp_v2.holdings.attribution import ZERO
from erp_v2.holdings.errors import refuse
from erp_v2.holdings.models import KIND_STRADDLE_POST, OWNER, STRADDLE_SOURCE, HoldingAccount, straddle_document_id
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import canonical_sha, day, fmt, money, strict_json, text, uuid_text

E39: Final = "E39"
SIDE_KEYS: Final = {"version", "cutoff", "currency", "journals"}


@dataclass(frozen=True, slots=True)
class Line:
    account_id: UUID
    day: date
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True, slots=True)
class Journal:
    journal_id: str
    status: str
    source_module: str
    lines: tuple[Line, ...]


def parse_side_file(raw: bytes) -> tuple[date, str, tuple[Journal, ...], str]:
    import hashlib

    sha = hashlib.sha256(raw).hexdigest()
    body = strict_json(raw, "invalid_straddle_file")
    if (
        not isinstance(body, dict)
        or set(body) != SIDE_KEYS
        or body["version"] != 1
        or not isinstance(body["journals"], list)
    ):
        refuse("invalid_straddle_file")
    cutoff = day(body["cutoff"], "invalid_straddle_file")
    currency = text(body["currency"], "invalid_straddle_file", 3)
    journals: list[Journal] = []
    seen: set[str] = set()
    for j in body["journals"]:
        if not isinstance(j, dict) or set(j) != {"journal_id", "legacy_status", "source_module", "lines"}:
            refuse("invalid_straddle_file")
        jid = text(j["journal_id"], "invalid_straddle_file", 120)
        if jid in seen or not isinstance(j["lines"], list) or len(j["lines"]) < 2:
            refuse("invalid_straddle_file", "duplicate journal or too few lines")
        seen.add(jid)
        lines = []
        for ln in j["lines"]:
            if not isinstance(ln, dict) or set(ln) != {"account_id", "date", "debit", "credit"}:
                refuse("invalid_straddle_file")
            debit, credit = money(ln["debit"], "invalid_straddle_file"), money(ln["credit"], "invalid_straddle_file")
            if debit < 0 or credit < 0 or (debit > 0) == (credit > 0):
                refuse("invalid_straddle_file", "each line carries exactly one positive side")
            lines.append(
                Line(
                    uuid_text(ln["account_id"], "invalid_straddle_file"),
                    day(ln["date"], "invalid_straddle_file"),
                    debit,
                    credit,
                )
            )
        journals.append(
            Journal(
                jid,
                text(j["legacy_status"], "invalid_straddle_file", 30),
                text(j["source_module"], "invalid_straddle_file", 60),
                tuple(lines),
            )
        )
    if not journals:
        refuse("invalid_straddle_file", "no journals")
    return cutoff, currency, tuple(sorted(journals, key=lambda x: x.journal_id)), sha


def _net(lines) -> Decimal:
    return sum((ln.debit - ln.credit for ln in lines), ZERO)


def _by_day(journal: Journal) -> dict[date, list[Line]]:
    out: dict[date, list[Line]] = defaultdict(list)
    for ln in journal.lines:
        out[ln.day].append(ln)
    return dict(sorted(out.items()))


def evidence_sha(journal: Journal) -> str:
    return canonical_sha(
        [[str(ln.account_id), ln.day.isoformat(), fmt(ln.debit), fmt(ln.credit)] for ln in journal.lines]
    )


def build_plan(db: Session, spec: planning.RunSpec, spec_sha: str, raw: bytes, *, planned_by: str) -> dict[str, Any]:
    """Read-only: validate every journal and assemble the sha-bound plan."""
    from erp_v2.journals.policy import functional_currency

    tenant = spec.tenant_id
    cutoff, currency, journals, side_sha = parse_side_file(raw)
    functional = functional_currency(db, tenant)
    if currency != functional:
        refuse("holding_fx_unsupported", "straddle file currency")
    accounts = {a.id: a for a in db.scalars(sa.select(Account).where(Account.tenant_id == tenant))}
    control_ids = {a.id for a in accounts.values() if a.code in cfg.TIE_CONTROL_CODES}
    control_ids.add(cfg.prepayment_account(db, tenant).id)
    control_ids |= set(
        db.scalars(sa.select(HoldingAccount.holding_account_id).where(HoldingAccount.tenant_id == tenant))
    )
    periods = list(db.scalars(sa.select(FiscalPeriod).where(FiscalPeriod.tenant_id == tenant)))
    out = []
    for j in journals:
        if any(ln.day <= cutoff for ln in j.lines):
            refuse("straddle_splits_cutoff", j.journal_id)
        if _net(j.lines) != 0:
            refuse("straddle_nonzero_net", j.journal_id)
        days = []
        for d, lines in _by_day(j).items():
            if _net(lines) != 0:
                refuse("straddle_nonzero_net", f"{j.journal_id} on one day")
            for ln in lines:
                acct = accounts.get(ln.account_id)
                if acct is None or not acct.is_active or not acct.posting_allowed or acct.kind != AccountKind.POSTING:
                    refuse("straddle_account_invalid", j.journal_id)
                if ln.account_id in control_ids:
                    refuse("straddle_control_line", j.journal_id)
            period = [p for p in periods if p.start_date <= d <= p.end_date]
            if len(period) != 1 or period[0].status != PeriodStatus.OPEN or period[0].is_adjustment:
                refuse("straddle_period_not_postable", f"{j.journal_id} {d.isoformat()}")
            days.append(
                {
                    "day": d.isoformat(),
                    "document_id": straddle_document_id(j.journal_id, d.isoformat()),
                    "lines": [
                        {"account_id": str(ln.account_id), "debit": fmt(ln.debit), "credit": fmt(ln.credit)}
                        for ln in lines
                    ],
                }
            )
        out.append({"journal_id": j.journal_id, "legacy_status": j.status, "source_module": j.source_module,
                    "evidence_sha256": evidence_sha(j), "days": days})  # fmt: skip
    body = {"version": 1, "spec_sha256": spec_sha, "side_file_sha256": side_sha, "cutoff": cutoff.isoformat(),
            "currency": functional, "journals": out}  # fmt: skip
    return {"plan": body, "plan_sha256": canonical_sha(body), "planned_by": text(planned_by, "operator_ref_required")}


def run_plan(engine: Engine, spec_bytes: bytes, *, planned_by: str) -> dict[str, Any]:
    spec, spec_sha = planning.parse_spec(spec_bytes)
    if spec.straddle is None:
        refuse("straddle_file_required")
    import hashlib
    from pathlib import Path

    raw = Path(spec.straddle.path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != spec.straddle.sha256:
        refuse("input_sha_mismatch", "straddle side file")
    with read_only_session(engine) as db:
        out = build_plan(db, spec, spec_sha, raw, planned_by=planned_by)
        db.rollback()
    return out


def apply(
    db: Session, spec: planning.RunSpec, plan: dict[str, Any], *, approved_sha: str, applied_by: str, now=None
) -> dict[str, Any]:
    """One transaction (the caller's): post every (journal, day) once and register the E39 review items."""
    if plan["plan_sha256"] != approved_sha:
        refuse("plan_sha_mismatch")
    tenant = spec.tenant_id
    planned_by = text(plan["planned_by"], "operator_ref_required")
    applied_by = text(applied_by, "operator_ref_required")
    if governance.dual_control(db, tenant) and planned_by == applied_by:
        refuse("dual_control_requires_distinct_operators")
    body = plan["plan"]
    posted: dict[str, list[str]] = {}
    count = 0
    for j in body["journals"]:
        ids = []
        for d in j["days"]:
            lines = tuple(
                posting.Line(
                    ln["account_id"], money(ln["debit"]), money(ln["credit"]), f"legacy-journal:{j['journal_id']}"
                )
                for ln in d["lines"]
            )
            consequence = posting.build(
                kind=KIND_STRADDLE_POST,
                document_id=d["document_id"],
                fingerprint=approved_sha,
                posting_date=date.fromisoformat(d["day"]),
                description=f"Straddle reproduction legacy-journal:{j['journal_id']} {d['day']}",
                currency=body["currency"],
                lines=lines,
            )
            result = posting.post(
                db, tenant, consequence, approval_ref=f"straddle-plan:{approved_sha}", actor=applied_by, now=now
            )
            ids.append(str(result.journal_id))
            count += 1
        posted[j["journal_id"]] = ids
    facts = [
        register.Fact(
            E39,
            f"straddle:{j['journal_id']}",
            STRADDLE_SOURCE,
            None,
            None,
            {
                "legacy_journal_id": j["journal_id"],
                "legacy_status": j["legacy_status"],
                "source_module": j["source_module"],
                "type": "S2",
                "evidence_sha256": j["evidence_sha256"],
                "reproduction_journal_ids": posted[j["journal_id"]],
                "review": "accept as recorded (evidence resolve) or correct by an ordinary journal",
            },
        )
        for j in body["journals"]
    ]
    register.reconcile(db, tenant, facts, loaded_sources={STRADDLE_SOURCE}, receipt_sha=approved_sha, apply=True)
    return {"ready": True, "plan_sha256": approved_sha, "journals": len(body["journals"]), "posted_journals": count}


def verify(db: Session, spec: planning.RunSpec, raw: bytes) -> dict[str, Any]:
    """RR READ ONLY: every (journal, day) is posted exactly once on its legacy day with exactly the legacy lines, and
    its E39 item exists."""
    tenant = spec.tenant_id
    _cutoff, _ccy, journals, _sha = parse_side_file(raw)
    problems: list[str] = []
    for j in journals:
        for d, lines in _by_day(j).items():
            doc = straddle_document_id(j.journal_id, d.isoformat())
            rows = list(
                db.scalars(
                    sa.select(JournalEntry).where(
                        JournalEntry.tenant_id == tenant,
                        JournalEntry.source_owner == OWNER,
                        JournalEntry.source_document_kind == KIND_STRADDLE_POST,
                        JournalEntry.source_document_id == doc,
                    )
                )
            )
            if len(rows) != 1 or rows[0].status != JournalStatus.POSTED or rows[0].posting_date != d:
                problems.append(f"{j.journal_id}:{d.isoformat()}:not_posted_once_on_day")
                continue
            got = sorted(
                (str(a), fmt(db_), fmt(cr))
                for a, db_, cr in db.execute(
                    sa.select(PostedLedgerLine.account_id, PostedLedgerLine.debit, PostedLedgerLine.credit).where(
                        PostedLedgerLine.tenant_id == tenant, PostedLedgerLine.journal_id == rows[0].id
                    )
                )
            )
            want = sorted((str(ln.account_id), fmt(ln.debit), fmt(ln.credit)) for ln in lines)
            if got != want:
                problems.append(f"{j.journal_id}:{d.isoformat()}:lines_differ")
        item = db.scalar(
            sa.select(m.MigrationException).where(
                m.MigrationException.tenant_id == tenant,
                m.MigrationException.class_code == E39,
                m.MigrationException.legacy_key == f"straddle:{j.journal_id}",
            )
        )
        if item is None:
            problems.append(f"{j.journal_id}:no_e39_item")
    return {"ready": not problems, "journals": len(journals), "problems": sorted(problems)}
