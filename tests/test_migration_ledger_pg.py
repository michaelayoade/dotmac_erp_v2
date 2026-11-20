# ruff: noqa: E501  (long legacy-schema SQL fixtures)
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
    sub = {"2000": Decimal(two_thousand_sub), "2040": Decimal("-100"), "1410": Decimal("50"), "1400": Decimal("300")}
    items = (opening.Ias8Item("IAS8-001", "2000", Decimal("1.00"), SHA, "AP overstated at 2025 close"),) if ias8 else ()
    return opening.OpeningInputs(as_of, parsed, "c" * 64, sub, "d" * 64, items, "e" * 64, "a" * 64, "b" * 64)


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
    proof = opening.opening_proof(bad)  # as_is_v1: a control difference is REPORTED (E37), never a refusal
    assert proof["ok"] and [d["account"] for d in proof["control_differences"]] == ["2000"]
    opening.require_proof(proof)
    unbalanced = opening_inputs()
    unbalanced = opening.OpeningInputs(
        unbalanced.as_of,
        {**unbalanced.tb, "1000": Decimal("500.01")},
        "c" * 64,
        unbalanced.subledger,
        "d" * 64,
        unbalanced.ias8,
        "e" * 64,
        "a" * 64,
        "b" * 64,
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
        other.as_of,
        other.tb,
        "9" * 64,
        other.subledger,
        other.subledger_sha256,
        other.ias8,
        other.ias8_sha256,
        other.recomputed_tb_sha256,
        other.import_receipt_sha256,
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
    """The owner subledgers not built in v2 yet (1400, 2045) stand in as zero; the gate REFUSES without them."""
    from erp_v2.migration_ledger import ledger_check

    zero = lambda db, tenant: Decimal(0)  # noqa: E731
    monkeypatch.setitem(ledger_check._SUBLEDGERS, "1400", zero)
    monkeypatch.setitem(ledger_check._SUBLEDGERS, "2045", zero)
    # tests of the other reasons: the unbuilt checks (None) become no-ops; the built ones stay (see the unbuilt tests)
    monkeypatch.setattr(
        register, "AS_IS_V1_CHECKS", {k: v or (lambda db, tenant: []) for k, v in register.AS_IS_V1_CHECKS.items()}
    )


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
    # as_is_v1: an open work-queue item is a REPORT, not a gate
    assert g["passed"] and g["profile"] == "as_is_v1" and g["reasons"] == [] and g["latest_bridge_signed"]
    assert g["open_count"] == 1 and g["reports"]["open_count"] == 1 and g["reports"]["open_by_class"] == {"E16a": 1}
    with Session(engine) as db:
        register.resolve(
            db, env.tenant_id, class_code="E16a", legacy_key="held_draft:j1", outcome="voided", evidence_sha256=SHA
        )
        db.commit()
    g = gate(engine, env)
    assert g["passed"] and g["reports"]["open_count"] == 0


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


def test_class_coverage_is_reported_not_gated_and_every_class_can_be_declared_na(engine, owners_available):
    from erp_v2.migration_ledger.classes import CLASSES

    env = world(engine)
    seed_clean(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    sign(engine, env, version, sha)
    g = gate(engine, env)
    assert g["passed"] and g["classes_missing"] == list(CLASSES) == g["reports"]["classes_missing"]
    assert {"E35", "E36a", "E36b", "E37", "E38"} <= set(CLASSES)
    cover_all(engine, env, except_classes=("E27",))
    assert gate(engine, env)["classes_missing"] == ["E27"] and gate(engine, env)["passed"]
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
        assert row.detail["plan_sha256"] == "a28adf06752c43cd69481830c1b0e8d02a36f94a3bbe3c2ce35443d46b28de54"
        db.commit()
    assert gate(engine, env)["passed"] and gate(engine, env)["classes_missing"] == []
    with engine.connect() as c, pytest.raises(DBAPIError, match="never deleted"):
        c.execute(
            sa.text("DELETE FROM erp_migration_exceptions WHERE class_code = 'E27' AND tenant_id = :t"),
            {"t": env.tenant_id},
        )


def test_manual_add_takes_only_a_class_from_the_closed_list_and_is_a_report_item(engine, owners_available):
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
    g = gate(engine, env)  # the new open item is queue work for Finance after the flip, not a gate reason
    assert g["passed"] and g["reasons"] == [] and g["reports"]["open_by_class"] == {"E26": 1}


SPEC_SECTION_4_CHECKS = {
    "completeness_per_module_currency",
    "control_ties_per_currency",
    "prepayment_tie_r1",
    "holding_ties",
    "setup_keyed_accounts",
    "setup_control_and_tax_codes_exist_once",
    "setup_vat_deferral_pair",
    "setup_periods",
    "setup_writer_role_m1",
    "straddle_reproduced",
    "e15_subkobo_posted",
    "post_flip_tools_unused",
}


def test_the_profile_check_names_are_pinned_to_spec_section_4():
    # removing a name (or adding one) is a visible change to this pinned set, never a silent tuple edit
    assert set(register.AS_IS_V1_CHECKS) == SPEC_SECTION_4_CHECKS
    assert callable(register.AS_IS_V1_CHECKS["post_flip_tools_unused"])
    assert register.profile_checks_pending() == sorted(SPEC_SECTION_4_CHECKS - {"post_flip_tools_unused"})


def test_the_gate_refuses_profile_checks_unbuilt_for_every_none(engine, owners_available, monkeypatch):
    env = world(engine)
    seed_clean(engine, env)
    cover_all(engine, env)
    version, sha = store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    sign(engine, env, version, sha)
    assert gate(engine, env)["passed"]  # the fixture wired every None to a no-op
    checks = {**register.AS_IS_V1_CHECKS, "holding_ties": None, "setup_periods": None}
    monkeypatch.setattr(register, "AS_IS_V1_CHECKS", checks)
    g = gate(engine, env)
    assert not g["passed"] and g["reasons"] == ["profile_checks_unbuilt"]
    assert g["profile_checks_pending"] == ["holding_ties", "setup_periods"]
    monkeypatch.undo()
    # the shipped mapping refuses: every owner check is still None
    assert register.profile_checks_pending() and all(
        register.AS_IS_V1_CHECKS[k] is None for k in SPEC_SECTION_4_CHECKS - {"post_flip_tools_unused"}
    )


def test_the_gate_refuses_when_a_post_flip_tool_was_used_before_the_flip(engine, owners_available):
    env = world(engine)
    with Session(engine) as db:
        assert register._post_flip_tools_unused(db, env.tenant_id) == []
    post_opening(engine, env)  # the opening input carries an IAS 8 item: a post-flip tool used before the flip
    with Session(engine) as db:
        assert register._post_flip_tools_unused(db, env.tenant_id) == ["post_flip_tool_used_before_flip"]
    assert "post_flip_tool_used_before_flip" in gate(engine, env)["reasons"]


def _held_decision(env, outcome):
    from erp_v2.held_drafts.models import HeldDraftDecision

    return HeldDraftDecision(
        tenant_id=env.tenant_id, journal_id=uuid4(), rule_id="R", outcome=outcome, reason_code="already_posted_duplicate",
        amount=Decimal(1), evidence={"key_sha256": "a" * 64}, evidence_sha256="d" * 64, rules_sha256="e" * 64, plan_sha256="f" * 64,
        plan_receipt_sha256="1" * 64, manual_journal_id=None, dual_control=False, planned_by="a", applied_by="b",
    )  # fmt: skip


def test_post_flip_tool_guard_counts_held_draft_voids_and_posts_but_not_queued(engine):
    env = world(engine)
    with Session(engine) as db:
        db.add(_held_decision(env, "queued"))  # a queued decision posts nothing: only a register item
        db.commit()
        assert register._post_flip_tools_unused(db, env.tenant_id) == []
        db.add(_held_decision(env, "voided"))
        db.commit()
        assert register._post_flip_tools_unused(db, env.tenant_id) == ["post_flip_tool_used_before_flip"]


def test_post_flip_tool_guard_counts_an_admission_review(engine):
    from datetime import UTC, datetime

    from erp_v2.legacy_ap.models import AdmissionReview

    env = world(engine)
    with Session(engine) as db:
        assert register._post_flip_tools_unused(db, env.tenant_id) == []
        db.add(
            AdmissionReview(
                tenant_id=env.tenant_id, legacy_invoice_id=uuid4(), decision="admit_residual", waived_checks=[],
                evidence_sha="a" * 64, decision_file_sha="b" * 64, snapshot_sha256="c" * 64, decided_by="f",
                decided_at=datetime.now(UTC), operator_ref="o", dual_control=False,
            )
        )  # fmt: skip
        db.commit()
        assert register._post_flip_tools_unused(db, env.tenant_id) == ["post_flip_tool_used_before_flip"]


def test_migration_straddle_journals_are_not_v2_native_in_the_bridge_comparison(engine):
    """STRADDLE-SPEC (W8): the B-14 reproduction journals (erp.migration / migration_straddle) are planned, not native."""
    from dotmac_accounting.contracts import CreateJournal, JournalKind, JournalLineInput, SourceIdentity

    env = world(engine)

    def post(owner, kind):
        with Session(engine) as db:
            scope = TenantScope(env.tenant_id)
            j = accounting.create_journal(
                db, scope=scope,
                command=CreateJournal(
                    number=f"S-{uuid4().hex[:12]}", kind=JournalKind.STANDARD, entry_date=date(2026, 1, 15),
                    posting_date=date(2026, 1, 15), currency_code="USD", exchange_rate=Decimal(1), description="s",
                    reference="s",
                    source=SourceIdentity(owner=owner, document_kind=kind, document_id=f"d-{uuid4().hex}",
                        version="1", fingerprint="f" * 64),
                    lines=(JournalLineInput(env.accounts["5000"], Decimal(2), Decimal(0), None),
                           JournalLineInput(env.accounts["1000"], Decimal(0), Decimal(2), None)),
                ),
                idempotency_key=f"t:{uuid4()}", idempotency_expires_at=None, recorded_at=NOW,
            )  # fmt: skip
            accounting.post_journal(
                db, scope=scope, journal_id=j.id, approval_reference="t", posted_by="t",
                idempotency_key=f"p:{uuid4()}", idempotency_expires_at=None, posted_at=NOW,
            )  # fmt: skip
            db.commit()

    post("erp.migration", "migration_straddle")
    post("erp.migration", "migration_subkobo")  # E15 reproduction (T-SUBKOBO items explain its residue)
    post("erp.migration", "something_else")
    with Session(engine) as db:
        total, native = ledger_check.v2_posted(db, env.tenant_id)
    assert total[("5000", "2026-01")] == Decimal(6)
    assert native[("5000", "2026-01")] == Decimal(2)  # only the other kind counts as native


def test_the_bridge_body_records_the_gate_profile(engine, owners_available):
    env = world(engine)
    seed_clean(engine, env)
    cover_all(engine, env)
    store(engine, env, CLEAN_ITEMS, CLEAN_LEGACY)
    with Session(engine) as db:
        body = bridge.verified_body(bridge.latest(db, env.tenant_id))
    assert body["gate_profile"] == "as_is_v1"  # recorded in the bridge body: Michael's signature covers it


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
    assert {c for c, v in state.items() if v["ok"]} == {"1410", "2000", "2040"}
    assert state["1400"]["reason"] == "control_subledger_unavailable:1400"
    assert state["2045"]["reason"] == "control_subledger_unavailable:2045"
    assert set(state) == {"1400", "1410", "2000", "2040", "2045"}  # 2030 and 2110 left the gate (report-only)
    with Session(engine) as db:
        reports = ledger_check.report_state(db, env.tenant_id)
    assert set(reports) == {"2030", "2110"} and reports["2030"]["zero"] is True
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


def tb_file_for(tmp_path, rows=None):
    rows = rows or [
        {
            "account_code": c,
            "debit": n if not n.startswith("-") else "0",
            "credit": n[1:] if n.startswith("-") else "0",
        }
        for c, n in tb_rows().items()
    ]
    path = tmp_path / "tb.json"
    return path, write(path, {"version": 1, "as_of": "2025-12-31", "rows": rows})


def seed_cli_legacy(engine, env):
    """Legacy documents behind the subledger the CLI RECOMPUTES: 2000 -399, 1400 300, 2040 -100, 1410 0; and the
    legacy ledger at the opening date, which is the trial balance the CLI RECOMPUTES (``tb_rows``)."""
    legacy_post(
        engine,
        uuid4(),
        date(2025, 12, 30),
        [
            (env.accounts[c], n if not n.startswith("-") else "0", n[1:] if n.startswith("-") else "0")
            for c, n in tb_rows().items()
        ],
    )
    inv = uuid4()
    legacy_exec(
        engine,
        "INSERT INTO ap.supplier_invoice (invoice_id, journal_entry_id, functional_currency_amount) VALUES (:i, :j, 399)",
        i=inv,
        j=posted_journal(engine, date(2025, 6, 1)),
    )
    legacy_exec(
        engine,
        "INSERT INTO ar.invoice (invoice_id, journal_entry_id, functional_currency_amount) VALUES (:i, :j, 300)",
        i=uuid4(),
        j=posted_journal(engine, date(2025, 6, 1)),
    )
    legacy_exec(
        engine,
        "INSERT INTO expense.expense_claim VALUES (:c, :j, 100)",
        c=uuid4(),
        j=posted_journal(engine, date(2025, 6, 1)),
    )


def import_receipt_file(tmp_path, cutoff="2025-12-31", **over):
    """A FIXTURE importer execute receipt in the shape #74 (B-13, history cutoff) emits: ``history_cutoff`` section
    with the ISO date. Replace by a real receipt once #74 lands."""
    body = {"kind": "execute", "dry_run": False, "complete": True, "history_cutoff": {"history_cutoff": cutoff}}
    body.update(over)
    path = tmp_path / "import-receipt.json"
    write(path, body)
    return path


def cli_files(tmp_path, env, ias_items):
    tb_path, tb_sha = tb_file_for(tmp_path)
    ias_path = tmp_path / "ia.json"
    ias_sha = write(ias_path, {"version": 1, "items": ias_items})
    spec = tmp_path / "spec.json"
    write(spec, {"version": 1, "tenant_id": str(env.tenant_id), "tb_sha256": tb_sha, "ias8_sha256": ias_sha,
                 "evidence_ref": "FIN-OPENING-1", "requester_party_id": str(env.parties["requester"]),
                 "approver_party_id": str(env.parties["approver"])})  # fmt: skip
    return ["--spec", str(spec), "--tb", str(tb_path), "--ias8", str(ias_path),
            "--import-receipt", str(import_receipt_file(tmp_path))]  # fmt: skip


def item_row(item_id, code, amount):
    return {
        "item_id": item_id,
        "account_code": code,
        "amount": amount,
        "evidence_sha256": SHA,
        "description": "restate",
    }


def test_opening_cli_recomputes_the_subledger_and_applies_with_private_receipts(
    engine, monkeypatch, tmp_path, legacy_schema
):
    env = world(engine)
    seed_cli_legacy(engine, env)
    # TB: 2000 -400 vs sub -399; 1410 +50 vs sub 0. as_is_v1: these differences are REPORTED (E37), IAS 8 is empty
    base = cli_files(tmp_path, env, [])
    url = engine.url.render_as_string(hide_password=False)

    def dsn():  # the CLI pops both DSNs from the environment
        monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
        monkeypatch.setenv("LEGACY_DATABASE_URL", url)
        monkeypatch.delenv("DATABASE_URL", raising=False)

    dsn()
    plan_receipt = tmp_path / "plan.json"
    assert opening_main(["plan", *base, "--receipt", str(plan_receipt), "--operator-ref", "operator-a"]) == 0
    plan = json.loads(plan_receipt.read_text())
    assert (os.stat(plan_receipt).st_mode & 0o777) == 0o600 and plan["ready"] and plan["proof"]["ok"]
    assert plan["proof"]["control_accounts"]["2000"]["subledger_net"] == "-399.000000"  # recomputed, not declared
    assert plan["proof"]["control_accounts"]["1400"]["subledger_net"] == "300.000000"
    assert {d["account"]: d["difference"] for d in plan["proof"]["control_differences"]} == {
        "1410": "50.000000",
        "2000": "-1.000000",
    }  # reported, never a refusal
    assert str(env.tenant_id) not in plan_receipt.read_text() and "postgresql" not in plan_receipt.read_text()
    assert count_journals(engine, env) == 0  # plan wrote nothing
    apply_args = ["apply", *base, "--plan-receipt", str(plan_receipt), "--approved-plan-sha256", plan["plan_sha256"],
                  "--operator-ref", "operator-b", "--apply"]  # fmt: skip
    # no environment switch: refused before any write, and the receipt path is not even consumed
    assert opening_main([*apply_args, "--receipt", str(tmp_path / "a0.json")]) == 2 and count_journals(engine, env) == 0
    monkeypatch.setenv("ERP_MIGRATION_LEDGER_APPLY", "1")
    dsn()
    assert opening_main([*apply_args[:-1], "--receipt", str(tmp_path / "a1.json")]) == 2  # no --apply flag
    receipt = tmp_path / "a2.json"
    dsn()
    assert opening_main([*apply_args, "--receipt", str(receipt)]) == 0, receipt.read_text()
    applied = json.loads(receipt.read_text())
    assert applied["applied"] == {"opening": 1, "ias8": 0, "replayed": 0} and count_journals(engine, env) == 1
    # the opening landed as recorded: the control accounts keep their GL balance (the flip holding covers the diffs)
    assert [ledger_net(engine, env, c) for c in ("2000", "2040", "1410", "1400")] == [-400, -100, 50, 300]
    # a bound plan cannot be reused with another approved sha
    dsn()
    bad = ["apply", *base, "--plan-receipt", str(plan_receipt), "--approved-plan-sha256", "0" * 64,
           "--operator-ref", "operator-b", "--apply", "--receipt", str(tmp_path / "a3.json")]  # fmt: skip
    assert opening_main(bad) == 2


def test_opening_cli_refuses_ias8_items_and_a_declared_tb_that_differs_from_the_legacy_ledger(
    engine, monkeypatch, tmp_path, legacy_schema
):
    env = world(engine)
    seed_cli_legacy(engine, env)
    url = engine.url.render_as_string(hide_password=False)
    ias = tmp_path / "ias"
    ias.mkdir()
    base = cli_files(ias, env, [item_row("I1", "2000", "1.00")])  # IAS 8 is empty before the flip
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    monkeypatch.setenv("LEGACY_DATABASE_URL", url)
    receipt = ias / "r.json"
    assert opening_main(["plan", *base, "--receipt", str(receipt), "--operator-ref", "operator-a"]) == 2
    assert json.loads(receipt.read_text())["refusal"] == "ias8_not_allowed_before_flip"
    # a declared TB that balances but differs from the legacy ledger is refused
    other = tmp_path / "other"
    other.mkdir()
    rows = [
        {"account_code": "1000", "debit": "2.00", "credit": "0"},
        {"account_code": "3100", "debit": "0", "credit": "2.00"},
    ]
    tb_path, tb_sha = tb_file_for(other, rows)
    ias_path = other / "ia.json"
    ias_sha = write(ias_path, {"version": 1, "items": []})
    spec = other / "spec.json"
    write(spec, {"version": 1, "tenant_id": str(env.tenant_id), "tb_sha256": tb_sha, "ias8_sha256": ias_sha,
                 "evidence_ref": "E", "requester_party_id": str(env.parties["requester"]),
                 "approver_party_id": str(env.parties["approver"])})  # fmt: skip
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    monkeypatch.setenv("LEGACY_DATABASE_URL", url)
    receipt2 = other / "r.json"
    code = opening_main(["plan", "--spec", str(spec), "--tb", str(tb_path), "--ias8", str(ias_path),
                         "--import-receipt", str(import_receipt_file(other)),
                         "--receipt", str(receipt2), "--operator-ref", "operator-a"])  # fmt: skip
    assert code == 2 and json.loads(receipt2.read_text())["refusal"] == "opening_tb_differs_from_legacy"
    assert count_journals(engine, env) == 0


def test_opening_cli_gen_tb_writes_the_recomputed_tb_in_the_parse_schema(engine, monkeypatch, tmp_path, legacy_schema):
    env = world(engine)
    seed_cli_legacy(engine, env)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("LEGACY_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    out = tmp_path / "tb.json"
    rcpt = str(import_receipt_file(tmp_path))
    assert opening_main(["gen-tb", "--tenant-id", str(env.tenant_id), "--as-of", "2025-12-31", "--receipt", str(out),
                         "--import-receipt", rcpt, "--operator-ref", "operator-a"]) == 0  # fmt: skip
    assert (os.stat(out).st_mode & 0o777) == 0o600
    as_of, tb = opening.parse_tb(json.loads(out.read_text()), "0" * 64)
    assert as_of == date(2025, 12, 31) and tb == {c: Decimal(n) for c, n in tb_rows().items()}


def test_opening_requires_the_import_receipt_and_its_cutoff_equal_to_the_opening_date(
    engine, monkeypatch, tmp_path, legacy_schema
):
    env = world(engine)
    seed_cli_legacy(engine, env)
    url = engine.url.render_as_string(hide_password=False)

    def plan(directory, receipt_over=None, cutoff="2025-12-31"):
        directory.mkdir()
        base = cli_files(directory, env, [])
        if receipt_over is not None or cutoff != "2025-12-31":
            import_receipt_file(directory, cutoff, **(receipt_over or {}))  # overwrites the default fixture
        monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
        monkeypatch.setenv("LEGACY_DATABASE_URL", url)
        out = directory / "r.json"
        code = opening_main(["plan", *base, "--receipt", str(out), "--operator-ref", "operator-a"])
        return code, json.loads(out.read_text())

    code, ok = plan(tmp_path / "ok")
    assert code == 0 and ok["ready"]
    code, out = plan(tmp_path / "differs", cutoff="2025-12-30")
    assert code == 2 and out["refusal"] == "opening_as_of_differs_from_history_cutoff"
    for name, over, refusal in (
        ("dry", {"dry_run": True}, "invalid_import_receipt"),
        ("kind", {"kind": "plan"}, "invalid_import_receipt"),
        ("incomplete", {"complete": False}, "import_receipt_incomplete"),
        ("nocutoff", {"history_cutoff": None}, "import_receipt_history_cutoff_missing"),
    ):
        code, out = plan(tmp_path / name, receipt_over=over)
        assert code == 2 and out["refusal"] == refusal, name
    # the plan sha binds the receipt sha: another (equally valid) receipt gives another plan sha
    d = tmp_path / "other"
    code, again = plan(d, receipt_over={"extra": "x"})
    assert code == 0 and again["plan_sha256"] != ok["plan_sha256"]
    # a bad --as-of is a named refusal, not a traceback
    out = tmp_path / "tb.json"
    code = opening_main(["gen-tb", "--tenant-id", str(env.tenant_id), "--as-of", "2025-13-45", "--receipt", str(out),
                         "--import-receipt", str(import_receipt_file(tmp_path)), "--operator-ref", "o"])  # fmt: skip
    assert code == 2 and json.loads(out.read_text())["refusal"] == "invalid_as_of"
    assert count_journals(engine, env) == 0


def test_opening_cli_refuses_an_untied_tb_before_touching_the_database(engine, monkeypatch, tmp_path):
    env = world(engine)
    rows = [
        {"account_code": "1000", "debit": "2.00", "credit": "0"},
        {"account_code": "3100", "debit": "0", "credit": "1.00"},
    ]
    tb_path, tb_sha = tb_file_for(tmp_path, rows)
    ias_path = tmp_path / "ia.json"
    ias_sha = write(ias_path, {"version": 1, "items": []})
    spec = tmp_path / "spec.json"
    write(spec, {"version": 1, "tenant_id": str(env.tenant_id), "tb_sha256": tb_sha, "ias8_sha256": ias_sha,
                 "evidence_ref": "E", "requester_party_id": str(env.parties["requester"]),
                 "approver_party_id": str(env.parties["approver"])})  # fmt: skip
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)  # no database at all: the refusal comes first
    monkeypatch.delenv("LEGACY_DATABASE_URL", raising=False)
    receipt = tmp_path / "r.json"
    code = opening_main(["plan", "--spec", str(spec), "--tb", str(tb_path), "--ias8", str(ias_path),
                         "--import-receipt", str(import_receipt_file(tmp_path)),
                         "--receipt", str(receipt), "--operator-ref", "operator-a"])  # fmt: skip
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
    # as_is_v1: the open item is reported, the gate passes
    assert register_main(["gate", "--spec", str(spec), "--receipt", str(receipt), "--operator-ref", "operator-a"]) == 0
    out = json.loads(receipt.read_text())
    assert out["reasons"] == [] and out["profile"] == "as_is_v1" and out["reports"]["open_count"] == 1
    # a resolve without APPLY is refused; with it, the report count drops
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
    ddl = [
        "CREATE SCHEMA IF NOT EXISTS gl",
        "CREATE SCHEMA IF NOT EXISTS ap",
        "CREATE SCHEMA IF NOT EXISTS ar",
        "CREATE SCHEMA IF NOT EXISTS expense",
        "CREATE SCHEMA IF NOT EXISTS payroll",
        "CREATE TABLE gl.journal_entry (journal_entry_id uuid PRIMARY KEY, status text NOT NULL DEFAULT 'POSTED', "
        "posting_date date, reversal_journal_id uuid)",
        "CREATE TABLE gl.posted_ledger_line (account_id uuid, debit_amount numeric, credit_amount numeric, "
        "posting_date date, journal_entry_id uuid, source_document_type text, source_document_id text)",
        "CREATE TABLE ap.supplier_invoice (invoice_id uuid PRIMARY KEY, journal_entry_id uuid, "
        "ap_control_account_id uuid, functional_currency_amount numeric, currency_code text NOT NULL DEFAULT 'USD', "
        "exchange_rate numeric)",
        "CREATE TABLE ap.supplier_payment (payment_id uuid PRIMARY KEY, journal_entry_id uuid)",
        "CREATE TABLE ap.payment_allocation (invoice_id uuid, payment_id uuid, allocation_date date, "
        "allocated_amount numeric, discount_taken numeric DEFAULT 0)",
        "CREATE TABLE ar.invoice (invoice_id uuid PRIMARY KEY, journal_entry_id uuid, "
        "functional_currency_amount numeric, currency_code text NOT NULL DEFAULT 'USD', exchange_rate numeric)",
        "CREATE TABLE ar.customer_payment (payment_id uuid PRIMARY KEY, journal_entry_id uuid, gross_amount numeric, "
        "functional_currency_amount numeric, exchange_rate numeric)",
        "CREATE TABLE ar.payment_allocation (invoice_id uuid, payment_id uuid, allocation_date date, "
        "allocated_amount numeric, discount_taken numeric DEFAULT 0, write_off_amount numeric DEFAULT 0)",
        "CREATE TABLE expense.expense_claim (claim_id uuid PRIMARY KEY, journal_entry_id uuid, net_payable_amount numeric)",
        "CREATE TABLE payroll.salary_slip (slip_id uuid PRIMARY KEY, journal_entry_id uuid)",
        "CREATE TABLE payroll.employee_loan (loan_id uuid PRIMARY KEY, disbursement_date date)",
    ]
    with engine.begin() as c:
        for statement in ddl:
            c.exec_driver_sql(statement)
    yield
    with engine.begin() as c:
        for schema in ("gl", "ap", "ar", "expense", "payroll"):
            c.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")


def legacy_post(engine, jid, day, lines, *, status="POSTED", reversal=None, doc=(None, None)):
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO gl.journal_entry (journal_entry_id, status, posting_date, reversal_journal_id) VALUES (:j, :s, :d, :r)"
            ),
            {"j": jid, "s": status, "d": day, "r": reversal},
        )
        for account, debit, credit in lines:
            c.execute(
                sa.text(
                    "INSERT INTO gl.posted_ledger_line (account_id, debit_amount, credit_amount, posting_date, journal_entry_id, "
                    "source_document_type, source_document_id) VALUES (:a, :d, :c, :p, :j, :t, :i)"
                ),
                {"a": account, "d": Decimal(debit), "c": Decimal(credit), "p": day, "j": jid, "t": doc[0], "i": doc[1]},
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
        ("3100", "OPENING"): Decimal("7"), ("1000", "OPENING"): Decimal("-7"),  # 5000 (expense) rolled into 3100
        ("5000", "2026-01"): Decimal("3.5"), ("2000", "2026-01"): Decimal("-3.5"),
        ("5000", "2026-02"): Decimal("1"), ("2000", "2026-02"): Decimal("-1"),
    }  # fmt: skip
    assert universe == {str(j2), str(j3)}  # the FY2026 journal ids; the opening-year journal is not a bridge ref
    # 12.3d: no income or expense account carries anything in OPENING
    assert not [k for k in net if k[1] == "OPENING" and k[0] == "5000"]
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
                sa.text(
                    "INSERT INTO ap.supplier_invoice (invoice_id, journal_entry_id, ap_control_account_id) VALUES (:i, :j, :a)"
                ),
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


# --- the opening-date legacy subledger (12.3b recipes) --------------------------------------------------------------

D_OPEN = date(2025, 12, 31)


def legacy_exec(engine, sql, **params):
    with engine.begin() as c:
        c.execute(sa.text(sql), params)


def posted_journal(engine, day, *, status="POSTED", reversal=None):
    jid = uuid4()
    legacy_exec(
        engine,
        "INSERT INTO gl.journal_entry (journal_entry_id, status, posting_date, reversal_journal_id) VALUES (:j, :s, :d, :r)",
        j=jid, s=status, d=day, r=reversal,
    )  # fmt: skip
    return jid


def seed_payables(engine):
    def invoice(amount, *, day=date(2025, 6, 1), status="POSTED", reversal=None, rate=None):
        inv = uuid4()
        j = posted_journal(engine, day, status=status, reversal=reversal)
        legacy_exec(
            engine,
            "INSERT INTO ap.supplier_invoice (invoice_id, journal_entry_id, functional_currency_amount, currency_code, "
            "exchange_rate) VALUES (:i, :j, :a, :c, :r)",
            i=inv,
            j=j,
            a=Decimal(amount),
            c="EUR" if rate else "USD",
            r=None if rate is None else Decimal(rate),
        )
        return inv

    def pay(inv, allocated, day, *, discount=0, journal_status="POSTED"):
        p = uuid4()
        j = posted_journal(engine, date(2025, 7, 1), status=journal_status)
        legacy_exec(engine, "INSERT INTO ap.supplier_payment VALUES (:p, :j)", p=p, j=j)
        legacy_exec(
            engine,
            "INSERT INTO ap.payment_allocation (invoice_id, payment_id, allocation_date, allocated_amount, discount_taken) VALUES (:i, :p, :d, :a, :x)",
            i=inv,
            p=p,
            d=day,
            a=Decimal(allocated),
            x=Decimal(discount),
        )

    inv1 = invoice("100")
    pay(inv1, "40", date(2025, 7, 1))  # allocated by D: residual 60
    inv2 = invoice("50")
    pay(inv2, "50", date(2026, 1, 5))  # allocated AFTER D: still 50 open at D
    invoice("-10")  # a credit note, stored negative: never negated
    reversing = posted_journal(engine, date(2025, 8, 1))
    invoice("70", reversal=reversing)  # reversed by D: excluded
    invoice("30", day=date(2026, 1, 2))  # posted after D: excluded
    invoice("20", status="DRAFT")  # never posted: excluded
    after = posted_journal(engine, date(2026, 2, 1))
    invoice("25", status="REVERSED", reversal=after)  # reversed only AFTER D: open at D
    inv8 = invoice("15")
    pay(inv8, "5", date(2025, 7, 1), journal_status="DRAFT")  # the payment was never posted: ignored
    inv9 = invoice("200", rate=2)
    pay(inv9, "10", date(2025, 7, 1), discount="5")  # (10 + 5) x rate 2 = 30 functional
    return Decimal("-310")  # -(60 + 50 - 10 + 25 + 15 + 170)


def seed_receivables(engine):
    a = uuid4()
    legacy_exec(
        engine,
        "INSERT INTO ar.invoice (invoice_id, journal_entry_id, functional_currency_amount) VALUES (:i, :j, 500)",
        i=a,
        j=posted_journal(engine, date(2025, 3, 1)),
    )
    legacy_exec(
        engine,
        "INSERT INTO ar.invoice (invoice_id, journal_entry_id, functional_currency_amount) VALUES (:i, :j, -40)",
        i=uuid4(),
        j=posted_journal(engine, date(2025, 3, 2)),
    )
    p = uuid4()
    legacy_exec(
        engine,
        "INSERT INTO ar.customer_payment (payment_id, journal_entry_id, gross_amount, functional_currency_amount) VALUES (:p, :j, 200, 200)",
        p=p,
        j=posted_journal(engine, date(2025, 4, 1)),
    )
    legacy_exec(
        engine,
        "INSERT INTO ar.payment_allocation (invoice_id, payment_id, allocation_date, allocated_amount, discount_taken, write_off_amount) VALUES (:i, :p, :d, 100, 10, 5)",
        i=a,
        p=p,
        d=date(2025, 5, 1),
    )
    # items 500 - 40 - (100 + 10 + 5) = 345; unallocated cash -(200 - 100) = -100
    return Decimal("245")


def seed_claims(engine, family_account):
    c1, c2, c3 = uuid4(), uuid4(), uuid4()
    for claim, amount, day in ((c1, 80, date(2025, 5, 1)), (c2, 50, date(2025, 5, 2)), (c3, 40, date(2026, 1, 3))):
        legacy_exec(
            engine,
            "INSERT INTO expense.expense_claim VALUES (:c, :j, :a)",
            c=claim,
            j=posted_journal(engine, day),
            a=Decimal(amount),
        )
    for amount, day in ((30, date(2025, 11, 1)), (99, date(2026, 2, 1))):  # the 2026 reimbursement is after D
        legacy_post(engine, uuid4(), day, [(family_account, str(amount), "0")], doc=("EXPENSE_REIMBURSEMENT", str(c1)))
    return Decimal("-100")  # -(80 + 50 - 30)


def test_the_legacy_subledger_recipes_at_the_opening_date(engine, legacy_schema):
    from erp_v2.migration_ledger import legacy_subledger

    env = world(engine)
    ap, ar = seed_payables(engine), seed_receivables(engine)
    claims = seed_claims(engine, env.accounts["2040"])
    with engine.connect() as legacy:
        out = legacy_subledger.recompute(legacy, D_OPEN, [str(env.accounts["2040"])], "USD")
    assert out == {"2000": ap, "1400": ar, "2040": claims, "1410": Decimal(0)}
    # a missing recipe is refused rather than ignored: payroll net pay or a staff loan at D
    legacy_exec(engine, "INSERT INTO payroll.employee_loan VALUES (:l, :d)", l=uuid4(), d=date(2025, 5, 1))
    with engine.connect() as legacy:
        assert refusal_code(legacy_subledger.recompute, legacy, D_OPEN, [], "USD") == "legacy_subledger_recipe_missing"


def test_the_pnl_rolls_up_into_3100_in_the_opening_period(engine, legacy_schema):
    from erp_v2.migration_ledger import legacy_ledger

    env = world(engine)
    legacy_post(
        engine, uuid4(), date(2025, 6, 1), [(env.accounts["1000"], "100", "0"), (env.accounts["4000"], "0", "100")]
    )
    legacy_post(
        engine, uuid4(), date(2025, 6, 2), [(env.accounts["5000"], "30", "0"), (env.accounts["1000"], "0", "30")]
    )
    with engine.connect() as legacy, Session(engine) as db:
        net, _ = legacy_ledger.recompute(legacy, db, env.tenant_id, opening_as_of=D_OPEN)
    # 4000 (revenue) -100 and 5000 (expense) +30 carry 0 and roll into 3100 as -70; the balance-sheet side is untouched
    assert ("5000", "OPENING") not in net and ("4000", "OPENING") not in net
    assert net[("3100", "OPENING")] == Decimal("-70") and net[("1000", "OPENING")] == Decimal("70")
    assert sum(v for (a, p), v in net.items() if p == "OPENING") == 0


def test_g9_pins_for_the_opening_date_subledgers():
    """The recipes against the real g9 snapshot (9fc8fb1e); skipped when no snapshot is present."""
    url = os.environ.get("ERP_G9_LEGACY_URL")
    if not url:
        pytest.skip("no g9 legacy snapshot present (set ERP_G9_LEGACY_URL to a read-only copy)")
    from erp_v2.migration_ledger import legacy_subledger

    eng = sa.create_engine(url)
    try:
        with eng.connect() as legacy, legacy.begin():
            legacy.execute(sa.text("SET TRANSACTION READ ONLY"))
            family = [
                r[0]
                for r in legacy.execute(
                    sa.text("SELECT account_id::text FROM gl.account WHERE account_code IN ('2040', '2030', '2110')")
                )
            ]
            out = legacy_subledger.recompute(legacy, date(2025, 12, 31), family, "NGN")
    finally:
        eng.dispose()
    assert out == {
        "2000": Decimal("-66479663.78"), "1400": Decimal("729671283.82"),
        "2040": Decimal("-409280.00"), "1410": Decimal("0.00"),
    }  # fmt: skip


def test_a_functional_currency_row_with_a_null_rate_counts_at_face_and_a_foreign_one_refuses(engine, legacy_schema):
    """Legacy left the rate NULL on tens of thousands of NGN rows; NULL x amount must not drop them from the sums."""
    from erp_v2.migration_ledger import legacy_subledger

    env = world(engine)
    inv, pay = uuid4(), uuid4()
    legacy_exec(
        engine,
        "INSERT INTO ar.invoice (invoice_id, journal_entry_id, functional_currency_amount, currency_code, exchange_rate) VALUES (:i, :j, 1000, 'NGN', NULL)",
        i=inv,
        j=posted_journal(engine, date(2025, 3, 1)),
    )
    legacy_exec(
        engine,
        "INSERT INTO ar.customer_payment (payment_id, journal_entry_id, gross_amount, functional_currency_amount, exchange_rate) VALUES (:p, :j, 400, 400, NULL)",
        p=pay,
        j=posted_journal(engine, date(2025, 4, 1)),
    )
    legacy_exec(
        engine,
        "INSERT INTO ar.payment_allocation (invoice_id, payment_id, allocation_date, allocated_amount, discount_taken, write_off_amount) VALUES (:i, :p, :d, 150, 0, 0)",
        i=inv,
        p=pay,
        d=date(2025, 5, 1),
    )
    with engine.connect() as legacy:
        # items 1000 - allocation 150 (rate 1, NULL stored) = 850; cash 400 - 150 = 250 unallocated: 850 - 250 = 600
        assert legacy_subledger.recompute(legacy, D_OPEN, [], "NGN")["1400"] == Decimal("600")
    legacy_exec(
        engine,
        "INSERT INTO ap.supplier_invoice (invoice_id, journal_entry_id, functional_currency_amount, currency_code, exchange_rate) VALUES (:i, :j, 10, 'EUR', NULL)",
        i=uuid4(),
        j=posted_journal(engine, date(2025, 3, 1)),
    )
    with engine.connect() as legacy:
        assert refusal_code(legacy_subledger.recompute, legacy, D_OPEN, [], "NGN") == "legacy_rate_missing"
    del env
