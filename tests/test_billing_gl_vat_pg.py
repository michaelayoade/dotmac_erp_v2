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
    env = world(engine)
    acct = account_and_model(engine, env)
    inv = invoice(engine, env, acct, lines=False)
    # The mapper dispositions the issue fact SKIP (as for a carried opening document): record it as the mapper would.
    run(engine, env, enable_once=True)
    with Session(engine) as db:
        db.execute(
            sa.text(
                "UPDATE erp_billing_fact_postings SET disposition = 'SKIP', reason = 'opening_gl_carried' "
                "WHERE tenant_id = :t AND effect_kind = 'invoice_issued'"
            ),
            {"t": env.tenant_id},
        )
        db.commit()
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
