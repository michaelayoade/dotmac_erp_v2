"""The published held-draft rules, as pure functions over plain data (no database).

Michael's rulings (v1537, v1606): a rule-matched void counts as "reviewed against source". Three rules may void;
every other draft goes to the Finance queue, to be decided one by one.

* ``R-BANK-FEE`` (legacy BANKING / BANK_FEE): the draft has exactly one line on a bank cash GL. Group by the key
  (bank cash GL, date, amount). With S statement lines, P POSTED fee journals (including any this tool posted) and
  D drafts on the key: S = 0 queues (``no_statement_evidence``); P > S voids every draft
  ``duplicate_of_posted_fee_excess`` and raises an E17 finding (P - S possibly over-posted fee journals) for Finance;
  otherwise ``postable = min(D, max(0, S - P))``: the first ``postable`` drafts (by number, id) go to the B2
  posting path and the rest are voided ``duplicate_of_posted_fee``. A key on a shared or re-pointed cash GL queues
  (``bank_gl_unresolved``), the same refusal as ``recon_service.resolve_gl_account``.
* ``R-AR-TWIN`` (legacy AR / INVOICE or CUSTOMER_PAYMENT): a POSTED twin exists for the same document. Equal
  per-account amounts void it ``already_posted_duplicate``; different amounts queue ``twin_amount_differs``.
* ``R-AR-UNAPPROVED-REVERSAL``: the draft is the EXACT sign mirror of a POSTED twin (same accounts, debit and
  credit swapped) and the legacy document is still live (PAID, POSTED or PARTIALLY_PAID): void
  ``unapproved_reversal_of_live_document``. A mirror of a document that is not live queues
  (``reversal_document_not_live``); anything not an exact mirror queues.
* ``R-QUEUE``: anything else queues.

``RULES_SHA256`` binds the exact rule text and thresholds; a plan is only applied by code with the same sha.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final
from uuid import UUID

from erp_v2.held_drafts.models import (
    OUTCOME_POSTED,
    OUTCOME_QUEUED,
    OUTCOME_VOIDED,
    VOID_ALREADY_POSTED_DUPLICATE,
    VOID_DUPLICATE_OF_POSTED_FEE,
    VOID_DUPLICATE_OF_POSTED_FEE_EXCESS,
    VOID_UNAPPROVED_REVERSAL,
)

RULE_BANK_FEE: Final = "R-BANK-FEE"
RULE_AR_TWIN: Final = "R-AR-TWIN"
RULE_AR_REVERSAL: Final = "R-AR-UNAPPROVED-REVERSAL"
RULE_QUEUE: Final = "R-QUEUE"

SOURCE_BANKING: Final = "BANKING"
SOURCE_AR: Final = "AR"
TYPE_BANK_FEE: Final = "BANK_FEE"
AR_TYPES: Final = ("INVOICE", "CUSTOMER_PAYMENT")

# Reasons a draft is queued (stable codes; receipts and the table carry them).
Q_NO_RULE: Final = "no_published_rule"
Q_NO_LEGACY: Final = "no_legacy_record"
Q_MISMATCH: Final = "legacy_record_mismatch"
Q_WORKFLOW: Final = "workflow_in_progress"
Q_NOT_SINGLE_BANK_LINE: Final = "not_single_bank_line"
Q_NO_STATEMENT: Final = "no_statement_evidence"
Q_BANK_GL_UNRESOLVED: Final = "bank_gl_unresolved"
Q_REVERSAL_NOT_LIVE: Final = "reversal_document_not_live"
LIVE_DOCUMENT_STATUSES: Final = ("PAID", "POSTED", "PARTIALLY_PAID")
HELD_LEGACY_STATUSES: Final = ("DRAFT", "SUBMITTED", "APPROVED")
Q_NO_DOCUMENT: Final = "no_document_reference"
Q_NO_TWIN: Final = "twin_missing"
Q_TWIN_DIFFERS: Final = "twin_amount_differs"
Q_POST_BLOCKED: Final = "post_blocked"
POST_REASON: Final = "statement_evidence_postable"

RULES_DEFINITION: Final = {
    "version": 1,
    "rules": {
        RULE_BANK_FEE: {
            "match": "legacy BANKING/BANK_FEE; exactly one line on a bank cash GL",
            "key": ["bank_cash_gl", "posting_date", "signed_functional_amount"],
            "postable": "min(drafts, max(0, S - P))",
            "S": "bank statement lines on the key (all bank accounts on that cash GL)",
            "P": "POSTED BANK_FEE journals on the key (excluding journals posted by this tool)",
            "order": ["number", "id"],
            "void": VOID_DUPLICATE_OF_POSTED_FEE,
            "excess": "P > S (S >= 1): void every draft as duplicate_of_posted_fee_excess + E17 finding",
            "void_excess": VOID_DUPLICATE_OF_POSTED_FEE_EXCESS,
            "queue": [Q_NO_STATEMENT, Q_NOT_SINGLE_BANK_LINE, Q_BANK_GL_UNRESOLVED],
        },
        RULE_AR_TWIN: {
            "match": "legacy AR/INVOICE or AR/CUSTOMER_PAYMENT with a document id",
            "twin": "POSTED journal with the same legacy (module, type, document id), landed POSTED in v2",
            "equal": "per-account (debit_functional, credit_functional) totals",
            "void": VOID_ALREADY_POSTED_DUPLICATE,
            "queue": [Q_NO_DOCUMENT, Q_NO_TWIN, Q_TWIN_DIFFERS],
        },
        RULE_AR_REVERSAL: {
            "match": "AR draft that is the exact sign mirror of a POSTED twin; legacy document status live",
            "mirror": "same accounts, debit and credit swapped, per-account functional totals",
            "live": list(LIVE_DOCUMENT_STATUSES),
            "void": VOID_UNAPPROVED_REVERSAL,
            "queue": [Q_REVERSAL_NOT_LIVE],
        },
        RULE_QUEUE: {"match": "every other draft", "queue": [Q_NO_RULE, Q_NO_LEGACY, Q_MISMATCH, Q_WORKFLOW]},
    },
    "held_population": {"legacy_status": list(HELD_LEGACY_STATUSES)},
    "post": "B2 legacy-held path in the original period; refusal (closed period, control account) queues",
}
RULES_SHA256: Final = hashlib.sha256(
    json.dumps(RULES_DEFINITION, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def quantize(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.000001"))


def sha256_json(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


# --- inputs -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Draft:
    journal_id: UUID
    number: str
    posting_date: date
    amount: Decimal  # total debit, functional
    kind: str | None  # destination source_document_kind (the legacy document type)
    # per-account (debit_functional, credit_functional) totals
    lines: dict[UUID, tuple[Decimal, Decimal]] = field(default_factory=dict)
    has_workflow: bool = False


@dataclass(frozen=True, slots=True)
class LegacyRecord:
    module: str | None
    doc_type: str | None
    doc_id: str | None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class Twin:
    journal_id: UUID
    lines: dict[UUID, tuple[Decimal, Decimal]]


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the rules read, gathered once. ``legacy`` is keyed by draft journal id."""

    legacy: dict[UUID, LegacyRecord]
    bank_gl_ids: frozenset[UUID]
    # (gl account id, date, signed amount) -> statement line ids / posted fee journal ids
    statement: dict[tuple[UUID, date, Decimal], list[UUID]]
    posted_fees: dict[tuple[UUID, date, Decimal], list[UUID]]
    # (module, type, doc id) -> POSTED twins in v2, ordered by journal id
    twins: dict[tuple[str, str, str], list[Twin]]
    # cash GL accounts of bank accounts the resolver refuses (shared or re-pointed): keys on them queue
    unsafe_gl_ids: frozenset[UUID] = frozenset()
    # (module, type, doc id) -> legacy document status (None when the document is not found)
    document_status: dict[tuple[str, str, str], str | None] = field(default_factory=dict)
    # a post pre-check supplied by the caller (period open, control accounts): reason code or None
    post_blockers: dict[UUID, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Decision:
    journal_id: UUID
    rule_id: str
    outcome: str
    reason_code: str
    amount: Decimal
    evidence: dict[str, object]

    @property
    def evidence_sha256(self) -> str:
        return sha256_json(self.evidence)

    def line(self) -> dict[str, object]:
        """The deterministic plan line (what the plan sha binds)."""
        return {
            "journal_id": str(self.journal_id),
            "rule_id": self.rule_id,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "amount": str(quantize(self.amount)),
            "evidence_sha256": self.evidence_sha256,
        }


def _queue(draft: Draft, rule: str, reason: str, **evidence: object) -> Decision:
    return Decision(draft.journal_id, rule, OUTCOME_QUEUED, reason, draft.amount, dict(evidence))


def ids_sha256(ids) -> str:
    """Count-independent digest of a set of ids (sorted), so evidence need not store every id."""
    return sha256_json(sorted(str(i) for i in ids))


def _bank_key(draft: Draft, bank_gl_ids: frozenset[UUID]) -> tuple[UUID, date, Decimal] | None:
    bank_lines = [(account, d, c) for account, (d, c) in draft.lines.items() if account in bank_gl_ids]
    if len(bank_lines) != 1:
        return None
    account, debit, credit = bank_lines[0]
    net = quantize(debit - credit)
    if net == 0:
        return None
    return account, draft.posting_date, net


def amounts_equal(a: dict[UUID, tuple[Decimal, Decimal]], b: dict[UUID, tuple[Decimal, Decimal]]) -> bool:
    def norm(lines):
        return {k: (quantize(d), quantize(c)) for k, (d, c) in lines.items() if quantize(d) or quantize(c)}

    return norm(a) == norm(b)


def is_mirror(draft: dict[UUID, tuple[Decimal, Decimal]], twin: dict[UUID, tuple[Decimal, Decimal]]) -> bool:
    """The draft is the exact sign mirror of the twin: same accounts, debit and credit swapped, non-empty."""

    def norm(lines):
        return {k: (quantize(d), quantize(c)) for k, (d, c) in lines.items() if quantize(d) or quantize(c)}

    a, b = norm(draft), norm(twin)
    return bool(a) and a == {k: (c, d) for k, (d, c) in b.items()} and a != b


def decide(drafts: list[Draft], evidence: Evidence) -> list[Decision]:
    """One decision per draft (exactly one bridge line each). Deterministic for the same inputs."""
    decisions: dict[UUID, Decision] = {}
    bank_groups: dict[tuple[UUID, date, Decimal], list[Draft]] = defaultdict(list)
    for draft in sorted(drafts, key=lambda d: (d.number, str(d.journal_id))):
        legacy = evidence.legacy.get(draft.journal_id)
        if draft.has_workflow:
            decisions[draft.journal_id] = _queue(draft, RULE_QUEUE, Q_WORKFLOW)
            continue
        if legacy is None:
            decisions[draft.journal_id] = _queue(draft, RULE_QUEUE, Q_NO_LEGACY)
            continue
        if legacy.doc_type is not None and draft.kind is not None and legacy.doc_type != draft.kind:
            decisions[draft.journal_id] = _queue(
                draft, RULE_QUEUE, Q_MISMATCH, legacy_type=legacy.doc_type, v2_kind=draft.kind
            )
            continue
        if legacy.module == SOURCE_BANKING and legacy.doc_type == TYPE_BANK_FEE:
            key = _bank_key(draft, evidence.bank_gl_ids | evidence.unsafe_gl_ids)
            if key is None:
                decisions[draft.journal_id] = _queue(draft, RULE_BANK_FEE, Q_NOT_SINGLE_BANK_LINE)
            elif key[0] in evidence.unsafe_gl_ids:
                decisions[draft.journal_id] = _queue(draft, RULE_BANK_FEE, Q_BANK_GL_UNRESOLVED)
            else:
                bank_groups[key].append(draft)
            continue
        if legacy.module == SOURCE_AR and legacy.doc_type in AR_TYPES:
            if not legacy.doc_id or not legacy.doc_id.strip():
                decisions[draft.journal_id] = _queue(draft, RULE_AR_TWIN, Q_NO_DOCUMENT, doc_type=legacy.doc_type)
                continue
            doc_key = (legacy.module, legacy.doc_type, legacy.doc_id)
            twins = [t for t in evidence.twins.get(doc_key, []) if t.journal_id != draft.journal_id]
            base = {"doc_type": legacy.doc_type, "document_sha256": sha256_json(list(doc_key))}
            if not twins:
                decisions[draft.journal_id] = _queue(draft, RULE_AR_TWIN, Q_NO_TWIN, **base)
                continue
            equal = sorted((t for t in twins if amounts_equal(draft.lines, t.lines)), key=lambda t: str(t.journal_id))
            if equal:
                decisions[draft.journal_id] = Decision(
                    draft.journal_id,
                    RULE_AR_TWIN,
                    OUTCOME_VOIDED,
                    VOID_ALREADY_POSTED_DUPLICATE,
                    draft.amount,
                    {"twin_journal_id": str(equal[0].journal_id), "twins_posted": len(twins), **base},
                )
                continue
            mirrors = sorted((t for t in twins if is_mirror(draft.lines, t.lines)), key=lambda t: str(t.journal_id))
            if not mirrors:
                decisions[draft.journal_id] = _queue(
                    draft,
                    RULE_AR_TWIN,
                    Q_TWIN_DIFFERS,
                    twins_posted=len(twins),
                    twin_journal_ids_sha256=ids_sha256([t.journal_id for t in twins]),
                    **base,
                )
                continue
            status = evidence.document_status.get(doc_key)
            mirror = mirrors[0]
            proof = {
                "accounts": len(mirror.lines),
                "debit_total": str(quantize(sum((d for d, _c in draft.lines.values()), Decimal(0)))),
                "credit_total": str(quantize(sum((c for _d, c in draft.lines.values()), Decimal(0)))),
                "mirror_sha256": sha256_json(
                    sorted([str(k), str(quantize(d)), str(quantize(c))] for k, (d, c) in draft.lines.items())
                ),
            }
            reversal = {
                "twin_journal_id": str(mirror.journal_id),
                "document_id": legacy.doc_id,
                "document_status": status,
                "mirror_proof": proof,
                **base,
            }
            if status not in LIVE_DOCUMENT_STATUSES:
                decisions[draft.journal_id] = _queue(draft, RULE_AR_REVERSAL, Q_REVERSAL_NOT_LIVE, **reversal)
                continue
            decisions[draft.journal_id] = Decision(
                draft.journal_id, RULE_AR_REVERSAL, OUTCOME_VOIDED, VOID_UNAPPROVED_REVERSAL, draft.amount, reversal
            )
            continue
        decisions[draft.journal_id] = _queue(draft, RULE_QUEUE, Q_NO_RULE, legacy_type=legacy.doc_type or "")

    for key, group in sorted(bank_groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2])):
        statement_ids = evidence.statement.get(key, [])
        posted_ids = evidence.posted_fees.get(key, [])
        s, p, d = len(statement_ids), len(posted_ids), len(group)
        key_sha = sha256_json([str(key[0]), key[1].isoformat(), str(key[2])])
        base: dict[str, object] = {
            "key_sha256": key_sha,
            "statement_lines": s,
            "posted_fee_journals": p,
            "drafts_on_key": d,
        }
        if s == 0:
            for draft in group:
                decisions[draft.journal_id] = _queue(draft, RULE_BANK_FEE, Q_NO_STATEMENT, **base)
            continue
        base_ev = {
            **base,
            "statement_line_ids_sha256": ids_sha256(statement_ids),
            "posted_journal_ids_sha256": ids_sha256(posted_ids),
        }
        if p > s:
            # E17: P - S posted fee journals have no statement line (possible over-posting); Finance corrects them.
            for draft in group:
                decisions[draft.journal_id] = Decision(
                    draft.journal_id,
                    RULE_BANK_FEE,
                    OUTCOME_VOIDED,
                    VOID_DUPLICATE_OF_POSTED_FEE_EXCESS,
                    draft.amount,
                    {**base_ev, "excess_posted_fee_journals": p - s},
                )
            continue
        postable = min(d, max(0, s - p))
        for index, draft in enumerate(group):  # group is already ordered by (number, id)
            if index < postable:
                blocker = evidence.post_blockers.get(draft.journal_id)
                if blocker:
                    decisions[draft.journal_id] = _queue(
                        draft, RULE_BANK_FEE, Q_POST_BLOCKED, blocker=blocker, **base_ev
                    )
                else:
                    decisions[draft.journal_id] = Decision(
                        draft.journal_id, RULE_BANK_FEE, OUTCOME_POSTED, POST_REASON, draft.amount, dict(base_ev)
                    )
            else:
                decisions[draft.journal_id] = Decision(
                    draft.journal_id,
                    RULE_BANK_FEE,
                    OUTCOME_VOIDED,
                    VOID_DUPLICATE_OF_POSTED_FEE,
                    draft.amount,
                    dict(base_ev),
                )
    # exactly one decision per draft, in a stable order
    if set(decisions) != {d.journal_id for d in drafts}:
        raise RuntimeError("held-draft rules produced a different set of decisions than drafts")
    return [decisions[d.journal_id] for d in sorted(drafts, key=lambda d: (d.number, str(d.journal_id)))]


def e17_findings(decisions: list[Decision]) -> list[dict[str, object]]:
    """One finding per key where POSTED fee journals exceed statement lines (derived from the decisions' evidence).

    ``excess_posted_fee_journals`` is P - S; ``posted_journal_ids_sha256`` digests all P posted journals on the key
    (the excess ones cannot be told apart from the matched ones, so Finance reviews the set).
    """
    found: dict[str, dict[str, object]] = {}
    for d in decisions:
        if d.reason_code != VOID_DUPLICATE_OF_POSTED_FEE_EXCESS:
            continue
        key = str(d.evidence["key_sha256"])
        entry = found.setdefault(
            key,
            {
                "finding": "E17",
                "key_sha256": key,
                "statement_lines": d.evidence["statement_lines"],
                "posted_fee_journals": d.evidence["posted_fee_journals"],
                "excess_posted_fee_journals": d.evidence["excess_posted_fee_journals"],
                "posted_journal_ids_sha256": d.evidence["posted_journal_ids_sha256"],
                "drafts_voided": 0,
            },
        )
        entry["drafts_voided"] = int(entry["drafts_voided"]) + 1  # type: ignore[call-overload]
    return [found[k] for k in sorted(found)]


def plan_sha256(decisions: list[Decision], *, rules_sha256: str = RULES_SHA256) -> str:
    lines = sorted((d.line() for d in decisions), key=lambda line: str(line["journal_id"]))
    return sha256_json({"rules_sha256": rules_sha256, "lines": lines})
