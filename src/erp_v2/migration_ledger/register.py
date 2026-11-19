"""The exceptions register (EXCEPTIONS-ZERO-PLAN): loaded and reconciled from every receipt; the flip gate.

Each item is one (class, legacy key) with a state, a terminal outcome and an evidence sha, and a fingerprint of the
legacy facts it was loaded from. "Resolved" never means parked: posted, admitted, voided, written off (IAS 8 or a
Tier F journal) or confirmed no-action, each with evidence.

Sources (each adapter is a pure function from a receipt or rows to ``Fact``s):

* ``held_drafts``: the B-2 APPLY receipt (``bridge_lines`` and ``e17_findings``). A queued draft is open (E16a); a
  voided or posted draft is resolved; every E17 finding is open until Finance resolves it. A PLAN receipt is refused.
* ``import_itemised``: the importer transform itemised file (#68: ``queue`` list); every queued invoice is open.
* ``admission_reviews`` (database rows of B-1 decisions): a decision that waived a ``requires_correcting_journal``
  check (E08) or whose action is not executed here (void, supplier credit, reopen, ias8_post) is open; a plain
  admission is resolved ``admitted``.

Reconciliation (``reconcile``): a new fact inserts; an open item takes the fact's state; a resolved item stays
resolved while its fingerprint is unchanged (fingerprinted decisions carry over, D-M1) and REOPENS when the legacy
facts changed; an open item whose source was loaded but no longer lists it is reported as an orphan and stays open
(never silently dropped). Finance resolves the rest with ``resolve`` and an evidence sha.

Flip gate: passes only when the open count is 0 AND the latest bridge version is signed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.migration_ledger import bridge
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger.common import LedgerRefused, canonical_sha, refuse, sha_text, text

SRC_HELD: Final = "held_drafts"
SRC_IMPORT: Final = "import_itemised"
SRC_REVIEWS: Final = "admission_reviews"
SRC_MANUAL: Final = "manual"
RECEIPT_KINDS: Final = (SRC_HELD, SRC_IMPORT)

# class by importer queue reason (#68 Q_* constants); an unknown reason is refused, never guessed.
IMPORT_CLASS: Final = {
    "expense_elsewhere_ambiguous": "E06",
    "wht_payment_fact_pending": "E06a",
    "expense_account_is_control": "E21",
    "expense_account_is_tax_role": "E21",
    "tax_account_not_input_tax": "E09",
    "taxed_line_without_valid_tax_code": "E10",
    "rounding_residue_too_large": "E06d",
    "total_mismatch_after_transform": "E04",
    "header_amount_missing": "E04",
}
DEFERRAL_CLASS: Final = "E07"
REVIEW_OPEN_DECISIONS: Final = ("void", "supplier_credit", "reopen", "ias8_post")
REVIEW_CLASS: Final = {"void": "E02", "supplier_credit": "E11", "reopen": "E11", "ias8_post": "E06f"}
E08: Final = "E08"


@dataclass(frozen=True)
class Fact:
    class_code: str
    legacy_key: str
    source_kind: str
    terminal_outcome: str | None  # None: open
    evidence_sha256: str | None
    detail: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        return canonical_sha({"c": self.class_code, "k": self.legacy_key, "d": self.detail})


# --- adapters --------------------------------------------------------------------------------------------------


def from_held_drafts(receipt: Any) -> list[Fact]:
    if not isinstance(receipt, dict) or receipt.get("mode") != "apply":
        refuse("receipt_not_applied", "only a B-2 apply receipt is loaded")
    lines, findings = receipt.get("bridge_lines"), receipt.get("e17_findings")
    if not isinstance(lines, list) or not isinstance(findings, list):
        refuse("invalid_held_drafts_receipt")
    facts: list[Fact] = []
    for line in lines:
        if not isinstance(line, dict):
            refuse("invalid_held_drafts_receipt")
        outcome, jid = line.get("outcome"), line.get("journal_id")
        evidence = sha_text(line.get("evidence_sha256"), "invalid_held_drafts_receipt")
        detail = {
            "rule_id": line.get("rule_id"),
            "reason_code": line.get("reason_code"),
            "amount": line.get("amount"),
            "outcome": outcome,
        }
        if outcome == "queued":
            facts.append(Fact("E16a", f"held_draft:{jid}", SRC_HELD, None, evidence, detail))
        elif outcome in ("voided", "posted"):
            facts.append(Fact("E16", f"held_draft:{jid}", SRC_HELD, outcome, evidence, detail))
        else:
            refuse("invalid_held_drafts_receipt")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("finding") != "E17":
            refuse("invalid_held_drafts_receipt")
        key = sha_text(finding.get("key_sha256"), "invalid_held_drafts_receipt")
        detail = {
            k: finding.get(k)
            for k in (
                "statement_lines",
                "posted_fee_journals",
                "excess_posted_fee_journals",
                "posted_journal_ids_sha256",
            )
        }
        facts.append(Fact("E17", f"e17:{key}", SRC_HELD, None, finding.get("posted_journal_ids_sha256"), detail))
    return facts


def from_import_itemised(body: Any) -> list[Fact]:
    if not isinstance(body, dict) or not isinstance(body.get("queue"), list):
        refuse("invalid_import_itemised")
    facts: list[Fact] = []
    for item in body["queue"]:
        if not isinstance(item, dict):
            refuse("invalid_import_itemised")
        reasons = item.get("reasons") or [item.get("reason")]
        if not isinstance(reasons, list) or not reasons:
            refuse("invalid_import_itemised")
        classes = []
        for reason in reasons:
            if isinstance(reason, str) and reason in IMPORT_CLASS:
                classes.append(IMPORT_CLASS[reason])
            elif isinstance(reason, str) and "deferral" in reason:
                classes.append(DEFERRAL_CLASS)
            else:
                refuse("unknown_queue_reason", "add the reason to the class table; never guess")
        invoice = text(item.get("invoice_id"), "invalid_import_itemised", 255)
        detail = {
            "reasons": sorted(set(map(str, reasons))),
            "lands": bool(item.get("lands")),
            "legacy_total": item.get("legacy_total"),
        }
        facts.append(Fact(sorted(classes)[0], f"invoice:{invoice}", SRC_IMPORT, None, None, detail))
    return facts


def from_admission_reviews(db: Session, tenant: UUID) -> list[Fact]:
    from erp_v2.importer.admission_waivers import REQUIRES_CORRECTING_JOURNAL
    from erp_v2.legacy_ap.models import AdmissionReview

    facts: list[Fact] = []
    for row in db.scalars(sa.select(AdmissionReview).where(AdmissionReview.tenant_id == tenant)):
        waived = sorted(set(row.waived_checks or []))
        detail = {"decision": row.decision, "waived": waived}
        key = f"invoice:{row.legacy_invoice_id}"
        if set(waived) & REQUIRES_CORRECTING_JOURNAL:
            facts.append(
                Fact(E08, key, SRC_REVIEWS, None, row.evidence_sha, {**detail, "requires_correcting_journal": True})
            )
        elif row.decision in REVIEW_OPEN_DECISIONS:
            facts.append(Fact(REVIEW_CLASS[row.decision], key, SRC_REVIEWS, None, row.evidence_sha, detail))
        else:
            facts.append(Fact("E02", key, SRC_REVIEWS, "admitted", row.evidence_sha, detail))
    return facts


# --- reconcile -------------------------------------------------------------------------------------------------


def reconcile(
    db: Session, tenant: UUID, facts: Iterable[Fact], *, loaded_sources: set[str], receipt_sha: str, apply: bool
) -> dict[str, Any]:
    """Merge facts into the register. Returns counts; writes only when ``apply``."""
    facts = list(facts)
    now = datetime.now(UTC)
    have = {
        (r.class_code, r.legacy_key): r
        for r in db.scalars(sa.select(m.MigrationException).where(m.MigrationException.tenant_id == tenant))
    }
    seen: set[tuple[str, str]] = set()
    counts: Counter[str] = Counter()
    for fact in facts:
        key = (fact.class_code, fact.legacy_key)
        if key in seen:
            refuse("duplicate_fact", "one receipt lists the same item twice")
        seen.add(key)
        row = have.get(key)
        resolved_fact = fact.terminal_outcome is not None
        if resolved_fact and not fact.evidence_sha256:
            refuse("resolved_fact_without_evidence")
        if row is None:
            counts["inserted_resolved" if resolved_fact else "inserted_open"] += 1
            if apply:
                db.add(
                    m.MigrationException(
                        tenant_id=tenant,
                        class_code=fact.class_code,
                        legacy_key=fact.legacy_key,
                        source_kind=fact.source_kind,
                        state=m.STATE_RESOLVED if resolved_fact else m.STATE_OPEN,
                        terminal_outcome=fact.terminal_outcome,
                        evidence_sha256=fact.evidence_sha256,
                        fingerprint=fact.fingerprint,
                        source_receipt_sha256=receipt_sha,
                        detail=fact.detail,
                        loaded_at=now,
                        resolved_at=now if resolved_fact else None,
                        updated_at=now,
                    )
                )
            continue
        if row.state == m.STATE_RESOLVED and row.fingerprint == fact.fingerprint:
            counts["carried_resolved"] += 1  # a fingerprinted decision carries over
            continue
        if row.state == m.STATE_RESOLVED:
            counts["reopened"] += 1
            if apply:
                row.reopened_count += 1
        elif resolved_fact:
            counts["resolved"] += 1
        else:
            counts["unchanged_open"] += 1
        if apply:
            row.fingerprint, row.detail, row.source_receipt_sha256, row.updated_at = (
                fact.fingerprint,
                fact.detail,
                receipt_sha,
                now,
            )
            row.source_kind = fact.source_kind
            if resolved_fact:
                row.state, row.terminal_outcome, row.evidence_sha256, row.resolved_at = (
                    m.STATE_RESOLVED,
                    fact.terminal_outcome,
                    fact.evidence_sha256,
                    now,
                )
            else:
                row.state, row.terminal_outcome, row.resolved_at = m.STATE_OPEN, None, None
    orphans = [
        k for k, r in have.items() if r.state == m.STATE_OPEN and r.source_kind in loaded_sources and k not in seen
    ]
    counts["orphan_open"] = len(orphans)
    if apply:
        db.flush()
    return dict(counts)


def resolve(
    db: Session, tenant: UUID, *, class_code: str, legacy_key: str, outcome: str, evidence_sha256: str
) -> m.MigrationException:
    """Finance's decision closes one item with a terminal outcome and an evidence sha."""
    if outcome not in m.TERMINAL_OUTCOMES:
        refuse("invalid_terminal_outcome")
    sha_text(evidence_sha256, "evidence_sha_required")
    row = db.scalar(
        sa.select(m.MigrationException).where(
            m.MigrationException.tenant_id == tenant,
            m.MigrationException.class_code == class_code,
            m.MigrationException.legacy_key == legacy_key,
        )
    )
    if row is None:
        refuse("exception_unknown")
    if row.state == m.STATE_RESOLVED:
        if row.terminal_outcome == outcome and row.evidence_sha256 == evidence_sha256:
            return row
        refuse("exception_already_resolved")
    row.state, row.terminal_outcome, row.evidence_sha256 = m.STATE_RESOLVED, outcome, evidence_sha256
    row.resolved_at = row.updated_at = datetime.now(UTC)
    db.flush()
    return row


def counts(db: Session, tenant: UUID) -> dict[str, Any]:
    rows = db.execute(
        sa.select(m.MigrationException.state, m.MigrationException.class_code, sa.func.count())
        .where(m.MigrationException.tenant_id == tenant)
        .group_by(m.MigrationException.state, m.MigrationException.class_code)
    )
    by: dict[str, dict[str, int]] = {m.STATE_OPEN: {}, m.STATE_RESOLVED: {}}
    for state, cls, n in rows:
        by[state][cls] = n
    return {
        "open": sum(by[m.STATE_OPEN].values()),
        "resolved": sum(by[m.STATE_RESOLVED].values()),
        "open_by_class": dict(sorted(by[m.STATE_OPEN].items())),
    }


def gate(db: Session, tenant: UUID) -> dict[str, Any]:
    """The flip gate: open count 0 AND the latest bridge version signed (its body re-hashed)."""
    c = counts(db, tenant)
    row = bridge.latest(db, tenant)
    signed = False
    version = sha = None
    if row is not None:
        version, sha = row.version, row.sha256
        try:
            bridge.verified_body(row)
            signed = row.signed_at is not None and row.ready
        except LedgerRefused:
            signed = False
    reasons = []
    if c["open"] != 0:
        reasons.append("open_exceptions")
    if row is None:
        reasons.append("no_bridge_version")
    elif not signed:
        reasons.append("latest_bridge_not_signed")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "open_count": c["open"],
        "resolved_count": c["resolved"],
        "open_by_class": c["open_by_class"],
        "latest_bridge_version": version,
        "latest_bridge_sha256": sha,
        "latest_bridge_signed": signed,
    }
