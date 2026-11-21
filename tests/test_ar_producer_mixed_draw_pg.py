"""B-16 review Finding 1: a draw that mixes opening cash and receipt money, on real PostgreSQL (hosted CI).

The producer tenant gets a real importer opening (census -> plan -> provision -> import -> seal), which leaves an
opening cash lot. Two receipts are then posted unallocated and one invoice issued; a manual credit apply draws the
opening lot AND both receipts in one posting group. Everything below is what the S11 cutover will exercise.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from ar_opening_support import CUTOFF, SNAPSHOT, alloc, capture, invoice, payment
from dotmac_billing import models as bm
from dotmac_kernel.cache import TenantScope
from sqlalchemy.orm import Session
from test_ar_opening_pg import (  # noqa: F401  (module fixture: billing's provision step runs as app_admin)
    _app_admin_provision_grants,
    _apply,
    _plan,
)
from test_ar_producer_pg import (  # noqa: F401  (module fixture: audit/setting registries)
    act,
    configure,
    line,
    post_receipt,
    refused,
    registries,
    world,
)

from erp_v2.ar import queries, service, settings
from erp_v2.importer.ar_assembly import ArAssembly
from erp_v2.importer.ar_census import build_census, normalise_rows
from erp_v2.importer.ar_opening_cli import mint_billing_accounts

engine = bridge_pg.engine  # disposable-database fixture under its own name

DAY = date(2026, 1, 12)


def _import_opening(engine, env):
    """Opening plan/apply/seal on the producer tenant: customer C-1 has one legacy payment of 450.00 of which 400.00
    was allocated, i.e. a 50.00 opening cash lot, plus an invoice the lot could not reach."""

    key = str(env.customers["C-1"])
    invoices = [invoice("inv-1", customer=key, total="1000.00", posted=True)]
    payments = [payment("pay-1", "450.00", customer=key, posted=True)]
    allocations = [alloc("pay-1", "inv-1", "400.00")]
    census = build_census(
        snapshot_sha256="b" * 64,
        cutoff=CUTOFF,
        invoices=invoices,
        payments=payments,
        allocations=allocations,
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    scope = TenantScope(env.tenant_id)
    with Session(engine) as db, db.begin():
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(env.tenant_id)})
        minted = mint_billing_accounts(db, scope=scope, partitions=list(census.carry_partitions))
    ni, np_, na = normalise_rows(invoices, payments, allocations)
    asm = ArAssembly(
        tenant_id=env.tenant_id,
        capture=capture(),
        census=census,
        invoices=ni,
        payments=np_,
        allocations=na,
        account_ids=minted,
        customer_snapshot=lambda _k: SNAPSHOT,
        seller=SNAPSHOT,
    )
    w = SimpleNamespace(tenant=env.tenant_id, scope=scope, census=census, asm=asm, minted=minted)
    plan, oid = _plan(engine, w, "off")  # the producer tenant's two-person policy is OFF (the default)

    receipt = _apply(engine, w, plan, oid, dual=False)
    assert receipt["sealed"]
    return minted[(key, "NGN")]


def _lots(db, env) -> tuple[Decimal, Decimal]:
    """(opening cash lot remaining, available-credit lane total of the account: opening lot + every live receipt)."""
    opening = db.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(bm.OpeningCashEffect.amount_delta), 0)).where(
            bm.OpeningCashEffect.tenant_id == env.tenant_id
        )
    )
    available = db.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(bm.PostingEffect.amount_delta), 0)).where(
            bm.PostingEffect.tenant_id == env.tenant_id, bm.PostingEffect.lane == "available_credit"
        )
    )
    return Decimal(opening), Decimal(available)


def _groups(db, env, *, kind: str | None = None, ref_prefix: str | None = None) -> list[bm.PostingGroup]:
    stmt = sa.select(bm.PostingGroup).where(bm.PostingGroup.tenant_id == env.tenant_id)
    if kind is not None:
        stmt = stmt.where(bm.PostingGroup.group_kind == kind)
    if ref_prefix is not None:
        stmt = stmt.where(bm.PostingGroup.source_ref.like(ref_prefix + "%"))
    return list(db.scalars(stmt))


def _outstanding(db, env, doc):
    return queries.exposure(db, env.tenant_id, doc).outstanding


def test_a_draw_mixing_opening_and_receipt_money_is_one_unit_reversed_whole_and_a_bounce_undoes_it_once(engine):
    """Finding 1 of the B-16 review: per-row deallocate refused; reverse_draw restores every source and a replay is a
    no-op; bouncing receipt A reverses the draw once, then the settlement."""

    env = world(engine, dual=False)  # the producer tenant's two-person policy is OFF (the default)
    configure(engine, env, **{settings.KEY_AUTO_APPLY: False})  # the draw below is a deliberate manual apply
    account = _import_opening(engine, env)
    receipt_a, _ = post_receipt(engine, env, "30.00", reference="BANK-A")
    post_receipt(engine, env, "20.00", reference="BANK-B")
    draft = act(
        engine,
        env,
        "preparer",
        service.create_draft,
        request_id=uuid4(),
        kind="invoice",
        customer_account_id=env.customers["C-1"],
        document_date=DAY,
        lines=[line(unit="150", env=env)],
    )
    doc = act(engine, env, "approver", service.issue_draft, draft_id=draft.id).document_id
    with Session(engine) as db:
        assert _lots(db, env) == (Decimal(50), Decimal(100))  # opening lot 50 + receipt A 30 + receipt B 20
        assert _outstanding(db, env, doc) == Decimal("150.00")

    # --- one manual apply of 100 draws all three sources in ONE posting group
    act(
        engine,
        env,
        "approver",
        service.apply_credit,
        billing_account_id=account,
        document_id=doc,
        amount=Decimal("100.00"),
        occurred_on=DAY,
        request_id=uuid4(),
    )
    with Session(engine) as db:
        assert _lots(db, env) == (Decimal(0), Decimal(0))
        assert _outstanding(db, env, doc) == Decimal("50.00")
        ((group, remaining),) = queries.opening_draws(db, env.tenant_id, doc)
        assert remaining == Decimal("100.00")  # billing's own observation of the whole draw, opening cash included
        rows = queries.allocation_rows(db, env.tenant_id, document_id=doc)
        assert {r.posting_group_id for r in rows} == {group}
        assert sorted(r.amount_delta for r in rows) == [Decimal(20), Decimal(30)]  # the receipt legs; opening has none
        assert len(_groups(db, env, kind="opening_cash_applied")) == 1
        row_ids = [r.id for r in rows]

    # --- a per-row deallocate is refused and changes nothing
    for allocation_id in row_ids:
        assert (
            refused(
                engine,
                env,
                "approver",
                service.deallocate,
                allocation_id=allocation_id,
                occurred_on=DAY,
                request_id=uuid4(),
            )
            == "opening_draw_unit"
        )
    with Session(engine) as db:
        assert _lots(db, env) == (Decimal(0), Decimal(0))
        assert _outstanding(db, env, doc) == Decimal("50.00")
        assert queries.draw_remaining(db, env.tenant_id, group, doc) == Decimal("100.00")

    # --- reverse_draw restores every source at once; a replay is a no-op
    first = act(engine, env, "approver", service.reverse_draw, group_id=group, document_id=doc, occurred_on=DAY)
    assert first.replayed is False
    with Session(engine) as db:
        assert _lots(db, env) == (Decimal(50), Decimal(100))  # opening lot back, both receipts available again
        assert _outstanding(db, env, doc) == Decimal("150.00")
        assert queries.draw_remaining(db, env.tenant_id, group, doc) == Decimal(0)
        groups_after_first = len(_groups(db, env))
        assert len(_groups(db, env, kind="cash_credit_draw_reversed")) == 1
    again = act(engine, env, "approver", service.reverse_draw, group_id=group, document_id=doc, occurred_on=DAY)
    assert again.replayed is True
    with Session(engine) as db:
        assert len(_groups(db, env)) == groups_after_first
        assert _lots(db, env) == (Decimal(50), Decimal(100))

    # --- draw again, then bounce receipt A: the whole draw is reversed ONCE (opening cash and receipt B too), then
    # --- the settlement. Receipt B and the opening lot are available again; only A's money is gone.
    act(
        engine,
        env,
        "approver",
        service.apply_credit,
        billing_account_id=account,
        document_id=doc,
        amount=Decimal("100.00"),
        occurred_on=DAY,
        request_id=uuid4(),
    )
    with Session(engine) as db:
        live = [(g, r) for g, r in queries.opening_draws(db, env.tenant_id, doc) if r]
        assert len(live) == 1 and live[0][0] != group
        second_draw = live[0][0]
        assert _lots(db, env) == (Decimal(0), Decimal(0))
    bounce = act(
        engine,
        env,
        "approver",
        service.reverse_receipt,
        receipt_draft_id=receipt_a.id,
        reason="bounced",
        occurred_on=DAY,
    )
    assert bounce.replayed is False
    with Session(engine) as db:
        settlement_ref = f"settlement:{bounce.settlement_id}"
        assert queries.draw_remaining(db, env.tenant_id, second_draw, doc) == Decimal(0)
        assert len(_groups(db, env, ref_prefix=f"erp_v2.ar.rdraw:{bounce.settlement_id}:")) == 1  # once
        assert len(_groups(db, env, kind="cash_credit_draw_reversed")) == 2  # the manual one + the bounce's
        settlement_group = queries.posting_group_for_source(db, env.tenant_id, settlement_ref)
        assert queries.group_reversed(db, env.tenant_id, settlement_group.id)
        assert len(_groups(db, env, kind="reversal")) == 1
        assert _lots(db, env) == (Decimal(50), Decimal(70))  # opening 50 + receipt B 20; receipt A's 30 is reversed
        assert _outstanding(db, env, doc) == Decimal("150.00")
        groups_after_bounce = len(_groups(db, env))
    replay = act(
        engine,
        env,
        "approver",
        service.reverse_receipt,
        receipt_draft_id=receipt_a.id,
        reason="bounced",
        occurred_on=DAY,
    )
    assert replay.replayed is True
    with Session(engine) as db:
        assert len(_groups(db, env)) == groups_after_bounce
        assert _lots(db, env) == (Decimal(50), Decimal(70))
