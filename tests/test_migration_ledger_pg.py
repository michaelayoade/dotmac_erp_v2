"""B-9 on real PostgreSQL: opening and IAS 8 journals, signed-bridge immutability, the flip gate and the governed
rounding-residue correction. Real migrations, the real accounting commands and the real B2 maker-checker path.

Hosted CI only (ERP_TEST_POSTGRES_URL), like test_held_drafts_pg.
"""

import json
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import (
    AccountClass,
    AccountKind,
    CloseCheck,
    NormalBalance,
    PeriodCloseEvidence,
    PeriodStatus,
)
from dotmac_accounting.models import Account, AccountCategory, FiscalPeriod, FiscalYear, JournalEntry, PostedLedgerLine
from dotmac_kernel.cache import TenantScope
from dual_control_support import set_dual_control_engine
from migration_ledger_support import bind_rounding
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_journals_pg import NOW, _registries, seed  # noqa: F401  (hosted-Postgres fixtures)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.journals import service as journals
from erp_v2.journals.service import JournalRefused
from erp_v2.migration_ledger import bridge, ledger_check, opening, register, rounding_verify
from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger.common import LedgerRefused
from erp_v2.migration_ledger.opening_cli import main as opening_main

SHA = "b" * 64


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


def world(engine, *, dual: bool = False) -> SimpleNamespace:
    """A tenant with FY2025 (Dec open), FY2026 (Jan open, Feb future) and the accounts the B-9 tools touch."""
    env = seed(engine)
    set_dual_control_engine(engine, env.tenant_id, dual)
    with Session(engine) as db:
        cats = {
            code: AccountCategory(tenant_id=env.tenant_id, code=code, name=code, account_class=klass)
            for code, klass in (("EQU", AccountClass.EQUITY), ("EXP", AccountClass.EXPENSE))
        }
        db.add_all(cats.values())
        db.flush()
        for code, cat, normal in (
            ("3100", "EQU", NormalBalance.CREDIT),
            ("6900", "EXP", NormalBalance.DEBIT),
            ("5000", "EXP", NormalBalance.DEBIT),
        ):
            row = Account(
                tenant_id=env.tenant_id,
                category_id=cats[cat].id,
                code=code,
                name=f"Account {code}",
                kind=AccountKind.POSTING,
                normal_balance=normal,
            )
            db.add(row)
            db.flush()
            env.accounts[code] = row.id
        year = FiscalYear(
            tenant_id=env.tenant_id,
            code="FY25",
            name="FY 2025",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )
        db.add(year)
        db.flush()
        december = FiscalPeriod(
            tenant_id=env.tenant_id,
            fiscal_year_id=year.id,
            period_number=12,
            name="Dec 2025",
            start_date=date(2025, 12, 1),
            end_date=date(2025, 12, 31),
            is_adjustment=False,
            status=PeriodStatus.FUTURE,
        )
        db.add(december)
        db.flush()
        env.periods["dec25"] = december.id
        accounting.open_period(
            db, scope=TenantScope(env.tenant_id), period_id=december.id, actor_ref="seed", occurred_at=NOW
        )
        db.commit()
    return env


def tb_rows(env=None):
    return {
        "1000": "500.00",
        "1400": "300.00",
        "1410": "50.00",
        "2000": "-400.00",
        "2040": "-100.00",
        "3100": "-350.00",
    }


def opening_inputs(*, two_thousand_sub="-399.00", ias8=True):
    rows = [
        {"account_code": c, "debit": n if not n.startswith("-") else "0", "credit": n[1:] if n.startswith("-") else "0"}
        for c, n in tb_rows().items()
    ]
    tb = {"version": 1, "as_of": "2025-12-31", "rows": rows}
    as_of, parsed = opening.parse_tb(tb, SHA)
    sub = {
        "version": 1,
        "openings": {
            c: {"net": n, "source_receipt_sha256": SHA}
            for c, n in (("2000", two_thousand_sub), ("2040", "-100.00"), ("1410", "50.00"), ("1400", "300.00"))
        },
    }
    items = (opening.Ias8Item("IAS8-001", "2000", Decimal("1.00"), SHA, "AP overstated at 2025 close"),) if ias8 else ()
    return opening.OpeningInputs(as_of, parsed, "c" * 64, opening.parse_subledger(sub), "d" * 64, items, "e" * 64)


def apply_inputs(env, *, dual=False):
    return opening.ApplyInputs(
        "f" * 64,
        "1" * 64,
        "operator-a",
        "operator-b",
        env.parties["requester"],
        env.parties["approver"],
        dual,
        "EVID-1",
    )


def post_opening(engine, env, inputs=None):
    with Session(engine) as db:
        out = opening.apply_opening(db, env.tenant_id, inputs or opening_inputs(), apply_inputs(env))
        db.commit()
    return out


def ledger_net(engine, env, code, *, owner=None):
    with Session(engine) as db:
        query = (
            sa.select(sa.func.coalesce(sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit), 0))
            .join(
                Account, (Account.tenant_id == PostedLedgerLine.tenant_id) & (Account.id == PostedLedgerLine.account_id)
            )
            .join(
                JournalEntry,
                (JournalEntry.tenant_id == PostedLedgerLine.tenant_id)
                & (JournalEntry.id == PostedLedgerLine.journal_id),
            )
            .where(PostedLedgerLine.tenant_id == env.tenant_id, Account.code == code)
        )
        if owner:
            query = query.where(JournalEntry.source_owner == owner)
        return Decimal(db.scalar(query))


def count_journals(engine, env) -> int:
    with Session(engine) as db:
        return db.scalar(
            sa.select(sa.func.count()).select_from(JournalEntry).where(JournalEntry.tenant_id == env.tenant_id)
        )


# --- opening and IAS 8 ---------------------------------------------------------------------------------------------


def test_opening_posts_one_balanced_journal_and_each_ias8_against_3100(engine):
    env = world(engine)
    assert post_opening(engine, env) == {"opening": 1, "ias8": 1, "replayed": 0}
    with Session(engine) as db:
        journals_by_owner = {
            j.source_owner: j
            for j in db.scalars(sa.select(JournalEntry).where(JournalEntry.tenant_id == env.tenant_id))
        }
        assert set(journals_by_owner) == {"erp.opening", "erp.ias8"}
        assert journals_by_owner["erp.opening"].posting_date == date(2025, 12, 31)
        assert journals_by_owner["erp.ias8"].posting_date == date(2026, 1, 1)  # D-3: the FY2026 opening
        rows = list(db.scalars(sa.select(m.OpeningJournal).where(m.OpeningJournal.tenant_id == env.tenant_id)))
        assert {(r.kind, r.item_ref) for r in rows} == {("opening", "opening"), ("ias8", "IAS8-001")}
    # Σ = 0 over the whole ledger, and every control account equals its subledger opening (-399 for 2000 after IAS 8)
    assert sum(ledger_net(engine, env, c) for c in ("1000", "1400", "1410", "2000", "2040", "3100")) == 0
    assert ledger_net(engine, env, "2000") == Decimal("-399")
    assert ledger_net(engine, env, "2000", owner="erp.opening") == Decimal("-400")
    assert ledger_net(engine, env, "3100", owner="erp.ias8") == Decimal("-1")  # retained earnings takes the other side
    assert ledger_net(engine, env, "1410") == Decimal("50") and ledger_net(engine, env, "1400") == Decimal("300")


def test_a_repeat_is_a_replay_and_posts_nothing_more(engine):
    env = world(engine)
    post_opening(engine, env)
    before = count_journals(engine, env)
    assert post_opening(engine, env) == {"opening": 0, "ias8": 0, "replayed": 2}
    assert count_journals(engine, env) == before == 2


def test_the_opening_refuses_unproven_inputs_before_any_write(engine):
    env = world(engine)
    bad = opening_inputs(two_thousand_sub="-390.00")  # the subledger does not tie, even with the IAS 8 item
    with Session(engine) as db:
        with pytest.raises(LedgerRefused) as caught:
            opening.apply_opening(db, env.tenant_id, bad, apply_inputs(env))
    assert caught.value.code == "opening_control_not_subledger" and count_journals(engine, env) == 0
    unbalanced = opening_inputs()
    unbalanced = opening.OpeningInputs(
        unbalanced.as_of,
        {**unbalanced.tb, "1000": Decimal("500.01")},
        "c" * 64,
        unbalanced.subledger,
        "d" * 64,
        unbalanced.ias8,
        "e" * 64,
    )
    with Session(engine) as db:
        with pytest.raises(LedgerRefused) as caught:
            opening.apply_opening(db, env.tenant_id, unbalanced, apply_inputs(env))
    assert caught.value.code == "opening_tb_not_zero_sum" and count_journals(engine, env) == 0


def test_a_second_trial_balance_is_refused_once_an_opening_exists(engine):
    env = world(engine)
    post_opening(engine, env)
    other = opening_inputs()
    other = opening.OpeningInputs(
        other.as_of, other.tb, "9" * 64, other.subledger, other.subledger_sha256, other.ias8, other.ias8_sha256
    )
    with Session(engine) as db:
        with pytest.raises(LedgerRefused) as caught:
            opening.apply_opening(db, env.tenant_id, other, apply_inputs(env))
    assert caught.value.code == "opening_already_posted_with_other_trial_balance"


def test_the_opening_needs_an_open_period_and_leaves_nothing_behind(engine):
    env = world(engine)
    with Session(engine) as db:
        accounting.soft_close_period(
            db,
            scope=TenantScope(env.tenant_id),
            period_id=env.periods["dec25"],
            evidence=PeriodCloseEvidence((CloseCheck("CHECK1", True, "evidence-1", "e" * 64),)),
            actor_ref="test",
            approval_reference="close-1",
            occurred_at=NOW,
        )
        db.commit()
    with Session(engine) as db:
        with pytest.raises(LedgerRefused) as caught:
            opening.apply_opening(db, env.tenant_id, opening_inputs(), apply_inputs(env))
    assert caught.value.code == "opening_period_not_open" and count_journals(engine, env) == 0


def test_provenance_is_insert_only(engine):
    env = world(engine)
    post_opening(engine, env)
    for statement in ("UPDATE erp_opening_journals SET amount = 1", "DELETE FROM erp_opening_journals"):
        with engine.connect() as c, pytest.raises(DBAPIError, match="insert-only"):
            c.execute(sa.text(statement + " WHERE tenant_id = :t"), {"t": env.tenant_id})


# --- bridge: versions, signing, immutability -----------------------------------------------------------------------


def item(item_id, bucket, ref, account, period, legacy, v2, *, rule=None, scope=None):
    return {
        "item_id": item_id,
        "bucket": bucket,
        "ref": ref,
        "account": account,
        "period": period,
        "rule": rule,
        "scope": scope,
        "legacy_amount": legacy,
        "v2_amount": v2,
        "evidence_sha256": SHA,
        "source_invoice_id": ref if rule == "T-ROUND-2DP" and scope == "header" else None,
    }


REF = "dotmac-debt-register v1700"
CLEAN_ITEMS = [
    item("1", "carry", "J1", "5000", "2026-01", "10.00", "10.00"),
    item("1b", "carry", "J1", "1000", "2026-01", "-10.00", "-10.00"),
]
CLEAN_LEGACY = {("5000", "2026-01"): "10.00", ("1000", "2026-01"): "-10.00"}


def post_legacy(engine, env, day, lines, *, owner="legacy-erp"):
    """Post one balanced journal straight through accounting, as the legacy import (or another owner) would."""
    from dotmac_accounting.contracts import CreateJournal, JournalKind, JournalLineInput, SourceIdentity

    with Session(engine) as db:
        scope = TenantScope(env.tenant_id)
        journal = accounting.create_journal(
            db, scope=scope,
            command=CreateJournal(
                number=f"LG-{uuid4().hex[:12]}", kind=JournalKind.STANDARD, entry_date=day, posting_date=day,
                currency_code="USD", exchange_rate=Decimal(1), description="legacy", reference="legacy",
                source=SourceIdentity(owner=owner, document_kind="t", document_id=f"d-{uuid4().hex}", version="1",
                    fingerprint="f" * 64),
                lines=tuple(
                    JournalLineInput(
                        env.accounts[code], max(Decimal(net), Decimal(0)), max(-Decimal(net), Decimal(0)), None
                    )
                    for code, net in lines
                ),
            ),
            idempotency_key=f"t:{uuid4()}", idempotency_expires_at=None, recorded_at=NOW,
        )  # fmt: skip
        accounting.post_journal(
            db, scope=scope, journal_id=journal.id, approval_reference="t", posted_by="t",
            idempotency_key=f"p:{uuid4()}", idempotency_expires_at=None, posted_at=NOW,
        )  # fmt: skip
        db.commit()


def seed_clean(engine, env):
    post_legacy(engine, env, date(2026, 1, 15), [("5000", "10.00"), ("1000", "-10.00")])


ROUND_ITEMS = [
    item("r1", "transform", "A", "2000", "2026-01", "-100.005", "-100.01", rule="T-ROUND-2DP", scope="header"),
    item("r2", "transform", "B", "2000", "2026-02", "-50.004", "-50.00", rule="T-ROUND-2DP", scope="header"),
]
ROUND_LEGACY = {("2000", "2026-01"): "-100.005", ("2000", "2026-02"): "-50.004"}


ROUND_FULL_ITEMS = [
    *ROUND_ITEMS,
    item("e1", "transform", "A", "5000", "2026-01", "100.005", "100.005", rule="T-EXP-LEGACY", scope="line"),
    item("e2", "transform", "B", "5000", "2026-02", "50.004", "50.004", rule="T-EXP-LEGACY", scope="line"),
]
ROUND_FULL_LEGACY = {**ROUND_LEGACY, ("5000", "2026-01"): "100.005", ("5000", "2026-02"): "50.004"}


def seed_round_ledger(engine, env):
    """The legacy journals behind ROUND_FULL_*: Dr expense / Cr 2000 at the legacy (sub-cent) amounts."""
    with Session(engine) as db:
        journals.open_period(
            db, tenant_id=env.tenant_id, period_id=env.periods[2], actor=env.parties["approver"], now=NOW
        )
        db.commit()
    post_legacy(engine, env, date(2026, 1, 15), [("5000", "100.005"), ("2000", "-100.005")])
    post_legacy(engine, env, date(2026, 2, 15), [("5000", "50.004"), ("2000", "-50.004")])


def store(engine, env, items, legacy, *, rounding="6900", extra_legacy=None, inputs_sha=None):
    items, itemised_68, import_sha = bind_rounding(items, SHA)
    raw = {"version": 1, "snapshot_sha256": SHA, "rounding_account_code": rounding, "items": items}
    # the legacy side and the universe stand in for the snapshot recomputation (the file never carries them)
    inputs = bridge.with_snapshot(
        bridge.parse_inputs(raw, inputs_sha or uuid4().hex * 2),
        {k: Decimal(v) for k, v in {**legacy, **(extra_legacy or {})}.items()},
        frozenset(i["ref"] for i in items),
    )
    with Session(engine) as db:
        opening_net, ias8, tb_sha, ias8_sha = bridge.ledger_inputs(db, env.tenant_id)
        body, _itemised = bridge.compute(
            inputs,
            opening_net=opening_net,
            ias8=ias8,
            opening_tb_sha=tb_sha,
            ias8_sha=ias8_sha,
            import_itemised=itemised_68,
            import_sha=import_sha,
        )
        row, _created = bridge.store_version(db, env.tenant_id, body, created_by="operator-a")
        db.commit()
        return row.version, row.sha256


def sign(engine, env, version, sha, who="Michael"):
    with Session(engine) as db:
        _row, now = bridge.sign_version(
            db, env.tenant_id, version=version, expect_sha=sha, signed_by=who, approval_reference=REF
        )
        db.commit()
        return now


def refusal_code(fn, *a, **kw):
    with pytest.raises((LedgerRefused, JournalRefused)) as caught:
        fn(*a, **kw)
    return caught.value.code


def test_a_ready_bridge_signs_once_and_a_signed_version_is_immutable(engine):
    env = world(engine)
    seed_clean(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    assert sign(engine, env, version, sha) is True
    assert sign(engine, env, version, sha) is False  # same signer: a replay
    with Session(engine) as db:
        assert (
            refusal_code(
                bridge.sign_version,
                db,
                env.tenant_id,
                version=version,
                expect_sha=sha,
                signed_by="Someone Else",
                approval_reference=REF,
            )
            == "bridge_already_signed"
        )
    for statement in (
        "UPDATE erp_bridge_versions SET signed_by = 'Mallory'",
        "UPDATE erp_bridge_versions SET body = '{}'::jsonb",
        "UPDATE erp_bridge_versions SET signed_at = NULL, signed_by = NULL",
        "DELETE FROM erp_bridge_versions",
    ):
        with engine.connect() as c, pytest.raises(DBAPIError):
            c.execute(sa.text(statement + " WHERE tenant_id = :t"), {"t": env.tenant_id})
    with Session(engine) as db:
        row = bridge.latest(db, env.tenant_id)
        assert row.signed_by == "Michael" and bridge.verified_body(row)["ready"]


def test_an_unsigned_version_body_is_immutable_too_and_only_the_latest_signs(engine):
    env = world(engine)
    v1, sha1 = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    with engine.connect() as c, pytest.raises(DBAPIError, match="body is immutable"):
        c.execute(sa.text("UPDATE erp_bridge_versions SET ready = false WHERE tenant_id = :t"), {"t": env.tenant_id})
    changed = [item("1", "carry", "J1", "5000", "2026-01", "11.00", "11.00")]
    v2, sha2 = store(engine, env, changed, {("5000", "2026-01"): "11.00"})
    assert (v1, v2) == (1, 2) and sha1 != sha2
    with Session(engine) as db:
        assert (
            refusal_code(
                bridge.sign_version,
                db,
                env.tenant_id,
                version=v1,
                expect_sha=sha1,
                signed_by="Michael",
                approval_reference=REF,
            )
            == "bridge_version_not_latest"
        )
        assert (
            refusal_code(
                bridge.sign_version,
                db,
                env.tenant_id,
                version=v2,
                expect_sha="0" * 64,
                signed_by="Michael",
                approval_reference=REF,
            )
            == "bridge_sha_mismatch"
        )
    # the same inputs replay the latest version instead of minting another
    again = ("5" * 64, "5000")
    first = store(engine, env, changed, {("5000", "2026-01"): "11.00"}, inputs_sha=again[0])
    assert store(engine, env, changed, {("5000", "2026-01"): "11.00"}, inputs_sha=again[0]) == first


def test_a_bridge_with_a_gap_or_a_queue_cannot_be_signed(engine):
    env = world(engine)
    version, sha = store(engine, env, CLEAN_ITEMS, {("5000", "2026-01"): "10.000001"})  # one micro off: no tolerance
    with Session(engine) as db:
        assert (
            refusal_code(
                bridge.sign_version,
                db,
                env.tenant_id,
                version=version,
                expect_sha=sha,
                signed_by="Michael",
                approval_reference=REF,
            )
            == "bridge_not_ready"
        )
        assert (
            db.scalar(sa.text("SELECT ready FROM erp_bridge_versions WHERE tenant_id = :t"), {"t": env.tenant_id})
            is False
        )
    with engine.connect() as c, pytest.raises(DBAPIError, match="signature"):  # the CHECK: a signature needs ready
        c.execute(
            sa.text("UPDATE erp_bridge_versions SET signed_by = 'M', signed_at = now() WHERE tenant_id = :t"),
            {"t": env.tenant_id},
        )


def test_a_tampered_body_is_detected(engine):
    env = world(engine)
    store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    row = SimpleNamespace(body={"version": 1, "ready": True}, sha256="0" * 64)
    assert refusal_code(bridge.verified_body, row) == "bridge_body_tampered"


def test_the_opening_period_of_the_bridge_is_carried_by_the_posted_ledger(engine):
    env = world(engine)
    post_opening(engine, env)
    legacy = {(c, "OPENING"): n for c, n in tb_rows().items()}
    version, sha = store(engine, env, [], {}, extra_legacy=legacy)
    with Session(engine) as db:
        body = bridge.verified_body(bridge.latest(db, env.tenant_id))
    ap = next(r for r in body["rows"] if r["account"] == "2000" and r["period"] == "OPENING")
    assert (ap["carry"], ap["ias8"], ap["partition_gap"]) == ("-400.000000", "1.000000", "0.000000")
    assert body["ready"]
    with Session(engine) as db:
        assert ledger_check.difference_rows(db, env.tenant_id, body) == []
    assert sign(engine, env, version, sha)


# --- the flip gate -------------------------------------------------------------------------------------------------


def fact(key="held_draft:j1", outcome=None, evidence=SHA, detail=None):
    return register.Fact("E16a", key, "held_drafts", outcome, evidence, detail or {"v": 1})


def load(engine, env, facts, sources=("held_drafts",)):
    with Session(engine) as db:
        out = register.reconcile(
            db, env.tenant_id, facts, loaded_sources=set(sources), receipt_sha="2" * 64, apply=True
        )
        db.commit()
        return out


def gate(engine, env):
    with Session(engine) as db:
        return register.gate(db, env.tenant_id)


@pytest.fixture
def owners_available(monkeypatch):
    """The owner subledgers not built in v2 yet (1400, 2045, 2110) stand in as zero; the gate REFUSES without them."""
    from erp_v2.migration_ledger import ledger_check

    zero = lambda db, tenant: Decimal(0)  # noqa: E731
    monkeypatch.setitem(ledger_check._SUBLEDGERS, "1400", zero)
    monkeypatch.setitem(ledger_check._SUBLEDGERS, "2045", zero)
    monkeypatch.setitem(ledger_check._SUBLEDGERS, "2110", zero)


def cover_all(engine, env, *, except_classes=()):
    """Declare every inventory class not applicable (except those the test loads itself)."""
    from erp_v2.migration_ledger.classes import CLASSES

    with Session(engine) as db:
        have = {
            c
            for (c,) in db.execute(
                sa.text("SELECT class_code FROM erp_migration_exceptions WHERE tenant_id = :t"), {"t": env.tenant_id}
            )
        }
        for code in CLASSES:
            if code not in have and code not in except_classes:
                register.declare_na(
                    db, env.tenant_id, class_code=code, reason="test", evidence_sha256=SHA, operator="o"
                )
        db.commit()


def test_the_gate_refuses_with_one_open_item_even_when_the_bridge_is_signed(engine, owners_available):
    env = world(engine)
    seed_clean(engine, env)
    load(engine, env, [fact()])
    cover_all(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    sign(engine, env, version, sha)
    g = gate(engine, env)
    assert (
        not g["passed"] and g["open_count"] == 1 and g["reasons"] == ["open_exceptions"] and g["latest_bridge_signed"]
    )
    with Session(engine) as db:
        register.resolve(
            db, env.tenant_id, class_code="E16a", legacy_key="held_draft:j1", outcome="voided", evidence_sha256=SHA
        )
        db.commit()
    assert gate(engine, env)["passed"]


def test_the_gate_needs_a_signed_latest_bridge_even_at_zero_open(engine, owners_available):
    env = world(engine)
    seed_clean(engine, env)
    cover_all(engine, env)
    assert gate(engine, env)["reasons"] == ["no_bridge_version"]
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    assert gate(engine, env)["reasons"] == ["latest_bridge_not_signed"]
    sign(engine, env, version, sha)
    assert gate(engine, env)["passed"]
    store(engine, env, [item("1", "carry", "J1", "5000", "2026-01", "12.00", "12.00")], {("5000", "2026-01"): "12.00"})
    g = gate(engine, env)  # a new version supersedes the signed one: Michael must sign it
    assert not g["passed"] and g["reasons"] == ["latest_bridge_not_signed"] and g["latest_bridge_version"] == 2


def test_the_gate_refuses_an_incomplete_class_coverage_until_every_class_is_loaded_or_declared_na(
    engine, owners_available
):
    from erp_v2.migration_ledger.classes import CLASSES

    env = world(engine)
    seed_clean(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    sign(engine, env, version, sha)
    g = gate(engine, env)
    assert g["reasons"] == ["class_coverage_incomplete"] and g["classes_missing"] == list(CLASSES)
    cover_all(engine, env, except_classes=("E27",))
    assert gate(engine, env)["classes_missing"] == ["E27"]
    with Session(engine) as db:
        assert (
            refusal_code(
                register.declare_na, db, env.tenant_id, class_code="E99", reason="x", evidence_sha256=SHA, operator="o"
            )
            == "unknown_exception_class"
        )
        assert (
            refusal_code(
                register.declare_na,
                db,
                env.tenant_id,
                class_code="E27",
                reason="x" * 501,
                evidence_sha256=SHA,
                operator="o",
            )
            == "reason_required"
        )
        row = register.declare_na(
            db,
            env.tenant_id,
            class_code="E27",
            reason="retained earnings is 3100 (ruled)",
            evidence_sha256=SHA,
            operator="o",
        )
        assert row.state == "resolved" and row.terminal_outcome == "not_applicable" and row.legacy_key == "class:E27"
        assert row.detail["plan_sha256"] == "7160002d479d502df384ea98c6b150c9ceadf2f0b3a5b36e00a71ad8b63be608"
        db.commit()
    assert gate(engine, env)["passed"]
    with engine.connect() as c, pytest.raises(DBAPIError, match="never deleted"):
        c.execute(
            sa.text("DELETE FROM erp_migration_exceptions WHERE class_code = 'E27' AND tenant_id = :t"),
            {"t": env.tenant_id},
        )


def test_manual_add_takes_only_a_class_from_the_closed_list_and_opens_the_gate_back_up(engine, owners_available):
    env = world(engine)
    seed_clean(engine, env)
    cover_all(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    sign(engine, env, version, sha)
    assert gate(engine, env)["passed"]
    with Session(engine) as db:
        assert (
            refusal_code(
                register.add_item,
                db,
                env.tenant_id,
                class_code="E99",
                legacy_key="k",
                evidence_sha256=SHA,
                operator="o",
            )
            == "unknown_exception_class"
        )
        assert (
            refusal_code(
                register.add_item,
                db,
                env.tenant_id,
                class_code="E26",
                legacy_key="class:E26",
                evidence_sha256=SHA,
                operator="o",
            )
            == "legacy_key_reserved"
        )
        register.add_item(
            db, env.tenant_id, class_code="E26", legacy_key="fallback-account", evidence_sha256=SHA, operator="o"
        )
        db.commit()
    assert gate(engine, env)["reasons"] == ["open_exceptions"]


def test_the_gate_refuses_when_the_ledger_moved_since_signature(engine, owners_available):
    env = world(engine)
    seed_clean(engine, env)
    cover_all(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    sign(engine, env, version, sha)
    assert gate(engine, env)["passed"]
    post_legacy(engine, env, date(2026, 1, 20), [("5000", "1.00"), ("1000", "-1.00")], owner="erp.test-native")
    g = gate(engine, env)  # a v2-native posting is expected by the bridge, but it moved the ledger after signing
    assert g["reasons"] == ["ledger_moved_since_signature"]


def test_the_gate_refuses_when_the_recomputed_ledger_differs_from_the_bridge(engine, owners_available):
    env = world(engine)
    seed_clean(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    post_legacy(
        engine, env, date(2026, 1, 20), [("5000", "0.01"), ("1000", "-0.01")]
    )  # a legacy-owned line the bridge lacks
    with Session(engine) as db:
        assert (
            refusal_code(
                bridge.sign_version,
                db,
                env.tenant_id,
                version=version,
                expect_sha=sha,
                signed_by="Michael",
                approval_reference=REF,
            )
            == "v2_ledger_differs_from_bridge"
        )


def test_control_accounts_are_tied_to_the_owner_subledgers_and_unavailable_ones_refuse_by_name(engine):
    from erp_v2.migration_ledger import ledger_check

    env = world(engine)
    with Session(engine) as db:
        state = ledger_check.control_state(db, env.tenant_id)
    # nothing posted: 2000, 2040, 1410 tie at 0 (the owner tables are read); 2030 is 0; the unbuilt owners refuse
    assert [c for c, v in state.items() if v["ok"]] == ["1410", "2000", "2040", "2030"] or {
        c for c, v in state.items() if v["ok"]
    } == {"1410", "2000", "2040", "2030"}
    assert state["1400"]["reason"] == "control_subledger_unavailable:1400"
    assert state["2045"]["reason"] == "control_subledger_unavailable:2045"
    assert state["2110"]["reason"] == "control_subledger_unavailable:2110"
    # a GL posting on 2000 with no payables document breaks the tie; a posting on 2030 breaks the zero rule
    post_legacy(engine, env, date(2026, 1, 15), [("2000", "-5.00"), ("1000", "5.00")])
    with Session(engine) as db:
        assert ledger_check.control_state(db, env.tenant_id)["2000"]["reason"] == "control_not_tied:2000"


def test_sign_binds_an_approval_reference_in_the_register_version_format(engine):
    env = world(engine)
    seed_clean(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    with Session(engine) as db:
        for bad in ("Michael said so", "dotmac-debt-register v", "dotmac-debt-register 1700", ""):
            assert (
                refusal_code(
                    bridge.sign_version,
                    db,
                    env.tenant_id,
                    version=version,
                    expect_sha=sha,
                    signed_by="Michael",
                    approval_reference=bad,
                )
                == "invalid_approval_reference"
            )
    sign(engine, env, version, sha)
    with Session(engine) as db:
        row = bridge.latest(db, env.tenant_id)
        assert row.approval_reference == REF and row.signed_state["control_lists"]["tie"] == [
            "1400",
            "1410",
            "2000",
            "2040",
            "2045",
        ]
        assert row.signed_state["register"]["missing_classes"]  # the signature covers the register counts too
    with engine.connect() as c, pytest.raises(DBAPIError, match="immutable"):  # a signed version never changes
        c.execute(
            sa.text("UPDATE erp_bridge_versions SET approval_reference = NULL WHERE tenant_id = :t"),
            {"t": env.tenant_id},
        )


def test_reconcile_carries_resolved_reopens_on_changed_facts_and_reports_orphans(engine):
    env = world(engine)
    load(engine, env, [fact("a"), fact("b")])
    with Session(engine) as db:
        register.resolve(db, env.tenant_id, class_code="E16a", legacy_key="a", outcome="no_action", evidence_sha256=SHA)
        db.commit()
    out = load(engine, env, [fact("a"), fact("b")])
    assert out["carried_resolved"] == 1 and out["unchanged_open"] == 1
    out = load(
        engine, env, [fact("a", detail={"v": 2})]
    )  # the legacy facts changed: reopen; b vanished: orphan, still open
    assert out["reopened"] == 1 and out["orphan_open"] == 1
    assert gate(engine, env)["open_count"] == 2
    with Session(engine) as db:
        assert (
            refusal_code(
                register.resolve,
                db,
                env.tenant_id,
                class_code="E16a",
                legacy_key="a",
                outcome="parked",
                evidence_sha256=SHA,
            )
            == "invalid_terminal_outcome"
        )
        assert (
            refusal_code(
                register.resolve,
                db,
                env.tenant_id,
                class_code="E16a",
                legacy_key="zz",
                outcome="voided",
                evidence_sha256=SHA,
            )
            == "exception_unknown"
        )


def test_a_resolved_row_must_carry_its_evidence_and_rows_are_never_deleted(engine):
    env = world(engine)
    load(engine, env, [fact()])
    with engine.connect() as c, pytest.raises(DBAPIError):  # the shape CHECK: resolved needs an outcome and evidence
        c.execute(
            sa.text("UPDATE erp_migration_exceptions SET state = 'resolved' WHERE tenant_id = :t"), {"t": env.tenant_id}
        )
    with engine.connect() as c, pytest.raises(DBAPIError, match="never deleted"):
        c.execute(sa.text("DELETE FROM erp_migration_exceptions WHERE tenant_id = :t"), {"t": env.tenant_id})


# --- the rounding-residue correction ---------------------------------------------------------------------------------


def prepare(engine, env, sha, period, *, account="6900", evidence="FIN-ROUND-1"):
    with Session(engine) as db:
        out = journals.prepare_rounding_correction(
            db,
            tenant_id=env.tenant_id,
            actor=env.parties["preparer"],
            now=NOW,
            command=journals.RoundingCorrection(sha, period, account, evidence, "Rounding residue"),
        )
        db.commit()
        return out


def request_and_post(engine, env, manual_id):
    with Session(engine) as db:
        journals.request_post(db, tenant_id=env.tenant_id, manual_id=manual_id, actor=env.parties["requester"], now=NOW)
        db.commit()
    with Session(engine) as db:
        out = journals.approve_and_post(
            db, tenant_id=env.tenant_id, manual_id=manual_id, approver=env.parties["approver"], now=NOW
        )
        db.commit()
        return out


def rounding_journals(engine, env):
    with Session(engine) as db:
        return list(
            db.scalars(
                sa.select(JournalEntry).where(
                    JournalEntry.tenant_id == env.tenant_id,
                    JournalEntry.source_document_kind == rounding_verify.ROUNDING_KIND,
                )
            )
        )


def test_the_rounding_journal_posts_once_against_the_named_account_and_clears_the_residue(engine):
    env = world(engine)
    seed_round_ledger(engine, env)
    version, sha = store(engine, env, ROUND_FULL_ITEMS, ROUND_FULL_LEGACY)
    # residue Jan = subledger - legacy GL on 2000 = -0.005 (the subledger holds more credit): the GL must move by -0.005
    out = prepare(engine, env, sha, "2026-01")
    assert out.state == "PREPARED" and not out.replayed
    posted = request_and_post(engine, env, out.manual_id)
    assert posted.state == "POSTED"
    (journal,) = [j for j in rounding_journals(engine, env) if j.source_document_id.endswith("2026-01")]
    assert journal.source_owner == "erp.manual" and journal.source_document_id == f"{sha}:2026-01"
    assert journal.posting_date == date(2026, 1, 1) and journal.status.value == "POSTED"
    assert ledger_net(engine, env, "2000", owner="erp.manual") == Decimal("-0.005")  # Cr 2000 0.005
    assert ledger_net(engine, env, "6900", owner="erp.manual") == Decimal("0.005")  # Dr rounding account
    with Session(engine) as db:
        body = bridge.verified_body(bridge.latest(db, env.tenant_id))
        assert rounding_verify.pending_periods(db, env.tenant_id, body) == ["2026-02"]
    # at most once: a repeat replays the posted journal, and nothing more is due for the period
    again = prepare(engine, env, sha, "2026-01")
    assert again.replayed and again.manual_id == out.manual_id
    with Session(engine) as db:
        assert (
            refusal_code(
                journals.request_post,
                db,
                tenant_id=env.tenant_id,
                manual_id=out.manual_id,
                actor=env.parties["requester"],
            )
            == "state_refused"
        )
    assert len(rounding_journals(engine, env)) == 1
    # sign refuses while Feb is outstanding, passes after it posts
    with Session(engine) as db:
        assert (
            refusal_code(
                bridge.sign_version,
                db,
                env.tenant_id,
                version=version,
                expect_sha=sha,
                signed_by="Michael",
                approval_reference=REF,
            )
            == "rounding_journal_pending"
        )
    request_and_post(engine, env, prepare(engine, env, sha, "2026-02").manual_id)
    assert sign(engine, env, version, sha)
    # Jan: Cr 2000 0.005; Feb: Dr 2000 0.004 (the subledger carries 0.004 less credit)
    assert ledger_net(engine, env, "2000", owner="erp.manual") == Decimal("-0.001")
    assert ledger_net(engine, env, "6900", owner="erp.manual") == Decimal("0.001")


def test_the_correction_is_dated_in_the_invoices_period_when_open_otherwise_the_first_open_period(engine):
    env = world(engine)
    with Session(engine) as db:  # the FY2025 months are closed after the opening: the first open period is Jan 2026
        accounting.soft_close_period(
            db,
            scope=TenantScope(env.tenant_id),
            period_id=env.periods["dec25"],
            evidence=PeriodCloseEvidence((CloseCheck("CHECK1", True, "evidence-1", "e" * 64),)),
            actor_ref="test",
            approval_reference="close-1",
            occurred_at=NOW,
        )
        db.commit()
    _, sha = store(engine, env, ROUND_ITEMS, ROUND_LEGACY)
    request_and_post(engine, env, prepare(engine, env, sha, "2026-02").manual_id)  # Feb is FUTURE: first open = Jan 1
    with Session(engine) as db:
        journals.open_period(
            db, tenant_id=env.tenant_id, period_id=env.periods[2], actor=env.parties["approver"], now=NOW
        )
        db.commit()
    _, sha2 = store(
        engine,
        env,
        [
            *ROUND_ITEMS,
            item("r3", "transform", "C", "2000", "2026-02", "-1.001", "-1.00", rule="T-ROUND-2DP", scope="header"),
        ],
        {**ROUND_LEGACY, ("2000", "2026-02"): "-51.005"},
    )
    dates = {j.source_document_id.rsplit(":", 1)[1]: j.posting_date for j in rounding_journals(engine, env)}
    assert dates == {"2026-02": date(2026, 1, 1)}
    # the later bridge version supersedes; a period already cleared leaves only the new difference due
    with Session(engine) as db:
        body = bridge.verified_body(bridge.latest(db, env.tenant_id))
        # residue Feb is now +0.005 (0.004 + 0.001): 2000 must net +0.005, of which +0.004 is already posted
        assert rounding_verify.due(db, env.tenant_id, body, "2026-02") == Decimal("0.001")
    assert sha2 != sha


def test_after_the_correction_gl_2000_equals_the_subledger_and_a_not_ready_bridge_cannot_anchor(engine):
    env = world(engine)
    seed_round_ledger(engine, env)
    version, sha = store(engine, env, ROUND_FULL_ITEMS, ROUND_FULL_LEGACY)
    for period in ("2026-01", "2026-02"):
        request_and_post(engine, env, prepare(engine, env, sha, period).manual_id)
    # GL(2000) = the legacy journals + the corrections; the subledger = the v2 (2 dp) totals of the rounded invoices
    subledger = sum((Decimal(i["v2_amount"]) for i in ROUND_ITEMS), Decimal(0))
    assert ledger_net(engine, env, "2000") == subledger == Decimal("-150.01")
    assert ledger_net(engine, env, "2000", owner="erp.manual") + ledger_net(engine, env, "6900") == 0
    assert sign(engine, env, version, sha)  # the recomputed v2 ledger equals the bridge exactly
    # a bridge with a queued item is not ready: it can no longer anchor a correction
    queued = [*ROUND_ITEMS, item("q1", "queue", "Q", "5000", "2026-01", "3.00", "0")]
    env2 = world(engine)
    _, sha2 = store(engine, env2, queued, {**ROUND_LEGACY, ("5000", "2026-01"): "3.00"})
    with Session(engine) as db:
        assert (
            refusal_code(
                journals.prepare_rounding_correction,
                db,
                tenant_id=env2.tenant_id,
                actor=env2.parties["preparer"],
                command=journals.RoundingCorrection(sha2, "2026-01", "6900", "E", "d"),
            )
            == "rounding_bridge_not_ready"
        )
    assert version == 1


def test_ias8_is_dated_exactly_in_the_first_fy2026_period_and_refused_when_it_is_not_open(engine):
    env = world(engine)
    with Session(engine) as db:  # Jan 2026 closes, Feb opens: a LATER open period is never used
        accounting.soft_close_period(
            db,
            scope=TenantScope(env.tenant_id),
            period_id=env.periods[1],
            evidence=PeriodCloseEvidence((CloseCheck("CHECK1", True, "evidence-1", "e" * 64),)),
            actor_ref="test",
            approval_reference="close-1",
            occurred_at=NOW,
        )
        journals.open_period(
            db, tenant_id=env.tenant_id, period_id=env.periods[2], actor=env.parties["approver"], now=NOW
        )
        db.commit()
    with Session(engine) as db, pytest.raises(LedgerRefused) as caught:
        opening.apply_opening(db, env.tenant_id, opening_inputs(), apply_inputs(env))
    assert caught.value.code == "ias8_first_period_not_open" and count_journals(engine, env) == 0


def test_the_correction_is_dated_in_the_invoices_period_when_that_period_is_open(engine):
    env = world(engine)
    with Session(engine) as db:
        journals.open_period(
            db, tenant_id=env.tenant_id, period_id=env.periods[2], actor=env.parties["approver"], now=NOW
        )
        db.commit()
    _, sha = store(engine, env, ROUND_ITEMS, ROUND_LEGACY)
    request_and_post(engine, env, prepare(engine, env, sha, "2026-02").manual_id)
    (journal,) = rounding_journals(engine, env)
    assert journal.posting_date == date(2026, 2, 1)


def test_a_missing_rounding_account_is_refused_everywhere(engine):
    env = world(engine)
    _, sha = store(engine, env, ROUND_ITEMS, ROUND_LEGACY)
    with Session(engine) as db:
        assert (
            refusal_code(
                journals.prepare_rounding_correction,
                db,
                tenant_id=env.tenant_id,
                actor=env.parties["preparer"],
                command=journals.RoundingCorrection(sha, "2026-01", None, "E", "d"),
            )
            == "rounding_account_required"
        )
        assert (
            refusal_code(
                journals.prepare_rounding_correction,
                db,
                tenant_id=env.tenant_id,
                actor=env.parties["preparer"],
                command=journals.RoundingCorrection(sha, "2026-01", "6999", "E", "d"),
            )
            == "rounding_account_mismatch"
        )
    # the bridge itself refuses to be built without the account when a residue exists
    raw = {
        "version": 1,
        "snapshot_sha256": SHA,
        "rounding_account_code": None,
        "items": ROUND_ITEMS,
    }
    raw["items"], itemised_68, import_sha = bind_rounding(ROUND_ITEMS, SHA)
    inputs = bridge.with_snapshot(
        bridge.parse_inputs(raw, "3" * 64), {k: Decimal(v) for k, v in ROUND_LEGACY.items()}, frozenset({"A", "B"})
    )
    assert (
        refusal_code(
            bridge.compute,
            inputs,
            opening_net=None,
            ias8=[],
            opening_tb_sha=None,
            ias8_sha=None,
            import_itemised=itemised_68,
            import_sha=import_sha,
        )
        == "rounding_account_required"
    )
    assert count_journals(engine, env) == 0


def test_the_correction_is_re_verified_against_the_bridge_at_request_and_approval(engine):
    env = world(engine)
    _, sha = store(engine, env, ROUND_ITEMS, ROUND_LEGACY)
    out = prepare(engine, env, sha, "2026-01")
    # a different journal amount than the bridge says is due (tampered at rest) is refused at the request
    with engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE mod_accounting.journal_lines SET debit_functional = debit_functional + 1, debit = debit + 1 "
                "WHERE tenant_id = :t AND debit > 0 "
                "AND journal_id = (SELECT journal_id FROM erp_manual_journals WHERE id = :m)"
            ),
            {"t": env.tenant_id, "m": out.manual_id},
        )
    with Session(engine) as db:
        assert (
            refusal_code(
                journals.request_post,
                db,
                tenant_id=env.tenant_id,
                manual_id=out.manual_id,
                actor=env.parties["requester"],
            )
            == "rounding_amount_changed"
        )


def test_a_superseded_bridge_version_can_no_longer_anchor_a_correction(engine):
    env = world(engine)
    _, sha = store(engine, env, ROUND_ITEMS, ROUND_LEGACY)
    out = prepare(engine, env, sha, "2026-01")
    store(
        engine,
        env,
        [*ROUND_ITEMS, item("r9", "carry", "Z", "5000", "2026-01", "1.00", "1.00")],
        {**ROUND_LEGACY, ("5000", "2026-01"): "1.00"},
    )
    with Session(engine) as db:
        assert (
            refusal_code(
                journals.request_post,
                db,
                tenant_id=env.tenant_id,
                manual_id=out.manual_id,
                actor=env.parties["requester"],
            )
            == "rounding_bridge_superseded"
        )
        assert (
            refusal_code(
                journals.prepare_rounding_correction,
                db,
                tenant_id=env.tenant_id,
                actor=env.parties["preparer"],
                command=journals.RoundingCorrection(sha, "2026-02", "6900", "E", "d"),
            )
            == "rounding_bridge_superseded"
        )


def test_an_ordinary_manual_journal_still_cannot_touch_2000(engine):
    env = world(engine)
    cmd = journals.PrepareJournal(
        request_id=uuid4(),
        posting_date=date(2026, 1, 10),
        kind=journals.JournalKind.STANDARD,
        description="x",
        evidence_ref="E",
        lines=(
            journals.LineInput(env.accounts["2000"], credit=Decimal("1")),
            journals.LineInput(env.accounts["6900"], debit=Decimal("1")),
        ),
    )
    with Session(engine) as db:
        assert (
            refusal_code(
                journals.prepare_journal,
                db,
                tenant_id=env.tenant_id,
                actor=env.parties["preparer"],
                command=cmd,
                now=NOW,
            )
            == "control_account_requires_subledger_document"
        )


def test_a_rounding_account_that_is_a_control_account_is_refused(engine):
    env = world(engine)
    _, sha = store(engine, env, ROUND_ITEMS, ROUND_LEGACY, rounding="1410")
    with Session(engine) as db:
        assert (
            refusal_code(
                journals.prepare_rounding_correction,
                db,
                tenant_id=env.tenant_id,
                actor=env.parties["preparer"],
                command=journals.RoundingCorrection(sha, "2026-01", "1410", "E", "d"),
            )
            == "offset_is_control_account"
        )


# --- CLIs -----------------------------------------------------------------------------------------------------------


def write(path, body):
    path.write_text(json.dumps(body))
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_opening_cli_plan_then_apply_with_private_receipts(engine, monkeypatch, tmp_path):
    env = world(engine)
    inputs = opening_inputs()
    tb = {
        "version": 1,
        "as_of": "2025-12-31",
        "rows": [
            {
                "account_code": c,
                "debit": n if not n.startswith("-") else "0",
                "credit": n[1:] if n.startswith("-") else "0",
            }
            for c, n in tb_rows().items()
        ],
    }
    sub = {
        "version": 1,
        "openings": {
            c: {"net": n, "source_receipt_sha256": SHA}
            for c, n in (("2000", "-399.00"), ("2040", "-100.00"), ("1410", "50.00"), ("1400", "300.00"))
        },
    }
    ias = {
        "version": 1,
        "items": [
            {
                "item_id": "IAS8-001",
                "account_code": "2000",
                "amount": "1.00",
                "evidence_sha256": SHA,
                "description": "AP overstated",
            }
        ],
    }
    files = {"tb": tmp_path / "tb.json", "subledger": tmp_path / "sl.json", "ias8": tmp_path / "ia.json"}
    shas = {k: write(files[k], v) for k, v in (("tb", tb), ("subledger", sub), ("ias8", ias))}
    spec = tmp_path / "spec.json"
    write(
        spec,
        {
            "version": 1,
            "tenant_id": str(env.tenant_id),
            "tb_sha256": shas["tb"],
            "subledger_sha256": shas["subledger"],
            "ias8_sha256": shas["ias8"],
            "evidence_ref": "FIN-OPENING-1",
            "requester_party_id": str(env.parties["requester"]),
            "approver_party_id": str(env.parties["approver"]),
        },
    )
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    base = [
        "--spec",
        str(spec),
        "--tb",
        str(files["tb"]),
        "--subledger",
        str(files["subledger"]),
        "--ias8",
        str(files["ias8"]),
    ]

    plan_receipt = tmp_path / "plan.json"
    assert opening_main(["plan", *base, "--receipt", str(plan_receipt), "--operator-ref", "operator-a"]) == 0
    plan = json.loads(plan_receipt.read_text())
    assert (os.stat(plan_receipt).st_mode & 0o777) == 0o600 and plan["ready"] and plan["proof"]["ok"]
    assert str(env.tenant_id) not in plan_receipt.read_text() and "postgresql" not in plan_receipt.read_text()
    assert count_journals(engine, env) == 0  # plan wrote nothing
    apply_args = [
        "apply",
        *base,
        "--plan-receipt",
        str(plan_receipt),
        "--approved-plan-sha256",
        plan["plan_sha256"],
        "--operator-ref",
        "operator-b",
        "--apply",
    ]
    dsn = lambda: monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))  # noqa: E731  (the CLI pops it)
    # no environment switch: refused before any write, and the receipt path is not even consumed
    assert opening_main([*apply_args, "--receipt", str(tmp_path / "a0.json")]) == 2 and count_journals(engine, env) == 0
    monkeypatch.setenv("ERP_MIGRATION_LEDGER_APPLY", "1")
    dsn()
    assert opening_main([*apply_args[:-1], "--receipt", str(tmp_path / "a1.json")]) == 2  # no --apply flag
    receipt = tmp_path / "a2.json"
    dsn()
    assert opening_main([*apply_args, "--receipt", str(receipt)]) == 0, receipt.read_text()
    applied = json.loads(receipt.read_text())
    assert applied["applied"] == {"opening": 1, "ias8": 1, "replayed": 0} and count_journals(engine, env) == 2
    # a bound plan cannot be reused with other bytes
    dsn()
    assert (
        opening_main(
            [
                "apply",
                *base[:-1],
                str(files["ias8"]),
                "--plan-receipt",
                str(plan_receipt),
                "--approved-plan-sha256",
                "0" * 64,
                "--operator-ref",
                "operator-b",
                "--apply",
                "--receipt",
                str(tmp_path / "a3.json"),
            ]
        )
        == 2
    )
    del inputs


def test_opening_cli_refuses_an_untied_control_before_touching_the_database(engine, monkeypatch, tmp_path):
    env = world(engine)
    tb = {
        "version": 1,
        "as_of": "2025-12-31",
        "rows": [
            {"account_code": "1000", "debit": "1.00", "credit": "0"},
            {"account_code": "3100", "debit": "0", "credit": "1.00"},
        ],
    }
    sub = {
        "version": 1,
        "openings": {c: {"net": "0", "source_receipt_sha256": SHA} for c in ("2000", "2040", "1410", "1400")},
    }
    ias = {"version": 1, "items": []}
    paths = {k: tmp_path / f"{k}.json" for k in ("tb", "sl", "ia", "spec")}
    shas = {k: write(paths[k], v) for k, v in (("tb", tb), ("sl", sub), ("ia", ias))}
    write(
        paths["spec"],
        {
            "version": 1,
            "tenant_id": str(env.tenant_id),
            "tb_sha256": shas["tb"],
            "subledger_sha256": shas["sl"],
            "ias8_sha256": shas["ia"],
            "evidence_ref": "E",
            "requester_party_id": str(env.parties["requester"]),
            "approver_party_id": str(env.parties["approver"]),
        },
    )
    tb["rows"][0]["debit"] = "2.00"  # the TB no longer sums to zero
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)  # no database at all: the refusal comes first
    receipt = tmp_path / "r.json"
    shas["tb"] = write(paths["tb"], tb)
    write(
        paths["spec"],
        {
            "version": 1,
            "tenant_id": str(env.tenant_id),
            "tb_sha256": shas["tb"],
            "subledger_sha256": shas["sl"],
            "ias8_sha256": shas["ia"],
            "evidence_ref": "E",
            "requester_party_id": str(env.parties["requester"]),
            "approver_party_id": str(env.parties["approver"]),
        },
    )
    code = opening_main(
        [
            "plan",
            "--spec",
            str(paths["spec"]),
            "--tb",
            str(paths["tb"]),
            "--subledger",
            str(paths["sl"]),
            "--ias8",
            str(paths["ia"]),
            "--receipt",
            str(receipt),
            "--operator-ref",
            "operator-a",
        ]
    )
    out = json.loads(receipt.read_text())
    assert code == 3 and out["refusal"] == "opening_tb_not_zero_sum" and not out["ready"]


def test_gate_cli_exit_codes(engine, monkeypatch, tmp_path, owners_available):
    from erp_v2.migration_ledger.exceptions_register import main as register_main

    env = world(engine)
    seed_clean(engine, env)
    spec = tmp_path / "spec.json"
    write(spec, {"version": 1, "tenant_id": str(env.tenant_id), "receipts": [], "include_admission_reviews": False})
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    load(engine, env, [fact()])
    cover_all(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    sign(engine, env, version, sha)
    receipt = tmp_path / "g1.json"
    assert register_main(["gate", "--spec", str(spec), "--receipt", str(receipt), "--operator-ref", "operator-a"]) == 3
    assert json.loads(receipt.read_text())["reasons"] == ["open_exceptions"]
    # a resolve without APPLY is refused; with it, the gate passes
    assert (
        register_main(
            [
                "resolve",
                "--spec",
                str(spec),
                "--class",
                "E16a",
                "--key",
                "held_draft:j1",
                "--outcome",
                "voided",
                "--evidence-sha256",
                SHA,
                "--receipt",
                str(tmp_path / "x.json"),
                "--operator-ref",
                "o",
            ]
        )
        == 2
    )
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("ERP_MIGRATION_LEDGER_APPLY", "1")
    assert (
        register_main(
            [
                "resolve",
                "--spec",
                str(spec),
                "--class",
                "E16a",
                "--key",
                "held_draft:j1",
                "--outcome",
                "voided",
                "--evidence-sha256",
                SHA,
                "--receipt",
                str(tmp_path / "y.json"),
                "--operator-ref",
                "o",
                "--apply",
            ]
        )
        == 0
    )
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    assert (
        register_main(
            ["gate", "--spec", str(spec), "--receipt", str(tmp_path / "g2.json"), "--operator-ref", "operator-a"]
        )
        == 0
    )


# --- migration ------------------------------------------------------------------------------------------------------


def test_migration_downgrade_refuses_while_evidence_exists(engine):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from erp_v2.migrations.versions import e2_0028_migration_ledger as migration

    env = world(engine)
    load(engine, env, [fact()])
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="migration ledger evidence exists"):
                    migration.downgrade()
        finally:
            transaction.rollback()


def test_declare_na_and_add_clis_are_governed_and_private(engine, monkeypatch, tmp_path):
    from erp_v2.migration_ledger.exceptions_register import main as register_main

    env = world(engine)
    spec = tmp_path / "spec.json"
    write(spec, {"version": 1, "tenant_id": str(env.tenant_id), "receipts": [], "include_admission_reviews": False})
    base = ["--spec", str(spec), "--evidence-sha256", SHA, "--operator-ref", "o"]

    def run(args, name, *, apply=True):
        monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        out = tmp_path / name
        code = register_main([*args, *base, "--receipt", str(out), *(["--apply"] if apply else [])])
        return code, out

    assert run(["declare-na", "--class", "E27", "--reason", "ruled"], "a.json")[0] == 2  # no environment switch
    monkeypatch.setenv("ERP_MIGRATION_LEDGER_APPLY", "1")
    assert run(["declare-na", "--class", "E27", "--reason", "ruled"], "b.json", apply=False)[0] == 2  # no --apply
    code, out = run(["declare-na", "--class", "E27", "--reason", "ruled"], "c.json")
    assert code == 0 and (os.stat(out).st_mode & 0o777) == 0o600
    assert run(["declare-na", "--class", "E99", "--reason", "x"], "d.json")[0] == 2
    assert run(["add", "--class", "E26", "--key", "fallback-account"], "e.json")[0] == 0
    with Session(engine) as db:
        rows = {
            r.class_code: r
            for r in db.scalars(sa.select(m.MigrationException).where(m.MigrationException.tenant_id == env.tenant_id))
        }
    assert rows["E27"].terminal_outcome == "not_applicable" and rows["E26"].state == "open"


def test_b2_outcomes_load_from_the_decisions_table_and_e17_is_derived(engine):
    from erp_v2.held_drafts.models import HeldDraftDecision

    env = world(engine)
    ev = {
        "key_sha256": "a" * 64,
        "statement_lines": 1,
        "posted_fee_journals": 2,
        "excess_posted_fee_journals": 1,
        "posted_journal_ids_sha256": "c" * 64,
    }
    with Session(engine) as db:
        for outcome, reason, jid in (
            ("queued", "no_published_rule", uuid4()),
            ("voided", "duplicate_of_posted_fee_excess", uuid4()),
            ("voided", "already_posted_duplicate", uuid4()),
        ):
            db.add(
                HeldDraftDecision(
                    tenant_id=env.tenant_id,
                    journal_id=jid,
                    rule_id="R",
                    outcome=outcome,
                    reason_code=reason,
                    amount=Decimal(1),
                    evidence=ev,
                    evidence_sha256="d" * 64,
                    rules_sha256="e" * 64,
                    plan_sha256="f" * 64,
                    plan_receipt_sha256="1" * 64,
                    manual_journal_id=None,
                    dual_control=False,
                    planned_by="a",
                    applied_by="b",
                )
            )
        db.commit()
        facts = register.from_held_decisions(db, env.tenant_id)
    by = {(f.class_code, f.terminal_outcome) for f in facts}
    assert by == {("E16a", None), ("E16", "voided"), ("E17", None)}
    assert sum(1 for f in facts if f.class_code == "E17") == 1 and sum(1 for f in facts if f.class_code == "E16") == 2


# --- recomputed legacy side and the #68 adapter ------------------------------------------------------------------


@pytest.fixture
def legacy_schema(engine):
    with engine.begin() as c:
        c.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS gl")
        c.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS ap")
        c.exec_driver_sql("CREATE TABLE gl.journal_entry (journal_entry_id uuid PRIMARY KEY, posting_date date)")
        c.exec_driver_sql(
            "CREATE TABLE gl.posted_ledger_line (account_id uuid, debit_amount numeric, credit_amount numeric, "
            "posting_date date, journal_entry_id uuid)"
        )
        c.exec_driver_sql(
            "CREATE TABLE ap.supplier_invoice "
            "(invoice_id uuid PRIMARY KEY, journal_entry_id uuid, ap_control_account_id uuid)"
        )
    yield
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA gl CASCADE")
        c.exec_driver_sql("DROP SCHEMA ap CASCADE")


def legacy_post(engine, jid, day, lines):
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO gl.journal_entry VALUES (:j, :d)"), {"j": jid, "d": day})
        for account, debit, credit in lines:
            c.execute(
                sa.text("INSERT INTO gl.posted_ledger_line VALUES (:a, :d, :c, :p, :j)"),
                {"a": account, "d": Decimal(debit), "c": Decimal(credit), "p": day, "j": jid},
            )


def test_the_legacy_side_is_recomputed_from_the_snapshot_by_account_id_and_period(engine, legacy_schema):
    from erp_v2.migration_ledger import legacy_ledger

    env = world(engine)
    j1, j2, j3 = uuid4(), uuid4(), uuid4()
    legacy_post(engine, j1, date(2025, 12, 20), [(env.accounts["5000"], "7", "0"), (env.accounts["1000"], "0", "7")])
    legacy_post(engine, j2, date(2026, 1, 5), [(env.accounts["5000"], "3.5", "0"), (env.accounts["2000"], "0", "3.5")])
    legacy_post(engine, j3, date(2026, 2, 6), [(env.accounts["5000"], "1", "0"), (env.accounts["2000"], "0", "1")])
    with engine.connect() as legacy, Session(engine) as db:
        net, universe = legacy_ledger.recompute(legacy, db, env.tenant_id, opening_as_of=date(2025, 12, 31))
    assert net == {
        ("5000", "OPENING"): Decimal("7"), ("1000", "OPENING"): Decimal("-7"),
        ("5000", "2026-01"): Decimal("3.5"), ("2000", "2026-01"): Decimal("-3.5"),
        ("5000", "2026-02"): Decimal("1"), ("2000", "2026-02"): Decimal("-1"),
    }  # fmt: skip
    assert universe == {str(j2), str(j3)}  # the FY2026 journal ids; the opening-year journal is not a bridge ref
    legacy_post(engine, uuid4(), date(2026, 3, 1), [(uuid4(), "1", "0")])  # an account the destination chart lacks
    with engine.connect() as legacy, Session(engine) as db:
        assert (
            refusal_code(legacy_ledger.recompute, legacy, db, env.tenant_id, opening_as_of=None)
            == "legacy_account_unmapped"
        )


def test_the_68_adapter_takes_period_and_account_from_the_snapshot(engine, legacy_schema):
    from erp_v2.migration_ledger import bridge_inputs

    env = world(engine)
    posted, unposted, wrong = uuid4(), uuid4(), uuid4()
    j = uuid4()
    legacy_post(
        engine, j, date(2026, 1, 9), [(env.accounts["5000"], "100.005", "0"), (env.accounts["2000"], "0", "100.005")]
    )
    with engine.begin() as c:
        for invoice, journal, control in (
            (posted, j, env.accounts["2000"]),
            (unposted, None, env.accounts["2000"]),
            (wrong, j, env.accounts["1000"]),
        ):
            c.execute(
                sa.text("INSERT INTO ap.supplier_invoice VALUES (:i, :j, :a)"),
                {"i": invoice, "j": journal, "a": control},
            )
    line = lambda inv, legacy_t, v2_t: {  # noqa: E731
        **{
            "rule": "T-ROUND-2DP",
            "scope": "header",
            "invoice_id": str(inv),
            "line_id": None,
            "legacy_account": None,
            "v2_account": None,
            "legacy_amount": legacy_t,
            "v2_amount": v2_t,
            "delta": format(Decimal(v2_t) - Decimal(legacy_t), "f"),
            "note": "n",
        }
    }

    def fingerprinted(lines):
        from migration_ledger_support import fingerprint_of

        out = []
        for ln in lines:
            ln = {**ln}
            ln["fingerprint"] = fingerprint_of(ln)
            out.append(ln)
        return out

    def run(lines, snapshot=SHA):
        itemised = {"binding": {"snapshot_sha256": SHA}, "bridge_lines": fingerprinted(lines), "queue": []}
        with engine.connect() as legacy, Session(engine) as db:
            return bridge_inputs.rounding_items(
                itemised, bridge.canonical_sha(itemised), snapshot, legacy, db, env.tenant_id
            )

    out = run([line(posted, "100.005", "100.01"), line(unposted, "20.004", "20.00")])
    assert out["excluded_not_legacy_posted"] == 1  # never legacy-posted: no GL residue, subledger-only
    (it,) = out["items"]
    assert (it["period"], it["account"], it["ref"], it["source_invoice_id"]) == ("2026-01", "2000", str(j), str(posted))
    assert (it["legacy_amount"], it["v2_amount"]) == ("-100.005000", "-100.010000")  # 2000 net terms: a credit
    assert refusal_code(run, [line(wrong, "1.005", "1.01")]) == "rounding_invoice_control_account_not_2000"
    assert refusal_code(run, [line(uuid4(), "1.005", "1.01")]) == "rounding_invoice_not_in_snapshot"
    assert (
        refusal_code(run, [line(posted, "100.005", "100.01")], snapshot="9" * 64) == "import_itemised_snapshot_mismatch"
    )
    # the produced item is exactly what the bridge's rounding door accepts
    items = [{**it, "evidence_sha256": it["evidence_sha256"]}]
    raw = {"version": 1, "snapshot_sha256": SHA, "rounding_account_code": "6900", "items": items}
    itemised = {
        "binding": {"snapshot_sha256": SHA},
        "bridge_lines": fingerprinted([line(posted, "100.005", "100.01")]),
        "queue": [],
    }
    inputs = bridge.with_snapshot(
        bridge.parse_inputs(raw, SHA), {("2000", "2026-01"): Decimal("-100.005")}, frozenset({str(j)})
    )
    body, _ = bridge.compute(
        inputs,
        opening_net=None,
        ias8=[],
        opening_tb_sha=None,
        ias8_sha=None,
        import_itemised=itemised,
        import_sha=bridge.canonical_sha(itemised),
    )
    assert body["rounding_residue_by_period"] == {"2026-01": "-0.005000"}
