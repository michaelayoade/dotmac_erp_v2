"""B-2 held drafts on real PostgreSQL: real migrations, the real B2 post path and the accounting a2 governed void.

Hosted CI only (ERP_TEST_POSTGRES_URL), like test_journals_pg. The legacy snapshot is a throwaway ``gl.journal_entry``
table in the same test database; the owner role bypasses RLS, so tenant isolation is asserted through per-test tenants.
"""

import json
import os
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import CreateJournal, JournalKind, JournalLineInput, SourceIdentity
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, PeriodEvent
from dotmac_banking.models import BankAccount, BankInstitution, BankStatement, BankStatementLine
from dotmac_kernel.cache import TenantScope
from dual_control_support import set_dual_control_engine
from sqlalchemy.orm import Session
from test_journals_pg import (  # noqa: F401  (hosted-Postgres fixtures)
    DAY,
    NOW,
    _registries,
    close_period,
    seed,
)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.held_drafts import rules, service
from erp_v2.held_drafts.cli import main
from erp_v2.held_drafts.models import HeldDraftDecision
from erp_v2.held_drafts.service import ApplyInputs, Scope
from erp_v2.journals.models import ManualJournal

FY_START, FY_END = date(2026, 1, 1), date(2026, 12, 31)


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


@pytest.fixture(autouse=True, scope="module")
def legacy_schema(engine):
    with engine.begin() as c:
        c.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS gl")
        c.exec_driver_sql(
            "CREATE TABLE gl.journal_entry (journal_entry_id uuid PRIMARY KEY, status text, source_module text, "
            "source_document_type text, source_document_id uuid)"
        )
    yield
    with engine.begin() as c:
        c.exec_driver_sql("DROP SCHEMA gl CASCADE")


_counter = {"n": 0}


def _n() -> int:
    _counter["n"] += 1
    return _counter["n"]


def world(engine, *, dual: bool | None = False) -> SimpleNamespace:
    env = seed(engine)
    set_dual_control_engine(engine, env.tenant_id, bool(dual))
    with Session(engine) as db:
        institution = BankInstitution(
            id=uuid4(), tenant_id=env.tenant_id, code="BK", name="Bank", country_code="NG", status="active"
        )
        db.add(institution)
        db.flush()
        bank = BankAccount(
            id=uuid4(),
            tenant_id=env.tenant_id,
            institution_id=institution.id,
            account_code="OP1",
            account_name="Operating",
            account_identifier="id-1",
            account_type_code="current",
            currency_code="USD",
            minor_units=2,
            cash_account_ref="1000",
            status="active",
        )
        db.add(bank)
        statement = BankStatement(
            id=uuid4(),
            tenant_id=env.tenant_id,
            account_id=bank.id,
            statement_ref="JAN",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            opening_balance=Decimal(0),
            closing_balance=Decimal(0),
            total_credits=Decimal(0),
            total_debits=Decimal(0),
            currency_code="USD",
            minor_units=2,
            status="imported",
            total_lines=0,
            matched_lines=0,
            source_ref=f"src-{uuid4().hex[:8]}",
            source_version="1",
            evidence_ref="sha",
            imported_by_id=env.parties["preparer"],
            imported_at=NOW,
        )
        db.add(statement)
        statement_id = statement.id
        db.commit()
    env.statement_id = statement_id
    env.lines = 0
    env.scope = Scope(env.tenant_id, FY_START, FY_END)
    return env


def statement_lines(engine, env, count, *, amount="5", direction="debit", day=DAY):
    with Session(engine) as db:
        for _ in range(count):
            env.lines += 1
            db.add(
                BankStatementLine(
                    id=uuid4(),
                    tenant_id=env.tenant_id,
                    statement_id=env.statement_id,
                    line_number=env.lines,
                    transaction_date=day,
                    direction=direction,
                    amount=Decimal(amount),
                    external_ref=f"ext-{uuid4().hex[:8]}",
                    description="Bank narration",
                    is_matched=False,
                )
            )
        db.commit()


def journal(
    engine,
    env,
    *,
    kind,
    module,
    doc_type,
    amount="5",
    status="DRAFT",
    doc_id=None,
    debit="1400",
    credit="1000",
    day=DAY,
    with_legacy=True,
    extra_debit=None,
):
    """A legacy-erp journal (DRAFT or POSTED) and its legacy snapshot row."""
    number = _n()
    document = doc_id or uuid4()
    lines = [
        JournalLineInput(env.accounts[debit], debit=Decimal(amount)),
        JournalLineInput(env.accounts[credit], credit=Decimal(amount)),
    ]
    with Session(engine) as db:
        created = accounting.create_journal(
            db,
            scope=TenantScope(env.tenant_id),
            command=CreateJournal(
                number=f"LEG-{number:06d}",
                kind=JournalKind.STANDARD,
                entry_date=day,
                posting_date=day,
                currency_code="USD",
                exchange_rate=Decimal(1),
                description="Imported legacy journal",
                source=SourceIdentity("legacy-erp", kind, f"{document}-{number}", "1", "b" * 64),
                lines=tuple(lines),
            ),
            idempotency_key=f"legacy:{number}",
            idempotency_expires_at=None,
            recorded_at=NOW,
        )
        if status == "POSTED":
            accounting.post_journal(
                db,
                scope=TenantScope(env.tenant_id),
                journal_id=created.id,
                approval_reference=f"legacy:{number}",
                posted_by="legacy-system",
                idempotency_key=f"legacy-post:{number}",
                idempotency_expires_at=None,
                posted_at=NOW,
            )
        journal_id = created.id
        db.commit()
    if with_legacy:
        with engine.begin() as c:
            c.execute(
                sa.text("INSERT INTO gl.journal_entry VALUES (:j, :s, :m, :t, :d)"),
                {"j": journal_id, "s": status, "m": module, "t": doc_type, "d": document},
            )
    return SimpleNamespace(id=journal_id, document=document)


def fee(engine, env, status="DRAFT", **kw):
    return journal(engine, env, kind="BANK_FEE", module="BANKING", doc_type="BANK_FEE", status=status, **kw)


def ar_pair(engine, env, *, twin_amount="100", amount="100", kind="INVOICE"):
    draft = journal(engine, env, kind=kind, module="AR", doc_type=kind, amount=amount, debit="1400", credit="4000")
    twin = journal(
        engine,
        env,
        kind=kind,
        module="AR",
        doc_type=kind,
        amount=twin_amount,
        status="POSTED",
        doc_id=draft.document,
        debit="1400",
        credit="4000",
    )
    return draft, twin


def requester_approver(env):
    return env.parties["requester"], env.parties["approver"]


def plan_and_apply(engine, env, *, planned_by="operator-a", applied_by="operator-b", dual=False):
    requester, approver = requester_approver(env)
    with Session(engine) as db, engine.connect() as legacy:
        plan = service.build_plan(db, legacy, env.scope)
        result = service.apply_plan(
            db,
            env.scope,
            plan,
            ApplyInputs(plan.sha256, "c" * 64, planned_by, applied_by, requester, approver, dual),
        )
    return plan, result


def statuses(engine, env) -> dict[UUID, str]:
    with Session(engine) as db:
        return {
            r.id: r.status.value
            for r in db.execute(
                sa.select(JournalEntry.id, JournalEntry.status).where(JournalEntry.tenant_id == env.tenant_id)
            )
        }


def decisions(engine, env) -> dict[UUID, HeldDraftDecision]:
    with Session(engine) as db:
        return {
            r.journal_id: r
            for r in db.scalars(sa.select(HeldDraftDecision).where(HeldDraftDecision.tenant_id == env.tenant_id))
        }


def counts(plan):
    out = {}
    for d in plan.decisions:
        out[d.outcome] = out.get(d.outcome, 0) + 1
    return out


def census(engine, env):
    """The scenario the census describes, in miniature."""
    statement_lines(engine, env, 2, amount="5")  # key K1: S=2
    k1_posted = [fee(engine, env, status="POSTED")]  # P=1
    k1 = [fee(engine, env) for _ in range(4)]  # D=4 -> postable 1, void 3
    statement_lines(engine, env, 1, amount="6")  # key K2: S=1, P=1, D=2 -> void both
    k2_posted = [fee(engine, env, status="POSTED", amount="6")]
    k2 = [fee(engine, env, amount="6") for _ in range(2)]
    k3 = [fee(engine, env, amount="7")]  # S=0 -> queue
    statement_lines(engine, env, 1, amount="8")  # key K4: S=1, P=2 -> queue
    k4_posted = [fee(engine, env, status="POSTED", amount="8") for _ in range(2)]
    k4 = [fee(engine, env, amount="8")]
    ar_ok, _ = ar_pair(engine, env)
    ar_pay_ok, _ = ar_pair(engine, env, kind="CUSTOMER_PAYMENT")
    ar_differs, _ = ar_pair(engine, env, twin_amount="99")
    ar_alone = journal(
        engine, env, kind="INVOICE", module="AR", doc_type="INVOICE", amount="100", debit="1400", credit="4000"
    )
    other = [
        journal(engine, env, kind="BANK_RECONCILIATION", module="BANKING", doc_type="BANK_RECONCILIATION"),
        journal(engine, env, kind="EXPENSE", module="EXPENSE", doc_type="EXPENSE_CLAIM"),
        journal(engine, env, kind="JOURNAL", module="GL", doc_type="JOURNAL", with_legacy=False),
    ]
    return SimpleNamespace(
        k1=k1, k2=k2, k3=k3, k4=k4, ar_ok=ar_ok, ar_pay_ok=ar_pay_ok, ar_differs=ar_differs, ar_alone=ar_alone,
        other=other, posted=k1_posted + k2_posted + k4_posted,
    )  # fmt: skip


def test_census_scenario_partitions_every_draft_into_exactly_one_bridge_line(engine):
    env = world(engine)
    world_ = census(engine, env)
    all_drafts = [*world_.k1, *world_.k2, *world_.k3, *world_.k4, world_.ar_ok, world_.ar_pay_ok, world_.ar_differs]
    all_drafts += [world_.ar_alone, *world_.other]
    with Session(engine) as db, engine.connect() as legacy:
        plan = service.build_plan(db, legacy, env.scope)
        before = service.partition_proof(db, env.scope, plan.decisions)
    # K1: 1 posts + 3 void; K2: 2 void; K3, K4 queue; two AR void; differing twin, no twin and three others queue.
    assert counts(plan) == {"posted": 1, "voided": 7, "queued": 7}
    assert before["ok"] and before["held_total"] == len(all_drafts) == 15
    assert sum(before["counts"].values()) == before["held_total"]

    plan2, result = plan_and_apply(engine, env)
    assert plan2.sha256 == plan.sha256 and result["decided_now"] == 15 and result["refusals"] == {}
    state, rows = statuses(engine, env), decisions(engine, env)
    assert set(rows) == {d.id for d in all_drafts}  # one decision row per draft, no more
    by_outcome = {}
    for journal_id, row in rows.items():
        by_outcome.setdefault(row.outcome, []).append(journal_id)
        assert state[journal_id] == {"voided": "VOID", "posted": "POSTED", "queued": "DRAFT"}[row.outcome]
    assert {k: len(v) for k, v in by_outcome.items()} == {"posted": 1, "voided": 7, "queued": 7}
    assert {r.reason_code for r in rows.values() if r.outcome == "voided"} == {
        "duplicate_of_posted_fee",
        "already_posted_duplicate",
    }
    # Fee voids are evidenced by the key and counts; AR voids by the twin's journal id.
    fee_void = rows[world_.k2[0].id]
    assert fee_void.evidence["statement_lines"] == 1 and fee_void.evidence["posted_fee_journals"] == 1
    assert rows[world_.ar_ok.id].evidence["twin_journal_id"]
    assert rows[world_.k3[0].id].reason_code == "no_statement_evidence"
    assert rows[world_.k4[0].id].reason_code == "posted_exceeds_statement"
    assert rows[world_.ar_differs.id].reason_code == "twin_amount_differs"
    assert rows[world_.ar_alone.id].reason_code == "twin_missing"
    assert rows[world_.other[0].id].reason_code == "no_published_rule"
    assert rows[world_.other[2].id].reason_code == "no_legacy_record"

    with Session(engine) as db:
        events = db.scalar(
            sa.select(sa.func.count())
            .select_from(PeriodEvent)
            .where(PeriodEvent.tenant_id == env.tenant_id, PeriodEvent.event_kind == "JOURNAL_VOID")
        )
        posted_rows = db.scalars(sa.select(ManualJournal).where(ManualJournal.tenant_id == env.tenant_id)).all()
        proof = service.partition_proof(db, env.scope, plan.decisions)
        check = service.status_check(db, env.scope)
    assert events == 7  # every void went through the accounting a2 governed void
    assert len(posted_rows) == 1 and posted_rows[0].origin == "legacy_held" and posted_rows[0].state == "POSTED"
    assert proof["ok"] and check["mismatches"] == 0


def test_rerun_is_a_no_op_and_the_plan_sha_is_stable(engine):
    env = world(engine)
    census(engine, env)
    plan, _ = plan_and_apply(engine, env)
    before = decisions(engine, env)
    again, result = plan_and_apply(engine, env)
    assert result["decided_now"] == 0 and again.pending == frozenset()
    assert again.sha256 == plan.sha256
    after = decisions(engine, env)
    assert {k: v.id for k, v in before.items()} == {k: v.id for k, v in after.items()}
    with Session(engine) as db:
        idem = db.scalar(
            sa.text(
                "SELECT count(*) FROM idempotency_records "
                "WHERE tenant_id = :t AND scope = 'accounting.void_draft_journal'"
            ),
            {"t": env.tenant_id},
        )
    assert idem == 7


def test_plan_writes_nothing(engine):
    env = world(engine)
    census(engine, env)
    with Session(engine) as db, engine.connect() as legacy:
        db.execute(sa.text("SET TRANSACTION READ ONLY"))
        service.build_plan(db, legacy, env.scope)
        db.rollback()
    assert decisions(engine, env) == {}
    assert set(statuses(engine, env).values()) == {"DRAFT", "POSTED"}


def test_statement_exceeding_posted_posts_the_difference(engine):
    env = world(engine)
    statement_lines(engine, env, 3)
    fee(engine, env, status="POSTED")
    drafts = [fee(engine, env) for _ in range(4)]
    plan, _ = plan_and_apply(engine, env)
    assert counts(plan) == {"posted": 2, "voided": 2}
    state = statuses(engine, env)
    assert sorted(state[d.id] for d in drafts) == ["POSTED", "POSTED", "VOID", "VOID"]


def test_closed_period_blocks_the_post_at_plan_and_queues_it(engine):
    env = world(engine)
    statement_lines(engine, env, 1)
    draft = fee(engine, env)
    close_period(engine, env, 1)
    plan, _ = plan_and_apply(engine, env)
    row = decisions(engine, env)[draft.id]
    assert (row.outcome, row.reason_code) == ("queued", "post_blocked")
    assert row.evidence["blocker"] == "period_not_open"
    assert statuses(engine, env)[draft.id] == "DRAFT" and counts(plan) == {"queued": 1}


def test_control_account_guard_is_respected_for_a_postable_draft(engine):
    env = world(engine)
    statement_lines(engine, env, 1)
    draft = fee(engine, env, debit="2000")  # AP control account on the debit side
    plan_and_apply(engine, env)
    row = decisions(engine, env)[draft.id]
    assert (row.outcome, row.reason_code) == ("queued", "post_blocked")
    assert row.evidence["blocker"] == "control_account_requires_subledger_document"
    assert statuses(engine, env)[draft.id] == "DRAFT"


def test_a_b2_refusal_at_apply_queues_with_the_refusal_code(engine, monkeypatch):
    env = world(engine)
    statement_lines(engine, env, 1)
    draft = fee(engine, env)
    with Session(engine) as db, engine.connect() as legacy:
        plan = service.build_plan(db, legacy, env.scope)
        assert counts(plan) == {"posted": 1}
        from erp_v2.journals import service as journals

        def refuse(*args, **kwargs):
            raise journals.JournalRefused("period_closed", "refused in test")

        monkeypatch.setattr(journals, "request_legacy_post", refuse)
        requester, approver = requester_approver(env)
        result = service.apply_plan(
            db, env.scope, plan, ApplyInputs(plan.sha256, "c" * 64, "a", "b", requester, approver, False)
        )
    assert result["refusals"] == {"posted:period_closed": 1}
    row = decisions(engine, env)[draft.id]
    assert (row.outcome, row.reason_code) == ("queued", "posted_refused:period_closed")
    assert statuses(engine, env)[draft.id] == "DRAFT"


def test_filed_vat_month_with_a_tax_account_queues_the_post(engine):
    env = world(engine)
    with Session(engine) as db:
        category = db.scalar(sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id))
        tax = Account(
            tenant_id=env.tenant_id,
            category_id=category.id,
            code="1455",
            name="Deferred input VAT",
            kind=db.scalar(sa.select(Account.kind).where(Account.tenant_id == env.tenant_id)),
            normal_balance=db.scalar(sa.select(Account.normal_balance).where(Account.tenant_id == env.tenant_id)),
        )
        db.add(tax)
        db.flush()
        env.accounts["1455"] = tax.id
        db.commit()
    statement_lines(engine, env, 1)
    draft = fee(engine, env, debit="1455")
    env.scope = Scope(env.tenant_id, FY_START, FY_END, frozenset({"2026-01"}))
    plan_and_apply(engine, env)
    row = decisions(engine, env)[draft.id]
    assert row.reason_code == "post_blocked" and row.evidence["blocker"] == "vat_month_filed"


def test_a_draft_already_in_the_b2_workflow_is_queued_not_decided_twice(engine):
    env = world(engine)
    statement_lines(engine, env, 2)
    draft = fee(engine, env)
    from erp_v2.journals import service as journals

    with Session(engine) as db:
        journals.request_legacy_post(
            db, tenant_id=env.tenant_id, journal_id=draft.id, actor=env.parties["preparer"], review_ref="FIN-1", now=NOW
        )
        db.commit()
    plan_and_apply(engine, env)
    row = decisions(engine, env)[draft.id]
    assert row.reason_code == "workflow_in_progress" and statuses(engine, env)[draft.id] == "DRAFT"


def test_drafts_dated_before_the_fiscal_year_are_out_of_scope(engine):
    env = world(engine)
    env.scope = Scope(env.tenant_id, date(2026, 1, 5), FY_END)
    old = fee(engine, env, day=date(2026, 1, 2))
    statement_lines(engine, env, 1)
    fee(engine, env)
    with Session(engine) as db, engine.connect() as legacy:
        plan = service.build_plan(db, legacy, env.scope)
    assert len(plan.decisions) == 1 and plan.out_of_scope_before_fy == 1
    plan_and_apply(engine, env)
    assert old.id not in decisions(engine, env) and statuses(engine, env)[old.id] == "DRAFT"


def test_drift_after_the_plan_changes_the_sha(engine):
    env = world(engine)
    statement_lines(engine, env, 1)
    fee(engine, env, status="POSTED")
    fee(engine, env)
    with Session(engine) as db, engine.connect() as legacy:
        first = service.build_plan(db, legacy, env.scope).sha256
    statement_lines(engine, env, 1)  # more statement evidence after the plan: the decision would change
    with Session(engine) as db, engine.connect() as legacy:
        assert service.build_plan(db, legacy, env.scope).sha256 != first


def test_decision_rows_are_immutable_and_the_runtime_role_cannot_write_them(engine):
    env = world(engine)
    census(engine, env)
    plan_and_apply(engine, env)
    row = next(iter(decisions(engine, env).values()))
    for statement in (
        "UPDATE erp_held_draft_decisions SET outcome = 'queued' WHERE id = :i",
        "DELETE FROM erp_held_draft_decisions WHERE id = :i",
    ):
        with engine.connect() as c, pytest.raises(sa.exc.DBAPIError, match="immutable"):
            c.execute(sa.text(statement), {"i": row.id})
    with engine.connect() as c:
        assert not c.scalar(sa.text("SELECT has_table_privilege('app_user', 'erp_held_draft_decisions', 'INSERT')"))
        assert c.scalar(sa.text("SELECT has_table_privilege('app_user', 'erp_held_draft_decisions', 'SELECT')"))


def test_table_constraints_refuse_bad_shapes_and_a_second_decision(engine):
    env = world(engine)
    census(engine, env)
    plan_and_apply(engine, env)
    row = next(iter(decisions(engine, env).values()))
    base = {
        "tenant_id": env.tenant_id,
        "rule_id": "R",
        "amount": Decimal(1),
        "evidence": {},
        "evidence_sha256": "a" * 64,
        "rules_sha256": "a" * 64,
        "plan_sha256": "a" * 64,
        "plan_receipt_sha256": "a" * 64,
        "dual_control": False,
        "planned_by": "a",
        "applied_by": "b",
    }
    bad = [
        {"journal_id": row.journal_id, "outcome": "queued", "reason_code": "x"},  # a second decision for the draft
        {"journal_id": uuid4(), "outcome": "voided", "reason_code": "other_reason"},  # a void needs a ruled reason
        {"journal_id": uuid4(), "outcome": "posted", "reason_code": "x"},  # a post names its workflow row
        {"journal_id": uuid4(), "outcome": "queued", "reason_code": "x", "dual_control": True, "applied_by": "a"},
    ]
    for values in bad:
        with Session(engine) as db:
            db.add(HeldDraftDecision(**{**base, **values}))
            with pytest.raises(sa.exc.IntegrityError):
                db.flush()
            db.rollback()


def test_migration_downgrade_refuses_while_decisions_exist(engine):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from erp_v2.migrations.versions import e2_0027_held_draft_decisions as migration

    env = world(engine)
    census(engine, env)
    plan_and_apply(engine, env)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="held draft decisions exist"):
                    migration.downgrade()
        finally:
            transaction.rollback()
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("DROP TRIGGER erp_held_draft_decisions_guard ON erp_held_draft_decisions"))
            connection.execute(sa.text("DELETE FROM erp_held_draft_decisions"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert connection.scalar(sa.text("SELECT to_regclass('public.erp_held_draft_decisions')")) is None
        finally:
            transaction.rollback()


# --- the CLI end to end, with the dual-control policy ----------------------------------------------------------


def write_spec(tmp_path, env, *, same_party=False):
    requester, approver = requester_approver(env)
    path = tmp_path / f"spec-{uuid4().hex[:6]}.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": str(env.tenant_id),
                "fiscal_year_start": "2026-01-01",
                "fiscal_year_end": "2026-12-31",
                "rules_sha256": rules.RULES_SHA256,
                "evidence_ref": "Michael v1537 held drafts ruling",
                "requester_party_id": str(requester),
                "approver_party_id": str(requester if same_party else approver),
                "filed_vat_months": [],
            }
        )
    )
    return path


def run_cli(engine, monkeypatch, tmp_path, env, mode, *, operator, spec, extra=()):
    url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    monkeypatch.setenv("LEGACY_DATABASE_URL", url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if mode == "apply":
        monkeypatch.setenv("ERP_HELD_DRAFTS_APPLY", "1")
    receipt = tmp_path / f"{mode}-{uuid4().hex[:6]}.json"
    code = main([mode, "--spec", str(spec), "--receipt", str(receipt), "--operator-ref", operator, *extra])
    return code, receipt


def cli_plan(engine, monkeypatch, tmp_path, env, spec, operator="operator-a"):
    code, receipt = run_cli(engine, monkeypatch, tmp_path, env, "plan", operator=operator, spec=spec)
    assert code == 0, receipt.read_text()
    return receipt, json.loads(receipt.read_text())


def cli_apply(engine, monkeypatch, tmp_path, env, spec, plan_receipt, plan, operator="operator-b"):
    return run_cli(
        engine, monkeypatch, tmp_path, env, "apply", operator=operator, spec=spec,
        extra=("--plan-receipt", str(plan_receipt), "--approved-plan-sha256", plan["plan_sha256"]),
    )  # fmt: skip


def test_cli_plan_then_apply_end_to_end_with_private_receipts(engine, monkeypatch, tmp_path):
    env = world(engine, dual=False)
    census(engine, env)
    spec = write_spec(tmp_path, env)
    plan_receipt, plan = cli_plan(engine, monkeypatch, tmp_path, env, spec)
    assert (os.stat(plan_receipt).st_mode & 0o777) == 0o600
    text = plan_receipt.read_text()
    assert str(env.tenant_id) not in text and "postgresql" not in text
    assert plan["outcomes"] == {"posted": 1, "voided": 7, "queued": 7} and plan["partition_proof"]["ok"]
    assert len(plan["bridge_lines"]) == 15 and {"rule_id", "outcome", "amount", "evidence_sha256"} <= set(
        plan["bridge_lines"][0]
    )
    assert decisions(engine, env) == {}  # plan wrote nothing
    code, receipt = cli_apply(engine, monkeypatch, tmp_path, env, spec, plan_receipt, plan)
    applied = json.loads(receipt.read_text())
    assert code == 0 and applied["ready"] and applied["final_outcomes"] == plan["outcomes"]
    assert applied["status_check"]["mismatches"] == 0 and applied["final_partition_proof"]["ok"]
    assert (os.stat(receipt).st_mode & 0o777) == 0o600
    rows = decisions(engine, env)
    receipt_sha = __import__("hashlib").sha256(plan_receipt.read_bytes()).hexdigest()
    assert len(rows) == 15 and {r.plan_receipt_sha256 for r in rows.values()} == {receipt_sha}
    assert {r.plan_sha256 for r in rows.values()} == {plan["plan_sha256"]}
    assert {r.rules_sha256 for r in rows.values()} == {rules.RULES_SHA256}
    assert {(r.planned_by, r.applied_by, r.dual_control) for r in rows.values()} == {
        ("operator-a", "operator-b", False)
    }
    # replay: same approved sha, nothing left to do
    code2, receipt2 = cli_apply(engine, monkeypatch, tmp_path, env, spec, plan_receipt, plan)
    assert code2 == 0 and json.loads(receipt2.read_text())["apply"]["decided_now"] == 0


def test_cli_apply_refuses_when_the_world_drifted_after_the_plan(engine, monkeypatch, tmp_path):
    env = world(engine, dual=False)
    statement_lines(engine, env, 1)
    fee(engine, env, status="POSTED")
    fee(engine, env)
    spec = write_spec(tmp_path, env)
    plan_receipt, plan = cli_plan(engine, monkeypatch, tmp_path, env, spec)
    statement_lines(engine, env, 1)  # evidence changed after the reviewed plan
    code, receipt = cli_apply(engine, monkeypatch, tmp_path, env, spec, plan_receipt, plan)
    assert code == 3 and json.loads(receipt.read_text())["refusal"] == "plan_drift"
    assert decisions(engine, env) == {} and set(statuses(engine, env).values()) == {"DRAFT", "POSTED"}


def test_dual_control_off_lets_one_operator_and_one_party_do_both_steps(engine, monkeypatch, tmp_path):
    env = world(engine, dual=False)
    statement_lines(engine, env, 2)
    fee(engine, env)
    spec = write_spec(tmp_path, env, same_party=True)
    plan_receipt, plan = cli_plan(engine, monkeypatch, tmp_path, env, spec, operator="operator-a")
    code, receipt = cli_apply(engine, monkeypatch, tmp_path, env, spec, plan_receipt, plan, operator="operator-a")
    assert code == 0, receipt.read_text()
    with Session(engine) as db:
        row = db.scalar(sa.select(ManualJournal).where(ManualJournal.tenant_id == env.tenant_id))
        assert row.state == "POSTED" and row.dual_control is False
        assert row.approved_by_party_id == row.requested_by_party_id  # the same person: allowed with the policy off


def test_dual_control_on_requires_two_operators_and_two_parties(engine, monkeypatch, tmp_path):
    env = world(engine, dual=True)
    statement_lines(engine, env, 2)
    fee(engine, env)
    draft = fee(engine, env, amount="5")
    spec = write_spec(tmp_path, env)
    plan_receipt, plan = cli_plan(engine, monkeypatch, tmp_path, env, spec, operator="operator-a")
    code, receipt = cli_apply(engine, monkeypatch, tmp_path, env, spec, plan_receipt, plan, operator="operator-a")
    assert code == 2 and json.loads(receipt.read_text())["refusal"] == "dual_control_same_operator"
    same_party = write_spec(tmp_path, env, same_party=True)
    pr2, plan2 = cli_plan(engine, monkeypatch, tmp_path, env, same_party, operator="operator-a")
    code, receipt = cli_apply(engine, monkeypatch, tmp_path, env, same_party, pr2, plan2, operator="operator-b")
    assert code == 2 and json.loads(receipt.read_text())["refusal"] == "dual_control_same_party"
    assert decisions(engine, env) == {} and statuses(engine, env)[draft.id] == "DRAFT"  # nothing written by refusals
    code, receipt = cli_apply(engine, monkeypatch, tmp_path, env, spec, plan_receipt, plan, operator="operator-b")
    assert code == 0, receipt.read_text()
    rows = decisions(engine, env)
    assert {r.dual_control for r in rows.values()} == {True}
    with Session(engine) as db:
        manual = db.scalar(sa.select(ManualJournal).where(ManualJournal.tenant_id == env.tenant_id))
        assert manual.dual_control is True and manual.approved_by_party_id != manual.requested_by_party_id


def test_the_cli_source_carries_no_estate_literals_and_no_secret_output():
    from pathlib import Path

    text = (Path(service.__file__).parent / "cli.py").read_text()
    assert not re.search(r"postgresql\+psycopg://", text)
    assert datetime.now(UTC).year >= 2026
