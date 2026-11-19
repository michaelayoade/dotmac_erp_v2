"""The exceptions register (EXCEPTIONS-ZERO-PLAN): loaded and reconciled from every receipt; the flip gate.

Each item is one (class, legacy key) with a state, a terminal outcome and an evidence sha, and a fingerprint of the
legacy facts it was loaded from. "Resolved" never means parked: posted, admitted, voided, written off (IAS 8 or a
Tier F journal) or confirmed no-action, each with evidence.

Sources (each adapter is a pure function from a receipt or rows to ``Fact``s):

* ``held_drafts``: the immutable ``erp_held_draft_decisions`` rows (no receipt adapter). A queued draft is open
  (E16a); a voided or posted draft is resolved; E17 findings derive from the ``duplicate_of_posted_fee_excess`` rows.
* ``import_itemised``: the importer transform itemised file (#68: ``queue`` list); every queued invoice is open.
* ``admission_reviews`` (database rows of B-1 decisions): a decision that waived a ``requires_correcting_journal``
  check (E08) or whose action is not executed here (void, supplier credit, reopen, ias8_post) is open; a plain
  admission is resolved ``admitted``.

Reconciliation (``reconcile``): a new fact inserts; an open item takes the fact's state; a resolved item stays
resolved while its fingerprint is unchanged (fingerprinted decisions carry over, D-M1) and REOPENS when the legacy
facts changed; an open item whose source was loaded but no longer lists it is reported as an orphan and stays open
(never silently dropped). Finance resolves the rest with ``resolve`` and an evidence sha.

Flip gate (profile ``as_is_v1``, AS-IS-MIGRATION-REPLAN section 4): the open work-queue count and the class coverage
are REPORTS, not gates (Michael's as-is ruling: Finance works the queue after the flip). It passes only when the
latest bridge version is signed (and was built under this profile), the recomputed ledger equals it and has not moved,
and every gated control tie holds. The profile is a code constant; no setting can relax it.
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
SRC_NA: Final = "not_applicable"
RECEIPT_KINDS: Final = (SRC_IMPORT,)  # B-2 outcomes load from the database, never from a receipt file

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


def from_held_decisions(db: Session, tenant: UUID) -> list[Fact]:
    """B-2 outcomes from the immutable ``erp_held_draft_decisions`` table (never from receipt files).

    A queued draft is open (E16a); a voided or posted draft is resolved (E16). E17 findings are derived from the
    decision rows whose reason is ``duplicate_of_posted_fee_excess``: one open finding per key.
    """
    from erp_v2.held_drafts.models import (
        OUTCOME_POSTED,
        OUTCOME_QUEUED,
        OUTCOME_VOIDED,
        VOID_DUPLICATE_OF_POSTED_FEE_EXCESS,
        HeldDraftDecision,
    )

    facts: list[Fact] = []
    e17: dict[str, dict[str, Any]] = {}
    for row in db.scalars(sa.select(HeldDraftDecision).where(HeldDraftDecision.tenant_id == tenant)):
        detail = {
            "rule_id": row.rule_id,
            "reason_code": row.reason_code,
            "amount": str(row.amount),
            "outcome": row.outcome,
        }
        key = f"held_draft:{row.journal_id}"
        if row.outcome == OUTCOME_QUEUED:
            facts.append(Fact("E16a", key, SRC_HELD, None, row.evidence_sha256, detail))
        elif row.outcome in (OUTCOME_VOIDED, OUTCOME_POSTED):
            facts.append(Fact("E16", key, SRC_HELD, row.outcome, row.evidence_sha256, detail))
        else:
            refuse("invalid_held_draft_decision")
        if row.reason_code == VOID_DUPLICATE_OF_POSTED_FEE_EXCESS:
            ev = row.evidence or {}
            k = str(ev.get("key_sha256", ""))
            entry = e17.setdefault(
                k,
                {
                    "statement_lines": ev.get("statement_lines"),
                    "posted_fee_journals": ev.get("posted_fee_journals"),
                    "excess_posted_fee_journals": ev.get("excess_posted_fee_journals"),
                    "posted_journal_ids_sha256": ev.get("posted_journal_ids_sha256"),
                    "drafts_voided": 0,
                },
            )
            entry["drafts_voided"] += 1
    for k, detail in sorted(e17.items()):
        facts.append(Fact("E17", f"e17:{k}", SRC_HELD, None, detail.get("posted_journal_ids_sha256"), detail))
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


def _class_row(db: Session, tenant: UUID, code: str, key: str):
    return db.scalar(
        sa.select(m.MigrationException).where(
            m.MigrationException.tenant_id == tenant,
            m.MigrationException.class_code == code,
            m.MigrationException.legacy_key == key,
        )
    )


def add_item(
    db: Session, tenant: UUID, *, class_code: str, legacy_key: str, evidence_sha256: str, operator: str
) -> m.MigrationException:
    """Governed manual add of a Finance item: the class is from the closed list; the item starts OPEN."""
    from erp_v2.migration_ledger.classes import CLASSES

    if class_code not in CLASSES:
        refuse("unknown_exception_class")
    key = text(legacy_key, "legacy_key_required", 255)
    if key.startswith("class:"):
        refuse("legacy_key_reserved")
    sha_text(evidence_sha256, "evidence_sha_required")
    existing = _class_row(db, tenant, class_code, key)
    if existing is not None:
        refuse("exception_exists")
    now = datetime.now(UTC)
    detail = {"added_by": text(operator, "operator_ref_required")}
    row = m.MigrationException(
        tenant_id=tenant,
        class_code=class_code,
        legacy_key=key,
        source_kind=SRC_MANUAL,
        state=m.STATE_OPEN,
        evidence_sha256=evidence_sha256,
        fingerprint=canonical_sha({"c": class_code, "k": key, "d": detail}),
        source_receipt_sha256=evidence_sha256,
        detail=detail,
        loaded_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def declare_na(
    db: Session, tenant: UUID, *, class_code: str, reason: str, evidence_sha256: str, operator: str
) -> m.MigrationException:
    """Declare an inventory class not applicable: a RESOLVED ``not_applicable`` row (``class:<code>``) with a
    reason, the
    declarer and the plan sha, in the same table under the same immutability triggers."""
    from erp_v2.migration_ledger.classes import CLASSES, NA_REASON_MAX, PLAN_SHA256

    if class_code not in CLASSES:
        refuse("unknown_exception_class")
    sha_text(evidence_sha256, "evidence_sha_required")
    why = text(reason, "reason_required", NA_REASON_MAX)
    key = f"class:{class_code}"
    existing = _class_row(db, tenant, class_code, key)
    if existing is not None:
        if existing.evidence_sha256 == evidence_sha256 and existing.detail.get("reason") == why:
            return existing
        refuse("not_applicable_already_declared")
    now = datetime.now(UTC)
    detail = {
        "reason": why,
        "declared_by": text(operator, "operator_ref_required"),
        "declared_at": now.isoformat(),
        "plan_sha256": PLAN_SHA256,
    }
    row = m.MigrationException(
        tenant_id=tenant,
        class_code=class_code,
        legacy_key=key,
        source_kind=SRC_NA,
        state=m.STATE_RESOLVED,
        terminal_outcome="not_applicable",
        evidence_sha256=evidence_sha256,
        fingerprint=canonical_sha({"c": class_code, "k": key, "d": {"reason": why}}),
        source_receipt_sha256=evidence_sha256,
        detail=detail,
        loaded_at=now,
        resolved_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def coverage(db: Session, tenant: UUID) -> dict[str, Any]:
    """Every class in the closed list needs at least one row (a fact, a manual item or a not-applicable declaration)."""
    from erp_v2.migration_ledger.classes import CLASSES

    have = set(
        db.scalars(
            sa.select(m.MigrationException.class_code).where(m.MigrationException.tenant_id == tenant).distinct()
        )
    )
    missing = [c for c in CLASSES if c not in have]
    na = sorted(
        db.scalars(
            sa.select(m.MigrationException.class_code).where(
                m.MigrationException.tenant_id == tenant, m.MigrationException.terminal_outcome == "not_applicable"
            )
        )
    )
    return {"missing": missing, "not_applicable": na, "complete": not missing}


def signature_summary(db: Session, tenant: UUID) -> dict[str, Any]:
    """Register counts by class and the not-applicable list, recorded at signature."""
    rows = db.execute(
        sa.select(m.MigrationException.class_code, m.MigrationException.state, sa.func.count())
        .where(m.MigrationException.tenant_id == tenant)
        .group_by(m.MigrationException.class_code, m.MigrationException.state)
    )
    by: dict[str, dict[str, int]] = {}
    for cls, state, n in rows:
        by.setdefault(cls, {})[state] = n
    cov = coverage(db, tenant)
    return {
        "by_class": dict(sorted(by.items())),
        "not_applicable": cov["not_applicable"],
        "missing_classes": cov["missing"],
    }


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


# Section-4 gate checks that need owners not on main yet. They are not silently skipped: the gate output lists them,
# and the control ties that depend on them (1400, 2045) already refuse by name until the owners are pinned.
PROFILE_CHECKS_PENDING: Final = (
    "completeness_per_module_currency",  # needs the per-module legacy = landed + held census (B-13/B-14/#72)
    "holding_ties",  # B-14 (e2_0030 holdings)
    "prepayment_tie_r1",  # billing a13 (R1)
    "setup_keyed_accounts_and_periods",  # B-10-lite keyed accounts
    "writer_role_m1",  # #72
)


def gate(db: Session, tenant: UUID) -> dict[str, Any]:
    """The flip gate, profile ``as_is_v1``: the latest bridge signed under this profile (body re-hashed), the
    recomputed v2 ledger equal to the bridge and unmoved since signature, and every gated control account tied (or
    named-unavailable, which refuses). The open count, class coverage, 2030, 2110 are reported only."""
    from erp_v2.migration_ledger import ledger_check

    c = counts(db, tenant)
    cov = coverage(db, tenant)
    row = bridge.latest(db, tenant)
    signed = False
    version = sha = None
    reasons: list[str] = []
    reports: dict[str, Any] = {}
    if row is None:
        reasons.append("no_bridge_version")
    else:
        version, sha = row.version, row.sha256
        try:
            body = bridge.verified_body(row)
            signed = row.signed_at is not None and row.ready
        except LedgerRefused:
            body, signed = None, False
        if not signed:
            reasons.append("latest_bridge_not_signed")
        else:
            if body.get("gate_profile") != ledger_check.GATE_PROFILE:
                reasons.append("gate_profile_changed_since_signature")
            if ledger_check.difference_rows(db, tenant, body):
                reasons.append("v2_ledger_differs_from_bridge")
            state = ledger_check.ledger_state(db, tenant)
            was = row.signed_state or {}
            if state["v2_posted_sha256"] != was.get("v2_posted_sha256") or state["controls"] != was.get("controls"):
                reasons.append("ledger_moved_since_signature")
            if body.get("control_lists") != state["control_lists"]:
                reasons.append("control_lists_changed_since_signature")
            reasons.extend(v["reason"] for v in state["controls"].values() if not v["ok"])
            reports = state["reports"]
    return {
        "passed": not reasons,
        "profile": ledger_check.GATE_PROFILE,
        "reasons": reasons,
        "profile_checks_pending": list(PROFILE_CHECKS_PENDING),
        "reports": {
            "open_count": c["open"],
            "resolved_count": c["resolved"],
            "open_by_class": c["open_by_class"],
            "classes_missing": cov["missing"],
            "report_balances": reports,
        },
        "open_count": c["open"],
        "resolved_count": c["resolved"],
        "open_by_class": c["open_by_class"],
        "classes_missing": cov["missing"],
        "latest_bridge_version": version,
        "latest_bridge_sha256": sha,
        "latest_bridge_signed": signed,
    }
