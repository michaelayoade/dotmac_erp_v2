"""B-15 rev 2 on real PostgreSQL: cash-basis output VAT through the real billing lane (a11 commands) and the real
accounting commands. The mapper runs as ``app_user`` (W4/W6 grants are exercised). Each step ties GL 1400, the
prepayment account and the VAT position exactly.

Hosted CI only (ERP_TEST_POSTGRES_URL).
"""

from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from billing_gl_support import (
    allocate,
    at,
    credit_note,
    deallocate,
    new_account,
    new_invoice,
    positions,
    receipt,
    void,
)
from dotmac_billing.models import BillingAccount
from dotmac_kernel.cache import TenantScope
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_billing_gl_pg import (  # noqa: F401  (hosted-Postgres fixtures and helpers)
    RUN_AT,
    _registries,
    configure,
    dispositions,
    gl,
    rows,
    run,
    runtime_session,
    seed_billing,
    with_lines,
    world,
)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.billing_gl import vat_opening
from erp_v2.billing_gl.models import VatMovement, VatPosition
from erp_v2.journals import service as journals
from erp_v2.journals.service import JournalRefused

D = Decimal


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


def account_and_model(engine, env):
    with Session(engine) as db:
        account = new_account(db, TenantScope(env.tenant_id))
        db.commit()
        return account.id


def invoice(engine, env, account_id, *, when=0, lines=True, **line):
    issued = seed_billing(
        engine, env, lambda db, sc: new_invoice(db, sc, db.get(BillingAccount, account_id), when=at(when))
    )
    return with_lines(engine, env, issued, **line) if lines else issued


def vat_state(engine, env, document_id):
    with Session(engine) as db:
        position = db.scalars(
            sa.select(VatPosition).where(VatPosition.tenant_id == env.tenant_id, VatPosition.document_id == document_id)
        ).one()
        moves = db.scalars(sa.select(VatMovement).where(VatMovement.position_id == position.id)).all()
    remaining = sum((D(m.deferral_delta) for m in moves), D(0))
    return D(position.deferred), remaining


def tie_all(engine, env, account_ids, document_id=None):
    with Session(engine) as db:
        pos = positions(db, env.tenant_id, account_ids)
    bal = gl(engine, env)
    assert bal.get("1400", D(0)) == pos["receivable"]
    assert -bal.get("2100", D(0)) == pos["available_credit"]
    if document_id is not None:
        _, remaining = vat_state(engine, env, document_id)
        assert -bal.get("1455", D(0)) == remaining  # GL 1455 equals the position's R exactly
    return pos, bal


def test_three_payments_release_exactly_d_and_close_out(engine):
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct)
    settlement = seed_billing(
        engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, acct), "500.00", when=at(1))
    )
    run(engine, env)
    _, bal = tie_all(engine, env, [acct], inv.id)
    assert bal["1400"] == D("120.00") and bal["4000"] == D("-100.00") and bal["1455"] == D("-20.00")
    released = []
    for hours, amount in ((2, "50.00"), (3, "40.00"), (4, "30.00")):
        seed_billing(engine, env, lambda db, sc, a=amount, h=hours: allocate(db, sc, settlement, inv, a, when=at(h)))
        run(engine, env)
        _, bal = tie_all(engine, env, [acct], inv.id)
        released.append(-bal.get("1440", D(0)))
    assert released == [D("8.33"), D("15.00"), D("20.00")]  # 8.33, +6.67, then the closing payment takes the 5.00
    assert bal.get("1455", D(0)) == 0 and -bal["1440"] == D("20.00")
    assert dispositions(engine, env) == {"POSTED": 5}


def test_full_deallocation_unreleases_exactly_and_a_replay_is_a_no_op(engine):
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct)
    settlement = seed_billing(
        engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, acct), "500.00", when=at(1))
    )
    first = seed_billing(engine, env, lambda db, sc: allocate(db, sc, settlement, inv, "50.00", when=at(2)))
    seed_billing(engine, env, lambda db, sc: allocate(db, sc, settlement, inv, "40.00", when=at(3)))
    run(engine, env)
    tie_all(engine, env, [acct], inv.id)
    seed_billing(engine, env, lambda db, sc: deallocate(db, sc, first, "50.00", when=at(4)))
    run(engine, env)
    _, bal = tie_all(engine, env, [acct], inv.id)
    assert -bal["1440"] == D("6.67") and -bal["1455"] == D("13.33")  # only the 40 allocation remains released
    snapshot = (gl(engine, env), len(rows(engine, env)))
    run(engine, env)
    assert (gl(engine, env), len(rows(engine, env))) == snapshot


def test_credit_note_reduces_the_deferral_and_the_closing_payment_takes_the_rest(engine):
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct)
    settlement = seed_billing(
        engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, acct), "200.00", when=at(1))
    )
    note = seed_billing(engine, env, lambda db, sc: credit_note(db, sc, inv, pre_tax="50.00", tax="10.00", when=at(2)))
    with_lines(engine, env, note, pre_tax="50.00", tax="10.00")
    run(engine, env)
    _, bal = tie_all(engine, env, [acct], inv.id)
    assert bal["1400"] == D("60.00") and bal["4000"] == D("-50.00") and bal["1455"] == D("-10.00")
    seed_billing(engine, env, lambda db, sc: allocate(db, sc, settlement, inv, "60.00", when=at(3)))
    run(engine, env)
    _, bal = tie_all(engine, env, [acct], inv.id)
    assert bal.get("1455", D(0)) == 0 and bal["1440"] == D("-10.00") and bal["1400"] == 0


def test_void_is_the_exact_negation_of_the_posted_journal(engine):
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct)
    run(engine, env)
    assert gl(engine, env)["1455"] == D("-20.00")
    seed_billing(engine, env, lambda db, sc: void(db, sc, inv, when=at(2)))
    report = run(engine, env)
    assert dispositions(engine, env) == {"POSTED": 2}, report["blocked_by_reason"]
    assert all(total == 0 for total in gl(engine, env).values())
    deferred, remaining = vat_state(engine, env, inv.id)
    assert (deferred, remaining) == (D("20.00"), D(0))


def test_unmapped_revenue_or_tax_blocks_that_account_only_and_heals(engine):
    env = world(engine)
    blocked_acct, other = account_and_model(engine, env), account_and_model(engine, env)
    no_lines = invoice(engine, env, blocked_acct, lines=False)  # revenue account unmapped
    invoice(engine, env, other, when=1)
    report = run(engine, env)
    assert report["blocked_by_reason"] == {"BLOCKED:revenue_account_unmapped": 1}
    assert dispositions(engine, env) == {"BLOCKED": 1, "POSTED": 1}
    # While a VAT fact is blocked the period cannot soft-close.
    with Session(engine) as db, pytest.raises(JournalRefused) as refused:
        journals.soft_close_period(
            db, tenant_id=env.tenant_id, period_id=env.periods[1], actor=env.parties["approver"], review_ref="R1"
        )
    assert refused.value.code == "billing_facts_incomplete"
    with_lines(engine, env, no_lines)
    healed = run(engine, env)
    assert dispositions(engine, env) == {"POSTED": 2} and healed["blocked"] == 0
    # An unmapped tax treatment blocks the same way and heals after config.
    configure(engine, env, tax_accounts={})
    third = account_and_model(engine, env)
    invoice(engine, env, third, when=2)
    assert run(engine, env)["blocked_by_reason"] == {"BLOCKED:tax_account_unmapped": 1}
    configure(
        engine,
        env,
        tax_accounts={"standard": {"current": str(env.accounts["1440"]), "deferral": str(env.accounts["1455"])}},
    )
    assert run(engine, env)["blocked"] == 0 and dispositions(engine, env) == {"POSTED": 3}


def test_line_accounts_that_disagree_with_the_document_fact_block_without_absorbing(engine):
    env = world(engine)
    acct = account_and_model(engine, env)
    invoice(engine, env, acct, pre_tax="99.99", tax="20.00")  # one cent off the document: never absorbed
    assert run(engine, env)["blocked_by_reason"] == {"BLOCKED:line_accounts_mismatch": 1}
    assert gl(engine, env) == {}


def test_opening_position_releases_vat_on_payment(engine):
    """A migrated invoice (carried by #72, so its issue fact is SKIP) gets its remaining deferral loaded by the
    opening loader (Q1); a payment then releases it 1455 -> 1440 from that position."""
    env = world(engine, enabled=False)  # the loader runs at F7, before F10 enables the mapper
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct, lines=False)
    # The mapper dispositions the issue fact SKIP (as for a carried opening document): record it as the mapper would.
    run(engine, env, enable_once=True)
    with engine.begin() as conn:
        # The owner role plays the mapper here: SKIP of a V1 fact is not something the runtime role may write, so
        # the guard trigger is lifted for this one fixture statement and restored immediately.
        conn.execute(sa.text("ALTER TABLE erp_billing_fact_postings DISABLE TRIGGER erp_billing_fact_postings_guard"))
        conn.execute(
            sa.text(
                "UPDATE erp_billing_fact_postings SET disposition = 'SKIP', reason = 'opening_gl_carried' "
                "WHERE tenant_id = :t AND effect_kind = 'invoice_issued'"
            ),
            {"t": env.tenant_id},
        )
        conn.execute(sa.text("ALTER TABLE erp_billing_fact_postings ENABLE TRIGGER erp_billing_fact_postings_guard"))
    spec = {
        "version": 1,
        "tenant_id": str(env.tenant_id),
        "items": [
            {
                "document_id": str(inv.id),
                "treatment": "standard",
                "total": "120.00",
                "tax": "20.00",
                "open_initial": "120.00",
                "journal_1455_credit": "20.00",
                "reclass": [],
                "deferral_account": str(env.accounts["1455"]),
                "current_account": str(env.accounts["1440"]),
                "legacy_ref": "INV-OPEN-1",
                "finance_override": None,
            }
        ],
    }
    import json

    parsed = vat_opening.parse(json.dumps(spec).encode())
    planned = vat_opening.plan(parsed, "d" * 64)
    with Session(engine) as db:
        assert vat_opening.apply(db, env.tenant_id, planned) == {"written": 1, "already_loaded": 0}
        db.commit()
    with Session(engine) as db:
        assert vat_opening.apply(db, env.tenant_id, planned) == {"written": 0, "already_loaded": 1}  # replay
        db.commit()
    settlement = seed_billing(
        engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, acct), "120.00", when=at(2))
    )
    seed_billing(engine, env, lambda db, sc: allocate(db, sc, settlement, inv, "120.00", when=at(3)))
    configure(engine, env, enabled=True)  # F10
    run(engine, env)
    deferred, remaining = vat_state(engine, env, inv.id)
    assert (deferred, remaining) == (D("20.00"), D(0))
    bal = gl(engine, env)
    assert bal["1440"] == D("-20.00") and bal["1455"] == D("20.00")  # the migrated 1455 balance moves to 1440


def test_triggers_refuse_over_release_update_and_delete_and_runtime_has_insert_only(engine):
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct)
    run(engine, env)
    with Session(engine) as db:
        position = db.scalars(sa.select(VatPosition).where(VatPosition.tenant_id == env.tenant_id)).one()
        move = db.scalars(sa.select(VatMovement).where(VatMovement.position_id == position.id)).first()
    forbidden = (
        ("UPDATE erp_billing_vat_movements SET deferral_delta = 0 WHERE id = :i", {"i": move.id}),
        ("DELETE FROM erp_billing_vat_movements WHERE id = :i", {"i": move.id}),
        ("UPDATE erp_billing_vat_positions SET deferred = 1 WHERE id = :i", {"i": position.id}),
        ("DELETE FROM erp_billing_vat_positions WHERE id = :i", {"i": position.id}),
    )
    for sql, params in forbidden:
        with Session(engine) as db, pytest.raises(DBAPIError):
            db.execute(sa.text(sql), params)
            db.commit()
    # Over-release: a movement that takes R below zero is refused by the database whatever wrote it.
    with Session(engine) as db, pytest.raises(DBAPIError) as over:
        db.add(
            VatMovement(
                tenant_id=env.tenant_id,
                position_id=position.id,
                fact_id=uuid4(),
                fact_version=1,
                kind="release",
                deferral_delta=D("-20.01"),
                current_delta=D("20.01"),
                allocation_ref="x",
            )
        )
        db.commit()
    assert "vat_invariant" in str(over.value)
    with runtime_session(engine, env) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(VatPosition)) == 1
        for sql in ("UPDATE erp_billing_vat_movements SET kind = 'defer'", "DELETE FROM erp_billing_vat_positions"):
            with pytest.raises(DBAPIError, match="permission denied"), db.begin_nested():
                db.execute(sa.text(sql))
    assert inv is not None


def test_downgrade_refuses_while_vat_rows_exist(engine):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    env = world(engine)
    acct = account_and_model(engine, env)
    invoice(engine, env, acct)
    run(engine, env)
    migration = importlib.import_module("erp_v2.migrations.versions.e2_0033_billing_gl_vat")
    with engine.connect() as conn:
        with (
            Operations.context(MigrationContext.configure(conn)),
            pytest.raises(RuntimeError, match="refusing to drop"),
        ):
            migration.downgrade()
        conn.rollback()


# ------------------------------------------------ review findings (architect, 2026-11-19)


def _raw_lines(engine, env, document_id, rows):
    with Session(engine) as db:
        for number, (account, pre_tax, tax) in enumerate(rows, 1):
            db.execute(
                sa.text(
                    "INSERT INTO erp_ar_line_accounts (id, tenant_id, billing_document_id, line_number, "
                    "revenue_account_id, pre_tax, tax, treatment_code) VALUES (:i, :t, :d, :n, :a, :p, :x, 'standard')"
                ),
                {"i": uuid4(), "t": env.tenant_id, "d": document_id, "n": number, "a": account, "p": pre_tax, "x": tax},
            )
        db.commit()


def test_an_allocation_sorting_before_its_invoice_waits_and_does_not_block_the_account(engine):
    """Finding 7: the allocation's occurred_at precedes the invoice's issue time, so it is processed first."""
    env = world(engine)
    acct = account_and_model(engine, env)
    settlement = seed_billing(
        engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, acct), "500.00", when=at(1))
    )
    inv = invoice(engine, env, acct, when=5)
    seed_billing(engine, env, lambda db, sc: allocate(db, sc, settlement, inv, "50.00", when=at(2)))
    first = run(engine, env)
    assert first["blocked"] == 0, first["blocked_by_reason"]  # the account is NOT blocked; the invoice could post
    assert first["blocked_by_reason"] == {"WAITING:waiting_document": 1}
    second = run(engine, env)  # the retry now finds the invoice's position
    assert second["waiting"] == 0 and dispositions(engine, env) == {"POSTED": 3}
    tie_all(engine, env, [acct], inv.id)


def test_an_erp_receipt_takes_its_cash_from_its_receipt_row_and_never_from_a_setting(engine):
    env = world(engine)
    configure(engine, env, cash_account={"erp_v2.ar": str(env.accounts["1010"])})  # a typed bank must NOT be used
    acct = account_and_model(engine, env)
    settlement = seed_billing(
        engine,
        env,
        lambda db, sc: receipt(db, sc, db.get(BillingAccount, acct), "30.00", when=at(1), source="erp_v2.ar"),
    )
    assert run(engine, env)["blocked_by_reason"] == {"BLOCKED:receipt_cash_unresolved": 1}
    assert gl(engine, env) == {}
    with Session(engine) as db:
        db.execute(
            sa.text(
                "INSERT INTO erp_ar_receipts (id, tenant_id, billing_settlement_id, cash_account_id) "
                "VALUES (:i, :t, :s, :c)"
            ),
            {"i": uuid4(), "t": env.tenant_id, "s": settlement.id, "c": env.accounts["1010"]},
        )
        db.commit()
    assert run(engine, env)["blocked"] == 0 and gl(engine, env)["1010"] == D("30.00")


def test_a_credit_note_is_compared_by_sums_not_line_counts(engine):
    """B-16 writes one row per credit-note line; billing writes one line per credit note (#78 review)."""
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct)
    note = seed_billing(engine, env, lambda db, sc: credit_note(db, sc, inv, pre_tax="50.00", tax="10.00", when=at(2)))
    _raw_lines(engine, env, note.id, [(env.accounts["4000"], "30.00", "6.00"), (env.accounts["4000"], "20.00", "4.00")])
    run(engine, env)
    assert dispositions(engine, env) == {"POSTED": 2}
    tie_all(engine, env, [acct], inv.id)
    # Sums that disagree still block.
    other = account_and_model(engine, env)
    inv2 = invoice(engine, env, other, when=3)
    note2 = seed_billing(
        engine, env, lambda db, sc: credit_note(db, sc, inv2, pre_tax="50.00", tax="10.00", when=at(4))
    )
    _raw_lines(
        engine, env, note2.id, [(env.accounts["4000"], "30.00", "6.00"), (env.accounts["4000"], "19.99", "4.00")]
    )
    assert run(engine, env)["blocked_by_reason"] == {"BLOCKED:line_accounts_mismatch": 1}


def test_an_unknown_or_inactive_revenue_account_blocks(engine):
    env = world(engine)
    unknown, inactive = account_and_model(engine, env), account_and_model(engine, env)
    seed_billing(engine, env, lambda db, sc: new_invoice(db, sc, db.get(BillingAccount, unknown), when=at(1)))
    first = seed_billing(engine, env, lambda db, sc: new_invoice(db, sc, db.get(BillingAccount, inactive), when=at(2)))
    # (fixture rows: the first invoice's document id is found through the posting-group read below)
    with Session(engine) as db:
        from dotmac_billing.models import BillingDocument

        docs = {
            d.billing_account_id: d.id
            for d in db.scalars(sa.select(BillingDocument).where(BillingDocument.tenant_id == env.tenant_id))
        }
    _raw_lines(engine, env, docs[unknown], [(uuid4(), "100.00", "20.00")])  # an id that is no account
    with Session(engine) as db:
        db.execute(
            sa.text("UPDATE mod_accounting.accounts SET is_active = false WHERE id = :a"), {"a": env.accounts["4000"]}
        )
        db.commit()
    _raw_lines(engine, env, first.id, [(env.accounts["4000"], "100.00", "20.00")])
    report = run(engine, env)
    assert report["blocked_by_reason"] == {"BLOCKED:revenue_account_invalid": 2} and gl(engine, env) == {}


def test_the_runtime_role_cannot_create_opening_positions_or_opening_movements(engine):
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct)
    run(engine, env)
    with Session(engine) as db:
        native = db.scalars(sa.select(VatPosition).where(VatPosition.tenant_id == env.tenant_id)).one()
    with runtime_session(engine, env) as db:
        with pytest.raises(DBAPIError, match="migration role"), db.begin_nested():
            db.execute(
                sa.text(
                    "INSERT INTO erp_billing_vat_positions (id, tenant_id, document_id, treatment_code, origin, total, "
                    "deferred, open_initial, source) VALUES (gen_random_uuid(), :t, gen_random_uuid(), 'standard', "
                    "'opening', 10, 0, 10, 'x')"
                ),
                {"t": env.tenant_id},
            )
        with pytest.raises(DBAPIError, match="migration role"), db.begin_nested():
            db.execute(
                sa.text(
                    "INSERT INTO erp_billing_vat_movements (id, tenant_id, position_id, fact_id, fact_version, kind, "
                    "deferral_delta, current_delta) VALUES (gen_random_uuid(), :t, :p, gen_random_uuid(), 0, 'defer', "
                    "0, 0)"
                ),
                {"t": env.tenant_id, "p": native.id},
            )
    assert inv is not None


def test_a_vat_persist_failure_blocks_that_fact_only_and_rolls_its_journal_back(engine, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from erp_v2.billing_gl import vat_plan

    env = world(engine)
    bad, good = account_and_model(engine, env), account_and_model(engine, env)
    invoice(engine, env, bad, when=1)
    seed_billing(engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, good), "9.00", when=at(2)))
    real = vat_plan.persist

    def failing(db, tenant, fact_id, version, journal_id, spec):
        if spec.new_positions:
            raise IntegrityError("insert", {}, Exception("vat_invariant"))
        return real(db, tenant, fact_id, version, journal_id, spec)

    monkeypatch.setattr(vat_plan, "persist", failing)
    report = run(engine, env)  # does not abort: the other account still posts
    assert report["blocked_by_reason"] == {"BLOCKED:vat_invariant": 1}
    assert dispositions(engine, env) == {"BLOCKED": 1, "POSTED": 1}
    assert set(gl(engine, env)) == {"1010", "2100"}  # the invoice's journal rolled back with its savepoint


def test_opening_lot_applications_release_and_unrelease_the_loaded_vat(engine):
    """Spec 4.8 / finding 6: an application from an opening cash lot against a migrated invoice. The V4 fact is a
    handcrafted stand-in (the opening-cash machinery is not driven here); the mapper path, plan and journal are real."""
    from types import SimpleNamespace

    from dotmac_billing import AccountingFactCursorV1, AccountingFactIndexEntryV1

    from erp_v2.billing_gl import mapper as mapper_module
    from erp_v2.billing_gl.mapper import Mapper

    env = world(engine, enabled=False)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct, lines=False)  # a billing document standing in for the migrated invoice
    item = {
        "document_id": str(inv.id),
        "treatment": "standard",
        "total": "120.00",
        "tax": "20.00",
        "open_initial": "120.00",
        "journal_1455_credit": "20.00",
        "reclass": [],
        "deferral_account": str(env.accounts["1455"]),
        "current_account": str(env.accounts["1440"]),
        "legacy_ref": "INV-OPEN-2",
        "finance_override": None,
    }
    import json

    planned = vat_opening.plan(
        vat_opening.parse(json.dumps({"version": 1, "tenant_id": str(env.tenant_id), "items": [item]}).encode()),
        "d" * 64,
    )
    with Session(engine) as db:
        vat_opening.apply(db, env.tenant_id, planned)
        db.commit()
    configure(engine, env, enabled=True)  # F10

    def money(amount):
        return SimpleNamespace(amount=D(amount), currency=SimpleNamespace(code="NGN", minor_units=2))

    def v4(kind, lanes, effects):
        return SimpleNamespace(
            effect_kind=kind,
            committed_at=RUN_AT,
            source_ref="opening-lot-draw",
            effects=tuple(SimpleNamespace(lane=lane, amount_delta=money(d)) for lane, d in lanes),
            settlement_allocations=(),
            opening_cash_effects=effects,
        )

    effect_id = uuid4()
    application = SimpleNamespace(
        effect_kind="application",
        effect_id=effect_id,
        document_id=inv.id,
        amount=money("60.00"),
        original_effect_id=None,
    )
    inverse = SimpleNamespace(
        effect_kind="application_inverse",
        effect_id=uuid4(),
        document_id=inv.id,
        amount=money("60.00"),
        original_effect_id=effect_id,
    )
    facts = {
        "opening_cash_applied": v4(
            "opening_cash_applied", [("available_credit", "-60.00"), ("receivable", "-60.00")], (application,)
        ),
        "cash_credit_draw_reversed": v4(
            "cash_credit_draw_reversed", [("available_credit", "60.00"), ("receivable", "60.00")], (inverse,)
        ),
    }
    monkeypatch_target = mapper_module

    def process(kind, hours):
        entry = AccountingFactIndexEntryV1(
            AccountingFactCursorV1(at(hours), acct, 1, 4, uuid4()), uuid4(), kind, "dotmac-billing", "x", "d" * 64
        )
        original = monkeypatch_target.get_accounting_fact_v4
        monkeypatch_target.get_accounting_fact_v4 = lambda db, scope, posting_group_id: facts[kind]
        try:
            with Session(engine) as db:
                db.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(env.tenant_id)})
                outcome = Mapper(db, env.tenant_id, now=RUN_AT, commit=lambda: None).process(entry)
                db.commit()
        finally:
            monkeypatch_target.get_accounting_fact_v4 = original
        return outcome

    assert process("opening_cash_applied", 3) == "POSTED"
    _, remaining = vat_state(engine, env, inv.id)
    bal = gl(engine, env)
    assert remaining == D("10.00") and bal["1455"] == D("10.00")  # the carried 1455 is debited by the release
    assert -bal["1440"] == D("10.00")  # half of the remaining deferral on a half payment: released, not left on 1455
    assert process("cash_credit_draw_reversed", 4) == "POSTED"
    _, remaining = vat_state(engine, env, inv.id)
    assert remaining == D("20.00") and gl(engine, env).get("1440", D(0)) == 0  # exactly un-released


# ------------------------------------------------ re-review R5-R9 (architect, 2026-11-20)


def test_voiding_a_credit_note_after_a_deallocation_keeps_gl_and_positions_tied(engine):
    """R5: issue, pay, credit with a close-out, deallocate (uncloseout), void the credit note. The void's VAT lines come
    from the movements still in force; negating the old journal in full would drift GL 1455/1440 from R."""
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct)
    settlement = seed_billing(
        engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, acct), "200.00", when=at(1))
    )
    first = seed_billing(engine, env, lambda db, sc: allocate(db, sc, settlement, inv, "90.00", when=at(2)))
    note = seed_billing(engine, env, lambda db, sc: credit_note(db, sc, inv, pre_tax="29.00", tax="1.00", when=at(3)))
    with_lines(engine, env, note, pre_tax="29.00", tax="1.00")
    run(engine, env)
    _, bal = tie_all(engine, env, [acct], inv.id)
    assert bal.get("1455", D(0)) == 0  # the credit closed the invoice out
    seed_billing(engine, env, lambda db, sc: deallocate(db, sc, first, "90.00", when=at(4)))
    run(engine, env)
    tie_all(engine, env, [acct], inv.id)
    seed_billing(engine, env, lambda db, sc: void(db, sc, note, when=at(5)))
    report = run(engine, env)
    assert report["blocked"] == 0, report["blocked_by_reason"]
    _, remaining = vat_state(engine, env, inv.id)
    _, bal = tie_all(engine, env, [acct], inv.id)  # GL 1455 == -R exactly
    assert -bal.get("1455", D(0)) == remaining and remaining == D(
        "20.00"
    )  # the invoice is as if never credited or paid


def test_a_login_member_of_app_user_without_bypassrls_is_refused_opening_rows(engine):
    """R6: prod logs in as a member of app_user (never current_user = app_user); the guard follows rolbypassrls."""
    env = world(engine)
    acct = account_and_model(engine, env)
    invoice(engine, env, acct)
    run(engine, env)
    with Session(engine) as db:
        position = db.scalars(sa.select(VatPosition).where(VatPosition.tenant_id == env.tenant_id)).one()
    role = f"erp_rt_{uuid4().hex[:10]}"
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOBYPASSRLS IN ROLE app_user')
        conn.exec_driver_sql(f'GRANT "{role}" TO CURRENT_USER WITH SET TRUE')
    try:
        with engine.connect() as conn, Session(bind=conn) as db:
            db.execute(sa.text(f'SET ROLE "{role}"'))
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(env.tenant_id)})
            assert db.scalar(sa.text("SELECT current_user")) == role  # NOT 'app_user'
            with pytest.raises(DBAPIError, match="migration role"), db.begin_nested():
                db.execute(
                    sa.text(
                        "INSERT INTO erp_billing_vat_positions (id, tenant_id, document_id, treatment_code, origin, "
                        "total, deferred, open_initial, source) VALUES (gen_random_uuid(), :t, gen_random_uuid(), "
                        "'standard', 'opening', 10, 0, 10, 'x')"
                    ),
                    {"t": env.tenant_id},
                )
            with pytest.raises(DBAPIError, match="migration role"), db.begin_nested():
                db.execute(
                    sa.text(
                        "INSERT INTO erp_billing_vat_movements (id, tenant_id, position_id, fact_id, fact_version, "
                        "kind, deferral_delta, current_delta) VALUES (gen_random_uuid(), :t, :p, gen_random_uuid(), "
                        "0, 'defer', 0, 0)"
                    ),
                    {"t": env.tenant_id, "p": position.id},
                )
            db.rollback()
            conn.execute(sa.text("RESET ROLE"))
            conn.commit()
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP OWNED BY "{role}"')
            conn.exec_driver_sql(f'DROP ROLE "{role}"')


def test_the_opening_loader_refuses_after_the_flip(engine):
    from erp_v2.migration_ledger.common import LedgerRefused

    env = world(engine, enabled=False)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct, lines=False)
    spec = {
        "version": 1,
        "tenant_id": str(env.tenant_id),
        "items": [
            {
                "document_id": str(inv.id),
                "treatment": "standard",
                "total": "120.00",
                "tax": "20.00",
                "open_initial": "120.00",
                "journal_1455_credit": "20.00",
                "reclass": [],
                "deferral_account": str(env.accounts["1455"]),
                "current_account": str(env.accounts["1440"]),
                "legacy_ref": "INV-LATE",
                "finance_override": None,
            }
        ],
    }
    import json

    planned = vat_opening.plan(vat_opening.parse(json.dumps(spec).encode()), "d" * 64)
    # A payment already exists on the listed document: the exposure is no longer the opening exposure.
    settlement = seed_billing(
        engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, acct), "50.00", when=at(1))
    )
    seed_billing(engine, env, lambda db, sc: allocate(db, sc, settlement, inv, "50.00", when=at(2)))
    with Session(engine) as db, pytest.raises(LedgerRefused) as paid:
        vat_opening.apply(db, env.tenant_id, planned)
    assert paid.value.code == "vat_opening_after_flip"
    # The mapper being enabled (F10) refuses too.
    configure(engine, env, enabled=True)
    with Session(engine) as db, pytest.raises(LedgerRefused) as flipped:
        vat_opening.apply(db, env.tenant_id, planned)
    assert flipped.value.code == "vat_opening_after_flip"


def test_only_the_vat_trigger_message_is_a_vat_invariant(engine, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from erp_v2.billing_gl import vat_plan

    env = world(engine)
    acct = account_and_model(engine, env)
    invoice(engine, env, acct)

    def failing(db, tenant, fact_id, version, journal_id, spec):
        raise IntegrityError("insert", {}, Exception("some other constraint"))

    monkeypatch.setattr(vat_plan, "persist", failing)
    assert run(engine, env)["blocked_by_reason"] == {"BLOCKED:integrity_refused": 1}
