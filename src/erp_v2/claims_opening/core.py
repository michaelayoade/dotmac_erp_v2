"""Plan / apply / verify core of the claims opening (S8 sections 2-4). Callers own the transactions."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_expenses import ExpensesError, admit_historical_claim_liability
from dotmac_expenses.models import (
    ClaimLiability,
    ClaimLiabilityAdjustment,
    ClaimLiabilityLine,
    ClaimLiabilityReversal,
    ClaimLiabilitySettlement,
    ClaimSettlementReversal,
    HistoricalClaimReceipt,
)
from sqlalchemy.orm import Session

from erp_v2.claims_opening import extract, reclass
from erp_v2.claims_opening.reasons import FAMILY, FUNCTIONAL, OWNER_CODES, REASONS, ClaimsRefusal
from erp_v2.claims_opening.verifier import ClaimEvidenceError, ClaimOpeningVerifier, carried_code_of
from erp_v2.holdings import plan as holdings_plan
from erp_v2.migration_ledger.common import canonical_sha, fmt

OWNER_TABLES = (
    ClaimLiability,
    ClaimLiabilityLine,
    ClaimLiabilitySettlement,
    ClaimSettlementReversal,
    ClaimLiabilityAdjustment,
    ClaimLiabilityReversal,
    HistoricalClaimReceipt,
)
KIND = "claims-opening"
FRAGMENT_KIND = "claims-2040-fragment"


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _minor_signed(amount: Decimal) -> int:
    """GL-signed minor units of a credit-positive liability amount (a liability is a negative GL net)."""
    return int(-(amount * 100))


def owner_state_empty(db: Session, tenant: UUID) -> bool:
    return all(
        db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == tenant)) == 0 for m in OWNER_TABLES
    )


def _owner_reason(exc: ExpensesError) -> str:
    code = getattr(exc, "code", None)
    return f"owner_refused:{code}" if code in OWNER_CODES else "owner_refused:conflict"


def _claimant_of(db: Session, tenant: UUID, claim_id: str) -> str | None:
    value = db.execute(
        sa.text("SELECT claimant_party_id FROM mod_expenses.expense_claims WHERE tenant_id = :t AND id = :c"),
        {"t": tenant, "c": UUID(claim_id)},
    ).scalar()
    return None if value is None else str(value)


def _refusal(db, tenant, row: extract.ClaimRow, reason: str, carried: str | None, amount: Decimal | None) -> dict:
    if reason not in REASONS:
        raise ClaimsRefusal(f"claims_evidence_failure:{reason}")
    value = amount if amount is not None else extract.owed(row)
    return dict(
        outcome="refused",
        legacy_claim_id=row.claim_id,
        reason=reason,
        carried_code=carried,
        amount=_money(value),
        claimant_party_id=_claimant_of(db, tenant, row.claim_id),
        employee_id=row.employee_id,
    )


def classify(db: Session, scope, verifier: ClaimOpeningVerifier, row: extract.ClaimRow, admitted_at: datetime) -> dict:
    """eligible / already_done / refused for one bucket A claim. Read only: nothing is written."""
    from dotmac_expenses.contracts import HistoricalClaimLiabilityCommand  # noqa: F401
    from dotmac_expenses.liability import _check_historical_evidence

    tenant = scope.tenant_id
    try:
        evidence = verifier.evidence(scope=scope, claim_id=UUID(row.claim_id), destination=db)
    except ClaimEvidenceError as exc:
        return _refusal(db, tenant, row, exc.reason, exc.carried_code, exc.amount)
    command = verifier.command(evidence)
    receipt = db.scalar(
        sa.select(HistoricalClaimReceipt).where(
            HistoricalClaimReceipt.tenant_id == tenant, HistoricalClaimReceipt.claim_id == UUID(row.claim_id)
        )
    )
    base = dict(
        legacy_claim_id=row.claim_id,
        claim_id=row.claim_id,
        evidence_fingerprint=command.evidence_fingerprint,
        residual=_money(evidence.residual),
        carried_code=carried_code_of(evidence),
        claimant_party_id=str(evidence.claimant_party_id),
        employee_id=row.employee_id,
        settlements=len(evidence.settlements),
    )
    if receipt is not None:
        same = (
            receipt.evidence_fingerprint == command.evidence_fingerprint
            and receipt.evidence_reference == command.evidence_reference
            and receipt.admitted_by == command.admitted_by
        )
        if not same:
            return _refusal(db, tenant, row, "owner_refused:conflict", base["carried_code"], evidence.residual)
        return dict(outcome="already_done", liability_id=str(receipt.liability_id), **base)
    if db.scalar(
        sa.select(ClaimLiability.id).where(
            ClaimLiability.tenant_id == tenant, ClaimLiability.claim_id == UUID(row.claim_id)
        )
    ):
        return _refusal(
            db, tenant, row, "owner_refused:claim_already_accepted", base["carried_code"], evidence.residual
        )
    try:
        _check_historical_evidence(evidence, tenant_id=tenant, command=command, admitted_at=admitted_at)
    except ExpensesError as exc:
        return _refusal(db, tenant, row, _owner_reason(exc), base["carried_code"], evidence.residual)
    return dict(outcome="eligible", **base)


def build_plan(
    db: Session, src: sa.Connection, verifier: ClaimOpeningVerifier, tenant: UUID, *, window_id: str,
    capture_date: date, bindings: dict, admitted_at: datetime,
) -> dict:  # fmt: skip
    from dotmac_kernel.cache import TenantScope

    scope = TenantScope(tenant)
    census = extract.census(src, tenant)
    rows = [*census.buckets[extract.BUCKET_A], *census.buckets[extract.BUCKET_A_PARTIAL]]
    # a claim id the snapshot lists twice is classified once (the verifier refuses it ``source_claim_duplicate``)
    rows = list({r.claim_id: r for r in sorted(rows, key=lambda r: r.claim_id)}.values())
    detail = [classify(db, scope, verifier, r, admitted_at) for r in rows]
    for claim_id in census.n_posted:  # N with a POSTED journal: refused, never guessed
        row = next(r for r in census.buckets[extract.BUCKET_N] if r.claim_id == claim_id)
        detail.append(_refusal(db, tenant, row, "claim_net_payable_missing", None, Decimal(0)))
    detail.sort(key=lambda d: d["legacy_claim_id"])
    eligible = [d for d in detail if d["outcome"] == "eligible"]
    done = [d for d in detail if d["outcome"] == "already_done"]
    refused = [d for d in detail if d["outcome"] == "refused"]
    admitted_rows = [*eligible, *done]
    items = [
        reclass.Admitted(
            d["legacy_claim_id"], d["claim_id"], d.get("liability_id", ""), Decimal(d["residual"]),
            d["carried_code"], d["claimant_party_id"], d["employee_id"],
        )
        for d in admitted_rows
    ]  # fmt: skip
    posting_date = holdings_plan.posting_date(db, tenant, capture_date)
    totals = {}
    for code in FAMILY:
        subset = [
            Decimal(d.get("residual") or d["amount"]) for d in (*admitted_rows, *refused) if d["carried_code"] == code
        ]
        totals[code] = dict(count=len(subset), sum=_money(sum(subset, Decimal(0))))
    a_count, a_sum, _ = census.totals(extract.BUCKET_A)
    p_count, p_sum, _ = census.totals(extract.BUCKET_A_PARTIAL)
    u_count, u_sum, u_emps = census.totals(extract.BUCKET_U)
    other_open = [r for r in census.buckets[extract.BUCKET_O] if r.balance is not None and r.balance > 0]
    paid_artefact = [r for r in census.buckets[extract.BUCKET_S] if r.status == "PAID" and r.balance and r.balance > 0]
    admitted_sum = sum((Decimal(d["residual"]) for d in admitted_rows), Decimal(0))
    refused_sum = sum((Decimal(d["amount"]) for d in refused if d["reason"] != "claim_net_payable_missing"), Decimal(0))
    refused_a = [d for d in refused if d["reason"] != "claim_net_payable_missing"]
    employees_a = len({r.employee_id for r in rows if r.employee_id})
    body = dict(
        version=1,
        kind="claims-opening-plan",
        bindings=bindings,
        window_id=window_id,
        capture_date=capture_date.isoformat(),
        posting_date=posting_date.isoformat(),
        buckets={
            "A": dict(count=a_count + p_count, sum=_money(a_sum + p_sum), employees=employees_a, partial=p_count),
            "U": dict(count=u_count, sum=_money(u_sum), employees=u_emps),
            "N": dict(count=len(census.buckets[extract.BUCKET_N]), refused_with_posted_journal=len(census.n_posted)),
            "S": dict(count=len(census.buckets[extract.BUCKET_S]), paid_with_balance_artefact=len(paid_artefact)),
            "O": dict(
                count=len(census.buckets[extract.BUCKET_O]),
                open_balance_count=len(other_open),
                open_balance_sum=_money(sum((r.balance for r in other_open), Decimal(0))),
            ),
        },
        by_status=dict(sorted(census.by_status.items())),
        carried_split=totals,
        completeness=extract.completeness(census, (len(admitted_rows), admitted_sum), (len(refused_a), refused_sum)),
        counts=dict(
            eligible=len(eligible),
            already_done=len(done),
            refused=len(refused),
            admitted_plus_refused_a=len(admitted_rows) + len(refused_a),
        ),
        refusals_by_reason=dict(sorted(Counter(d["reason"] for d in refused).items())),
        # Only known when every admission already exists (liability ids are owner-generated): the replay case.
        set_fingerprint=reclass.set_fingerprint(items, currency=FUNCTIONAL, snapshot_sha256=verifier.snapshot_sha256)
        if items and not eligible
        else None,
        detail=detail,
    )
    body["digest"] = plan_digest(body)
    return body


def plan_digest(body: dict) -> str:
    """Stable across the admission itself: an eligible claim and an already-done one are the same admitted row, and
    the owner-generated liability id is not part of the reviewed facts, so a replay re-plans to the same digest."""
    detail = []
    for d in body["detail"]:
        row = {k: v for k, v in d.items() if k not in ("outcome", "liability_id")}
        row["admitted"] = d["outcome"] != "refused"
        detail.append(row)
    view = {k: v for k, v in body.items() if k not in ("digest", "detail", "counts", "set_fingerprint")}
    view["detail"] = detail
    view["admitted_count"] = sum(1 for d in detail if d["admitted"])
    return canonical_sha(view)


def public_receipt(plan: dict, *, mode: str) -> dict:
    """Counts, sums and digests only: no claim, employee or party ids, no names."""
    return dict(
        kind="claims-opening-plan-receipt",
        mode=mode,
        production_ready=False,
        bindings=plan["bindings"],
        plan_digest=plan["digest"],
        buckets=plan["buckets"],
        carried_split=plan["carried_split"],
        completeness=plan["completeness"],
        counts=plan["counts"],
        refusals_by_reason=plan["refusals_by_reason"],
        set_fingerprint=plan["set_fingerprint"],
    )


def _assert_landed(db: Session, tenant: UUID, row: dict, liability_ref: str) -> UUID:
    liability = db.scalar(
        sa.select(ClaimLiability).where(
            ClaimLiability.tenant_id == tenant, ClaimLiability.claim_id == UUID(row["claim_id"])
        )
    )
    ok = (
        liability is not None
        and Decimal(liability.amount) == Decimal(row["residual"])
        and liability.origin == "historical"
        and liability.liability_account_ref == liability_ref
        and liability.status == "open"
    )
    if not ok:
        raise ClaimsRefusal("apply_amounts_changed")
    return liability.id


def _idempotency_key(legacy_claim_id: str) -> str:
    return f"erp.claims-opening.admit:{legacy_claim_id}"


def apply_plan(
    db: Session, src: sa.Connection, verifier: ClaimOpeningVerifier, tenant: UUID, *, window_id: str,
    capture_date: date, bindings: dict, reviewed_digest: str, now: datetime,
) -> dict:  # fmt: skip
    """ONE transaction: lock, re-plan, compare with the reviewed plan, admissions, reclass, post-condition."""
    from dotmac_kernel.cache import TenantScope

    scope = TenantScope(tenant)
    db.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"erp.claims-opening:{tenant}"})
    existing = reclass.posted_reclass(db, tenant)
    if existing is None and not owner_state_empty(db, tenant):
        raise ClaimsRefusal("claims_owner_state_not_empty")
    plan = build_plan(
        db, src, verifier, tenant, window_id=window_id, capture_date=capture_date, bindings=bindings, admitted_at=now
    )
    if plan["digest"] != reviewed_digest:
        raise ClaimsRefusal("reviewed_plan_changed")
    if existing is not None:
        # same set (the plan's admitted rows all exist) -> replay; any other -> the window is closed, nothing written
        if plan["set_fingerprint"] is not None and existing.source_fingerprint == plan["set_fingerprint"]:
            replay_items = [
                reclass.Admitted(
                    d["legacy_claim_id"], d["claim_id"], d["liability_id"], Decimal(d["residual"]),
                    d["carried_code"], d["claimant_party_id"], d["employee_id"],
                )
                for d in plan["detail"]
                if d["outcome"] == "already_done"
            ]  # fmt: skip
            return dict(
                plan=plan, replayed=True, journal_id=str(existing.id), items=_items(plan), admitted=0,
                admitted_items=replay_items, accounts=reclass.account_ids(db, tenant),
            )  # fmt: skip
        raise ClaimsRefusal("historical_claim_window_closed")
    accounts = reclass.account_ids(db, tenant)
    liability_ref = str(accounts["2045"])
    admitted = 0
    for row in [d for d in plan["detail"] if d["outcome"] == "eligible"]:
        try:
            with db.begin_nested():
                evidence = verifier.evidence(scope=scope, claim_id=UUID(row["claim_id"]), destination=db)
                command = verifier.command(evidence)
                if command.evidence_fingerprint != row["evidence_fingerprint"]:
                    raise ClaimsRefusal("evidence_changed")
                admit_historical_claim_liability(
                    db,
                    scope=scope,
                    command=command,
                    verifier=verifier,
                    idempotency_key=_idempotency_key(row["legacy_claim_id"]),
                    idempotency_expires_at=None,
                    admitted_at=now,
                )
                row["liability_id"] = str(_assert_landed(db, tenant, row, liability_ref))
        except ExpensesError as exc:
            raise ClaimsRefusal(f"apply_owner_refused:{getattr(exc, 'code', None) or 'refused'}") from exc
        except ClaimEvidenceError as exc:
            raise ClaimsRefusal(f"apply_evidence_changed:{exc.reason}") from exc
        admitted += 1
    items, snapshot = reclass.admitted_from_receipts(db, tenant)
    planned = reclass.set_fingerprint(
        [
            reclass.Admitted(
                d["legacy_claim_id"],
                d["claim_id"],
                d["liability_id"],
                Decimal(d["residual"]),
                d["carried_code"],
                d["claimant_party_id"],
            )
            for d in plan["detail"]
            if d["outcome"] == "eligible"
        ],
        currency=FUNCTIONAL,
        snapshot_sha256=verifier.snapshot_sha256,
    )
    if reclass.set_fingerprint(items, currency=FUNCTIONAL, snapshot_sha256=snapshot or "") != planned:
        raise ClaimsRefusal("apply_amounts_changed")
    by_legacy = {d["legacy_claim_id"]: d["employee_id"] for d in plan["detail"] if d["outcome"] == "eligible"}
    items = [replace(i, employee_id=by_legacy.get(i.legacy_claim_id)) for i in items]
    consequence = reclass.build(
        tenant, window_id, items, accounts, posting_date=date.fromisoformat(plan["posting_date"]),
        snapshot_sha256=verifier.snapshot_sha256,
    )  # fmt: skip
    result = reclass.post(
        db,
        tenant,
        consequence,
        approval_ref=f"erp.claims-opening:{plan['digest'][:16]}",
        actor=verifier.operator_ref,
        now=now,
    )
    reclass.postcondition(db, tenant, result.journal_id, accounts)
    return dict(
        plan=plan, replayed=False, journal_id=str(result.journal_id), items=_items(plan), admitted=admitted,
        admitted_items=items, accounts=accounts,
    )  # fmt: skip


def journal_dr(db: Session, tenant: UUID, journal_id: UUID | str, account_id: UUID) -> Decimal:
    """The reclass journal's actual net debit on one account (read back from the posted ledger)."""
    from dotmac_accounting.models import PostedLedgerLine

    return Decimal(
        db.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0)).where(
                PostedLedgerLine.tenant_id == tenant,
                PostedLedgerLine.journal_id == UUID(str(journal_id)),
                PostedLedgerLine.account_id == account_id,
            )
        )
    )


def _items(plan: dict) -> list[dict]:
    """The E37 register rows (refusals)."""
    out = []
    for d in plan["detail"]:
        if d["outcome"] != "refused":
            continue
        amount = Decimal(d["amount"])
        out.append(
            dict(
                {"class": "E37"},
                key=f"claim:{d['legacy_claim_id']}",
                reason=d["reason"],
                carried_code=d["carried_code"],
                amount=_money(amount),
                gl_signed_minor=_minor_signed(amount),
                claimant_party_id=d["claimant_party_id"],
                employee_id=d["employee_id"],
            )
        )
    return out


def dumps(body: dict) -> bytes:
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()


def attribution_2045(items: list[reclass.Admitted]) -> dict:
    """The 2045 gl_attribution receipt in the exact ``parse_gl_attribution`` v1 shape (party ids; GL-signed)."""
    per: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for i in items:
        per[i.claimant_party_id] += i.residual
    return dict(
        version=1,
        code="2045",
        currency=FUNCTIONAL,
        parties=[dict(kind="employee", key=k, gl=_money(-v)) for k, v in sorted(per.items())],
        held=[],
        unattributed=[],
        opening_residue="0.00",
    )


def fragment_2040(items: list[reclass.Admitted], refusals: list[dict], dr_2040: Decimal) -> dict:
    """ONE 2040 fragment keyed by LEGACY employee id (GL-ATTRIBUTION-RECEIPTS-SPEC section 6): the Dr 2040 per employee
    (sums to the journal's 2040 line, else that journal is unattributed) and the held 2040 claims."""
    per: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for i in items:
        if i.carried_code == "2040":
            if i.employee_id is None:
                raise ClaimsRefusal("fragment_employee_missing")
            per[i.employee_id] += i.residual
    if sum(per.values(), Decimal(0)) != dr_2040:
        raise ClaimsRefusal("fragment_not_exact")
    held = [
        dict(
            kind="employee",
            key=r["employee_id"],
            class_code="E37",
            reason=r["reason"],
            legacy_key=r["key"],
            amount=_money(-Decimal(r["amount"])),
        )
        for r in refusals
        if r["carried_code"] == "2040" and r["employee_id"] is not None
    ]
    return dict(
        kind=FRAGMENT_KIND,
        version=1,
        currency=FUNCTIONAL,
        reclass_2040=[dict(key=k, amount=_money(v)) for k, v in sorted(per.items())],
        held_2040=sorted(held, key=lambda h: h["legacy_key"]),
        held_2040_unkeyed=sum(1 for r in refusals if r["carried_code"] == "2040" and r["employee_id"] is None),
    )


def apply_receipt(result: dict, *, mode: str, attribution_sha256: str, fragment_sha256: str) -> dict:
    plan = result["plan"]
    body = dict(
        kind=KIND,
        version=1,
        mode=mode,
        production_ready=False,
        bindings=plan["bindings"],
        window_id=plan["window_id"],
        plan_digest=plan["digest"],
        posting_date=plan["posting_date"],
        counts=dict(plan["counts"], admitted_now=result["admitted"]),
        buckets=plan["buckets"],
        carried_split=plan["carried_split"],
        completeness=plan["completeness"],
        refusals_by_reason=plan["refusals_by_reason"],
        set_fingerprint=plan["set_fingerprint"],
        journal=dict(
            document_id_window=plan["window_id"], journal_id=result["journal_id"], replayed=result["replayed"]
        ),
        attribution_2045_sha256=attribution_sha256,
        fragment_2040_sha256=fragment_sha256,
        items=result["items"],
    )
    body["receipt_sha256"] = receipt_sha(body)
    return body


def receipt_sha(body: dict) -> str:
    return canonical_sha({k: v for k, v in body.items() if k != "receipt_sha256"})


def verify(db: Session, tenant: UUID, window_id: str, apply_receipt_body: dict | None = None) -> dict:
    """Read-only re-derivation: set_fp from the owner receipts == the posted journal fingerprint; GL(2045) == -total;
    per claimant equal; counts equal the apply receipt (when given)."""
    accounts = reclass.account_ids(db, tenant)
    journal = reclass.posted_reclass(db, tenant)
    items, snapshot = reclass.admitted_from_receipts(db, tenant)
    reasons: list[str] = []
    if journal is None:
        reasons.append("reclass_journal_missing" if items else "")
    else:
        if journal.source_document_id != reclass.document_id(tenant, window_id):
            reasons.append("reclass_window_differs")
        if not items or journal.source_fingerprint != reclass.set_fingerprint(
            items, currency=FUNCTIONAL, snapshot_sha256=snapshot or ""
        ):
            reasons.append("reclass_fingerprint_differs")
        try:
            reclass.postcondition(db, tenant, journal.id, accounts)
        except ClaimsRefusal as exc:
            reasons.append(exc.reason)
    if apply_receipt_body is not None:
        c = apply_receipt_body.get("counts", {})
        n = len(items)
        if receipt_sha(apply_receipt_body) != apply_receipt_body.get("receipt_sha256") or (
            c.get("eligible", 0) + c.get("already_done", 0) != n
        ):
            reasons.append("apply_receipt_counts_differ")
    reasons = [r for r in reasons if r]
    return dict(
        ok=not reasons, reasons=reasons, admitted=len(items), total=fmt(sum((i.residual for i in items), Decimal(0)))
    )
