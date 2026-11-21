"""Holdings through the UNPATCHED default billing reader, on the real a13 AR-opening lane (S9b d-rest).

The AR opening is applied for real (mint -> plan -> provision -> import -> seal). ``account_positions`` and
``window_manifest_fingerprint`` are bound to it (the admission rows and billing's sealed window), so a full T1 and a full
holding plan run through ``cli._reader`` with no fake billing. Every unavailable path refuses, never zero.
"""

from __future__ import annotations

# ruff: noqa: E501
import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance, PeriodStatus
from dotmac_accounting.models import Account, AccountCategory, FiscalPeriod, FiscalYear
from dotmac_kernel.cache import TenantScope
from sqlalchemy.orm import Session
from test_ar_opening_pg import (  # noqa: F401  (hosted-Postgres fixtures)
    NOW,
    _app_admin_provision_grants,
    _apply,
    _plan,
    _world,
)
from test_journals_pg import _registries  # noqa: F401  (setting registries)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.holdings import apply as applying
from erp_v2.holdings import billing as hbilling
from erp_v2.holdings import cli
from erp_v2.holdings import plan as planning
from erp_v2.holdings import posting as hposting
from erp_v2.holdings import t1 as t1_mod
from erp_v2.holdings.errors import HoldingRefused
from erp_v2.journals import policy as journal_policy
from erp_v2.posting.bridge import post_consequence


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


D = Decimal
SHA = "b" * 64
OP = "operator-a"


def _chart(engine, tenant):
    """The accounts, an open January 2026 period and NGN as functional currency, for the opening tenant."""
    accounts = {}
    with Session(engine) as db:
        cats = {}
        for code, klass in (("AST", AccountClass.ASSET), ("LIA", AccountClass.LIABILITY), ("EQU", AccountClass.EQUITY)):
            cats[code] = AccountCategory(tenant_id=tenant, code=code, name=code, account_class=klass)
            db.add(cats[code])
        db.flush()
        for code, name, cat, normal in (
            ("1400", "Trade receivables", "AST", NormalBalance.DEBIT),
            ("1410", "Staff loans", "AST", NormalBalance.DEBIT),
            ("1901", "AR migration clearing", "AST", NormalBalance.DEBIT),
            ("1902", "Staff-loan migration clearing", "AST", NormalBalance.DEBIT),
            ("2000", "AP", "LIA", NormalBalance.CREDIT),
            ("2040", "Payroll payable", "LIA", NormalBalance.CREDIT),
            ("2045", "Claims", "LIA", NormalBalance.CREDIT),
            ("2105", "Customer prepayments", "LIA", NormalBalance.CREDIT),
            ("2901", "AP migration clearing", "LIA", NormalBalance.CREDIT),
            ("2902", "Employee liabilities migration clearing", "LIA", NormalBalance.CREDIT),
            ("3100", "Retained", "EQU", NormalBalance.CREDIT),
        ):
            row = Account(
                tenant_id=tenant,
                category_id=cats[cat].id,
                code=code,
                name=name,
                kind=AccountKind.POSTING,
                normal_balance=normal,
            )
            db.add(row)
            db.flush()
            accounts[code] = row.id
        year = FiscalYear(
            tenant_id=tenant, code="FY26", name="FY 2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
        )
        db.add(year)
        db.flush()
        period = FiscalPeriod(
            tenant_id=tenant, fiscal_year_id=year.id, period_number=1, name="P1", start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31), is_adjustment=False, status=PeriodStatus.FUTURE,
        )  # fmt: skip
        db.add(period)
        db.flush()
        journal_policy.set_functional_currency(db, tenant_id=tenant, code="NGN", reason="test seed")
        accounting.open_period(db, scope=TenantScope(tenant), period_id=period.id, actor_ref="seed", occurred_at=NOW)
        db.commit()
    return accounts


def _post(engine, tenant, accounts, code, net: Decimal):
    debit, credit = (code, "3100") if net > 0 else ("3100", code)
    amount = abs(net)
    lines = (
        hposting.Line(str(accounts[debit]), amount, D(0), "seed"),
        hposting.Line(str(accounts[credit]), D(0), amount, "seed"),
    )
    source = hposting.Source("erp", "staff_loan_entry", str(uuid4()), "1", SHA)
    c = hposting.Consequence(source, date(2026, 1, 12), "seed", "NGN", D(1), lines, SHA)
    with Session(engine) as db:
        post_consequence(db, scope=TenantScope(tenant), consequence=c, approval_ref="seed", actor="seed", now=NOW)
        db.commit()


def _flip(engine, tmp_path):
    """A real opened-and-sealed AR window plus the chart, the legacy GL(1400) and the holdings run spec."""
    w = _world(engine)
    plan, oid = _plan(engine, w)
    receipt = _apply(engine, w, plan, oid)
    accounts = _chart(engine, w.tenant)
    controls = w.asm.manifest().controls
    parties = [
        {
            "customer": c.partition.source_account_key, "currency": "NGN",
            "gl_minor": c.collectible_receivable_minor - c.available_cash_credit_minor,
            "carried_minor": c.collectible_receivable_minor, "cash_lot_minor": c.available_cash_credit_minor,
        }
        for c in controls
    ]  # fmt: skip
    legacy_net = sum(p["gl_minor"] for p in parties)
    _post(engine, w.tenant, accounts, "1400", D(legacy_net).scaleb(-2))
    census = {
        "census_sha256": w.census.sha256,
        "gl_attribution": {"parties": parties, "held": [], "unattributed": [], "shared_ar_journal": []},
    }
    raw = json.dumps(census).encode()
    path = tmp_path / f"census-{uuid4().hex[:6]}.json"
    path.write_bytes(raw)
    import hashlib

    spec = {
        "version": 1, "tenant_id": str(w.tenant), "window_id": receipt["window_id"], "capture_date": "2026-01-20",
        "evidence_ref": "EVID-1", "params": {},
        "receipts": {"census": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}},
    }  # fmt: skip
    w.spec = json.dumps(spec).encode()
    w.receipt, w.accounts, w.parties = receipt, accounts, parties
    w.readers = {c: (lambda db, tenant, code, functional: {}) for c in ("2000", "2040", "2045", "1410")}
    return w


def _t1(engine, w):
    plan = t1_mod.run_t1_plan(engine, w.spec, cli.load_receipts, cli._reader(w.spec), planned_by=OP)
    spec, _ = planning.parse_spec(w.spec)
    with Session(engine) as db, db.begin():
        out = t1_mod.apply_t1(db, spec, plan, approved_sha=plan["plan_sha256"], applied_by=OP)
    return plan, out


# --- the happy path: nothing patched ----------------------------------------------------------------------------


def test_an_unpatched_default_reader_runs_the_t1_plan_and_the_holding_plan_on_the_real_opening(engine, tmp_path):
    w = _flip(engine, tmp_path)
    assert isinstance(cli.billing_reader(), hbilling.A13Billing)  # the default, not a fake
    plan, _ = _t1(engine, w)
    assert plan["plan"]["amount"] == "70.000000" or D(plan["plan"]["amount"]) == D("70")
    holding = applying.run_plan(
        engine, w.spec, cli.load_receipts, cli._reader(w.spec), planned_by=OP, owner_readers=w.readers
    )
    assert holding["plan_sha256"]
    spec, _ = planning.parse_spec(w.spec)
    with applying.read_only_session(engine) as db:
        verified = applying.verify(db, spec, cli._reader(w.spec), owner_readers=w.readers)
        db.rollback()
    ties = {c["code"]: c["ok"] for c in verified["checks"] if c["check"] == "control_equals_subledger"}
    assert ties["1400"] and ties[planning.PREPAYMENT]


def test_per_party_positions_equal_the_census_split(engine, tmp_path):
    w = _flip(engine, tmp_path)
    with Session(engine) as db:
        got = hbilling.A13Billing(w.tenant).account_positions(db)
        db.rollback()
    assert set(got) == {(p["customer"], "NGN") for p in w.parties}
    for p in w.parties:
        pos = got[(p["customer"], "NGN")]
        assert (pos.receivable(), pos.available_credit_minor) == (p["carried_minor"], p["cash_lot_minor"])


def test_the_window_manifest_fingerprint_is_the_sealed_windows_and_the_admission_rows(engine, tmp_path):
    w = _flip(engine, tmp_path)
    reader = hbilling.A13Billing(w.tenant)
    with applying.read_only_session(engine) as db:
        assert (
            reader.window_manifest_fingerprint(db, w.receipt["window_id"]) == w.receipt["window_manifest_fingerprint"]
        )
        db.rollback()


# --- fail closed --------------------------------------------------------------------------------------------------


def test_a_stray_billing_account_makes_the_holding_plan_refuse_billing_positions_mismatch(engine, tmp_path):
    """R1 sums every billing account; per-account positions cover only the admitted partitions. A non-zero account
    outside the admissions must refuse, not be ignored."""
    w = _flip(engine, tmp_path)
    _t1(engine, w)
    real = hbilling.A13Billing.account_positions

    def drop_one(self, db):
        out = dict(real(self, db))
        out.pop(next(iter(out)))
        return out

    import pytest as _p

    with _p.MonkeyPatch.context() as mp:
        mp.setattr(hbilling.A13Billing, "account_positions", drop_one)
        with pytest.raises(HoldingRefused) as e:
            applying.run_plan(
                engine, w.spec, cli.load_receipts, cli._reader(w.spec), planned_by=OP, owner_readers=w.readers
            )
    assert e.value.code == "billing_positions_mismatch"


def test_another_tenants_reader_sees_neither_this_tenants_window_nor_admissions(engine, tmp_path):
    w = _flip(engine, tmp_path)
    other = uuid4()
    with Session(engine) as db:
        with pytest.raises(HoldingRefused) as e:
            hbilling.A13Billing(other).account_positions(db)
        db.rollback()
    assert e.value.code == "billing_window_missing"
    with applying.read_only_session(engine) as db:
        with pytest.raises(HoldingRefused) as e:
            hbilling.A13Billing(other).window_manifest_fingerprint(db, w.receipt["window_id"])
        db.rollback()
    assert e.value.code == "billing_window_missing"


def test_a_wrong_window_id_and_an_unsealed_window_refuse(engine, tmp_path, monkeypatch):
    w = _flip(engine, tmp_path)
    reader = hbilling.A13Billing(w.tenant)
    with applying.read_only_session(engine) as db:
        with pytest.raises(HoldingRefused) as e:
            reader.window_manifest_fingerprint(db, str(uuid4()))
        assert e.value.code == "billing_window_mismatch"
        db.rollback()
    import dotmac_billing

    monkeypatch.setattr(dotmac_billing, "get_opening_window_seal", lambda db, *, scope: None)
    with applying.read_only_session(engine) as db:
        with pytest.raises(HoldingRefused) as e:
            reader.window_manifest_fingerprint(db, w.receipt["window_id"])
        assert e.value.code == "billing_window_not_sealed"
        db.rollback()
    with Session(engine) as db:
        with pytest.raises(HoldingRefused) as e:
            reader.account_positions(db)
        assert e.value.code == "billing_window_not_sealed"  # never zero positions from an unsealed window
        db.rollback()


def test_a_wrong_posture_refuses_and_never_reads_zero(engine, tmp_path):
    w = _flip(engine, tmp_path)
    with applying.read_only_session(engine) as db:  # account positions need read-write READ COMMITTED
        with pytest.raises(HoldingRefused) as e:
            hbilling.A13Billing(w.tenant).account_positions(db)
        db.rollback()
    assert e.value.code == "account_positions_unavailable"


def test_an_admission_on_another_window_refuses(engine, tmp_path, monkeypatch):
    """The admission rows are immutable (a trigger), so the window billing reports is the one that is swapped."""
    import dataclasses

    import dotmac_billing

    w = _flip(engine, tmp_path)
    real = dotmac_billing.get_opening_window

    def other_window(db, *, scope):
        window = real(db, scope=scope)
        return dataclasses.replace(window, window_id=uuid4())

    monkeypatch.setattr(dotmac_billing, "get_opening_window", other_window)
    with Session(engine) as db:
        with pytest.raises(HoldingRefused) as e:
            hbilling.A13Billing(w.tenant).account_positions(db)
        db.rollback()
    assert e.value.code == "admission_window_mismatch"


def test_a_duplicate_admission_key_refuses_because_assemble_would_silently_overwrite(engine, tmp_path):
    """The table is unique per billing account, NOT per (key, currency): a second row for the same partition key is
    possible, and a dict keyed by (key, currency) would hide one of them. The reader must refuse."""
    from erp_v2.importer.ar_opening_models import ArOpeningAdmission

    w = _flip(engine, tmp_path)
    with Session(engine) as db:
        first = db.scalars(sa.select(ArOpeningAdmission).where(ArOpeningAdmission.tenant_id == w.tenant)).first()
        db.add(
            ArOpeningAdmission(
                tenant_id=w.tenant, source_account_key=first.source_account_key, currency=first.currency,
                billing_account_id=uuid4(), billing_receipt_id=uuid4(), posting_group_id=uuid4(), window_id=first.window_id,
                command_digest=first.command_digest, manifest_fingerprint=first.manifest_fingerprint,
                census_sha256=first.census_sha256, snapshot_sha256=first.snapshot_sha256, plan_sha256=first.plan_sha256,
                applied_by="tester",
            )
        )  # fmt: skip
        db.commit()
    with Session(engine) as db:
        with pytest.raises(HoldingRefused) as e:
            hbilling.A13Billing(w.tenant).account_positions(db)
        db.rollback()
    assert e.value.code == "account_positions_key_duplicate"


# --- (d2): the flip gate ties 1400 and the prepayment account to real billing R1 ----------------------------------


def _gate(engine, w):
    from erp_v2.migration_ledger import ledger_check

    with Session(engine) as db:
        return ledger_check.control_state(db, w.tenant)


def test_the_gate_ties_1400_and_the_prepayment_account_to_billing_r1_after_t1(engine, tmp_path):
    w = _flip(engine, tmp_path)
    before = _gate(engine, w)  # legacy GL still carries the cash-lot credit inside 1400: not yet tied
    assert before["1400"]["ok"] is False and before["1400"]["reason"] == "control_not_tied:1400"
    assert before["prepayment"]["reason"] == "control_not_tied:prepayment"
    _t1(engine, w)
    after = _gate(engine, w)
    assert after["1400"]["ok"] and after["prepayment"]["ok"], after
    assert (
        D(after["1400"]["gl"])
        == D(after["1400"]["subledger"])
        == D(sum(p["carried_minor"] for p in w.parties)).scaleb(-2)
    )
    assert (
        D(after["prepayment"]["gl"])
        == D(after["prepayment"]["subledger"])
        == -D(sum(p["cash_lot_minor"] for p in w.parties)).scaleb(-2)
    )


def test_the_gate_refuses_1400_on_a_difference_and_when_the_ledger_moves_during_the_read(engine, tmp_path, monkeypatch):
    w = _flip(engine, tmp_path)
    _t1(engine, w)
    _post(engine, w.tenant, w.accounts, "1400", D("1.00"))  # a 1.00 difference against billing
    state = _gate(engine, w)
    assert state["1400"]["ok"] is False and state["1400"]["reason"] == "control_not_tied:1400"
    real = hbilling.A13Billing.r1

    def moving(self, db):
        out = real(self, db)
        _post(engine, w.tenant, w.accounts, "1400", D("0.01"))  # the ledger moves while R1 is being read
        return out

    monkeypatch.setattr(hbilling.A13Billing, "r1", moving)
    state = _gate(engine, w)
    assert state["1400"] == {**state["1400"], "ok": False, "subledger": None, "reason": "moved_during_gate:1400"}


def test_a_foreign_currency_billing_balance_has_no_tie_and_refuses(engine, tmp_path, monkeypatch):
    w = _flip(engine, tmp_path)
    real = hbilling.A13Billing.r1

    def with_usd(self, db):
        out = real(self, db)
        return hbilling.R1Snapshot(out.position_fingerprint, {**out.currencies, "USD": hbilling.Position(5, 0, 0, 0)})

    monkeypatch.setattr(hbilling.A13Billing, "r1", with_usd)
    assert _gate(engine, w)["1400"]["reason"] == "control_subledger_fx:1400"
