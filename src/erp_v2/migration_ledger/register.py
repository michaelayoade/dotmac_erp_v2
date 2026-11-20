"""The exceptions register (EXCEPTIONS-ZERO-PLAN): loaded and reconciled from every receipt; the flip gate.

Each item is one (class, legacy key) with a state, a terminal outcome and an evidence sha, and a fingerprint of the
legacy facts it was loaded from. "Resolved" never means parked: posted, admitted, voided, written off (IAS 8 or a
Tier F journal) or confirmed no-action, each with evidence.

Sources (each adapter is a pure function from a receipt or rows to ``Fact``s):

* ``held_drafts``: the immutable ``erp_held_draft_decisions`` rows (no receipt adapter). A queued draft is open
  (E16a); a voided or posted draft is resolved; E17 findings derive from the ``duplicate_of_posted_fee_excess`` rows.
* ``import_itemised``: the importer transform itemised file (#68: ``queue`` list); every queued invoice is open.
* ``ar_census``: the AR opening census receipt (``importer/ar_census``; the AS-IS replan section 2 item 5), by sha:
  E35 ``partition:<cust>:<ccy>`` (a partition billing cannot carry; its GL goes to the AR holding account),
  E35 ``doc:<id>`` (the DRAFT / SUBMITTED AR invoices: an H-DOC work item, no GL), E35
  ``ar_unapplied_credit_note:<id>`` (a credit note kept out of the billing manifest: party, document number,
  amount; Finance applies or issues it after the flip), E36a ``custgap:<cust>:<ccy>`` / ``gljournal:<id>`` (GL that
  has no billing counterpart, from the receipt's ``gl_attribution`` when the destination chart was supplied) and E36b
  ``variance:<source key>`` (a stored-balance difference billing records at the seal; memo basis).
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
SRC_AR_CENSUS: Final = "ar_census"
SRC_MANUAL: Final = "manual"
SRC_NA: Final = "not_applicable"
RECEIPT_KINDS: Final = (SRC_IMPORT, SRC_AR_CENSUS)  # B-2 outcomes load from the database, never from a receipt file

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


AR_CLASS_PARTITION: Final = "E35"
AR_CLASS_GL_GAP: Final = "E36a"
AR_CLASS_VARIANCE: Final = "E36b"


def from_ar_census(body: Any) -> list[Fact]:
    """The AR opening census receipt (counts, keys, amounts: no names) as open work items. Refuses anything else."""
    if not isinstance(body, dict) or body.get("kind") != "ar-census" or not isinstance(body.get("census_sha256"), str):
        refuse("invalid_ar_census")
    census_sha = body["census_sha256"]
    for name in (
        "excluded_partitions",
        "unapplied_credit_notes",
        "unposted_documents",
        "posted_after_cutoff",
        "variances",
    ):
        if not isinstance(body.get(name, []), list):
            refuse("invalid_ar_census")
    attribution = body.get("gl_attribution")
    if not isinstance(attribution, dict):  # without the GL attribution the E36a items cannot be derived: refuse
        refuse("ar_census_without_gl_attribution", "run the census with the destination chart so 1400 is attributed")
    from erp_v2.importer.ar_attribution import attribution_sha256

    if body.get("gl_attribution_sha256") != attribution_sha256(attribution):  # the split was edited after the census
        refuse("gl_attribution_sha_mismatch")
    facts: list[Fact] = []

    def add(class_code: str, key: str, detail: dict[str, Any]) -> None:
        facts.append(Fact(class_code, key, SRC_AR_CENSUS, None, census_sha, detail))

    signed = {str(h["legacy_key"]): int(h["amount_minor"]) for h in attribution.get("held", []) if isinstance(h, dict)}

    for item in body.get("excluded_partitions", []):
        cust, ccy = (str(x) for x in item["partition"])
        add(
            AR_CLASS_PARTITION,
            f"partition:{cust}:{ccy}",
            {
                "holding": "H-SUB",
                "blockers": sorted({str(b[1]) for b in item["blockers"]}),
                "open_residual_minor": int(item["open_residual_minor"]),
                "currency": ccy,
            },
        )
    for item in body.get("unapplied_credit_notes", []):
        add(
            AR_CLASS_PARTITION,
            f"ar_unapplied_credit_note:{text(item['key'], 'invalid_ar_census', 255)}",
            {
                "work_item": "ar_unapplied_credit_note",
                "holding": "H-SUB",
                "party": list(item["partition"]) if item.get("partition") else None,
                "document_number": item.get("document_number"),
                "amount_minor": item.get("gross_minor"),
                "gl_signed_minor": signed.get(str(item["key"])),  # the amount B-14 itemises on 1400
                "currency": item["partition"][1] if item.get("partition") else None,
                "residual_minor": item.get("residual_minor"),
                "reason": item.get("reason"),
            },
        )
    for item in body.get("unposted_documents", []):
        add(
            AR_CLASS_PARTITION,
            f"doc:{text(item['key'], 'invalid_ar_census', 255)}",
            {"holding": "H-DOC", "status": item.get("status"), "amount_minor": item.get("gross_minor")},
        )
    for item in body.get("posted_after_cutoff", []):  # posted by D but dated after it: GL is in the ledger, held
        key = text(item["key"], "invalid_ar_census", 255)
        add(
            AR_CLASS_PARTITION,
            f"row:{key}",
            {
                "holding": "H-SUB",
                "reason": "row_after_cutoff",
                "kind": item.get("kind"),
                "document_number": item.get("document_number"),
                "amount_minor": item.get("gross_minor"),
                "gl_signed_minor": signed.get(key),
                "currency": item["partition"][1] if item.get("partition") else None,
            },
        )
    for item in attribution.get("parties", []):
        if int(item["custgap_minor"]) != 0:  # GL not explained by the carried or refused items of this party
            cust, ccy = (
                text(item["customer"], "invalid_ar_census", 255),
                text(item["currency"], "invalid_ar_census", 3),
            )
            add(
                AR_CLASS_GL_GAP,
                f"custgap:{cust}:{ccy}",
                {"amount_minor": int(item["custgap_minor"]), "holding": "H-SUB", "currency": ccy},
            )
    for item in attribution.get("unattributed", []):  # includes shared journals (flagged), never split
        journal = text(item["gljournal_id"], "invalid_ar_census", 255)
        add(
            AR_CLASS_GL_GAP,
            f"gljournal:{journal}",
            {
                "amount_minor": int(item["amount_minor"]),
                "holding": "H-SUB",
                "shared": bool(item.get("shared")),
                "currency": item.get("currency"),
            },
        )
    for item in body.get("variances", []):
        add(
            AR_CLASS_VARIANCE,
            f"variance:{text(item['source_key'], 'invalid_ar_census', 255)}",
            {
                "partition": list(item["partition"]),
                "reason": item.get("reason"),
                "amount_minor": int(item["amount_minor"]),
                "basis": "memo",
            },
        )
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


def gate(db: Session, tenant: UUID) -> dict[str, Any]:
    """The flip gate: open count 0, every class covered, the latest bridge signed (body re-hashed), the recomputed v2
    ledger equal to the bridge and unmoved since signature, and every control account tied (or named-unavailable,
    which refuses)."""
    from erp_v2.migration_ledger import ledger_check

    c = counts(db, tenant)
    cov = coverage(db, tenant)
    row = bridge.latest(db, tenant)
    signed = False
    version = sha = None
    reasons: list[str] = []
    if c["open"] != 0:
        reasons.append("open_exceptions")
    if not cov["complete"]:
        reasons.append("class_coverage_incomplete")
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
            if ledger_check.difference_rows(db, tenant, body):
                reasons.append("v2_ledger_differs_from_bridge")
            state = ledger_check.ledger_state(db, tenant)
            was = row.signed_state or {}
            if state["v2_posted_sha256"] != was.get("v2_posted_sha256") or state["controls"] != was.get("controls"):
                reasons.append("ledger_moved_since_signature")
            if body.get("control_lists") != state["control_lists"]:
                reasons.append("control_lists_changed_since_signature")
            reasons.extend(v["reason"] for v in state["controls"].values() if not v["ok"])
    return {
        "passed": not reasons,
        "reasons": reasons,
        "open_count": c["open"],
        "resolved_count": c["resolved"],
        "open_by_class": c["open_by_class"],
        "classes_missing": cov["missing"],
        "latest_bridge_version": version,
        "latest_bridge_sha256": sha,
        "latest_bridge_signed": signed,
    }
