"""T1 ``ar_opening_credit`` (B14.6): Dr 1400 / Cr the AR prepayment liability account, per currency (F7, before the
holding plan).

The amount is the sum of cash-lot residuals of the admitted partitions: the census receipt's ``cash_lot_minor`` per
party (the receipt also carries the per-account split B14.4 consumes). Inside the fence (the mapper is disabled and no
live writer exists) it must EQUAL R1 ``available_credit_minor`` for that currency, read in a preceding REPEATABLE READ
READ ONLY transaction and bound by fingerprint: otherwise ``t1_r1_mismatch``. Amount 0 posts nothing and is recorded.
Identity: owner ``erp.migration``, kind ``ar_opening_credit``, document_id = uuid5(T1_NS, "<window>:<ccy>").
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from erp_v2.governance import policy as governance
from erp_v2.holdings import attribution as attr
from erp_v2.holdings import plan as planning
from erp_v2.holdings import positions
from erp_v2.holdings.apply import read_only_session
from erp_v2.holdings.billing import BillingReader, Position
from erp_v2.holdings.config import control_account_id, prepayment_account
from erp_v2.holdings.errors import refuse
from erp_v2.holdings.models import KIND_T1_POST, t1_document_id
from erp_v2.migration_ledger.common import canonical_sha, fmt, money, sha_text, text


def run_t1_plan(
    engine: Engine,
    spec_bytes: bytes,
    receipts_loader: Callable[[planning.RunSpec], planning.Receipts],
    billing: BillingReader,
    *,
    planned_by: str,
) -> dict[str, Any]:
    from erp_v2.journals.policy import functional_currency

    spec, spec_sha = planning.parse_spec(spec_bytes)
    receipts = receipts_loader(spec)
    census = receipts.census
    if census is None:
        refuse("census_required", "1400")
    with read_only_session(engine) as db:
        tenant = spec.tenant_id
        f = functional_currency(db, tenant)
        receivable = control_account_id(db, tenant, "1400")
        prepayment = prepayment_account(db, tenant)
        gl = positions.gl_net(db, tenant, receivable.id)
        t1_net = positions.t1_net_1400(db, tenant, receivable.id)
        r1 = billing.r1(db)
        ledger_sha = positions.v2_posted_sha(db, tenant)
        date_ = planning.posting_date(db, tenant, spec.capture_date)
        doc = t1_document_id(spec.window_id, f)
        existing = None
        from erp_v2.holdings import posting

        existing = posting.existing_journal(db, tenant, KIND_T1_POST, doc)
        posted_fingerprint = None if existing is None else existing.source_fingerprint
        already_posted = existing is not None
        # F1: the census must be the one #72 admitted from, and the billing window must carry the same manifest.
        admitted_manifest = planning.anchor_census(db, tenant, census, spec.window_id)
        window_manifest = billing.window_manifest_fingerprint(db, spec.window_id)
        db.rollback()
    if window_manifest != admitted_manifest:
        refuse("manifest_fingerprint_mismatch", "the billing window manifest differs from the admission rows")
    with read_only_session(engine) as db:
        again = billing.r1(db)
        ledger_again = positions.v2_posted_sha(db, tenant)
        db.rollback()
    if again.position_fingerprint != r1.position_fingerprint or ledger_again != ledger_sha:
        refuse("moved_during_plan")
    if census.currencies() - {f}:
        refuse("holding_fx_unsupported", "1400")
    lot_minor = census.cash_lot_total(f)
    if lot_minor < 0:
        refuse("t1_amount_invalid", "negative cash-lot total")
    if r1.currencies.get(f, Position(0, 0, 0, 0)).available_credit_minor != lot_minor:
        refuse("t1_r1_mismatch", "cash-lot residuals differ from R1 available credit")
    amount = Decimal(lot_minor).scaleb(-attr.MINOR_EXPONENT)
    already = t1_net != 0
    if already and t1_net != amount:
        refuse("t1_conflict", "a different T1 amount is already posted")
    if not already and Decimal(census.gl_total_minor(f, f)).scaleb(-attr.MINOR_EXPONENT) != gl:
        refuse("ar_attribution_stale", "census does not equal GL(1400)")
    body = {
        "version": 1,
        "window_id": spec.window_id,
        "spec_sha256": spec_sha,
        "census_sha256": census.sha256,
        "r1_fingerprint": r1.position_fingerprint,
        "v2_posted_sha256": ledger_sha,
        "posting_date": date_.isoformat(),
        "currency": f,
        "amount": fmt(amount),
        "debit_account_id": str(receivable.id),
        "credit_account_id": str(prepayment.id),
        "document_id": doc,
        "manifest_fingerprint": admitted_manifest,
        "already_posted": already_posted,
        "posted_fingerprint": posted_fingerprint,
        "per_account": [
            {"customer_ref_sha256": canonical_sha(p.customer), "cash_lot_minor": p.cash_lot_minor}
            for p in sorted(census.parties, key=lambda p: p.customer)
            if p.cash_lot_minor
        ],
    }
    return {"plan": body, "plan_sha256": canonical_sha(body), "planned_by": planned_by}


def apply_t1(
    db: Session, spec: planning.RunSpec, plan: dict[str, Any], *, approved_sha: str, applied_by: str, now=None
) -> dict[str, Any]:
    from erp_v2.holdings import posting

    sha_text(approved_sha, "plan_sha_required")
    if plan["plan_sha256"] != approved_sha:
        refuse("plan_sha_mismatch")
    body = plan["plan"]
    planned_by = text(plan["planned_by"], "operator_ref_required")
    applied_by = text(applied_by, "operator_ref_required")
    dual = governance.dual_control(db, spec.tenant_id)
    if dual and planned_by == applied_by:
        refuse("dual_control_requires_distinct_operators")
    amount = money(body["amount"])
    if amount == 0:
        return {"ready": True, "plan_sha256": approved_sha, "posted": False, "amount": fmt(amount), "reason": "zero"}
    lines = (
        posting.Line(body["debit_account_id"], amount, Decimal(0), "Opening cash credit (T1)"),
        posting.Line(body["credit_account_id"], Decimal(0), amount, "Opening cash credit (T1)"),
    )
    consequence = posting.build(
        kind=KIND_T1_POST,
        document_id=body["document_id"],
        fingerprint=approved_sha,
        posting_date=_date(body["posting_date"]),
        description=f"AR opening cash credit {body['currency']}",
        currency=body["currency"],
        lines=lines,
    )
    result = posting.post(
        db, spec.tenant_id, consequence, approval_ref=f"t1-plan:{approved_sha}", actor=applied_by, now=now
    )
    return {"ready": True, "plan_sha256": approved_sha, "posted": not result.replayed, "replayed": result.replayed,
            "amount": fmt(amount)}  # fmt: skip


def _date(value: str):
    from datetime import date

    return date.fromisoformat(value)
