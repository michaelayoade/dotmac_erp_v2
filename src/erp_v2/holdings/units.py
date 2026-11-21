# ruff: noqa: E501
"""Legacy journals the importer refuses as UNITS, reproduced exactly by system journals (STRADDLE-SPEC W8 and the
E15/side-file ruling). Two modes in one module:

* ``straddle`` (E39, kind ``migration_straddle``): a legacy journal dated across the history cutoff. One balanced journal
  per ledger-line day, at the legacy account, amount and date. No suspense line.
* ``subkobo`` (E15, kind ``migration_subkobo``): a legacy journal whose posted ledger lines do not balance (a sub-kobo
  residue). One journal re-posting every legacy line EXACTLY (6 dp) plus ONE residue line on the rounding account, on its
  legacy day.

Input is #74's own side file, exactly as ``importer/history_cutoff`` writes it (``<receipt>.straddlers.json`` /
``<receipt>.subkobo.json``): top level ``{history_cutoff, class, reason, journals}``, #74's per-journal and per-line key
sets, with ``evidence_sha256`` recomputed here through the shared evidence function. The file must be bound to the import
receipt (``receipts.import_receipt`` in the run-spec): its sha equals
``history_cutoff_census.{straddlers|subkobo}.side_file_sha256``, its ``history_cutoff`` equals the receipt's and the posted
opening's as-of date, and the full journal-id set and count equal the receipt's.

Refusals (fail closed, nothing written): a legacy journal that is not POSTED; a non-functional currency; a control, holding,
tax-role or prepaid-funding account line; an inactive or non-postable account; a day whose period is not OPEN (never
re-dated); a changed fingerprint for an identity already posted (``<mode>_already_applied``, the window closure).
Straddle also refuses S1 (a line on or before the cutoff), a non-zero net (total or per day) and s_j != 0. Sub-kobo also
refuses lines on more than one day, lines on or before the cutoff, and a net outside 0 < |net| < 0.01.

Each journal gets one register item: E39 OPEN (a review item, not a balance) or E15 RESOLVED (Michael's rounding ruling
decides it), carrying #74's evidence sha and the reproduction journal ids.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
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
from erp_v2.holdings.models import (
    KIND_STRADDLE_POST,
    KIND_SUBKOBO_POST,
    OWNER,
    STRADDLE_SOURCE,
    HoldingAccount,
    straddle_document_id,
    subkobo_document_id,
)
from erp_v2.importer.history_cutoff import unit_evidence_sha  # B-13 owns it (re-exports the shared function)
from erp_v2.ledger_roles import tax_role_account_ids
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import canonical_sha, day, fmt, money, sha_text, strict_json, text, uuid_text

TOP_KEYS: Final = {"history_cutoff", "class", "reason", "journals"}
LINE_KEYS: Final = {
    "ledger_line_id", "journal_line_id", "account_id", "account_code", "posting_date", "debit", "credit",
    "original_currency",
}  # fmt: skip
COMMON_KEYS: Final = {
    "class", "reason", "journal", "type", "legacy_status", "journal_date", "source_module", "source_document_type",
    "source_document_id", "currency", "lines", "evidence_sha256", "post_net",
}  # fmt: skip
SUBKOBO_SOURCE: Final = "migration_subkobo"
SUBKOBO_RANGE: Final = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Mode:
    name: str  # refusal prefix
    kind: str
    klass: str
    reason: str
    section: str  # history_cutoff_census.<section> of the import receipt
    extra_keys: frozenset[str]
    source: str


STRADDLE: Final = Mode(
    "straddle", KIND_STRADDLE_POST, "E39", "journal_straddles_cutoff", "straddlers",
    frozenset({"pre_net", "s_j"}), STRADDLE_SOURCE,
)  # fmt: skip
SUBKOBO: Final = Mode(
    "subkobo", KIND_SUBKOBO_POST, "E15", "unbalanced_journal_unit", "subkobo", frozenset({"net"}), SUBKOBO_SOURCE
)  # fmt: skip
MODES: Final = {"straddle": STRADDLE, "subkobo": SUBKOBO}


@dataclass(frozen=True, slots=True)
class Line:
    account_id: UUID
    day: date
    debit: Decimal
    credit: Decimal
    ledger_line_id: str


@dataclass(frozen=True, slots=True)
class Journal:
    journal_id: str
    type: str
    status: str
    journal_date: str | None
    source_module: str | None
    source_document_type: str | None
    source_document_id: str | None
    currency: str | None
    evidence_sha256: str
    post_net: dict[str, dict[str, str]]
    lines: tuple[Line, ...]
    raw_lines: tuple[dict[str, Any], ...]


def _net(lines) -> Decimal:
    return sum((ln.debit - ln.credit for ln in lines), ZERO)


def _by_day(lines) -> dict[date, list[Line]]:
    out: dict[date, list[Line]] = defaultdict(list)
    for ln in lines:
        out[ln.day].append(ln)
    return dict(sorted(out.items()))


def _decimal_map(value: object, nested: bool) -> dict[Any, Any]:
    if not isinstance(value, dict):
        refuse("invalid_unit_file")
    if nested:
        return {k: _decimal_map(v, False) for k, v in value.items()}
    return {str(k): money(v, "invalid_unit_file") for k, v in value.items()}


def parse_side_file(raw: bytes, mode: Mode) -> tuple[date, tuple[Journal, ...], str]:
    """#74's side file, exactly. Refuses any other shape, a changed line (evidence sha) and recomputed-value mismatches."""
    sha = hashlib.sha256(raw).hexdigest()
    body = strict_json(raw, "invalid_unit_file")
    if not isinstance(body, dict) or set(body) != TOP_KEYS or not isinstance(body["journals"], list):
        refuse("invalid_unit_file")
    if body["class"] != mode.klass or body["reason"] != mode.reason:
        refuse("invalid_unit_file", "class or reason differs from the mode")
    cutoff = day(body["history_cutoff"], "invalid_unit_file")
    keys = COMMON_KEYS | mode.extra_keys
    journals: list[Journal] = []
    seen: set[str] = set()
    for j in body["journals"]:
        if not isinstance(j, dict) or set(j) != keys or j["class"] != mode.klass or j["reason"] != mode.reason:
            refuse("invalid_unit_file", "journal key set")
        jid = text(j["journal"], "invalid_unit_file", 120)
        if jid in seen or not isinstance(j["lines"], list) or len(j["lines"]) < 2:
            refuse("invalid_unit_file", "duplicate journal or too few lines")
        seen.add(jid)
        lines = []
        for ln in j["lines"]:
            if not isinstance(ln, dict) or set(ln) != LINE_KEYS:
                refuse("invalid_unit_file", "line key set")
            debit, credit = money(ln["debit"], "invalid_unit_file"), money(ln["credit"], "invalid_unit_file")
            if debit < 0 or credit < 0 or (debit > 0) == (credit > 0):
                refuse("invalid_unit_file", "each line carries exactly one positive side")
            lines.append(
                Line(
                    uuid_text(ln["account_id"], "invalid_unit_file"),
                    day(ln["posting_date"], "invalid_unit_file"),
                    debit,
                    credit,
                    text(str(ln["ledger_line_id"]), "invalid_unit_file", 120),
                )
            )
        if unit_evidence_sha(j["lines"]) != sha_text(j["evidence_sha256"], "invalid_unit_file"):
            refuse("evidence_sha_mismatch", jid)
        journals.append(
            Journal(
                jid,
                text(j["type"], "invalid_unit_file", 20),
                text(j["legacy_status"], "invalid_unit_file", 30) if j["legacy_status"] is not None else "",
                j["journal_date"],
                j["source_module"],
                j["source_document_type"],
                j["source_document_id"],
                j["currency"],
                j["evidence_sha256"],
                _decimal_map(j["post_net"], True),  # type: ignore[arg-type]
                tuple(lines),
                tuple(j["lines"]),
            )
        )
        _check_recomputed(mode, cutoff, j, tuple(lines), journals[-1])
    if not journals:
        refuse("invalid_unit_file", "no journals")
    return cutoff, tuple(sorted(journals, key=lambda x: x.journal_id)), sha


def _check_recomputed(mode: Mode, cutoff: date, raw: dict[str, Any], lines: tuple[Line, ...], j: Journal) -> None:
    """Recompute post_net (and pre_net / s_j / type, or net) from the lines and refuse any difference from the file."""
    pre: dict[str, Decimal] = {}
    post: dict[str, dict[str, Decimal]] = {}
    for ln in lines:
        value = ln.debit - ln.credit
        if ln.day <= cutoff:
            pre[str(ln.account_id)] = pre.get(str(ln.account_id), ZERO) + value
        else:
            d = post.setdefault(ln.day.isoformat(), {})
            d[str(ln.account_id)] = d.get(str(ln.account_id), ZERO) + value
    if _decimal_map(raw["post_net"], True) != post:
        refuse("side_file_net_mismatch", f"{j.journal_id} post_net")
    if mode is STRADDLE:
        if _decimal_map(raw["pre_net"], False) != pre:
            refuse("side_file_net_mismatch", f"{j.journal_id} pre_net")
        if money(raw["s_j"], "invalid_unit_file") != sum(pre.values(), ZERO):
            refuse("side_file_net_mismatch", f"{j.journal_id} s_j")
        kind = "S1" if pre and post else "S2"
        if raw["type"] != kind:
            refuse("side_file_net_mismatch", f"{j.journal_id} type")
    else:
        if money(raw["net"], "invalid_unit_file") != _net(lines):
            refuse("side_file_net_mismatch", f"{j.journal_id} net")
        if raw["type"] != "UNBALANCED":
            refuse("side_file_net_mismatch", f"{j.journal_id} type")


def bind_to_import_receipt(
    db: Session,
    tenant: UUID,
    mode: Mode,
    receipt_raw: bytes,
    side_sha: str,
    cutoff: date,
    journals: tuple[Journal, ...],
) -> None:
    """N2 / ruling 6: the side file is the one #74 wrote, for the posted opening's as-of date."""
    body = strict_json(receipt_raw, "invalid_import_receipt")
    census = body.get("history_cutoff_census") if isinstance(body, dict) else None
    section = census.get(mode.section) if isinstance(census, dict) else None
    if not isinstance(section, dict):
        refuse("import_receipt_unit_section_missing", mode.section)
    if section.get("side_file_sha256") != side_sha:
        refuse("side_file_sha_mismatch", "the side file is not the one the import receipt names")
    if body.get("history_cutoff") != cutoff.isoformat():
        refuse("history_cutoff_mismatch", "the side file and the import receipt disagree on the cutoff")
    listed = section.get("journals")
    if not isinstance(listed, list) or sorted(str(x) for x in listed) != sorted(j.journal_id for j in journals):
        refuse("unit_set_mismatch", "the journal set differs from the import receipt")
    if section.get("count") != len(journals):
        refuse("unit_set_mismatch", "the journal count differs from the import receipt")
    opening = db.scalar(
        sa.select(sa.func.max(JournalEntry.posting_date)).where(
            JournalEntry.tenant_id == tenant, JournalEntry.source_owner == planning.OPENING_OWNER
        )
    )
    if opening is None:
        refuse("opening_missing", "no posted opening")
    if opening != cutoff:
        refuse("history_cutoff_mismatch", "the cutoff differs from the posted opening's as-of date")


def bridge_rounding_code(db: Session, tenant: UUID) -> str | None:
    """The rounding account the latest bridge version names (its T-SUBKOBO items post there); None before any bridge (callers refuse as unbound)."""
    body = db.scalar(
        sa.select(m.BridgeVersion.body)
        .where(m.BridgeVersion.tenant_id == tenant)
        .order_by(m.BridgeVersion.version.desc())
        .limit(1)
    )
    code = body.get("rounding_account_code") if isinstance(body, dict) else None
    return code if isinstance(code, str) and code else None


def check_rounding_matches_bridge(db: Session, tenant: UUID, rounding_code: str) -> None:
    bridge_code = bridge_rounding_code(db, tenant)
    if bridge_code is None:
        refuse("rounding_account_unbound", "no bridge version names a rounding account yet")
    if bridge_code != rounding_code:
        refuse("rounding_account_differs_from_bridge", "the bridge's T-SUBKOBO items name another account")


def evidence_of(journal: Journal) -> str:
    return journal.evidence_sha256


# --- plan ---------------------------------------------------------------------------------------------------------


def _refused_account_ids(
    db: Session, tenant: UUID, accounts: dict[UUID, Account], spec: planning.RunSpec
) -> tuple[set[UUID], set[UUID]]:
    """(control-like ids, other refused ids): control codes, the prepayment, persisted and run-spec holding accounts;
    tax-role accounts and the prepaid-funding account."""
    control = {a.id for a in accounts.values() if a.code in cfg.TIE_CONTROL_CODES}
    control.add(cfg.prepayment_account(db, tenant).id)
    control |= set(db.scalars(sa.select(HoldingAccount.holding_account_id).where(HoldingAccount.tenant_id == tenant)))
    control |= {ref.id for ref in cfg.resolve_config(db, tenant, spec.params).accounts.values()}  # before holding apply
    other = {UUID(t) for t in tax_role_account_ids(db, tenant) if _is_uuid(t)}
    funding = cfg.prepaid_funding_account(db, tenant)
    if funding is not None:
        other.add(funding.id)
    return control, other


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def window_closed(db: Session, tenant: UUID, mode: Mode, sha: str) -> None:
    """S1: an identity of this kind already posted from another plan closes the window."""
    other = db.scalar(
        sa.select(JournalEntry.id)
        .where(
            JournalEntry.tenant_id == tenant,
            JournalEntry.source_owner == OWNER,
            JournalEntry.source_document_kind == mode.kind,
            JournalEntry.source_fingerprint != sha,
        )
        .limit(1)
    )
    if other is not None:
        refuse(f"{mode.name}_already_applied", "this kind was already applied from a different plan")


def build_plan(
    db: Session, spec: planning.RunSpec, spec_sha: str, mode: Mode, raw: bytes, receipt_raw: bytes, *, planned_by: str
) -> dict[str, Any]:
    """Read-only: validate every journal and assemble the sha-bound plan."""
    from erp_v2.journals.policy import functional_currency

    tenant = spec.tenant_id
    cutoff, journals, side_sha = parse_side_file(raw, mode)
    bind_to_import_receipt(db, tenant, mode, receipt_raw, side_sha, cutoff, journals)
    functional = functional_currency(db, tenant)
    accounts = {a.id: a for a in db.scalars(sa.select(Account).where(Account.tenant_id == tenant))}
    control_ids, other_ids = _refused_account_ids(db, tenant, accounts, spec)
    barred = control_ids | other_ids
    rounding = None
    if mode is SUBKOBO:
        if "migration.rounding_account" not in spec.params:
            refuse("rounding_account_required", "name migration.rounding_account in the run-spec params")
        rounding = cfg.resolve_account(db, tenant, spec.params["migration.rounding_account"], what="rounding account")
        if rounding.id in barred:
            refuse("rounding_account_invalid", "a control, tax-role or holding account cannot be the rounding account")
        check_rounding_matches_bridge(db, tenant, rounding.code)
    periods = list(db.scalars(sa.select(FiscalPeriod).where(FiscalPeriod.tenant_id == tenant)))
    out = []
    for j in journals:
        if j.status != "POSTED":
            refuse(f"{mode.name}_not_posted", j.journal_id)
        if j.currency != functional or any(ln.get("original_currency") not in (None, functional) for ln in j.raw_lines):
            refuse("holding_fx_unsupported", j.journal_id)
        if mode is STRADDLE:
            if j.type != "S2" or any(ln.day <= cutoff for ln in j.lines):
                refuse("straddle_splits_cutoff", j.journal_id)
            if _net(j.lines) != 0:
                refuse("straddle_nonzero_net", j.journal_id)
        else:
            if any(ln.day <= cutoff for ln in j.lines):
                refuse("subkobo_splits_cutoff", j.journal_id)
            if len(_by_day(j.lines)) != 1:
                refuse("subkobo_multi_day", j.journal_id)
            net = _net(j.lines)
            if not (0 < abs(net) < SUBKOBO_RANGE):
                refuse("subkobo_residue_out_of_range", j.journal_id)
        days = []
        for d, lines in _by_day(j.lines).items():
            if mode is STRADDLE and _net(lines) != 0:
                refuse("straddle_nonzero_net", f"{j.journal_id} on one day")
            for ln in lines:
                acct = accounts.get(ln.account_id)
                if acct is None or not acct.is_active or not acct.posting_allowed or acct.kind != AccountKind.POSTING:
                    refuse(f"{mode.name}_account_invalid", j.journal_id)
                if ln.account_id in control_ids:
                    refuse(f"{mode.name}_control_line", j.journal_id)
                if ln.account_id in other_ids:
                    refuse(f"{mode.name}_account_refused", j.journal_id)
            period = [p for p in periods if p.start_date <= d <= p.end_date]
            if len(period) != 1 or period[0].status != PeriodStatus.OPEN or period[0].is_adjustment:
                refuse(f"{mode.name}_period_not_postable", f"{j.journal_id} {d.isoformat()}")
            plan_lines = [
                {"account_id": str(ln.account_id), "debit": fmt(ln.debit), "credit": fmt(ln.credit), "ledger_line_id": ln.ledger_line_id}
                for ln in lines
            ]  # fmt: skip
            if mode is SUBKOBO:
                residue = -_net(lines)
                plan_lines.append(
                    {"account_id": str(rounding.id), "debit": fmt(max(residue, ZERO)), "credit": fmt(max(-residue, ZERO)), "ledger_line_id": "residue"}
                )  # fmt: skip
            doc = (straddle_document_id if mode is STRADDLE else subkobo_document_id)(j.journal_id, d.isoformat())
            days.append({"day": d.isoformat(), "document_id": doc, "lines": plan_lines})
        out.append(
            {
                "journal_id": j.journal_id, "type": j.type, "legacy_status": j.status, "journal_date": j.journal_date,
                "source_module": j.source_module, "source_document_type": j.source_document_type,
                "source_document_id": j.source_document_id,
                "post_net": {d: {a: fmt(v) for a, v in m_.items()} for d, m_ in j.post_net.items()},
                "evidence_sha256": j.evidence_sha256, "days": days,
            }
        )  # fmt: skip
    body = {
        "version": 1, "mode": mode.name, "spec_sha256": spec_sha, "side_file_sha256": side_sha,
        "cutoff": cutoff.isoformat(), "currency": functional, "journals": out,
    }  # fmt: skip
    return {"plan": body, "plan_sha256": canonical_sha(body), "planned_by": text(planned_by, "operator_ref_required")}


def _load(spec: planning.RunSpec, mode: Mode) -> tuple[bytes, bytes]:
    ref = spec.straddle if mode is STRADDLE else spec.subkobo
    if ref is None or spec.import_receipt is None:
        refuse(f"{mode.name}_file_required", "name the side file and the import receipt in receipts")
    out = []
    for r in (ref, spec.import_receipt):
        data = Path(r.path).read_bytes()
        if hashlib.sha256(data).hexdigest() != r.sha256:
            refuse("input_sha_mismatch", Path(r.path).name)
        out.append(data)
    return out[0], out[1]


def run_plan(engine: Engine, spec_bytes: bytes, mode: Mode, *, planned_by: str) -> dict[str, Any]:
    spec, spec_sha = planning.parse_spec(spec_bytes)
    raw, receipt_raw = _load(spec, mode)
    with read_only_session(engine) as db:
        out = build_plan(db, spec, spec_sha, mode, raw, receipt_raw, planned_by=planned_by)
        db.rollback()
    return out


# --- apply / verify ------------------------------------------------------------------------------------------------


def apply(
    db: Session,
    spec: planning.RunSpec,
    mode: Mode,
    plan: dict[str, Any],
    *,
    approved_sha: str,
    applied_by: str,
    now=None,
) -> dict[str, Any]:
    """One transaction (the caller's): post every (journal, day) once and register the items."""
    if plan["plan_sha256"] != approved_sha:
        refuse("plan_sha_mismatch")
    tenant = spec.tenant_id
    planned_by = text(plan["planned_by"], "operator_ref_required")
    applied_by = text(applied_by, "operator_ref_required")
    if governance.dual_control(db, tenant) and planned_by == applied_by:
        refuse("dual_control_requires_distinct_operators")
    window_closed(db, tenant, mode, approved_sha)  # S1
    body = plan["plan"]
    if mode is SUBKOBO:  # the residue account must still be the bridge's T-SUBKOBO account
        residue_account = UUID(body["journals"][0]["days"][0]["lines"][-1]["account_id"])
        code = db.scalar(sa.select(Account.code).where(Account.tenant_id == tenant, Account.id == residue_account))
        check_rounding_matches_bridge(db, tenant, str(code))
    posted: dict[str, list[str]] = {}
    count = 0
    for j in body["journals"]:
        ids = []
        for d in j["days"]:
            lines = tuple(
                posting.Line(
                    ln["account_id"], money(ln["debit"]), money(ln["credit"]),
                    "residue" if ln["ledger_line_id"] == "residue" else f"legacy-ledger-line:{ln['ledger_line_id']}",
                )
                for ln in d["lines"]
            )  # fmt: skip
            consequence = posting.build(
                kind=mode.kind,
                document_id=d["document_id"],
                fingerprint=approved_sha,
                posting_date=date.fromisoformat(d["day"]),
                description=f"{mode.name.capitalize()} reproduction legacy-journal:{j['journal_id']} {d['day']}",
                currency=body["currency"],
                lines=lines,
            )
            result = posting.post(
                db, tenant, consequence, approval_ref=f"{mode.name}-plan:{approved_sha}", actor=applied_by, now=now
            )
            ids.append(str(result.journal_id))
            count += 1
        posted[j["journal_id"]] = ids
    facts = []
    for j in body["journals"]:
        detail: dict[str, Any] = {
            "legacy_journal_id": j["journal_id"], "legacy_status": j["legacy_status"], "type": j["type"],
            "journal_date": j["journal_date"], "source_module": j["source_module"],
            "source_document_type": j["source_document_type"], "source_document_id": j["source_document_id"],
            "post_net": j["post_net"], "evidence_sha256": j["evidence_sha256"],
            "reproduction_journal_ids": posted[j["journal_id"]],
        }  # fmt: skip
        if mode is STRADDLE:
            detail["review"] = "accept as recorded (evidence resolve) or correct by an ordinary journal"
            facts.append(register.Fact(mode.klass, f"straddle:{j['journal_id']}", mode.source, None, None, detail))
        else:  # Michael's rounding ruling decides E15: recorded RESOLVED with the evidence
            facts.append(
                register.Fact(
                    mode.klass, f"subkobo:{j['journal_id']}", mode.source, "posted", j["evidence_sha256"], detail
                )
            )
    register.reconcile(db, tenant, facts, loaded_sources={mode.source}, receipt_sha=approved_sha, apply=True)
    return {"ready": True, "plan_sha256": approved_sha, "journals": len(body["journals"]), "posted_journals": count}


def verify(db: Session, spec: planning.RunSpec, mode: Mode) -> dict[str, Any]:
    """RR READ ONLY: every (journal, day) is posted exactly once on its legacy day with the legacy lines (plus exactly one
    residue line on the rounding account for E15), and its register item exists."""
    tenant = spec.tenant_id
    raw, _receipt = _load(spec, mode)
    _cutoff, journals, _sha = parse_side_file(raw, mode)
    rounding = None
    if mode is SUBKOBO:
        rounding = cfg.resolve_account(
            db, tenant, spec.params.get("migration.rounding_account"), what="rounding account"
        )
    problems: list[str] = []
    for j in journals:
        for d, lines in _by_day(j.lines).items():
            doc = (straddle_document_id if mode is STRADDLE else subkobo_document_id)(j.journal_id, d.isoformat())
            rows = list(
                db.scalars(
                    sa.select(JournalEntry).where(
                        JournalEntry.tenant_id == tenant,
                        JournalEntry.source_owner == OWNER,
                        JournalEntry.source_document_kind == mode.kind,
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
            want = [(str(ln.account_id), fmt(ln.debit), fmt(ln.credit)) for ln in lines]
            if rounding is not None:
                residue = -_net(lines)
                want.append((str(rounding.id), fmt(max(residue, ZERO)), fmt(max(-residue, ZERO))))
            if got != sorted(want):
                problems.append(f"{j.journal_id}:{d.isoformat()}:lines_differ")
        item = db.scalar(
            sa.select(m.MigrationException).where(
                m.MigrationException.tenant_id == tenant,
                m.MigrationException.class_code == mode.klass,
                m.MigrationException.legacy_key == f"{mode.name}:{j.journal_id}",
            )
        )
        if item is None:
            problems.append(f"{j.journal_id}:no_{mode.klass.lower()}_item")
        elif mode is SUBKOBO and (item.state != "resolved" or item.evidence_sha256 != j.evidence_sha256):
            problems.append(f"{j.journal_id}:e15_not_resolved_with_evidence")
    if rounding is not None:
        bound = bridge_rounding_code(db, tenant)
        if bound is None:
            problems.append("rounding_account_unbound")
        elif bound != rounding.code:
            problems.append("rounding_account_differs_from_bridge")
    return {"ready": not problems, "journals": len(journals), "problems": sorted(problems)}
