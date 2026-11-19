"""Holding apply and verify (B14.5), and the plan runner that owns the transaction postures.

``run_plan``: three transactions (see ``plan``). ``apply``: ONE transaction. Per control code x currency with items it
registers the items (``register.reconcile``, source ``migration_holdings``, receipt = the plan), posts ONE journal
through the posting bridge (owner ``erp.migration``, kind ``migration_holding``, uuid5 identity, version "1",
fingerprint = plan sha) and inserts one ``reclass`` row per item. 2000/2040/2045/1410 also assert control == subledger
in the same transaction and roll back on failure; 1400 and prepayment are proven by ``verify`` (R1 needs its own
REPEATABLE READ READ ONLY transaction).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.contracts import JournalStatus
from dotmac_accounting.models import JournalEntry
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from erp_v2.governance import policy as governance
from erp_v2.holdings import plan as planning
from erp_v2.holdings import positions
from erp_v2.holdings.attribution import ZERO
from erp_v2.holdings.billing import BillingReader
from erp_v2.holdings.errors import refuse
from erp_v2.holdings.models import (
    HOLDINGS_TABLE,
    KIND_HOLDING_POST,
    KIND_RECLASS,
    OWNER,
    PREPAYMENT,
    SOURCE_KIND,
    Holding,
)
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import fmt, money, sha_text, text

RR_RO = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
IN_TX_TIED = ("2000", "2040", "2045", "1410")


def read_only_session(engine: Engine) -> Session:
    """A session whose transaction is REPEATABLE READ READ ONLY (the posture is the first statement)."""
    db = Session(engine)
    db.execute(sa.text(RR_RO))
    return db


def run_plan(
    engine: Engine,
    spec_bytes: bytes,
    receipts_loader: Callable[[planning.RunSpec], planning.Receipts],
    billing: BillingReader,
    *,
    planned_by: str,
    owner_readers: Mapping[str, planning.OwnerReader] | None = None,
) -> dict[str, Any]:
    spec, spec_sha = planning.parse_spec(spec_bytes)
    receipts = receipts_loader(spec)
    with read_only_session(engine) as db:
        snap = planning.read_snapshot(db, spec, billing, owner_readers=owner_readers)
        db.rollback()
    with Session(engine) as db:  # READ COMMITTED (the default), read-write, rolled back
        try:
            account_positions = dict(billing.account_positions(db))
        finally:
            db.rollback()
    with read_only_session(engine) as db:
        r1_again = billing.r1(db)
        ledger_again = positions.v2_posted_sha(db, spec.tenant_id)
        db.rollback()
    return planning.assemble(
        spec, spec_sha, snap, receipts, account_positions, r1_again, ledger_again, planned_by=planned_by
    )


# --- apply --------------------------------------------------------------------------------------------------------


def _facts(plan: dict[str, Any]) -> list[register.Fact]:
    facts = []
    for entry in plan["plan"]["codes"]:
        for it in entry["items"]:
            detail = {k: v for k, v in it.items() if k not in ("class", "key")}
            facts.append(register.Fact(it["class"], it["key"], SOURCE_KIND, None, None, detail))
    return facts


def apply(
    db: Session,
    spec: planning.RunSpec,
    plan: dict[str, Any],
    *,
    approved_sha: str,
    applied_by: str,
    now=None,
) -> dict[str, Any]:
    """Apply an already re-planned ``plan`` (the CLI re-runs the plan and passes it). Caller owns the transaction."""
    from erp_v2.holdings import posting

    sha_text(approved_sha, "plan_sha_required")
    if plan["plan_sha256"] != approved_sha:
        refuse("plan_sha_mismatch", "the re-planned sha differs from the approved sha")
    tenant = spec.tenant_id
    planned_by = text(plan["planned_by"], "operator_ref_required")
    applied_by = text(applied_by, "operator_ref_required")
    dual = governance.dual_control(db, tenant)
    if dual and planned_by == applied_by:
        refuse("dual_control_requires_distinct_operators")
    body = plan["plan"]
    facts = _facts(plan)
    register.reconcile(db, tenant, facts, loaded_sources={SOURCE_KIND}, receipt_sha=approved_sha, apply=True)
    ids = {
        (r.class_code, r.legacy_key): r.id
        for r in db.scalars(
            sa.select(m.MigrationException).where(
                m.MigrationException.tenant_id == tenant, m.MigrationException.source_kind == SOURCE_KIND
            )
        )
    }
    posted: list[dict[str, Any]] = []
    for entry in body["codes"]:
        lines = tuple(
            posting.Line(
                ln["account_id"], money(ln["debit"]), money(ln["credit"]), f"Migration holding {entry['code']}"
            )
            for ln in entry["lines"]
        )
        consequence = posting.build(
            kind=KIND_HOLDING_POST,
            document_id=entry["document_id"],
            fingerprint=approved_sha,
            posting_date=_date(body["posting_date"]),
            description=f"Migration holding reclass {entry['code']} {entry['currency']}",
            currency=entry["currency"],
            lines=lines,
        )
        result = posting.post(
            db, tenant, consequence, approval_ref=f"holding-plan:{approved_sha}", actor=applied_by, now=now
        )
        inserted = 0
        for it in entry["items"]:
            exception_id = ids[(it["class"], it["key"])]
            prior = db.scalar(
                sa.select(Holding).where(
                    Holding.tenant_id == tenant, Holding.exception_id == exception_id, Holding.kind == KIND_RECLASS
                )
            )
            if prior is not None:
                if prior.journal_id != result.journal_id or prior.amount != money(it["amount"]):
                    refuse("holding_already_reclassed", "this item was reclassed by a different journal or amount")
                continue
            db.add(
                Holding(
                    tenant_id=tenant,
                    exception_id=exception_id,
                    control_code=entry["code"],
                    currency=entry["currency"],
                    holding_account_id=UUID(entry["holding_account_id"]),
                    kind=KIND_RECLASS,
                    amount=money(it["amount"]),
                    journal_id=result.journal_id,
                    journal_source_document_id=entry["document_id"],
                    plan_sha256=approved_sha,
                    planned_by=planned_by,
                    applied_by=applied_by,
                    dual_control=dual,
                )
            )
            inserted += 1
        db.flush()
        posted.append(
            {"code": entry["code"], "currency": entry["currency"], "replayed": result.replayed, "rows": inserted}
        )
        if entry["code"] in IN_TX_TIED:
            after = positions.gl_net(db, tenant, UUID(entry["control_account_id"]))
            if after != money(entry["subledger"]):
                refuse("holding_postcondition_failed", f"{entry['code']}: control differs from the subledger")
    return {"ready": True, "plan_sha256": approved_sha, "posted": posted, "dual_control": dual}


def _date(value: str):
    from datetime import date

    return date.fromisoformat(value)


# --- verify -------------------------------------------------------------------------------------------------------


def verify(
    db: Session,
    spec: planning.RunSpec,
    billing: BillingReader,
    *,
    owner_readers: Mapping[str, planning.OwnerReader] | None = None,
) -> dict[str, Any]:
    """RR READ ONLY. Per code x currency: control == subledger; GL(holding) == sum of e2_0030 amounts; open/resolved
    items agree with their outstanding; every ``migration_holding`` identity is posted exactly once."""
    from erp_v2.holdings.billing import Position
    from erp_v2.journals.policy import functional_currency

    tenant = spec.tenant_id
    f = functional_currency(db, tenant)
    snap = planning.read_snapshot(db, spec, billing, owner_readers=owner_readers)
    checks: list[dict[str, Any]] = []
    r1 = snap.r1.currencies.get(f, Position(0, 0, 0, 0))
    for code in planning.ORDER:
        gl = snap.gl[code]
        if code == "1400":
            sub = Decimal(r1.receivable()).scaleb(-2)
        elif code == PREPAYMENT:
            sub = -Decimal(r1.credit()).scaleb(-2)
        else:
            sub = sum((v for (_p, c), v in snap.owner_sub[code].items() if c == f), ZERO)
        checks.append(
            {"check": "control_equals_subledger", "code": code, "ok": gl == sub, "gl": fmt(gl), "sub": fmt(sub)}
        )
    by_account = {
        row.account_id: row.total
        for row in db.execute(
            sa.text(
                f"SELECT holding_account_id AS account_id, sum(amount) AS total FROM public.{HOLDINGS_TABLE} "  # noqa: S608
                "WHERE tenant_id = :t GROUP BY 1"
            ),
            {"t": tenant},
        )
    }
    for cfg_code, ref in snap.config.accounts.items():
        gl = positions.gl_net(db, tenant, ref.id)
        total = Decimal(by_account.get(ref.id, 0))
        checks.append(
            {
                "check": "holding_account_equals_rows",
                "code": cfg_code,
                "ok": gl == total,
                "gl": fmt(gl),
                "rows": fmt(total),
            }
        )
    bad_state = list(
        db.execute(
            sa.text(
                "SELECT e.legacy_key, e.state, coalesce(sum(h.amount), 0) AS outstanding "  # noqa: S608
                f"FROM public.{m.EXCEPTIONS_TABLE} e JOIN public.{HOLDINGS_TABLE} h ON h.exception_id = e.id "
                "WHERE e.tenant_id = :t GROUP BY e.id, e.legacy_key, e.state "
                "HAVING (e.state = 'open' AND coalesce(sum(h.amount), 0) = 0) "
                "OR (e.state = 'resolved' AND coalesce(sum(h.amount), 0) <> 0)"
            ),
            {"t": tenant},
        )
    )
    checks.append({"check": "item_state_matches_outstanding", "ok": not bad_state, "violations": len(bad_state)})
    docs = list(
        db.execute(
            sa.text(
                f"SELECT journal_source_document_id AS d, count(DISTINCT journal_id) AS n FROM public.{HOLDINGS_TABLE} "  # noqa: S608
                "WHERE tenant_id = :t AND kind = 'reclass' GROUP BY 1"
            ),
            {"t": tenant},
        )
    )
    posted = {
        d: n
        for d, n in db.execute(
            sa.select(JournalEntry.source_document_id, sa.func.count())
            .where(
                JournalEntry.tenant_id == tenant,
                JournalEntry.source_owner == OWNER,
                JournalEntry.source_document_kind == KIND_HOLDING_POST,
                JournalEntry.status == JournalStatus.POSTED,
            )
            .group_by(JournalEntry.source_document_id)
        )
    }
    once = all(r.n == 1 and posted.get(r.d) == 1 for r in docs) and set(posted) == {r.d for r in docs}
    checks.append({"check": "each_holding_identity_posted_once", "ok": once})
    return {"ready": all(c["ok"] for c in checks), "checks": checks, "r1_fingerprint": snap.r1.position_fingerprint}
