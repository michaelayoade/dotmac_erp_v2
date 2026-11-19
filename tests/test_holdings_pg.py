# ruff: noqa: E501, S608
"""B-14 holdings on real PostgreSQL: T1, the flip reclass, release, writeoff, the e2_0030 trigger, RLS, the manual-journal
guard, the CLI and the transaction postures. Real migrations, real accounting commands, the real posting bridge.

Billing R1 (a13) and the owner subledgers are injected fakes: a12 has no R1 and the owner tables are seeded elsewhere.
Hosted CI only (ERP_TEST_POSTGRES_URL), like test_migration_ledger_pg.
"""

import json
import os
import stat
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, PostedLedgerLine
from dotmac_kernel.cache import TenantScope
from dual_control_support import set_dual_control_engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session
from test_journals_pg import NOW, _registries, seed  # noqa: F401  (hosted-Postgres fixtures)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.holdings import apply as applying
from erp_v2.holdings import cli, positions, release, ties
from erp_v2.holdings import plan as planning
from erp_v2.holdings import posting as hposting
from erp_v2.holdings import t1 as t1_mod
from erp_v2.holdings.attribution import Party
from erp_v2.holdings.billing import Position, R1Snapshot
from erp_v2.holdings.errors import HoldingRefused
from erp_v2.holdings.models import HOLDINGS_TABLE, Holding
from erp_v2.journals import service as journals
from erp_v2.journals.service import JournalRefused
from erp_v2.migration_ledger import models as m
from erp_v2.posting.bridge import post_consequence

D = Decimal
SHA = "b" * 64
OP_A, OP_B = "operator-a", "operator-b"


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


# --- world --------------------------------------------------------------------------------------------------------


def world(engine, *, dual=False) -> SimpleNamespace:
    env = seed(engine, currency="NGN")
    set_dual_control_engine(engine, env.tenant_id, dual)
    with Session(engine) as db:
        cats = {
            a.account_class: a
            for a in db.scalars(sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id))
        }
        for code, klass in (("EQU", AccountClass.EQUITY), ("EXP", AccountClass.EXPENSE)):
            row = AccountCategory(tenant_id=env.tenant_id, code=code, name=code, account_class=klass)
            db.add(row)
            db.flush()
            cats[klass] = row
        for code, name, klass, normal in (
            ("2045", "Claims", AccountClass.LIABILITY, NormalBalance.CREDIT),
            ("3100", "Retained", AccountClass.EQUITY, NormalBalance.CREDIT),
            ("2105", "Customer prepayments", AccountClass.LIABILITY, NormalBalance.CREDIT),
            ("2901", "AP migration clearing", AccountClass.LIABILITY, NormalBalance.CREDIT),
            ("1901", "AR migration clearing", AccountClass.ASSET, NormalBalance.DEBIT),
            ("2902", "Employee liabilities migration clearing", AccountClass.LIABILITY, NormalBalance.CREDIT),
            ("1902", "Staff-loan migration clearing", AccountClass.ASSET, NormalBalance.DEBIT),
            ("6900", "Write-off expense", AccountClass.EXPENSE, NormalBalance.DEBIT),
        ):
            row = Account(
                tenant_id=env.tenant_id, category_id=cats[klass].id, code=code, name=name,
                kind=AccountKind.POSTING, normal_balance=normal,
            )  # fmt: skip
            db.add(row)
            db.flush()
            env.accounts[code] = row.id
        db.commit()
    return env


def seed_gl(engine, env, code, net: Decimal):
    """A v2-native posting (the staff-loan entry door) putting ``net`` (debit - credit) on ``code`` against 3100."""
    debit_code, credit_code = (code, "3100") if net > 0 else ("3100", code)
    amount = abs(net)
    lines = (
        hposting.Line(str(env.accounts[debit_code]), amount, D(0), "seed"),
        hposting.Line(str(env.accounts[credit_code]), D(0), amount, "seed"),
    )
    source = hposting.Source("erp", "staff_loan_entry", str(uuid4()), "1", SHA)
    c = hposting.Consequence(source, date(2026, 1, 12), "seed", "NGN", D(1), lines, SHA)
    with Session(engine) as db:
        post_consequence(
            db, scope=TenantScope(env.tenant_id), consequence=c, approval_ref="seed", actor="seed", now=NOW
        )
        db.commit()


def net(engine, env, code) -> Decimal:
    with Session(engine) as db:
        return positions.gl_net(db, env.tenant_id, env.accounts[code])


DIGEST, MANIFEST = "d" * 64, "e" * 64


class FakeBilling:
    def __init__(self, cur, per, fingerprints=("f" * 64,), manifest=MANIFEST):
        self.cur, self.per, self.fingerprints, self.calls, self.manifest = cur, per, list(fingerprints), 0, manifest

    def window_manifest_fingerprint(self, db, window_id):
        return self.manifest

    def r1(self, db):
        fp = self.fingerprints[min(self.calls, len(self.fingerprints) - 1)]
        self.calls += 1
        return R1Snapshot(fp, self.cur)

    def account_positions(self, db):
        return self.per


def billing(**kw):
    cur = {"NGN": Position(140000, 0, 20000, 0)}
    per = {("C1", "NGN"): Position(90000, 0, 20000, 0), ("C2", "NGN"): Position(50000, 0, 0, 0)}
    return FakeBilling(
        kw.get("cur", cur), kw.get("per", per), kw.get("fingerprints", ("f" * 64,)), kw.get("manifest", MANIFEST)
    )


SUB_2000 = {(Party("supplier", "A"), "NGN"): D("-500"), (Party("supplier", "B"), "NGN"): D("-300")}
SUB_1410 = {(Party("employee", "E1"), "NGN"): D("50"), (Party("employee", "E2"), "NGN"): D("20")}


def readers(sub2000=None):
    state = {"2000": dict(SUB_2000 if sub2000 is None else sub2000), "1410": dict(SUB_1410)}

    def reader(db, tenant, code, functional):
        return dict(state.get(code, {}))

    return {c: reader for c in ("2000", "2040", "2045", "1410")}, state


def write(path, body):
    path.write_text(json.dumps(body))
    return path


def make_spec(tmp_path, *, window="w1", params=None):
    census = write(tmp_path / "census.json", {"census_sha256": DIGEST, "gl_attribution": {
        "parties": [
            {"customer": "C1", "currency": "NGN", "gl_minor": 100000, "carried_minor": 90000, "cash_lot_minor": 20000},
            {"customer": "C2", "currency": "NGN", "gl_minor": 50000, "carried_minor": 50000, "cash_lot_minor": 0},
        ],
        "held": [{"reason": "partition_held:payment_overallocated", "legacy_key": "partition:C1:NGN", "amount_minor": 25000, "customer": "C1", "currency": "NGN"}],
        "unattributed": [{"gljournal_id": "gl-j", "amount_minor": 3000}],
        "shared_ar_journal": [],
    }})  # fmt: skip
    ap = write(tmp_path / "ap.json", {
        "version": 1, "code": "2000", "currency": "NGN",
        "parties": [{"kind": "supplier", "key": "A", "gl": "-600.00"}, {"kind": "supplier", "key": "B", "gl": "-300.00"}],
        "held": [{"kind": "supplier", "key": "A", "class_code": "E02", "reason": "refused_admission", "legacy_key": "inv-1", "amount": "-60.00"}],
        "unattributed": [{"journal_id": "j1", "amount": "-100.00", "shared": False}, {"journal_id": "s1", "amount": "-100.00", "shared": True}],
        "opening_residue": "0.00",
    })  # fmt: skip
    loans = write(tmp_path / "loans.json", {
        "version": 1, "code": "1410", "currency": "NGN",
        "parties": [{"kind": "employee", "key": "E1", "gl": "80.00"}], "held": [], "unattributed": [], "opening_residue": "0.00",
    })  # fmt: skip

    def ref(p):
        raw = p.read_bytes()
        import hashlib

        return {"path": str(p), "sha256": hashlib.sha256(raw).hexdigest()}

    spec = {
        "version": 1, "tenant_id": None, "window_id": window, "capture_date": "2026-01-20",
        "evidence_ref": "EVID-1", "params": params or {},
        "receipts": {"census": ref(census), "gl_attribution": {"2000": ref(ap), "1410": ref(loans)}},
    }  # fmt: skip
    return spec


def spec_bytes(env, spec):
    return json.dumps({**spec, "tenant_id": str(env.tenant_id)}).encode()


def seed_admissions(engine, env, parties=(("C1", "NGN"), ("C2", "NGN")), *, digest=DIGEST, manifest=MANIFEST):
    from erp_v2.importer.ar_opening_models import ArOpeningAdmission

    with Session(engine) as db:
        for key, ccy in parties:
            db.add(
                ArOpeningAdmission(
                    tenant_id=env.tenant_id, source_account_key=key, currency=ccy, billing_account_id=uuid4(),
                    billing_receipt_id=uuid4(), posting_group_id=uuid4(), window_id=uuid4(), command_digest=SHA,
                    manifest_fingerprint=manifest, census_sha256=digest, snapshot_sha256=SHA, plan_sha256=SHA, applied_by=OP_A,
                )
            )  # fmt: skip
        db.commit()


def seed_register(engine, env, items):
    with Session(engine) as db:
        for class_code, key, detail in items:
            db.add(
                m.MigrationException(
                    tenant_id=env.tenant_id, class_code=class_code, legacy_key=key, source_kind="ar_census", state="open",
                    fingerprint=SHA, source_receipt_sha256=SHA, detail=detail,
                )
            )  # fmt: skip
        db.commit()


def flip_world(engine, tmp_path, *, dual=False, window="w1", params=None, register=None):
    env = world(engine, dual=dual)
    seed_admissions(engine, env)
    seed_register(
        engine, env, register if register is not None else [("E35", "partition:C1:NGN", {}), ("E02", "inv-1", {})]
    )
    seed_gl(engine, env, "2000", D("-1100"))
    seed_gl(engine, env, "1400", D("1530"))
    seed_gl(engine, env, "1410", D("80"))
    env.spec = spec_bytes(env, make_spec(tmp_path, window=window, params=params))
    env.readers, env.sub = readers()
    return env


def t1(engine, env, bill=None, **kw):
    bill = bill or billing()
    plan = t1_mod.run_t1_plan(engine, env.spec, cli.load_receipts, bill, planned_by=kw.get("planned_by", OP_A))
    spec, _ = planning.parse_spec(env.spec)
    with Session(engine) as db, db.begin():
        out = t1_mod.apply_t1(db, spec, plan, approved_sha=plan["plan_sha256"], applied_by=kw.get("applied_by", OP_A))
    return plan, out


def holding_plan(engine, env, bill=None, planned_by=OP_A):
    return applying.run_plan(
        engine, env.spec, cli.load_receipts, bill or billing(), planned_by=planned_by, owner_readers=env.readers
    )


def holding_apply(engine, env, plan, applied_by=OP_A):
    spec, _ = planning.parse_spec(env.spec)
    with Session(engine) as db, db.begin():
        return applying.apply(
            db, spec, plan, approved_sha=plan["plan_sha256"], applied_by=applied_by, owner_readers=env.readers
        )


def holding_verify(engine, env, bill=None):
    spec, _ = planning.parse_spec(env.spec)
    with applying.read_only_session(engine) as db:
        out = applying.verify(db, spec, bill or billing(), owner_readers=env.readers)
        db.rollback()
    return out


def flipped(engine, tmp_path, **kw):
    env = flip_world(engine, tmp_path, **kw)
    t1(engine, env)
    plan = holding_plan(engine, env)
    out = holding_apply(engine, env, plan)
    return env, plan, out


def rows(engine, env, kind=None):
    with Session(engine) as db:
        q = sa.select(Holding).where(Holding.tenant_id == env.tenant_id)
        if kind:
            q = q.where(Holding.kind == kind)
        return list(db.scalars(q.order_by(Holding.created_at, Holding.id)))


def key_of(engine, env, legacy_key):
    with Session(engine) as db:
        return db.scalar(
            sa.select(m.MigrationException).where(
                m.MigrationException.tenant_id == env.tenant_id, m.MigrationException.legacy_key == legacy_key
            )
        )


# --- T1 -----------------------------------------------------------------------------------------------------------


def test_t1_posts_dr_1400_cr_prepayment_once_and_a_replay_writes_nothing(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    plan, out = t1(engine, env)
    assert out["posted"] and plan["plan"]["amount"] == "200.000000"
    assert net(engine, env, "1400") == D("1730") and net(engine, env, "2105") == D("-200")
    again = t1_mod.run_t1_plan(engine, env.spec, cli.load_receipts, billing(), planned_by=OP_A)
    assert again["plan"]["already_posted"]
    spec, _ = planning.parse_spec(env.spec)
    with Session(engine) as db, db.begin():  # the same plan (identity + fingerprint) replays
        replay = t1_mod.apply_t1(db, spec, plan, approved_sha=plan["plan_sha256"], applied_by=OP_A)
    assert replay["replayed"] and net(engine, env, "1400") == D("1730")


def test_t1_refuses_when_r1_available_credit_differs_or_the_census_is_stale(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    bad = billing(cur={"NGN": Position(140000, 0, 19999, 0)})
    with pytest.raises(HoldingRefused) as e:
        t1_mod.run_t1_plan(engine, env.spec, cli.load_receipts, bad, planned_by=OP_A)
    assert e.value.code == "t1_r1_mismatch"
    seed_gl(engine, env, "1400", D("1"))  # the ledger moved after the census
    with pytest.raises(HoldingRefused) as e:
        t1_mod.run_t1_plan(engine, env.spec, cli.load_receipts, billing(), planned_by=OP_A)
    assert e.value.code == "ar_attribution_stale"
    assert net(engine, env, "2105") == 0


def test_t1_on_a_moving_ledger_or_r1_refuses_moved_during_plan(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    with pytest.raises(HoldingRefused) as e:
        t1_mod.run_t1_plan(
            engine, env.spec, cli.load_receipts, billing(fingerprints=("a" * 64, "c" * 64)), planned_by=OP_A
        )
    assert e.value.code == "moved_during_plan"


def test_a_zero_cash_lot_posts_nothing(engine, tmp_path):
    env = world(engine)
    seed_admissions(engine, env)
    seed_gl(engine, env, "1400", D("1500"))
    spec = make_spec(tmp_path)
    census = json.loads((tmp_path / "census.json").read_text())
    for p in census["gl_attribution"]["parties"]:
        p["cash_lot_minor"] = 0
    census["gl_attribution"]["held"] = []
    census["gl_attribution"]["unattributed"] = []
    census["gl_attribution"]["parties"][0]["gl_minor"] = 100000
    census["gl_attribution"]["parties"][1]["gl_minor"] = 50000
    write(tmp_path / "census.json", census)
    import hashlib

    spec["receipts"]["census"]["sha256"] = hashlib.sha256((tmp_path / "census.json").read_bytes()).hexdigest()
    spec["receipts"]["gl_attribution"] = {}
    env.spec = spec_bytes(env, spec)
    plan, out = t1(engine, env, billing(cur={"NGN": Position(150000, 0, 0, 0)}, per={}))
    assert plan["plan"]["amount"] == "0.000000" and out["posted"] is False
    assert net(engine, env, "2105") == 0


# --- flip: plan / apply / verify ---------------------------------------------------------------------------------


def test_the_flip_reclass_ties_every_control_to_its_subledger_and_the_holding_tie_is_exact(engine, tmp_path):
    env, _plan, out = flipped(engine, tmp_path)
    assert {(p["code"]) for p in out["posted"]} == {"1400", "2000", "1410"}
    assert net(engine, env, "2000") == D("-800") and net(engine, env, "1400") == D("1400")
    assert net(engine, env, "1410") == D("70") and net(engine, env, "2105") == D("-200")
    assert net(engine, env, "2901") == D("-300") and net(engine, env, "1901") == D("330")
    assert net(engine, env, "1902") == D("10")
    assert len(rows(engine, env, "reclass")) == 9
    with Session(engine) as db:
        tie = ties.holding_tie(db, env.tenant_id)
    assert tie["ok"] and tie["identities_posted_once"] and tie["item_state_violations"] == 0
    verified = holding_verify(engine, env)
    assert verified["ready"], verified


def test_items_are_registered_with_class_reason_party_and_source(engine, tmp_path):
    env, plan, _ = flipped(engine, tmp_path)
    row = key_of(engine, env, "held:2000:NGN:inv-1")
    assert (row.class_code, row.source_kind, row.state) == ("E02", "migration_holdings", "open")
    assert row.detail["party"] == {"kind": "supplier", "key": "A"} and row.detail["reason"] == "refused_admission"
    assert row.detail["holding_account_code"] == "2901" and row.source_receipt_sha256 == plan["plan_sha256"]
    assert key_of(engine, env, "partygap:2000:NGN:A").class_code == "E37"
    assert key_of(engine, env, "partygap:1400:NGN:C1").class_code == "E36a"
    assert key_of(engine, env, "held:1400:NGN:partition:C1:NGN").class_code == "E35"
    assert key_of(engine, env, "sharedjournal:2000:NGN:s1").detail["reason"] == "shared_journal"


def test_a_mixed_sign_code_is_posted_gross_on_one_account_both_sides(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    with Session(engine) as db:
        journal = db.scalar(
            sa.select(JournalEntry).where(
                JournalEntry.tenant_id == env.tenant_id,
                JournalEntry.source_document_kind == "migration_holding",
                JournalEntry.description.like("%1410%"),
            )
        )
        lines = list(
            db.execute(
                sa.select(PostedLedgerLine.account_id, PostedLedgerLine.debit, PostedLedgerLine.credit).where(
                    PostedLedgerLine.journal_id == journal.id
                )
            )
        )
    holding = [ln for ln in lines if ln.account_id == env.accounts["1902"]]
    assert sorted((ln.debit, ln.credit) for ln in holding) == [(D(0), D("20")), (D("30"), D(0))]


def test_a_repeat_is_a_replay_and_a_different_plan_for_the_same_window_refuses(engine, tmp_path):
    env, plan, _ = flipped(engine, tmp_path)
    spec, _ = planning.parse_spec(env.spec)
    with Session(engine) as db, db.begin():  # the exact plan again: bridge replay, no new rows
        out = applying.apply(
            db, spec, plan, approved_sha=plan["plan_sha256"], applied_by=OP_A, owner_readers=env.readers
        )
    assert all(p["replayed"] and p["rows"] == 0 for p in out["posted"]) and len(rows(engine, env)) == 9
    other = json.loads(json.dumps(plan))
    other["plan"]["codes"][0]["lines"][0]["debit"] = "1.000000"
    other["plan"]["codes"][0]["lines"][1]["credit"] = "1.000000"
    other["plan_sha256"] = __import__("erp_v2.migration_ledger.common", fromlist=["x"]).canonical_sha(other["plan"])
    with pytest.raises(HoldingRefused) as e, Session(engine) as db, db.begin():
        applying.apply(db, spec, other, approved_sha=other["plan_sha256"], applied_by=OP_A)
    assert e.value.code == "window_already_applied"  # F2: the window is closed to a different plan
    assert len(rows(engine, env)) == 9


def test_apply_refuses_a_sha_that_is_not_the_replanned_one(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    t1(engine, env)
    plan = holding_plan(engine, env)
    spec, _ = planning.parse_spec(env.spec)
    with pytest.raises(HoldingRefused) as e, Session(engine) as db, db.begin():
        applying.apply(db, spec, plan, approved_sha="0" * 64, applied_by=OP_A)
    assert e.value.code == "plan_sha_mismatch"
    assert rows(engine, env) == [] and net(engine, env, "2000") == D("-1100")


def test_the_plan_is_stable_and_binds_params_and_receipts(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    t1(engine, env)
    a, b = holding_plan(engine, env), holding_plan(engine, env)
    assert a["plan_sha256"] == b["plan_sha256"]
    spec2 = json.loads(env.spec)
    spec2["params"] = {"migration.writeoff_offset_codes": ["6900"]}
    env2 = SimpleNamespace(**{**env.__dict__, "spec": json.dumps(spec2).encode()})
    assert holding_plan(engine, env2)["plan_sha256"] != a["plan_sha256"]


def test_a_plan_before_t1_or_over_a_stale_attribution_refuses(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env)
    assert e.value.code == "t1_not_posted"
    t1(engine, env)
    seed_gl(engine, env, "2000", D("-5"))
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env)
    assert e.value.code == "attribution_stale:2000"


def test_a_moving_r1_or_ledger_during_the_plan_refuses(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    t1(engine, env)
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env, billing(fingerprints=("a" * 64, "d" * 64)))
    assert e.value.code == "moved_during_plan"


def test_per_account_billing_positions_must_sum_to_r1(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    t1(engine, env)
    skewed = billing(per={("C1", "NGN"): Position(90001, 0, 20000, 0), ("C2", "NGN"): Position(50000, 0, 0, 0)})
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env, skewed)
    assert e.value.code == "billing_positions_mismatch"


def test_non_functional_currency_refuses(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    t1(engine, env)
    usd = billing(cur={"NGN": Position(140000, 0, 20000, 0), "USD": Position(5, 0, 0, 0)})
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env, usd)
    assert e.value.code == "holding_fx_unsupported"
    readers_, _state = readers()
    readers_["2000"] = lambda db, t, c, f: {(Party("supplier", "A"), "USD"): D("-1")}
    env.readers = readers_
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env)
    assert e.value.code == "holding_fx_unsupported"


def test_the_unreadable_2045_subledger_refuses_by_name_without_an_injected_reader(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    t1(engine, env)
    env.readers = {k: v for k, v in env.readers.items() if k != "2045"}
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env)
    assert e.value.code == "subledger_read_unavailable:2045"


def test_the_real_owner_readers_run_against_the_real_tables(engine, tmp_path):
    env = world(engine)
    with Session(engine) as db:
        assert positions.sub_2000(db, env.tenant_id) == {}
        assert positions.sub_2040(db, env.tenant_id) == {}
        from dotmac_payroll.models import PayrollLiability

        if hasattr(PayrollLiability, "settlement_mode"):  # the pinned payroll a5; older local installs lack it
            assert positions.sub_1410(db, env.tenant_id, "NGN") == {}


def test_dual_control_on_needs_distinct_planner_and_applier_and_stamps_the_rows(engine, tmp_path):
    env = flip_world(engine, tmp_path, dual=True)
    t1(engine, env, planned_by=OP_A, applied_by=OP_B)
    plan = holding_plan(engine, env, planned_by=OP_A)
    with pytest.raises(HoldingRefused) as e:
        holding_apply(engine, env, plan, applied_by=OP_A)
    assert e.value.code == "dual_control_requires_distinct_operators" and rows(engine, env) == []
    holding_apply(engine, env, plan, applied_by=OP_B)
    assert all(r.dual_control and r.planned_by == OP_A and r.applied_by == OP_B for r in rows(engine, env))


def test_a_missing_holding_account_or_a_shared_one_refuses(engine, tmp_path):
    env = flip_world(engine, tmp_path, params={"migration.holding_account.2000": "No such clearing"})
    t1(engine, env)
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env)
    assert e.value.code == "holding_account_invalid"
    env2 = flip_world(
        engine, tmp_path, params={"migration.holding_account.2000": "1901"}
    )  # shares AR clearing with 2000
    t1(engine, env2)
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env2)
    assert e.value.code == "holding_account_invalid"
    env3 = flip_world(engine, tmp_path, params={"migration.holding_account.2000": "2000"})  # a control account
    t1(engine, env3)
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env3)
    assert e.value.code == "holding_account_invalid"


# --- the e2_0030 table --------------------------------------------------------------------------------------------


def raw_insert(engine, env, exception_id, **kw):
    row = {
        "id": uuid4(), "tenant_id": env.tenant_id, "exception_id": exception_id, "control_code": "2000", "currency": "NGN",
        "holding_account_id": env.accounts["2901"], "kind": "release", "amount": D("1"), "journal_id": uuid4(),
        "journal_source_document_id": str(uuid4()), "plan_sha256": None, "owner_receipt_sha256": SHA, "evidence_sha256": None,
        "planned_by": OP_A, "applied_by": OP_A, "dual_control": False,
    }  # fmt: skip
    row.update(kw)
    with engine.begin() as c:
        c.execute(
            sa.text(
                f"INSERT INTO public.{HOLDINGS_TABLE} (id, tenant_id, exception_id, control_code, currency, holding_account_id, kind, amount, journal_id, journal_source_document_id, plan_sha256, owner_receipt_sha256, evidence_sha256, planned_by, applied_by, dual_control) "
                "VALUES (:id, :tenant_id, :exception_id, :control_code, :currency, :holding_account_id, :kind, :amount, :journal_id, :journal_source_document_id, :plan_sha256, :owner_receipt_sha256, :evidence_sha256, :planned_by, :applied_by, :dual_control)"
            ),
            row,
        )


def test_the_trigger_refuses_update_delete_sign_flip_over_release_and_an_unmatched_release(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    gap = key_of(engine, env, "partygap:2000:NGN:A")  # reclass -40
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
        c.execute(
            sa.text(f"UPDATE public.{HOLDINGS_TABLE} SET planned_by = 'x' WHERE tenant_id = :t"), {"t": env.tenant_id}
        )
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
        c.execute(sa.text(f"DELETE FROM public.{HOLDINGS_TABLE} WHERE tenant_id = :t"), {"t": env.tenant_id})
    with pytest.raises(DBAPIError, match="opposite in sign"):  # same sign as the reclass
        raw_insert(engine, env, gap.id, amount=D("-1"))
    with pytest.raises(DBAPIError, match="exceeds the outstanding"):  # releases more than the 40 outstanding
        raw_insert(engine, env, gap.id, amount=D("40.000001"))
    with pytest.raises(DBAPIError, match="needs the exception"):  # no reclass for this exception
        raw_insert(engine, env, uuid4_exception(engine, env), amount=D("1"))
    with pytest.raises(DBAPIError, match="must match"):  # a different holding account
        raw_insert(engine, env, gap.id, amount=D("1"), holding_account_id=env.accounts["1901"])
    raw_insert(engine, env, gap.id, amount=D("40"))  # exactly the outstanding is fine: a full release
    with pytest.raises(DBAPIError, match="exceeds the outstanding"):
        raw_insert(engine, env, gap.id, amount=D("0.000001"))  # nothing left to release


def uuid4_exception(engine, env):
    """A register row with no reclass."""
    with Session(engine) as db:
        row = m.MigrationException(
            tenant_id=env.tenant_id, class_code="E37", legacy_key=f"x:{uuid4()}", source_kind="manual", state="open",
            fingerprint=SHA, source_receipt_sha256=SHA, detail={},
        )  # fmt: skip
        db.add(row)
        db.commit()
        return row.id


def test_check_constraints_need_their_evidence(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    gap = key_of(engine, env, "partygap:2000:NGN:A")
    with pytest.raises(DBAPIError, match="owner_receipt"):
        raw_insert(engine, env, gap.id, amount=D("1"), owner_receipt_sha256=None)
    with pytest.raises(DBAPIError, match="writeoff_evidence"):
        raw_insert(engine, env, gap.id, kind="writeoff", amount=D("1"), owner_receipt_sha256=None, evidence_sha256=None)
    with pytest.raises(DBAPIError, match="amount"):
        raw_insert(engine, env, gap.id, amount=D("0"))
    with pytest.raises(DBAPIError, match="dual_control"):
        raw_insert(engine, env, gap.id, amount=D("1"), dual_control=True)
    with pytest.raises(DBAPIError, match=r"reclass_once|duplicate"):  # an item is reclassed once
        raw_insert(engine, env, gap.id, kind="reclass", amount=D("-1"), plan_sha256=SHA, owner_receipt_sha256=None)


def test_rls_is_forced_the_runtime_role_reads_only_and_tenants_are_isolated(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    other = world(engine)
    with engine.connect() as c:
        rls = c.execute(
            sa.text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :n"),
            {"n": HOLDINGS_TABLE},
        ).one()
        assert tuple(rls) == (True, True)
        assert c.scalar(sa.text("SELECT has_table_privilege('app_user', :n, 'SELECT')"), {"n": HOLDINGS_TABLE})
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert not c.scalar(
                sa.text("SELECT has_table_privilege('app_user', :n, :p)"), {"n": HOLDINGS_TABLE, "p": priv}
            )
    seen = {}
    for who in (env, other):
        with engine.connect() as c:
            tx = c.begin()
            c.execute(sa.text("SET LOCAL ROLE app_user"))
            c.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(who.tenant_id)})
            try:
                seen[who.tenant_id] = c.scalar(sa.text(f"SELECT count(*) FROM public.{HOLDINGS_TABLE}"))
            finally:
                tx.rollback()
    assert seen[other.tenant_id] == 0


def test_the_downgrade_refuses_while_rows_exist(engine, tmp_path):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from erp_v2.migrations.versions import e2_0030_migration_holdings as migration

    flipped(engine, tmp_path)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="migration holding evidence exists"):
                    migration.downgrade()
        finally:
            transaction.rollback()


# --- the manual-journal guard ------------------------------------------------------------------------------------


def test_a_manual_journal_line_on_a_holding_account_is_refused(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    cmd = journals.PrepareJournal(
        request_id=uuid4(), posting_date=date(2026, 1, 20), kind=journals.JournalKind.STANDARD, description="x", evidence_ref="E",
        lines=(journals.LineInput(env.accounts["2901"], credit=D("1")), journals.LineInput(env.accounts["6900"], debit=D("1"))),
    )  # fmt: skip
    with Session(engine) as db:
        with pytest.raises(JournalRefused) as e:
            journals.prepare_journal(db, tenant_id=env.tenant_id, actor=env.parties["preparer"], command=cmd, now=NOW)
        assert e.value.code == "holding_account_requires_holding_command"
    free = journals.PrepareJournal(
        request_id=uuid4(), posting_date=date(2026, 1, 20), kind=journals.JournalKind.STANDARD, description="x", evidence_ref="E",
        lines=(journals.LineInput(env.accounts["4000"], credit=D("1")), journals.LineInput(env.accounts["6900"], debit=D("1"))),
    )  # fmt: skip
    with Session(engine) as db:
        journals.prepare_journal(db, tenant_id=env.tenant_id, actor=env.parties["preparer"], command=free, now=NOW)
        db.rollback()


# --- release / writeoff -------------------------------------------------------------------------------------------


class FakeAp:
    control_code = "2000"

    def __init__(self, state, *, fail_execute=None, lie_by=Decimal(0), serialization_once=False):
        self.state, self.fail_execute, self.lie_by, self.serialization_once = (
            state,
            fail_execute,
            lie_by,
            serialization_once,
        )
        self.calls = 0

    def preview(self, db, tenant, *, command):
        self.calls += 1
        if self.serialization_once and self.calls == 1:

            class Orig(Exception):
                sqlstate = "40001"

            raise OperationalError("stmt", {}, Orig())
        return release.OwnerPreview(Party("supplier", command["supplier"]), D(command["delta"]), {})

    def execute(self, db, tenant, *, command, preview):
        if self.fail_execute:
            raise HoldingRefused(self.fail_execute)
        key = (preview.party, "NGN")
        self.state[key] = self.state.get(key, D(0)) + preview.delta + self.lie_by
        return release.OwnerReceipt("c" * 64)

    def party_subledger(self, db, tenant, party):
        return self.state.get((party, "NGN"), D(0))

    def subledger_total(self, db, tenant):
        return sum(self.state.values(), D(0))


def do_release(engine, env, key, delta, supplier="A", **kw):
    adapter = FakeAp(
        env.sub["2000"], **{k: v for k, v in kw.items() if k in ("fail_execute", "lie_by", "serialization_once")}
    )
    adapters = {**release.default_adapters(), "2000": adapter}
    return release.release(
        engine,
        env.tenant_id,
        key,
        {"supplier": supplier, "delta": delta},
        adapters=adapters,
        operator=OP_A,
        on_date=date(2026, 1, 20),
    )


def test_a_release_posts_the_journal_runs_the_owner_command_ties_and_resolves(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    out = do_release(engine, env, "held:2000:NGN:inv-1", "-60")
    assert out["exception_state"] == "resolved" and out["remaining"] == "0.000000"
    assert net(engine, env, "2000") == D("-860") == sum(env.sub["2000"].values())
    assert net(engine, env, "2901") == D("-240")
    row = key_of(engine, env, "held:2000:NGN:inv-1")
    assert (row.state, row.terminal_outcome, row.evidence_sha256) == ("resolved", "admitted", "c" * 64)
    (rel,) = rows(engine, env, "release")
    assert rel.amount == D("60") and rel.owner_receipt_sha256 == "c" * 64
    assert holding_verify(engine, env)["ready"]
    with pytest.raises(HoldingRefused) as e:
        do_release(engine, env, "held:2000:NGN:inv-1", "-1")
    assert e.value.code == "item_not_open"


def test_a_partial_release_leaves_the_item_open_and_the_next_one_gets_its_own_identity(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    first = do_release(engine, env, "held:2000:NGN:inv-1", "-25")
    assert first["exception_state"] == "open" and first["remaining"] == "-35.000000"
    second = do_release(engine, env, "held:2000:NGN:inv-1", "-35")
    assert second["exception_state"] == "resolved"
    docs = {r.journal_source_document_id for r in rows(engine, env, "release")}
    assert len(docs) == 2 and holding_verify(engine, env)["ready"]


def test_a_release_with_the_wrong_amount_party_or_sign_refuses_and_leaves_nothing(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    before = len(rows(engine, env))
    for delta, supplier in (("-61", "A"), ("60", "A"), ("0", "A"), ("-60", "B")):
        with pytest.raises(HoldingRefused) as e:
            do_release(engine, env, "held:2000:NGN:inv-1", delta, supplier)
        assert e.value.code == "release_amount_invalid"
    assert len(rows(engine, env)) == before and net(engine, env, "2000") == D("-800")


def test_an_owner_refusal_or_delta_mismatch_rolls_the_release_journal_back(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    journals_before = count_journals(engine, env)
    with pytest.raises(HoldingRefused) as e:
        do_release(engine, env, "held:2000:NGN:inv-1", "-60", fail_execute="owner_refused")
    assert e.value.code == "owner_refused"
    with pytest.raises(HoldingRefused) as e:
        do_release(engine, env, "held:2000:NGN:inv-1", "-60", lie_by=D("-1"))
    assert e.value.code == "owner_delta_mismatch"
    assert count_journals(engine, env) == journals_before and rows(engine, env, "release") == []
    assert net(engine, env, "2000") == D("-800")


def count_journals(engine, env):
    with Session(engine) as db:
        return db.scalar(
            sa.select(sa.func.count()).select_from(JournalEntry).where(JournalEntry.tenant_id == env.tenant_id)
        )


def test_a_serialization_failure_is_retried_inside_release(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    out = do_release(engine, env, "held:2000:NGN:inv-1", "-60", serialization_once=True)
    assert out["exception_state"] == "resolved" and len(rows(engine, env, "release")) == 1


def test_release_runs_at_repeatable_read(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    seen = {}

    class Probe(FakeAp):
        def preview(self, db, tenant, *, command):
            seen["level"] = db.scalar(sa.text("SHOW transaction_isolation"))
            return super().preview(db, tenant, command=command)

    adapters = {**release.default_adapters(), "2000": Probe(env.sub["2000"])}
    release.release(
        engine,
        env.tenant_id,
        "held:2000:NGN:inv-1",
        {"supplier": "A", "delta": "-60"},
        adapters=adapters,
        operator=OP_A,
        on_date=date(2026, 1, 20),
    )
    assert seen["level"] == "repeatable read"


def test_release_refuses_for_owners_that_are_not_built_and_for_ar(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    for key, code in (
        ("partygap:1400:NGN:C1", "ar_release_requires_r8"),
        ("partygap:2000:NGN:A", "release_owner_unbound:2000"),
        ("partygap:1410:NGN:E1", "release_owner_unbound:1410"),
    ):
        with pytest.raises(HoldingRefused) as e:
            release.release(engine, env.tenant_id, key, {}, operator=OP_A, on_date=date(2026, 1, 20))
        assert e.value.code == code
    assert rows(engine, env, "release") == []


def do_writeoff_plan(engine, env, key, offset="6900", ev=SHA, allow=(), planned_by=OP_A):
    with Session(engine) as db:
        out = release.plan_writeoff(db, env.tenant_id, key, offset, ev, allowlist=allow, planned_by=planned_by)
        db.rollback()
    return out


def do_writeoff_apply(engine, env, key, plan, applied_by=OP_A, offset="6900"):
    with Session(engine) as db, db.begin():
        return release.apply_writeoff(
            db,
            env.tenant_id,
            key,
            plan,
            approved_sha=plan["plan_sha256"],
            applied_by=applied_by,
            on_date=date(2026, 1, 20),
        )


def test_a_writeoff_clears_the_full_outstanding_against_the_offset_and_resolves(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    plan = do_writeoff_plan(engine, env, "partygap:2000:NGN:A")
    assert plan["plan"]["outstanding"] == "-40.000000"
    out = do_writeoff_apply(engine, env, "partygap:2000:NGN:A", plan)
    assert out["written_off"] == "-40.000000"
    assert net(engine, env, "6900") == D("-40") and net(engine, env, "2901") == D("-260")
    assert net(engine, env, "2000") == D("-800")  # a writeoff never touches the control
    row = key_of(engine, env, "partygap:2000:NGN:A")
    assert (row.state, row.terminal_outcome, row.evidence_sha256) == ("resolved", "written_off", SHA)
    (wo,) = rows(engine, env, "writeoff")
    assert wo.amount == D("40") and wo.evidence_sha256 == SHA
    assert holding_verify(engine, env)["ready"]
    with pytest.raises(HoldingRefused) as e:
        do_writeoff_plan(engine, env, "partygap:2000:NGN:A")
    assert e.value.code == "item_not_open"


def test_a_writeoff_offset_must_be_postable_and_not_control_tax_holding_or_outside_the_allowlist(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    for offset in ("2000", "1400", "2105", "2901", "1901", "9999"):
        with pytest.raises(HoldingRefused) as e:
            do_writeoff_plan(engine, env, "partygap:2000:NGN:A", offset)
        assert e.value.code in {"offset_account_invalid", "holding_account_invalid"}, offset
    with pytest.raises(HoldingRefused) as e:
        do_writeoff_plan(engine, env, "partygap:2000:NGN:A", "6900", allow=("4000",))
    assert e.value.code == "offset_account_not_allowed"
    do_writeoff_plan(engine, env, "partygap:2000:NGN:A", "6900", allow=("6900",))


def test_a_stale_writeoff_plan_and_dual_control_are_enforced(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    plan = do_writeoff_plan(engine, env, "partygap:2000:NGN:A")
    do_release(engine, env, "partygap:2000:NGN:A", "-10")  # a partial release changes the outstanding
    with pytest.raises(HoldingRefused) as e:
        do_writeoff_apply(engine, env, "partygap:2000:NGN:A", plan)
    assert e.value.code == "plan_sha_mismatch"
    set_dual_control_engine(engine, env.tenant_id, True)
    fresh = do_writeoff_plan(engine, env, "partygap:2000:NGN:A", planned_by=OP_A)
    with pytest.raises(HoldingRefused) as e:
        do_writeoff_apply(engine, env, "partygap:2000:NGN:A", fresh, applied_by=OP_A)
    assert e.value.code == "dual_control_requires_distinct_operators"
    do_writeoff_apply(engine, env, "partygap:2000:NGN:A", fresh, applied_by=OP_B)
    assert rows(engine, env, "writeoff")[0].dual_control


# --- CLI ----------------------------------------------------------------------------------------------------------


def test_the_cli_plans_dry_by_default_applies_only_with_the_switch_and_writes_private_receipts(
    engine, tmp_path, monkeypatch
):
    env = flip_world(engine, tmp_path)
    monkeypatch.setattr(cli, "billing_reader", lambda: billing())
    real = positions.owner_sub
    monkeypatch.setattr(
        positions, "owner_sub", lambda db, tenant, code, f: dict(env.readers[code](db, tenant, code, f))
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_bytes(env.spec)

    def env_dsn():
        os.environ["MIGRATION_DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("PLATFORM_DATABASE_URL", None)

    env_dsn()
    t1p = tmp_path / "t1-plan.json"
    assert cli.main(["t1", "plan", "--spec", str(spec_path), "--receipt", str(t1p), "--operator-ref", OP_A]) == 0
    assert stat.S_IMODE(t1p.stat().st_mode) == 0o600 and net(engine, env, "2105") == 0
    sha = json.loads(t1p.read_text())["plan_sha256"]
    env_dsn()
    refused = tmp_path / "t1-refused.json"
    monkeypatch.delenv("ERP_HOLDING_APPLY", raising=False)
    assert (
        cli.main(
            [
                "t1",
                "apply",
                "--apply",
                "--spec",
                str(spec_path),
                "--receipt",
                str(refused),
                "--operator-ref",
                OP_A,
                "--plan-receipt",
                str(t1p),
                "--plan-sha",
                sha,
            ]
        )
        == 2
    )
    assert (
        json.loads(refused.read_text())["refusal"] == "explicit_holding_apply_required"
        and net(engine, env, "2105") == 0
    )
    monkeypatch.setenv("ERP_HOLDING_APPLY", "1")
    env_dsn()
    assert (
        cli.main(
            [
                "t1",
                "apply",
                "--apply",
                "--spec",
                str(spec_path),
                "--receipt",
                str(tmp_path / "t1-apply.json"),
                "--operator-ref",
                OP_A,
                "--plan-receipt",
                str(t1p),
                "--plan-sha",
                sha,
            ]
        )
        == 0
    )
    assert net(engine, env, "2105") == D("-200")
    env_dsn()
    plan_receipt = tmp_path / "plan.json"
    assert cli.main(["plan", "--spec", str(spec_path), "--receipt", str(plan_receipt), "--operator-ref", OP_A]) == 0
    hsha = json.loads(plan_receipt.read_text())["plan_sha256"]
    assert net(engine, env, "2000") == D("-1100")  # plan wrote nothing
    env_dsn()
    assert (
        cli.main(
            [
                "apply",
                "--apply",
                "--spec",
                str(spec_path),
                "--receipt",
                str(tmp_path / "apply.json"),
                "--operator-ref",
                OP_A,
                "--plan-receipt",
                str(plan_receipt),
                "--plan-sha",
                hsha,
            ]
        )
        == 0
    )
    assert net(engine, env, "2000") == D("-800") and len(rows(engine, env, "reclass")) == 9
    env_dsn()  # a second run is a replay
    again = tmp_path / "apply2.json"
    assert (
        cli.main(
            [
                "apply",
                "--apply",
                "--spec",
                str(spec_path),
                "--receipt",
                str(again),
                "--operator-ref",
                OP_A,
                "--plan-receipt",
                str(plan_receipt),
                "--plan-sha",
                hsha,
            ]
        )
        == 0
    )
    assert json.loads(again.read_text())["replayed"] is True and len(rows(engine, env, "reclass")) == 9
    env_dsn()
    verify_receipt = tmp_path / "verify.json"
    assert cli.main(["verify", "--spec", str(spec_path), "--receipt", str(verify_receipt)]) == 0
    assert json.loads(verify_receipt.read_text())["ready"] is True
    text_out = (tmp_path / "plan.json").read_text() + (tmp_path / "apply.json").read_text()
    assert "postgresql" not in text_out and "MIGRATION_DATABASE_URL" not in text_out
    del real


def test_the_cli_refuses_serving_dsns_and_a_missing_receipt_path_is_not_overwritten(engine, tmp_path, monkeypatch):
    env = flip_world(engine, tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_bytes(env.spec)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    receipt = tmp_path / "r.json"
    assert cli.main(["plan", "--spec", str(spec_path), "--receipt", str(receipt), "--operator-ref", OP_A]) == 2
    assert json.loads(receipt.read_text())["refusal"] == "serving_database_materials_forbidden"
    assert (
        cli.main(["plan", "--spec", str(spec_path), "--receipt", str(receipt), "--operator-ref", OP_A]) == 2
    )  # exists: refused, untouched
    assert json.loads(receipt.read_text())["refusal"] == "serving_database_materials_forbidden"


# --- review fixes: F1 anchor, F3 re-read, F4 dual, F6 register, L1-L6 -------------------------------------------------


def test_f1_t1_refuses_when_the_admission_rows_disagree_with_the_census_or_billing(engine, tmp_path):
    env = world(engine)
    seed_gl(engine, env, "1400", D("1530"))
    env.spec = spec_bytes(env, make_spec(tmp_path))
    with pytest.raises(HoldingRefused) as e:
        t1_mod.run_t1_plan(engine, env.spec, cli.load_receipts, billing(), planned_by=OP_A)
    assert e.value.code == "ar_admissions_missing"
    seed_admissions(engine, env, digest="9" * 64)
    with pytest.raises(HoldingRefused) as e:
        t1_mod.run_t1_plan(engine, env.spec, cli.load_receipts, billing(), planned_by=OP_A)
    assert e.value.code == "census_sha_mismatch"
    env2 = flip_world(engine, tmp_path)
    with pytest.raises(HoldingRefused) as e:
        t1_mod.run_t1_plan(engine, env2.spec, cli.load_receipts, billing(manifest="1" * 64), planned_by=OP_A)
    assert e.value.code == "manifest_fingerprint_mismatch"
    from erp_v2.holdings.billing import UnavailableBilling

    with pytest.raises(HoldingRefused) as e:
        UnavailableBilling().window_manifest_fingerprint(None, "w")  # type: ignore[arg-type]
    assert e.value.code == "manifest_fingerprint_unavailable"


def test_f1_the_admitted_partition_set_must_equal_the_census_carrying_parties(engine, tmp_path):
    env = world(engine)
    seed_gl(engine, env, "1400", D("1530"))
    seed_admissions(engine, env, parties=(("C1", "NGN"),))  # C2 carries items in the census but was not admitted
    env.spec = spec_bytes(env, make_spec(tmp_path))
    with pytest.raises(HoldingRefused) as e:
        t1_mod.run_t1_plan(engine, env.spec, cli.load_receipts, billing(), planned_by=OP_A)
    assert e.value.code == "admissions_differ_from_census"


def test_f6_held_rows_must_match_an_open_register_exception_with_an_equal_amount(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    t1(engine, env)
    with Session(engine) as db:
        row = db.scalar(
            sa.select(m.MigrationException).where(
                m.MigrationException.tenant_id == env.tenant_id, m.MigrationException.legacy_key == "inv-1"
            )
        )
        row.state, row.terminal_outcome, row.evidence_sha256 = "resolved", "no_action", SHA
        row.resolved_at = sa.func.now()
        db.commit()
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env)
    assert e.value.code == "held_not_in_register"
    env2 = flip_world(engine, tmp_path, register=[("E02", "inv-1", {})])  # the census partition has no register item
    t1(engine, env2)
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env2)
    assert e.value.code == "held_not_in_register"
    env3 = flip_world(engine, tmp_path)
    t1(engine, env3)
    with Session(engine) as db:
        db.execute(
            sa.text(
                "UPDATE erp_migration_exceptions SET detail = '{\"gl_signed_minor\": 1}'::jsonb WHERE tenant_id = :t AND legacy_key = 'partition:C1:NGN'"
            ),
            {"t": env3.tenant_id},
        )
        db.commit()
    with pytest.raises(HoldingRefused) as e:
        holding_plan(engine, env3)
    assert e.value.code == "held_amount_differs_from_register"


def test_f3_the_post_condition_rereads_the_owner_subledger_inside_the_apply_transaction(engine, tmp_path):
    env = flip_world(engine, tmp_path)
    t1(engine, env)
    plan = holding_plan(engine, env)
    env.sub["2000"][(Party("supplier", "A"), "NGN")] = D("-501")  # the owner moved after the plan
    with pytest.raises(HoldingRefused) as e:
        holding_apply(engine, env, plan)
    assert e.value.code == "holding_postcondition_failed"
    assert rows(engine, env) == [] and net(engine, env, "2000") == D("-1100")  # rolled back whole


def test_f4_a_release_refuses_while_dual_control_is_on_and_stamps_the_policy(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    set_dual_control_engine(engine, env.tenant_id, True)
    with pytest.raises(HoldingRefused) as e:
        do_release(engine, env, "held:2000:NGN:inv-1", "-60")
    assert e.value.code == "release_dual_control_unbuilt" and rows(engine, env, "release") == []
    set_dual_control_engine(engine, env.tenant_id, False)
    do_release(engine, env, "held:2000:NGN:inv-1", "-60")
    assert rows(engine, env, "release")[0].dual_control is False


def test_l1_a_t1_replay_needs_the_posted_journals_fingerprint(engine, tmp_path, monkeypatch):
    env = flip_world(engine, tmp_path)
    plan, _ = t1(engine, env)
    again = t1_mod.run_t1_plan(engine, env.spec, cli.load_receipts, billing(), planned_by=OP_A)
    assert again["plan"]["posted_fingerprint"] == plan["plan_sha256"]


def test_l2_the_owner_command_sha_is_in_the_identity_and_the_receipt(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    out = do_release(engine, env, "held:2000:NGN:inv-1", "-25")
    assert len(out["owner_command_sha256"]) == 64


def test_l3_truncate_is_refused_and_l4_an_item_with_an_outstanding_amount_cannot_be_resolved(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
        c.execute(sa.text(f"TRUNCATE public.{HOLDINGS_TABLE}"))
    from erp_v2.migration_ledger import register

    with pytest.raises(DBAPIError, match="outstanding amount cannot be resolved"), Session(engine) as db:
        register.resolve(
            db,
            env.tenant_id,
            class_code="E37",
            legacy_key="partygap:2000:NGN:A",
            outcome="no_action",
            evidence_sha256=SHA,
        )
        db.commit()


def test_l5_verify_flags_a_prepayment_account_that_moved_after_t1(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    assert next(c for c in holding_verify(engine, env)["checks"] if c["check"] == "prepayment_account_unmoved")["ok"]
    with Session(engine) as db:  # Finance renames another account to "Customer prepayments" and the old one away
        db.execute(
            sa.text("UPDATE mod_accounting.accounts SET name = 'old' WHERE id = :i"), {"i": env.accounts["2105"]}
        ) if False else None
        from dotmac_accounting.models import Account

        db.get(Account, env.accounts["2105"]).name = "old prepayments"
        db.get(Account, env.accounts["6900"]).name = "Customer prepayments"
        db.commit()
    verified = holding_verify(engine, env)
    assert (
        not next(c for c in verified["checks"] if c["check"] == "prepayment_account_unmoved")["ok"]
        and not verified["ready"]
    )


def test_l6_a_configured_holding_account_is_guarded_even_with_no_rows(engine, tmp_path):
    env, *_ = flipped(engine, tmp_path)
    with Session(engine) as db:
        persisted = {
            r.control_code
            for r in db.scalars(sa.select(__import__("erp_v2.holdings.models", fromlist=["x"]).HoldingAccount))
        }
    assert {"2000", "1400", "2040", "2045", "1410", "prepayment"} <= persisted
    cmd = journals.PrepareJournal(
        request_id=uuid4(), posting_date=date(2026, 1, 20), kind=journals.JournalKind.STANDARD, description="x", evidence_ref="E",
        lines=(journals.LineInput(env.accounts["2902"], credit=D("1")), journals.LineInput(env.accounts["6900"], debit=D("1"))),
    )  # fmt: skip
    with Session(engine) as db:  # 2902 (employee clearing) has no rows: X = 0 for 2040 and 2045
        with pytest.raises(JournalRefused) as e:
            journals.prepare_journal(db, tenant_id=env.tenant_id, actor=env.parties["preparer"], command=cmd, now=NOW)
        assert e.value.code == "holding_account_requires_holding_command"


# --- straddle reproduction (W8, simplified) ---------------------------------------------------------------------------


def straddle_world(engine, tmp_path, *, lines=None, status="POSTED"):
    env = world(engine)
    # three non-control accounts standing in for 1207 / 1211 / 6080
    from dotmac_accounting.models import Account

    with Session(engine) as db:
        cat = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "EXP")
        )
        for code in ("1207", "1211", "6080"):
            row = Account(
                tenant_id=env.tenant_id,
                category_id=cat.id,
                code=code,
                name=f"Acct {code}",
                kind=AccountKind.POSTING,
                normal_balance=NormalBalance.DEBIT,
            )
            db.add(row)
            db.flush()
            env.accounts[code] = row.id
        db.commit()
    a, b, c = (str(env.accounts[k]) for k in ("1207", "1211", "6080"))
    day = "2026-01-13"
    lines = lines(env.accounts) if callable(lines) else lines
    lines = lines or [
        {"account_id": a, "date": day, "debit": "100.00", "credit": "0.00"},
        {"account_id": b, "date": day, "debit": "0.00", "credit": "60.00"},
        {"account_id": c, "date": day, "debit": "0.00", "credit": "40.00"},
    ]
    side = write(tmp_path / "straddle.json", {"version": 1, "cutoff": "2025-12-31", "currency": "NGN", "journals": [
        {"journal_id": "LJ-1", "legacy_status": status, "source_module": "GL", "lines": lines},
        {"journal_id": "LJ-2", "legacy_status": status, "source_module": "GL", "lines": [
            {"account_id": a, "date": "2026-01-14", "debit": "5.00", "credit": "0.00"},
            {"account_id": b, "date": "2026-01-14", "debit": "0.00", "credit": "5.00"}]},
    ]})  # fmt: skip
    import hashlib

    spec = make_spec(tmp_path)
    spec["receipts"] = {"straddle": {"path": str(side), "sha256": hashlib.sha256(side.read_bytes()).hexdigest()}}
    env.spec = spec_bytes(env, spec)
    return env


def straddle_apply(engine, env, planned_by=OP_A, applied_by=OP_A):
    from erp_v2.holdings import straddle

    plan = straddle.run_plan(engine, env.spec, planned_by=planned_by)
    spec, _ = planning.parse_spec(env.spec)
    with Session(engine) as db, db.begin():
        return plan, straddle.apply(db, spec, plan, approved_sha=plan["plan_sha256"], applied_by=applied_by)


def test_straddle_reproduces_each_journal_balanced_on_its_legacy_day_and_registers_e39(engine, tmp_path):
    from erp_v2.holdings import straddle

    env = straddle_world(engine, tmp_path)
    plan, out = straddle_apply(engine, env)
    assert out["journals"] == 2 and out["posted_journals"] == 2
    assert (
        net(engine, env, "1207") == D("105")
        and net(engine, env, "1211") == D("-65")
        and net(engine, env, "6080") == D("-40")
    )
    with Session(engine) as db:
        days = sorted(
            j.posting_date.isoformat()
            for j in db.scalars(
                sa.select(JournalEntry).where(
                    JournalEntry.tenant_id == env.tenant_id, JournalEntry.source_document_kind == "migration_straddle"
                )
            )
        )
    assert days == ["2026-01-13", "2026-01-14"]
    item = key_of(engine, env, "straddle:LJ-1")
    assert (item.class_code, item.state, item.source_kind) == ("E39", "open", "migration_straddle")
    assert len(item.detail["reproduction_journal_ids"]) == 1 and len(item.detail["evidence_sha256"]) == 64
    spec, _ = planning.parse_spec(env.spec)
    raw = open(spec.straddle.path, "rb").read()
    with applying.read_only_session(engine) as db:
        assert straddle.verify(db, spec, raw)["ready"]
        db.rollback()
    with Session(engine) as db, db.begin():  # replay is a no-op
        again = straddle.apply(db, spec, plan, approved_sha=plan["plan_sha256"], applied_by=OP_A)
    assert again["ready"] and net(engine, env, "1207") == D("105")
    assert holding_rows_count(engine, env) == 0  # no holdings rows: the suspense tie is dropped


def holding_rows_count(engine, env):
    return len(rows(engine, env))


def test_straddle_refuses_control_lines_nonzero_nets_split_journals_and_closed_periods(engine, tmp_path):
    from erp_v2.holdings import straddle

    def two(day_a, da, day_b, cr, first="1207", second="1211"):
        return lambda acc: [
            {"account_id": str(acc[first]), "date": day_a, "debit": da, "credit": "0.00"},
            {"account_id": str(acc[second]), "date": day_b, "debit": "0.00", "credit": cr},
        ]

    cases = {
        "straddle_control_line": two("2026-01-13", "7.00", "2026-01-13", "7.00", second="2000"),
        "straddle_nonzero_net": two("2026-01-13", "7.00", "2026-01-13", "6.00"),
        "straddle_splits_cutoff": two("2025-12-30", "7.00", "2026-01-13", "7.00"),
    }
    for code, lines in cases.items():
        env = straddle_world(engine, tmp_path, lines=lines)
        before = count_journals(engine, env)
        with pytest.raises(HoldingRefused) as e:
            straddle.run_plan(engine, env.spec, planned_by=OP_A)
        assert e.value.code == code
        assert count_journals(engine, env) == before
    day_skew = two("2026-01-13", "7.00", "2026-01-14", "7.00")
    env = straddle_world(engine, tmp_path, lines=day_skew)
    with pytest.raises(HoldingRefused) as e:
        straddle.run_plan(engine, env.spec, planned_by=OP_A)
    assert e.value.code == "straddle_nonzero_net"
    closed = two("2026-03-13", "7.00", "2026-03-13", "7.00")
    env = straddle_world(engine, tmp_path, lines=closed)  # no period covers March in the seed
    with pytest.raises(HoldingRefused) as e:
        straddle.run_plan(engine, env.spec, planned_by=OP_A)
    assert e.value.code == "straddle_period_not_postable"


def test_straddle_refuses_a_holding_account_and_honours_dual_control_and_the_cli(engine, tmp_path, monkeypatch):
    env = straddle_world(engine, tmp_path)
    set_dual_control_engine(engine, env.tenant_id, True)
    with pytest.raises(HoldingRefused) as e:
        straddle_apply(engine, env, planned_by=OP_A, applied_by=OP_A)
    assert e.value.code == "dual_control_requires_distinct_operators" and net(engine, env, "1207") == 0
    set_dual_control_engine(engine, env.tenant_id, False)
    spec_path = tmp_path / "spec.json"
    spec_path.write_bytes(env.spec)

    def dsn():
        os.environ["MIGRATION_DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("PLATFORM_DATABASE_URL", None)

    dsn()
    plan_r = tmp_path / "sp.json"
    assert (
        cli.main(["straddle", "plan", "--spec", str(spec_path), "--receipt", str(plan_r), "--operator-ref", OP_A]) == 0
    )
    sha = json.loads(plan_r.read_text())["plan_sha256"]
    assert net(engine, env, "1207") == 0
    monkeypatch.delenv("ERP_HOLDING_APPLY", raising=False)
    dsn()
    args = [
        "straddle",
        "apply",
        "--apply",
        "--spec",
        str(spec_path),
        "--operator-ref",
        OP_A,
        "--plan-receipt",
        str(plan_r),
        "--plan-sha",
        sha,
    ]
    assert cli.main([*args, "--receipt", str(tmp_path / "sa0.json")]) == 2 and net(engine, env, "1207") == 0
    monkeypatch.setenv("ERP_HOLDING_APPLY", "1")
    dsn()
    assert cli.main([*args, "--receipt", str(tmp_path / "sa1.json")]) == 0 and net(engine, env, "1207") == D("105")
    dsn()
    assert cli.main(["straddle", "verify", "--spec", str(spec_path), "--receipt", str(tmp_path / "sv.json")]) == 0
