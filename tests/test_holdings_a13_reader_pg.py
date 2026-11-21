"""The holdings billing reader bound to billing a13's real R1 (``get_receivable_control_position_v1``), on real PG.

Real billing a13 commands (the B-16 producer issues invoices and posts receipts) create the positions; the adapter reads
them back in a REPEATABLE READ READ ONLY transaction. The 1400 tie is ``holdings.apply.verify``'s
``control_equals_subledger`` check for 1400 (GL(1400) against R1 collectible less non-cash): it passes when the two are
equal and refuses on a difference. When billing cannot answer the reader still fails closed.
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import NormalBalance
from dotmac_accounting.models import AccountCategory
from dotmac_billing.errors import BillingRuleViolation
from dotmac_kernel.cache import TenantScope
from sqlalchemy.orm import Session
from test_ar_producer_pg import (  # noqa: F401  (hosted-Postgres fixtures)
    DAY,
    _account,
    issue_invoice,
    line,
    post_receipt,
    registries,
    world,
)
from test_posting_bridge_pg import NOW

from erp_v2.holdings import apply as applying
from erp_v2.holdings import billing as hbilling
from erp_v2.holdings import plan as planning
from erp_v2.holdings import posting as hposting
from erp_v2.holdings.errors import HoldingRefused
from erp_v2.posting.bridge import post_consequence

D = Decimal
SHA = "b" * 64
engine = bridge_pg.engine  # disposable-database fixture under its own name
WINDOW = "6f1d0a52-0c6e-4a58-9d0a-3a7c0f0e1b11"


def _env(engine):
    """The B-16 producer world plus the accounts the holdings tie reads (controls, prepayment, clearing)."""
    env = world(engine)
    with Session(engine) as db:
        cats = {
            c.code: c for c in db.scalars(sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id))
        }
        for code, name, cat, normal in (
            ("2000", "AP", "LIA", NormalBalance.CREDIT),
            ("2040", "Payroll", "LIA", NormalBalance.CREDIT),
            ("2045", "Claims", "LIA", NormalBalance.CREDIT),
            ("1410", "Staff loans", "AST", NormalBalance.DEBIT),
            ("2105", "Customer prepayments", "LIA", NormalBalance.CREDIT),
            ("2901", "AP migration clearing", "LIA", NormalBalance.CREDIT),
            ("1901", "AR migration clearing", "AST", NormalBalance.DEBIT),
            ("2902", "Employee liabilities migration clearing", "LIA", NormalBalance.CREDIT),
            ("1902", "Staff-loan migration clearing", "AST", NormalBalance.DEBIT),
        ):
            if code not in env.accounts:
                _account(db, env, cats[cat], code, name, normal)
        db.commit()
    return env


def _gl(engine, env, code, amount: Decimal):
    """Dr ``code`` / Cr 4000 through the posting bridge (a v2-native posting, as the holdings tests seed)."""
    lines = (
        hposting.Line(str(env.accounts[code]), amount, D(0), "seed"),
        hposting.Line(str(env.accounts["4000"]), D(0), amount, "seed"),
    )
    source = hposting.Source("erp", "staff_loan_entry", str(uuid4()), "1", SHA)
    c = hposting.Consequence(source, DAY, "seed", "NGN", D(1), lines, SHA)
    with Session(engine) as db:
        post_consequence(
            db, scope=TenantScope(env.tenant_id), consequence=c, approval_ref="seed", actor="seed", now=NOW
        )
        db.commit()


def _spec(env):
    raw = json.dumps(
        {
            "version": 1, "tenant_id": str(env.tenant_id), "window_id": WINDOW, "capture_date": "2026-01-20",
            "evidence_ref": "EVID-1", "params": {}, "receipts": {},
        }
    ).encode()  # fmt: skip
    return planning.parse_spec(raw)[0]


def _readers():
    return {c: (lambda db, tenant, code, functional: {}) for c in ("2000", "2040", "2045", "1410")}


def _tie_1400(engine, env, reader):
    spec = _spec(env)
    with applying.read_only_session(engine) as db:
        out = applying.verify(db, spec, reader, owner_readers=_readers())
        db.rollback()
    return next(c for c in out["checks"] if c["check"] == "control_equals_subledger" and c["code"] == "1400")


def _r1(engine, env, reader=None):
    reader = reader or hbilling.A13Billing(env.tenant_id)
    with applying.read_only_session(engine) as db:
        try:
            return reader.r1(db)
        finally:
            db.rollback()


def test_default_reader_is_the_a13_adapter_not_the_unavailable_stub():
    assert isinstance(hbilling.default_reader(), hbilling.A13Billing)
    bound = hbilling.default_reader().for_tenant(uuid4())
    assert isinstance(bound, hbilling.A13Billing) and bound.tenant_id is not None


def test_r1_returns_the_real_positions_and_a_stable_fingerprint_that_moves_with_billing(engine):
    env = _env(engine)
    assert _r1(engine, env).currencies == {}  # no billing history: a missing currency is zero, not a refusal
    issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-1")
    issue_invoice(engine, env, [line(unit="50", env=env)], customer="C-2")
    first, again = _r1(engine, env), _r1(engine, env)
    assert first.currencies == {"NGN": hbilling.Position(15000, 0, 0, 0)}
    assert first.position_fingerprint == again.position_fingerprint and len(first.position_fingerprint) == 64
    post_receipt(engine, env, "40", customer="C-1", reference="BANK-REF-A13-1")  # unapplied cash: available credit
    moved = _r1(engine, env)
    assert moved.currencies["NGN"].available_credit_minor == 4000
    assert moved.currencies["NGN"].receivable() == 15000
    assert moved.position_fingerprint != first.position_fingerprint


def test_the_1400_tie_passes_when_control_equals_the_subledger(engine):
    env = _env(engine)
    issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-1")
    issue_invoice(engine, env, [line(unit="50", env=env)], customer="C-2")
    _gl(engine, env, "1400", D("150.00"))
    check = _tie_1400(engine, env, hbilling.A13Billing(env.tenant_id))
    assert check["ok"] is True and D(check["gl"]) == D(check["sub"]) == D("150.00")


def test_the_1400_tie_refuses_on_a_difference(engine):
    env = _env(engine)
    issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-1")
    _gl(engine, env, "1400", D("110.00"))  # GL is 10.00 above billing
    check = _tie_1400(engine, env, hbilling.A13Billing(env.tenant_id))
    assert check["ok"] is False and (D(check["gl"]), D(check["sub"])) == (D("110.00"), D("100.00"))


def test_the_cli_binds_the_run_spec_tenant_to_the_default_reader(engine, monkeypatch):
    from erp_v2.holdings import cli

    env = _env(engine)
    issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-1")
    raw = json.dumps(
        {"version": 1, "tenant_id": str(env.tenant_id), "window_id": WINDOW, "capture_date": "2026-01-20",
         "evidence_ref": "E", "params": {}, "receipts": {}}
    ).encode()  # fmt: skip
    reader = cli._reader(raw)
    assert isinstance(reader, hbilling.A13Billing) and reader.tenant_id == env.tenant_id
    assert _r1(engine, env, reader).currencies["NGN"].collectible_minor == 10000
    monkeypatch.setattr(cli, "billing_reader", lambda: object())  # a reader with no ``for_tenant`` is used as is
    assert cli._reader(raw) is not None


# --- fail closed ------------------------------------------------------------------------------------------------


def test_a_read_write_or_read_committed_session_refuses_r1_unavailable(engine):
    env = _env(engine)
    issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-1")
    with Session(engine) as db:  # READ COMMITTED read-write: billing refuses the posture
        with pytest.raises(HoldingRefused) as e:
            hbilling.A13Billing(env.tenant_id).r1(db)
        db.rollback()
    assert e.value.code == "r1_unavailable"


def test_an_unbound_or_mismatching_tenant_refuses_and_a_session_tenant_can_bind(engine):
    env = _env(engine)
    issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-1")
    with pytest.raises(HoldingRefused) as e:
        _r1(engine, env, hbilling.A13Billing())
    assert e.value.code == "r1_tenant_unbound"
    with applying.read_only_session(engine) as db:
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(uuid4())})
        with pytest.raises(HoldingRefused) as e:
            hbilling.A13Billing(env.tenant_id).r1(db)
        db.rollback()
    assert e.value.code == "r1_tenant_mismatch"
    with applying.read_only_session(engine) as db:  # unbound adapter, tenant taken from the session
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(env.tenant_id)})
        assert hbilling.A13Billing().r1(db).currencies["NGN"].collectible_minor == 10000
        db.rollback()


def test_when_billing_cannot_answer_the_reader_refuses_and_the_tie_never_reads_zero(engine, monkeypatch):
    env = _env(engine)
    issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-1")

    def broken(db, *, scope):
        raise BillingRuleViolation("control_position_incomplete", "partial history")

    monkeypatch.setattr("dotmac_billing.get_receivable_control_position_v1", broken)
    with pytest.raises(HoldingRefused) as e:
        _tie_1400(engine, env, hbilling.A13Billing(env.tenant_id))
    assert e.value.code == "r1_unavailable"


def test_a_currency_with_a_non_ngn_precision_refuses(engine, monkeypatch):
    env = _env(engine)
    issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-1")
    import dotmac_billing as b

    real = b.get_receivable_control_position_v1

    def three_decimals(db, *, scope):
        from dataclasses import replace

        out = real(db, scope=scope)
        return replace(out, currencies=tuple(replace(c, precision=3) for c in out.currencies))

    monkeypatch.setattr("dotmac_billing.get_receivable_control_position_v1", three_decimals)
    with pytest.raises(HoldingRefused) as e:
        _r1(engine, env)
    assert e.value.code == "r1_precision_unsupported"
