"""Plan and apply for the FY2026 legacy held DRAFT journals (B-2).

Reads the destination (``mod_accounting`` journals and lines, ``mod_banking`` statements) and a read-only legacy
snapshot (``gl.journal_entry``: the document identity the destination does not keep). Everything the rules decide
is computed by ``rules.decide`` from plain data. Apply recomputes the plan inside the run and refuses when its sha
differs from the approved one, then performs each decision through an existing governed path:

* post: ``journals.service.request_legacy_post`` and ``approve_and_post`` (original period; control-account and
  open-period guards intact; a refusal queues the draft with the refusal code);
* void: accounting a2 ``void_draft_journal`` (requester and approver are distinct identities by prefix);
* queue: no journal change; the draft stays DRAFT for Finance.

Each draft gets exactly one immutable ``erp_held_draft_decisions`` row. Caller owns connections; this module
commits in batches (apply is resumable: decided drafts are never touched again).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.contracts import AccountingError
from sqlalchemy.orm import Session

from erp_v2.held_drafts import rules
from erp_v2.held_drafts.models import (
    OUTCOME_POSTED,
    OUTCOME_QUEUED,
    OUTCOME_VOIDED,
    OUTCOMES,
    HeldDraftDecision,
)
from erp_v2.held_drafts.rules import Decision, Draft, Evidence, LegacyRecord, Twin
from erp_v2.journals.service import JournalRefused

LEGACY_OWNER = "legacy-erp"
BATCH = 500
CHUNK = 5000
REQUESTER_PREFIX = "held-drafts-requester:"
APPROVER_PREFIX = "held-drafts-approver:"


class HeldDraftsRefused(ValueError):
    """Sanitized refusal; ``code`` only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _refuse(code: str):
    raise HeldDraftsRefused(code)


@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: UUID
    fy_start: date
    fy_end: date
    filed_vat_months: frozenset[str] = frozenset()
    # Independent legacy-side count of held drafts (14,271 on g9) the partition must equal.
    expected_held_total: int | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    scope_sha256: str
    decisions: tuple[Decision, ...]  # every draft, planned or already decided
    pending: frozenset[UUID]  # journal ids not yet decided in the table
    population_ids: frozenset[UUID]
    population_amount: Decimal
    out_of_scope_before_fy: int
    excluded_legacy_status: int = 0

    @property
    def sha256(self) -> str:
        return rules.plan_sha256(list(self.decisions))


def _chunks(items: list, size: int = CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _in(sql: str) -> sa.TextClause:
    return sa.text(sql).bindparams(sa.bindparam("ids", expanding=True))


# --- reads --------------------------------------------------------------------------------------------------


def _drafts(db: Session, scope: Scope) -> list[Draft]:
    rows = db.execute(
        sa.text(
            "SELECT je.id, je.number, COALESCE(je.posting_date, je.entry_date) AS day, "
            "je.total_debit_functional AS amount, "
            "je.source_document_kind AS kind, "
            "EXISTS (SELECT 1 FROM public.erp_manual_journals mj WHERE mj.tenant_id = je.tenant_id "
            "AND mj.journal_id = je.id) AS has_workflow "
            "FROM mod_accounting.journal_entries je "
            "WHERE je.tenant_id = :t AND je.status = 'DRAFT' AND je.source_owner = :owner "
            "AND COALESCE(je.posting_date, je.entry_date) BETWEEN :a AND :b "
            "AND NOT EXISTS (SELECT 1 FROM public.erp_held_draft_decisions d WHERE d.tenant_id = je.tenant_id "
            "AND d.journal_id = je.id)"
        ),
        {"t": scope.tenant_id, "owner": LEGACY_OWNER, "a": scope.fy_start, "b": scope.fy_end},
    ).all()
    lines: dict[UUID, dict[UUID, tuple[Decimal, Decimal]]] = defaultdict(dict)
    for row in db.execute(
        sa.text(
            "SELECT jl.journal_id, jl.account_id, sum(jl.debit_functional) AS d, sum(jl.credit_functional) AS c "
            "FROM mod_accounting.journal_lines jl JOIN mod_accounting.journal_entries je "
            "ON je.tenant_id = jl.tenant_id AND je.id = jl.journal_id "
            "WHERE je.tenant_id = :t AND je.status = 'DRAFT' AND je.source_owner = :owner "
            "AND COALESCE(je.posting_date, je.entry_date) BETWEEN :a AND :b "
            "AND NOT EXISTS (SELECT 1 FROM public.erp_held_draft_decisions d WHERE d.tenant_id = je.tenant_id "
            "AND d.journal_id = je.id) GROUP BY jl.journal_id, jl.account_id"
        ),
        {"t": scope.tenant_id, "owner": LEGACY_OWNER, "a": scope.fy_start, "b": scope.fy_end},
    ):
        lines[row.journal_id][row.account_id] = (Decimal(row.d), Decimal(row.c))
    return [
        Draft(r.id, r.number, r.day, Decimal(r.amount), r.kind, lines.get(r.id, {}), bool(r.has_workflow)) for r in rows
    ]


def _legacy_records(legacy: sa.Connection, ids: list[UUID]) -> dict[UUID, LegacyRecord]:
    out: dict[UUID, LegacyRecord] = {}
    for chunk in _chunks(ids):
        for row in legacy.execute(
            _in(
                "SELECT journal_entry_id AS id, source_module AS module, source_document_type AS doc_type, "
                "CAST(source_document_id AS TEXT) AS doc_id, status FROM gl.journal_entry "
                "WHERE journal_entry_id IN :ids"
            ),
            {"ids": chunk},
        ):
            out[UUID(str(row.id))] = LegacyRecord(row.module, row.doc_type, row.doc_id, row.status)
    return out


def _twins(
    db: Session, legacy: sa.Connection, tenant_id: UUID, documents: set[tuple[str, str, str]]
) -> dict[tuple[str, str, str], list[Twin]]:
    by_doc: dict[tuple[str, str, str], list[UUID]] = defaultdict(list)
    doc_ids = sorted({doc_id for _m, _t, doc_id in documents})
    for chunk in _chunks(doc_ids):
        for row in legacy.execute(
            _in(
                "SELECT journal_entry_id AS id, source_module AS module, source_document_type AS doc_type, "
                "CAST(source_document_id AS TEXT) AS doc_id FROM gl.journal_entry "
                "WHERE status = 'POSTED' AND source_module = 'AR' "
                "AND source_document_type IN ('INVOICE', 'CUSTOMER_PAYMENT') "
                "AND CAST(source_document_id AS TEXT) IN :ids"
            ),
            {"ids": chunk},
        ):
            key = (row.module, row.doc_type, row.doc_id)
            if key in documents:
                by_doc[key].append(UUID(str(row.id)))
    candidates = sorted({j for ids in by_doc.values() for j in ids}, key=str)
    posted: dict[UUID, dict[UUID, tuple[Decimal, Decimal]]] = {}
    for chunk in _chunks(candidates):
        for row in db.execute(
            _in(
                "SELECT id FROM mod_accounting.journal_entries "
                "WHERE tenant_id = :t AND status = 'POSTED' AND id IN :ids"
            ).bindparams(t=tenant_id),
            {"ids": chunk},
        ):
            posted[row.id] = {}
        for row in db.execute(
            _in(
                "SELECT journal_id, account_id, sum(debit_functional) AS d, sum(credit_functional) AS c "
                "FROM mod_accounting.journal_lines WHERE tenant_id = :t AND journal_id IN :ids "
                "GROUP BY journal_id, account_id"
            ).bindparams(t=tenant_id),
            {"ids": chunk},
        ):
            if row.journal_id in posted:
                posted[row.journal_id][row.account_id] = (Decimal(row.d), Decimal(row.c))
    return {key: [Twin(j, posted[j]) for j in sorted(ids, key=str) if j in posted] for key, ids in by_doc.items()}


def _resolve_bank_gls(db: Session, tenant_id: UUID) -> tuple[dict[UUID, UUID], frozenset[UUID]]:
    """(bank account id -> cash GL, GLs that must queue).

    Uses the reconciliation resolver's own refusal semantics (``recon_service.resolve_gl_account``): a bank account
    whose cash GL is shared with another bank account, or was re-pointed away, is refused there, so its GL is
    ``unsafe`` here and every fee key on it queues. A reference that resolves to no account is simply absent.
    """
    from dotmac_banking.models import BankAccount

    from erp_v2.banking.recon_service import ReconRefused, resolve_gl_account

    accounts = {
        r.code: r.id
        for r in db.execute(
            sa.text("SELECT id, code FROM mod_accounting.accounts WHERE tenant_id = :t"), {"t": tenant_id}
        )
    }
    ids = set(accounts.values())
    out: dict[UUID, UUID] = {}
    unsafe: set[UUID] = set()
    for bank in db.scalars(sa.select(BankAccount).where(BankAccount.tenant_id == tenant_id)):
        try:
            out[bank.id] = resolve_gl_account(db, tenant_id, bank)
            continue
        except ReconRefused as refused:
            if refused.code not in ("shared_cash_gl", "retired_cash_gl"):
                continue  # no usable cash GL reference at all: nothing of ours to match on
        ref = (bank.cash_account_ref or "").strip()
        try:
            parsed = UUID(ref)
        except ValueError:
            parsed = None
        resolved = parsed if parsed is not None and str(parsed) == ref and parsed in ids else accounts.get(ref)
        if resolved is not None:
            unsafe.add(resolved)
    return out, frozenset(unsafe)


def _statement_keys(
    db: Session, scope: Scope, bank_gl: dict[UUID, UUID]
) -> dict[tuple[UUID, date, Decimal], list[UUID]]:
    out: dict[tuple[UUID, date, Decimal], list[UUID]] = defaultdict(list)
    for row in db.execute(
        sa.text(
            "SELECT bs.account_id, sl.id, sl.transaction_date AS day, sl.direction, sl.amount "
            "FROM mod_banking.bank_statement_lines sl JOIN mod_banking.bank_statements bs "
            "ON bs.tenant_id = sl.tenant_id AND bs.id = sl.statement_id "
            "WHERE sl.tenant_id = :t AND sl.transaction_date BETWEEN :a AND :b"
        ),
        {"t": scope.tenant_id, "a": scope.fy_start, "b": scope.fy_end},
    ):
        gl = bank_gl.get(row.account_id)
        if gl is None:
            continue
        signed = rules.quantize(Decimal(row.amount)) * (-1 if row.direction == "debit" else 1)
        out[(gl, row.day, signed)].append(row.id)
    return out


def _posted_fees(db: Session, scope: Scope, bank_gls: frozenset[UUID]) -> dict[tuple[UUID, date, Decimal], list[UUID]]:
    out: dict[tuple[UUID, date, Decimal], list[UUID]] = defaultdict(list)
    if not bank_gls:
        return out
    for row in db.execute(
        sa.text(
            "SELECT je.id, COALESCE(je.posting_date, je.entry_date) AS day, jl.account_id, "
            "sum(jl.debit_functional - jl.credit_functional) AS net "
            "FROM mod_accounting.journal_entries je JOIN mod_accounting.journal_lines jl "
            "ON jl.tenant_id = je.tenant_id AND jl.journal_id = je.id "
            "WHERE je.tenant_id = :t AND je.status = 'POSTED' AND je.source_owner = :owner "
            "AND je.source_document_kind = 'BANK_FEE' "
            "AND COALESCE(je.posting_date, je.entry_date) BETWEEN :a AND :b "
            "AND jl.account_id IN :gls "
            "GROUP BY je.id, COALESCE(je.posting_date, je.entry_date), jl.account_id"
        ).bindparams(sa.bindparam("gls", expanding=True)),
        {
            "t": scope.tenant_id,
            "owner": LEGACY_OWNER,
            "a": scope.fy_start,
            "b": scope.fy_end,
            "gls": sorted(bank_gls, key=str),
        },
    ):
        out[(row.account_id, row.day, rules.quantize(Decimal(row.net)))].append(row.id)
    return out


def _post_blockers(db: Session, scope: Scope, drafts: dict[UUID, Draft], posting: set[UUID]) -> dict[UUID, str]:
    """Pre-check each draft that would post: the same guards the B2 path applies, so a plan does not promise a post
    that will be refused. The B2 path stays the authority at apply."""
    from erp_v2.ledger_roles import control_codes_among, tax_role_account_ids

    tax_ids = {UUID(i) for i in tax_role_account_ids(db, scope.tenant_id)}
    periods = db.execute(
        sa.text(
            "SELECT start_date, end_date, status, is_adjustment FROM mod_accounting.fiscal_periods WHERE tenant_id = :t"
        ),
        {"t": scope.tenant_id},
    ).all()
    out: dict[UUID, str] = {}
    for journal_id in posting:
        draft = drafts[journal_id]
        hit = [p for p in periods if p.start_date <= draft.posting_date <= p.end_date]
        if len(hit) != 1:
            out[journal_id] = "period_missing"
        elif hit[0].status != "OPEN":
            out[journal_id] = "period_not_open"
        elif control_codes_among(db, scope.tenant_id, set(draft.lines)):
            out[journal_id] = "control_account_requires_subledger_document"
        elif draft.posting_date.strftime("%Y-%m") in scope.filed_vat_months and tax_ids & set(draft.lines):
            out[journal_id] = "vat_month_filed"
    return out


def _decided(db: Session, scope: Scope) -> list[Decision]:
    rows = db.scalars(
        sa.select(HeldDraftDecision).where(
            HeldDraftDecision.tenant_id == scope.tenant_id,
        )
    ).all()
    return [
        Decision(r.journal_id, r.rule_id, r.outcome, r.reason_code, Decimal(r.amount), dict(r.evidence)) for r in rows
    ]


def scope_sha256(scope: Scope) -> str:
    return rules.sha256_json(
        {
            "tenant": str(scope.tenant_id),
            "fy_start": scope.fy_start.isoformat(),
            "fy_end": scope.fy_end.isoformat(),
            "filed_vat_months": sorted(scope.filed_vat_months),
        }
    )


def _legacy_held_ids(legacy: sa.Connection, scope: Scope) -> set[UUID]:
    """The held population as the LEGACY snapshot sees it: DRAFT, SUBMITTED or APPROVED journals in the fiscal year."""
    return {
        UUID(str(r))
        for r in legacy.scalars(
            sa.text(
                "SELECT journal_entry_id FROM gl.journal_entry "
                "WHERE status IN ('DRAFT', 'SUBMITTED', 'APPROVED') "
                "AND COALESCE(posting_date, entry_date) BETWEEN :a AND :b"
            ),
            {"a": scope.fy_start, "b": scope.fy_end},
        )
    }


# Legacy document tables read for the unapproved-reversal rule (the document's own status). The table and column
# names are the legacy AR schema as named in the pairing notes; the status is only read, never trusted blindly:
# anything but PAID, POSTED or PARTIALLY_PAID (or a missing document) queues the draft.
_DOCUMENT_TABLES = {
    "INVOICE": (
        "SELECT CAST(invoice_id AS TEXT) AS doc_id, status FROM ar.invoice WHERE CAST(invoice_id AS TEXT) IN :ids"
    ),
    "CUSTOMER_PAYMENT": (
        "SELECT CAST(payment_id AS TEXT) AS doc_id, status FROM ar.customer_payment "
        "WHERE CAST(payment_id AS TEXT) IN :ids"
    ),
}


def _document_statuses(
    legacy: sa.Connection, documents: set[tuple[str, str, str]]
) -> dict[tuple[str, str, str], str | None]:
    out: dict[tuple[str, str, str], str | None] = {d: None for d in documents}
    for doc_type, sql in _DOCUMENT_TABLES.items():
        ids = sorted({doc_id for _m, t, doc_id in documents if t == doc_type})
        for chunk in _chunks(ids):
            for row in legacy.execute(_in(sql), {"ids": chunk}):
                out[("AR", doc_type, row.doc_id)] = row.status
    return out


def build_plan(db: Session, legacy: sa.Connection, scope: Scope) -> Plan:
    """Read-only: gather the evidence and decide every held draft; decided drafts keep their recorded decision.

    A destination DRAFT enters the held population only when the legacy snapshot says DRAFT, SUBMITTED or APPROVED
    (a draft the legacy record shows as anything else is not a held draft); a draft with no legacy record is kept
    (and queued) so the independent partition proof, which counts the legacy side, flags the difference.
    """
    all_drafts = _drafts(db, scope)
    legacy_records = _legacy_records(legacy, sorted((d.journal_id for d in all_drafts), key=str))
    drafts = [
        d
        for d in all_drafts
        if d.journal_id not in legacy_records or legacy_records[d.journal_id].status in rules.HELD_LEGACY_STATUSES
    ]
    excluded = len(all_drafts) - len(drafts)
    by_id = {d.journal_id: d for d in drafts}
    bank_gl, unsafe_gl_ids = _resolve_bank_gls(db, scope.tenant_id)
    bank_gl_ids = frozenset(bank_gl.values())
    documents = {
        (r.module, r.doc_type, r.doc_id)
        for j, r in legacy_records.items()
        if j in by_id and r.module == rules.SOURCE_AR and r.doc_type in rules.AR_TYPES and r.doc_id and r.doc_id.strip()
    }
    evidence = Evidence(
        legacy=legacy_records,
        bank_gl_ids=bank_gl_ids,
        statement=_statement_keys(db, scope, bank_gl),
        posted_fees=_posted_fees(db, scope, bank_gl_ids),
        twins=_twins(db, legacy, scope.tenant_id, documents),
        unsafe_gl_ids=unsafe_gl_ids,
        document_status=_document_statuses(legacy, documents),
    )
    first = rules.decide(drafts, evidence)
    blockers = _post_blockers(db, scope, by_id, {d.journal_id for d in first if d.outcome == OUTCOME_POSTED})
    pending_decisions = rules.decide(drafts, replace(evidence, post_blockers=blockers)) if blockers else first
    decided = _decided(db, scope)
    decisions = sorted([*pending_decisions, *decided], key=lambda d: str(d.journal_id))
    population_amount = sum((d.amount for d in decisions), Decimal(0))
    before = db.scalar(
        sa.text(
            "SELECT count(*) FROM mod_accounting.journal_entries je WHERE je.tenant_id = :t AND je.status = 'DRAFT' "
            "AND je.source_owner = :owner AND COALESCE(je.posting_date, je.entry_date) < :a"
        ),
        {"t": scope.tenant_id, "owner": LEGACY_OWNER, "a": scope.fy_start},
    )
    return Plan(
        scope_sha256(scope),
        tuple(decisions),
        frozenset(d.journal_id for d in pending_decisions),
        frozenset(d.journal_id for d in decisions),
        population_amount,
        int(before or 0),
        excluded,
    )


# --- partition proof -----------------------------------------------------------------------------------------


def partition_proof(legacy: sa.Connection, scope: Scope, decisions: tuple[Decision, ...]) -> dict[str, Any]:
    """posted + voided + queued = held, by journal id.

    The held set and its total come from the LEGACY snapshot (journals the legacy ERP holds as DRAFT, SUBMITTED or
    APPROVED in the fiscal year), not from a re-read of the destination, and must also equal the count the operator
    declared in the spec (``expected_held_total``, 14,271 on g9).
    """
    held_ids = _legacy_held_ids(legacy, scope)
    buckets: dict[str, list[Decision]] = {o: [] for o in OUTCOMES}
    for d in decisions:
        buckets[d.outcome].append(d)
    ids = {o: {d.journal_id for d in items} for o, items in buckets.items()}
    union = set().union(*ids.values())
    disjoint = sum(len(v) for v in ids.values()) == len(union)
    counts = {o: len(items) for o, items in buckets.items()}
    amounts = {o: str(rules.quantize(sum((d.amount for d in items), Decimal(0)))) for o, items in buckets.items()}
    expected_ok = scope.expected_held_total is not None and scope.expected_held_total == len(held_ids)
    return {
        "held_total": len(held_ids),
        "held_total_source": "legacy_snapshot",
        "expected_held_total": scope.expected_held_total,
        "matches_expected_held_total": expected_ok,
        "counts": counts,
        "amounts": amounts,
        "amount_total": str(rules.quantize(sum((d.amount for d in decisions), Decimal(0)))),
        "sum_of_buckets": sum(counts.values()),
        "disjoint": disjoint,
        "ids_equal_held": union == held_ids,
        "decisions_not_in_legacy_held": len(union - held_ids),
        "legacy_held_without_decision": len(held_ids - union),
        "ok": disjoint and union == held_ids and sum(counts.values()) == len(held_ids) and expected_ok,
        "bucket_id_sha256": {o: rules.sha256_json(sorted(map(str, v))) for o, v in ids.items()},
    }


def reason_counts(decisions: tuple[Decision, ...]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for d in decisions:
        out[f"{d.rule_id}:{d.outcome}:{d.reason_code}"] += 1
    return dict(sorted(out.items()))


# --- apply ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApplyInputs:
    plan_sha256: str
    plan_receipt_sha256: str
    planned_by: str
    applied_by: str
    requester_party_id: UUID
    approver_party_id: UUID
    dual_control: bool


def _record(
    db: Session, scope: Scope, decision: Decision, inputs: ApplyInputs, *, manual_id: UUID | None, now: datetime
) -> None:
    db.add(
        HeldDraftDecision(
            tenant_id=scope.tenant_id,
            journal_id=decision.journal_id,
            rule_id=decision.rule_id,
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            amount=rules.quantize(decision.amount),
            evidence=decision.evidence,
            evidence_sha256=decision.evidence_sha256,
            rules_sha256=rules.RULES_SHA256,
            plan_sha256=inputs.plan_sha256,
            plan_receipt_sha256=inputs.plan_receipt_sha256,
            manual_journal_id=manual_id,
            dual_control=inputs.dual_control,
            planned_by=inputs.planned_by,
            applied_by=inputs.applied_by,
            decided_at=now,
        )
    )
    db.flush()


def _void(db: Session, scope: Scope, decision: Decision, inputs: ApplyInputs, now: datetime) -> None:
    from dotmac_accounting.contracts import VoidDraftJournal
    from dotmac_accounting.service import void_draft_journal
    from dotmac_kernel.cache import TenantScope

    void_draft_journal(
        db,
        scope=TenantScope(scope.tenant_id),
        command=VoidDraftJournal(
            journal_id=decision.journal_id,
            reason=f"{decision.reason_code}; rule {decision.rule_id}; evidence {decision.evidence_sha256[:16]}",
            voided_by=f"{REQUESTER_PREFIX}{inputs.planned_by}",
            approved_by=f"{APPROVER_PREFIX}{inputs.applied_by}",
            approval_reference=f"held-drafts:{rules.RULES_SHA256[:16]}:{inputs.plan_sha256[:16]}",
        ),
        idempotency_key=f"held-draft-void:{decision.journal_id}",
        idempotency_expires_at=None,
        voided_at=now,
    )


def _post(db: Session, scope: Scope, decision: Decision, inputs: ApplyInputs, now: datetime) -> UUID:
    from erp_v2.journals import service as journals

    review = f"held-drafts:{decision.rule_id}:{decision.evidence_sha256[:16]}"
    requested = journals.request_legacy_post(
        db,
        tenant_id=scope.tenant_id,
        journal_id=decision.journal_id,
        actor=inputs.requester_party_id,
        review_ref=review,
        now=now,
    )
    journals.approve_and_post(
        db, tenant_id=scope.tenant_id, manual_id=requested.manual_id, approver=inputs.approver_party_id, now=now
    )
    return requested.manual_id


def apply_plan(db: Session, scope: Scope, plan: Plan, inputs: ApplyInputs) -> dict[str, Any]:
    """Perform every pending decision. Commits per batch; a restart skips decided drafts."""
    refusals: dict[str, int] = defaultdict(int)
    done = 0
    for decision in (d for d in plan.decisions if d.journal_id in plan.pending):
        now = datetime.now(UTC)
        final = decision
        manual_id: UUID | None = None
        try:
            with db.begin_nested():
                if decision.outcome == OUTCOME_VOIDED:
                    _void(db, scope, decision, inputs, now)
                elif decision.outcome == OUTCOME_POSTED:
                    manual_id = _post(db, scope, decision, inputs, now)
                _record(db, scope, decision, inputs, manual_id=manual_id, now=now)
        except Exception as exc:
            if decision.outcome == OUTCOME_QUEUED or not isinstance(exc, JournalRefused | AccountingError):
                raise
            # The governed path refused: the draft stays DRAFT and is queued with the refusal (named, not improvised).
            code = exc.code if isinstance(exc, JournalRefused) else type(exc).__name__
            refusals[f"{decision.outcome}:{code}"] += 1
            final = Decision(
                decision.journal_id,
                decision.rule_id,
                OUTCOME_QUEUED,
                f"{decision.outcome}_refused:{code}"[:80],
                decision.amount,
                {**decision.evidence, "refused_outcome": decision.outcome, "refusal": code},
            )
            manual_id = None
            with db.begin_nested():
                _record(db, scope, final, inputs, manual_id=None, now=now)
        done += 1
        if done % BATCH == 0:
            db.commit()
    db.commit()
    return {"decided_now": done, "refusals": dict(refusals)}


def status_check(db: Session, scope: Scope) -> dict[str, Any]:
    """After apply: every voided decision is VOID, every posted one POSTED, every queued one still DRAFT."""
    expected = {OUTCOME_VOIDED: "VOID", OUTCOME_POSTED: "POSTED", OUTCOME_QUEUED: "DRAFT"}
    counts: dict[str, int] = defaultdict(int)
    mismatches = 0
    for row in db.execute(
        sa.text(
            "SELECT d.outcome, je.status FROM public.erp_held_draft_decisions d "
            "LEFT JOIN mod_accounting.journal_entries je "
            "ON je.tenant_id = d.tenant_id AND je.id = d.journal_id WHERE d.tenant_id = :t"
        ),
        {"t": scope.tenant_id},
    ):
        counts[f"{row.outcome}:{row.status}"] += 1
        if row.status != expected[row.outcome]:
            mismatches += 1
    return {"journal_status_by_outcome": dict(sorted(counts.items())), "mismatches": mismatches}
