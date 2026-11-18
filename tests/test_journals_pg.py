"""B2 manual journals, held legacy journals, periods and functional currency on real PostgreSQL.

Real migrations, real accounting commands, real maker-checker table. Hosted CI only
(ERP_TEST_POSTGRES_URL), like test_posting_bridge_pg; the owner role bypasses RLS, so tenant
isolation is asserted through explicit filters and per-test tenants.
"""

import threading
import time
from datetime import UTC, date, datetime
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
    CreateJournal,
    InvalidPeriod,
    JournalKind,
    JournalLineInput,
    NormalBalance,
    PeriodCloseEvidence,
    PeriodStatus,
    SourceIdentity,
)
from dotmac_accounting.models import (
    Account,
    AccountCategory,
    FiscalPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
    PeriodEvent,
    PostedLedgerLine,
)
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, PartyType, Tenant
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dual_control_support import set_dual_control, set_dual_control_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.journals import models as m
from erp_v2.journals import policy, queries, service
from erp_v2.journals.models import ManualJournal
from erp_v2.journals.routes import journals_web_feature
from erp_v2.journals.service import JournalRefused, LineInput, PrepareJournal

NOW = datetime(2026, 1, 15, tzinfo=UTC)
DAY = date(2026, 1, 10)


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


@pytest.fixture(autouse=True, scope="module")
def _registries():
    manifests = [journals_web_feature, FeatureManifest(name="kernel_stub")]
    install_audit_actions(AuditActionRegistry.from_manifests(manifests))
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


def seed(engine, *, currency: str | None = "USD", adjustment: bool = False) -> SimpleNamespace:
    tenant_id = uuid4()
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, slug=f"t{tenant_id.hex[:24]}", name="Journals"))
        db.flush()
        set_dual_control(db, tenant_id, True)  # existing separation-of-duties assertions run with the policy ON
        categories = {
            code: AccountCategory(tenant_id=tenant_id, code=code, name=code, account_class=klass)
            for code, klass in (
                ("AST", AccountClass.ASSET),
                ("REV", AccountClass.REVENUE),
                ("LIA", AccountClass.LIABILITY),
            )
        }
        db.add_all(categories.values())
        db.flush()
        accounts = {}
        for code, category, balance in (
            ("1000", "AST", NormalBalance.DEBIT),
            ("1400", "AST", NormalBalance.DEBIT),
            ("4000", "REV", NormalBalance.CREDIT),
            ("2000", "LIA", NormalBalance.CREDIT),
        ):
            row = Account(
                tenant_id=tenant_id,
                category_id=categories[category].id,
                code=code,
                name=f"Account {code}",
                kind=AccountKind.POSTING,
                normal_balance=balance,
            )
            db.add(row)
            db.flush()
            accounts[code] = row.id
        year = FiscalYear(
            tenant_id=tenant_id, code="FY26", name="FY 2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
        )
        db.add(year)
        db.flush()
        periods = {}
        for number, (start, end) in enumerate(
            ((date(2026, 1, 1), date(2026, 1, 31)), (date(2026, 2, 1), date(2026, 2, 28))), 1
        ):
            period = FiscalPeriod(
                tenant_id=tenant_id,
                fiscal_year_id=year.id,
                period_number=number,
                name=f"P{number}",
                start_date=start,
                end_date=end,
                is_adjustment=adjustment and number == 1,
                status=PeriodStatus.FUTURE,
            )
            db.add(period)
            db.flush()
            periods[number] = period.id
        parties = {}
        for name in ("preparer", "requester", "approver", "approver2"):
            party = Party(
                id=uuid4(),
                tenant_id=tenant_id,
                party_type=PartyType.person,
                display_name=name,
                email=f"{name}-{tenant_id.hex[:6]}@acme.test",
            )
            db.add(party)
            db.flush()
            parties[name] = party.id
        if currency:
            policy.set_functional_currency(db, tenant_id=tenant_id, code=currency, reason="test seed")
        accounting.open_period(
            db, scope=TenantScope(tenant_id), period_id=periods[1], actor_ref="seed", occurred_at=NOW
        )
        db.commit()
    return SimpleNamespace(tenant_id=tenant_id, accounts=accounts, periods=periods, parties=parties)


def close_period(engine, env, number=1, *, lock=False):
    scope = TenantScope(env.tenant_id)
    with Session(engine) as db:
        accounting.soft_close_period(
            db,
            scope=scope,
            period_id=env.periods[number],
            evidence=PeriodCloseEvidence((CloseCheck("CHECK1", True, "evidence-1", "e" * 64),)),
            actor_ref="test",
            approval_reference="close-1",
            occurred_at=NOW,
        )
        if lock:
            accounting.lock_period(
                db,
                scope=scope,
                period_id=env.periods[number],
                actor_ref="test",
                approval_reference="lock-1",
                occurred_at=NOW,
            )
        db.commit()


def command(env, *, amount="100", request_id=None, evidence="SUB-REPORT-1", day=DAY, kind=JournalKind.STANDARD):
    return PrepareJournal(
        request_id=request_id or uuid4(),
        posting_date=day,
        kind=kind,
        description="Option B summary journal",
        evidence_ref=evidence,
        lines=(
            LineInput(env.accounts["1000"], debit=Decimal(amount)),
            LineInput(env.accounts["4000"], credit=Decimal(amount)),
        ),
    )


def run(engine, env, fn, **kwargs):
    with Session(engine) as db:
        result = fn(db, tenant_id=env.tenant_id, now=NOW, **kwargs)
        db.commit()
        return result


def prepare(engine, env, who="preparer", **kw):
    return run(engine, env, service.prepare_journal, actor=env.parties[who], command=command(env, **kw))


def request(engine, env, outcome, who="preparer"):
    return run(engine, env, service.request_post, manual_id=outcome.manual_id, actor=env.parties[who])


def approve(engine, env, outcome, who="approver"):
    return run(engine, env, service.approve_and_post, manual_id=outcome.manual_id, approver=env.parties[who])


def counts(engine, env) -> dict[str, int]:
    with Session(engine) as db:

        def n(model):
            return db.scalar(sa.select(sa.func.count()).select_from(model).where(model.tenant_id == env.tenant_id))

        return {
            "workflow": n(ManualJournal),
            "journals": n(JournalEntry),
            "lines": n(JournalLine),
            "ledger": n(PostedLedgerLine),
            "audit": n(AuditEvent),
        }


def actions(engine, env) -> list[str]:
    with Session(engine) as db:
        return list(
            db.scalars(
                sa.select(AuditEvent.action)
                .where(AuditEvent.tenant_id == env.tenant_id)
                .order_by(AuditEvent.created_at)
            )
        )


def posted_flow(engine, env):
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    approve(engine, env, outcome)
    return outcome


# --- functional currency ---------------------------------------------------


def test_journal_uses_the_tenant_functional_currency_not_a_literal(engine):
    env = seed(engine, currency="USD")
    outcome = prepare(engine, env)
    with Session(engine) as db:
        journal = db.get(JournalEntry, outcome.journal_id)
        assert (journal.currency_code, journal.exchange_rate) == ("USD", Decimal(1))
        assert journal.source_owner == "erp.manual" and journal.reference == "SUB-REPORT-1"
        assert journal.status.value == "DRAFT"


def test_unset_functional_currency_fails_closed_and_writes_nothing(engine):
    env = seed(engine, currency=None)
    before = counts(engine, env)
    with pytest.raises(JournalRefused) as exc:
        prepare(engine, env)
    assert exc.value.code == "functional_currency_unset"
    assert counts(engine, env) == before


def test_functional_currency_is_tenant_scoped(engine):
    one, two = seed(engine, currency="USD"), seed(engine, currency="GHS")
    with Session(engine) as db:
        assert policy.functional_currency(db, one.tenant_id) == "USD"
        assert policy.functional_currency(db, two.tenant_id) == "GHS"


def test_functional_currency_locks_once_a_journal_is_posted(engine):
    env = seed(engine, currency="USD")
    with Session(engine) as db:  # nothing posted yet: a change is allowed
        assert policy.set_functional_currency(db, tenant_id=env.tenant_id, code="eur", reason="correction") == "EUR"
        db.commit()
    posted_flow(engine, env)  # prepared under EUR, posted
    with Session(engine) as db:
        assert policy.set_functional_currency(db, tenant_id=env.tenant_id, code="EUR", reason="same") == "EUR"
        with pytest.raises(policy.FunctionalCurrencyError) as exc:
            policy.set_functional_currency(db, tenant_id=env.tenant_id, code="USD", reason="change")
        assert exc.value.code == "functional_currency_locked"
        assert policy.functional_currency(db, env.tenant_id) == "EUR"


def test_imported_setting_row_is_the_value_the_service_reads(engine):
    from erp_v2.importer.minting import MintContext, run_mint_step

    env = seed(engine, currency=None)
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TEMP TABLE organization (organization_id TEXT, functional_currency_code TEXT)")
        )
        connection.execute(sa.text("INSERT INTO organization VALUES (:id, 'ghs')"), {"id": env.tenant_id.hex})
        ctx = MintContext(tenant_id=env.tenant_id.hex, occurred_at=NOW)
        first = run_mint_step("functional_currency_settings", connection, connection, ctx)
        again = run_mint_step("functional_currency_settings", connection, connection, ctx)
        connection.execute(sa.text("DROP TABLE organization"))
    assert (first.created, first.refused, first.blockers) == (1, None, [])
    assert (again.created, again.existing) == (0, 1)
    with Session(engine) as db:
        assert policy.functional_currency(db, env.tenant_id) == "GHS"
    outcome = prepare(engine, env)
    with Session(engine) as db:
        assert db.get(JournalEntry, outcome.journal_id).currency_code == "GHS"


@pytest.mark.parametrize("bad", ["", "US", "USDX", "U5D", "  ", None])
def test_invalid_functional_currency_refuses(engine, bad):
    env = seed(engine, currency=None)
    with Session(engine) as db:
        with pytest.raises(policy.FunctionalCurrencyError) as exc:
            policy.set_functional_currency(db, tenant_id=env.tenant_id, code=bad, reason="x")
        assert exc.value.code == "functional_currency_invalid"


# --- prepare ---------------------------------------------------------------


def test_prepare_replay_is_idempotent_and_a_changed_request_refuses(engine):
    env = seed(engine)
    request_id = uuid4()
    first = prepare(engine, env, request_id=request_id)
    after_first = counts(engine, env)
    again = prepare(engine, env, request_id=request_id)
    assert again.replayed is True and again.manual_id == first.manual_id
    assert counts(engine, env) == after_first
    with pytest.raises(JournalRefused) as exc:
        prepare(engine, env, request_id=request_id, amount="250")
    assert exc.value.code in {"accounting_refused", "conflict"}
    assert counts(engine, env) == after_first


@pytest.mark.parametrize(
    "case,code",
    [
        ("missing_evidence", "invalid_input"),
        ("closing_kind", "invalid_input"),
        ("unbalanced", "unbalanced"),
        ("closed_period", "period_closed"),
        ("no_period", "period_missing"),
        ("unknown_account", "accounting_refused"),
        ("adjustment_period_standard", "period_adjustment_only"),
    ],
)
def test_prepare_refusals_leave_zero_rows_and_keep_the_callers_write(engine, case, code):
    env = seed(engine)
    cmd = command(env)
    if case == "missing_evidence":
        cmd = command(env, evidence="  ")
    elif case == "closing_kind":
        cmd = command(env, kind=JournalKind.CLOSING)
    elif case == "unbalanced":
        cmd = PrepareJournal(
            cmd.request_id,
            DAY,
            cmd.kind,
            "d",
            "E-1",
            (LineInput(env.accounts["1000"], debit=Decimal(100)), LineInput(env.accounts["4000"], credit=Decimal(99))),
        )
    elif case == "closed_period":
        cmd = command(env, day=date(2026, 2, 10))  # February is still FUTURE
    elif case == "no_period":
        cmd = command(env, day=date(2027, 6, 1))
    elif case == "unknown_account":
        cmd = PrepareJournal(
            cmd.request_id,
            DAY,
            cmd.kind,
            "d",
            "E-1",
            (LineInput(uuid4(), debit=Decimal(100)), LineInput(env.accounts["4000"], credit=Decimal(100))),
        )
    elif case == "adjustment_period_standard":
        env = seed(engine, adjustment=True)
        cmd = command(env)
    before = counts(engine, env)
    with Session(engine) as db:
        db.add(
            AccountCategory(tenant_id=env.tenant_id, code="CALLER", name="Caller", account_class=AccountClass.EXPENSE)
        )
        db.flush()
        with pytest.raises(JournalRefused) as exc:
            service.prepare_journal(db, tenant_id=env.tenant_id, actor=env.parties["preparer"], command=cmd, now=NOW)
        assert exc.value.code == code
        db.commit()
    assert counts(engine, env) == before
    with Session(engine) as db:
        assert db.scalar(
            sa.select(AccountCategory.id).where(
                AccountCategory.code == "CALLER", AccountCategory.tenant_id == env.tenant_id
            )
        )


def test_prepare_refuses_an_actor_from_another_tenant(engine):
    env, other = seed(engine), seed(engine)
    with Session(engine) as db:
        with pytest.raises(JournalRefused) as exc:
            service.prepare_journal(
                db, tenant_id=env.tenant_id, actor=other.parties["preparer"], command=command(env), now=NOW
            )
        assert exc.value.code == "actor_invalid"


# --- request + approve + post ---------------------------------------------


def test_full_flow_posts_through_accounting_with_the_approver_recorded(engine):
    env = seed(engine)
    outcome = posted_flow(engine, env)
    with Session(engine) as db:
        journal = db.get(JournalEntry, outcome.journal_id)
        row = db.scalar(sa.select(ManualJournal).where(ManualJournal.id == outcome.manual_id))
        assert journal.status.value == "POSTED" and journal.posted_by == str(env.parties["approver"])
        assert journal.approval_reference == f"erp.manual:{row.id}:post"
        assert (row.state, row.prepared_by_party_id, row.approved_by_party_id) == (
            m.STATE_POSTED,
            env.parties["preparer"],
            env.parties["approver"],
        )
        ledger = db.scalar(
            sa.select(sa.func.count()).select_from(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal.id)
        )
        assert ledger == 2
    assert actions(engine, env) == ["erp.journal.prepared", "erp.journal.post_requested", "erp.journal.posted"]


def test_preparer_cannot_approve_and_nothing_changes(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    before = counts(engine, env)
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, outcome, who="preparer")
    assert exc.value.code == "sod_preparer_cannot_approve"
    assert counts(engine, env) == before and before["ledger"] == 0
    with Session(engine) as db:
        assert db.get(JournalEntry, outcome.journal_id).status.value == "DRAFT"


def test_requester_cannot_approve_even_when_not_the_preparer(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome, who="requester")
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, outcome, who="requester")
    assert exc.value.code == "sod_requester_cannot_approve"
    approve(engine, env, outcome, who="approver")  # a third person can


def test_database_refuses_self_approval_even_when_the_service_is_bypassed(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    with Session(engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                sa.update(ManualJournal)
                .where(ManualJournal.id == outcome.manual_id)
                .values(
                    state=m.STATE_POSTED,
                    approved_by_party_id=env.parties["preparer"],
                    approved_at=NOW,
                )
            )
            db.flush()
        db.rollback()


def test_cannot_approve_before_it_is_requested_or_from_a_wrong_state(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, outcome)
    assert exc.value.code == "state_refused"


def test_approve_replay_by_the_same_approver_and_refusal_for_a_second(engine):
    env = seed(engine)
    outcome = posted_flow(engine, env)
    before = counts(engine, env)
    assert approve(engine, env, outcome).replayed is True
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, outcome, who="approver2")
    assert exc.value.code == "state_refused"
    assert counts(engine, env) == before


def test_period_closed_between_request_and_approval_refuses_and_stays_requested(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    close_period(engine, env)
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, outcome)
    assert exc.value.code == "period_closed"
    with Session(engine) as db:
        assert (
            db.scalar(sa.select(ManualJournal.state).where(ManualJournal.id == outcome.manual_id))
            == m.STATE_POST_REQUESTED
        )
        assert db.get(JournalEntry, outcome.journal_id).status.value == "DRAFT"
    assert counts(engine, env)["ledger"] == 0


def test_failure_after_the_ledger_append_rolls_the_posting_back(engine, monkeypatch):
    env = seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    seen = {}

    def failing_audit(db, **kwargs):
        seen["ledger_rows"] = db.scalar(
            sa.select(sa.func.count()).select_from(PostedLedgerLine).where(PostedLedgerLine.tenant_id == env.tenant_id)
        )
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(service, "_audit", failing_audit)
    with Session(engine) as db:
        db.add(
            AccountCategory(tenant_id=env.tenant_id, code="CALLER", name="Caller", account_class=AccountClass.EXPENSE)
        )
        db.flush()
        with pytest.raises(RuntimeError):
            service.approve_and_post(
                db, tenant_id=env.tenant_id, manual_id=outcome.manual_id, approver=env.parties["approver"], now=NOW
            )
        db.commit()
    assert seen["ledger_rows"] == 2  # the real post had happened inside the savepoint
    assert counts(engine, env)["ledger"] == 0
    with Session(engine) as db:
        assert db.get(JournalEntry, outcome.journal_id).status.value == "DRAFT"
        assert (
            db.scalar(sa.select(ManualJournal.state).where(ManualJournal.id == outcome.manual_id))
            == m.STATE_POST_REQUESTED
        )
        assert db.scalar(
            sa.select(AccountCategory.id).where(
                AccountCategory.code == "CALLER", AccountCategory.tenant_id == env.tenant_id
            )
        )


def test_reject_is_terminal_and_cannot_be_approved(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    run(engine, env, service.reject, manual_id=outcome.manual_id, actor=env.parties["approver"], reason="wrong period")
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, outcome, who="approver2")
    assert exc.value.code == "state_refused"
    assert counts(engine, env)["ledger"] == 0


def test_another_tenants_journal_is_not_found(engine):
    env, other = seed(engine), seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    with pytest.raises(JournalRefused) as exc:
        run(engine, other, service.approve_and_post, manual_id=outcome.manual_id, approver=other.parties["approver"])
    assert exc.value.code == "not_found"


def test_two_concurrent_approvals_post_exactly_once(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    holder = Session(engine)
    service.approve_and_post(
        holder, tenant_id=env.tenant_id, manual_id=outcome.manual_id, approver=env.parties["approver"], now=NOW
    )
    result: dict[str, object] = {}

    def second():
        try:
            approve(engine, env, outcome, who="approver2")
        except JournalRefused as exc:
            result["refused"] = exc.code

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(1.0)  # blocked on the first approver's row lock
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and result == {"refused": "state_refused"}
    assert counts(engine, env)["ledger"] == 2


# --- reversal --------------------------------------------------------------


def test_reversal_flow_and_its_sod(engine):
    env = seed(engine)
    outcome = posted_flow(engine, env)
    run(
        engine,
        env,
        service.request_reversal,
        manual_id=outcome.manual_id,
        actor=env.parties["requester"],
        reason="booked twice",
        posting_date=DAY,
    )
    for who, code in (("requester", "sod_requester_cannot_approve"), ("preparer", "sod_preparer_cannot_approve")):
        with pytest.raises(JournalRefused) as exc:
            run(engine, env, service.approve_reversal, manual_id=outcome.manual_id, approver=env.parties[who])
        assert exc.value.code == code
    done = run(engine, env, service.approve_reversal, manual_id=outcome.manual_id, approver=env.parties["approver"])
    assert done.state == m.STATE_REVERSED
    with Session(engine) as db:
        original = db.get(JournalEntry, outcome.journal_id)
        row = db.scalar(sa.select(ManualJournal).where(ManualJournal.id == outcome.manual_id))
        reversal = db.get(JournalEntry, row.reversal_journal_id)
        assert original.status.value == "REVERSED" and reversal.status.value == "POSTED"
        assert reversal.kind == JournalKind.REVERSAL and reversal.reverses_journal_id == original.id
    again = run(engine, env, service.approve_reversal, manual_id=outcome.manual_id, approver=env.parties["approver"])
    assert again.replayed is True


def test_reversal_into_a_closed_period_is_refused(engine):
    env = seed(engine)
    outcome = posted_flow(engine, env)
    with pytest.raises(JournalRefused) as exc:
        run(
            engine,
            env,
            service.request_reversal,
            manual_id=outcome.manual_id,
            actor=env.parties["requester"],
            reason="x",
            posting_date=date(2026, 2, 10),
        )
    assert exc.value.code == "period_closed"


def test_withdrawn_reversal_returns_to_posted(engine):
    env = seed(engine)
    outcome = posted_flow(engine, env)
    run(
        engine,
        env,
        service.request_reversal,
        manual_id=outcome.manual_id,
        actor=env.parties["requester"],
        reason="x",
        posting_date=DAY,
    )
    back = run(engine, env, service.reject, manual_id=outcome.manual_id, actor=env.parties["approver"], reason="no")
    assert back.state == m.STATE_POSTED


# --- held legacy journals --------------------------------------------------


def legacy_draft(engine, env, *, document="legacy-1", amount="75", owner="legacy-erp") -> SimpleNamespace:
    with Session(engine) as db:
        journal = accounting.create_journal(
            db,
            scope=TenantScope(env.tenant_id),
            command=CreateJournal(
                number=f"LEG-{document}".upper(),
                kind=JournalKind.STANDARD,
                entry_date=DAY,
                posting_date=DAY,
                currency_code="USD",
                exchange_rate=Decimal(1),
                description="Imported legacy journal",
                source=SourceIdentity(owner, "journal", document, "1", "b" * 64),
                lines=(
                    JournalLineInput(env.accounts["1000"], debit=Decimal(amount)),
                    JournalLineInput(env.accounts["4000"], credit=Decimal(amount)),
                ),
            ),
            idempotency_key=f"legacy:{document}",
            idempotency_expires_at=None,
            recorded_at=NOW,
        )
        db.commit()
        return SimpleNamespace(id=journal.id)


def test_held_journal_posts_one_at_a_time_with_sod_and_provenance(engine):
    env = seed(engine)
    held, other = legacy_draft(engine, env, document="a"), legacy_draft(engine, env, document="b")
    with Session(engine) as db:
        page = queries.held_journals(db, tenant_id=env.tenant_id, page=1)
        assert page["total"] == 2
    with pytest.raises(JournalRefused) as exc:
        run(engine, env, service.request_legacy_post, journal_id=held.id, actor=env.parties["preparer"], review_ref=" ")
    assert exc.value.code == "invalid_input"
    requested = run(
        engine,
        env,
        service.request_legacy_post,
        journal_id=held.id,
        actor=env.parties["preparer"],
        review_ref="FIN-REVIEW-7",
    )
    assert requested.state == m.STATE_POST_REQUESTED
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, requested, who="preparer")
    assert exc.value.code == "sod_preparer_cannot_approve"  # the reviewer cannot self-approve
    approve(engine, env, requested, who="approver")
    with Session(engine) as db:
        posted = db.get(JournalEntry, held.id)
        untouched = db.get(JournalEntry, other.id)
        row = db.scalar(sa.select(ManualJournal).where(ManualJournal.id == requested.manual_id))
        assert posted.status.value == "POSTED"
        assert (posted.source_owner, posted.source_document_id, posted.source_fingerprint) == (
            "legacy-erp",
            "a",
            "b" * 64,
        )
        assert row.origin == m.ORIGIN_LEGACY_HELD and row.legacy_source_key == "legacy-erp:journal:a:1"
        assert row.evidence_ref == "FIN-REVIEW-7"
        assert untouched.status.value == "DRAFT"  # no bulk effect on the other held journal
        assert queries.held_journals(db, tenant_id=env.tenant_id, page=1)["total"] == 1


def test_held_journal_request_is_replay_safe_and_refused_when_taken_or_not_draft(engine):
    env = seed(engine)
    held = legacy_draft(engine, env)
    first = run(
        engine, env, service.request_legacy_post, journal_id=held.id, actor=env.parties["preparer"], review_ref="R-1"
    )
    again = run(
        engine, env, service.request_legacy_post, journal_id=held.id, actor=env.parties["preparer"], review_ref="R-1"
    )
    assert again.replayed is True and again.manual_id == first.manual_id
    with pytest.raises(JournalRefused) as exc:
        run(
            engine,
            env,
            service.request_legacy_post,
            journal_id=held.id,
            actor=env.parties["requester"],
            review_ref="R-2",
        )
    assert exc.value.code == "already_requested"
    posted = posted_flow(engine, env)
    with pytest.raises(JournalRefused) as exc:
        run(
            engine,
            env,
            service.request_legacy_post,
            journal_id=posted.journal_id,
            actor=env.parties["requester"],
            review_ref="R-3",
        )
    assert exc.value.code in {"already_requested", "journal_state_refused", "not_legacy_held"}


def test_held_journal_in_a_closed_period_is_refused_at_request(engine):
    env = seed(engine)
    held = legacy_draft(engine, env)
    close_period(engine, env, lock=True)
    before = counts(engine, env)
    with pytest.raises(JournalRefused) as exc:
        run(
            engine,
            env,
            service.request_legacy_post,
            journal_id=held.id,
            actor=env.parties["preparer"],
            review_ref="R-1",
        )
    assert exc.value.code == "period_closed"
    assert counts(engine, env) == before


def test_there_is_no_bulk_entry_point():
    import inspect

    params = inspect.signature(service.request_legacy_post).parameters
    assert "journal_id" in params and not any(name.endswith("ids") for name in params)
    assert not [name for name in dir(service) if "bulk" in name.lower() or name.endswith("_all")]


# --- periods ---------------------------------------------------------------


def test_period_open_soft_close_lock_with_accounting_events_and_audit(engine):
    env = seed(engine)
    actor = env.parties["approver"]
    assert run(engine, env, service.open_period, period_id=env.periods[2], actor=actor) == "OPEN"
    with pytest.raises(JournalRefused):
        run(engine, env, service.open_period, period_id=env.periods[2], actor=actor)  # only FUTURE opens
    assert (
        run(engine, env, service.soft_close_period, period_id=env.periods[2], actor=actor, review_ref="CLOSE-FEB")
        == "SOFT_CLOSED"
    )
    with pytest.raises(JournalRefused) as exc:
        prepare(engine, env, day=date(2026, 2, 10))
    assert exc.value.code == "period_closed"
    assert run(engine, env, service.lock_period, period_id=env.periods[2], actor=actor) == "LOCKED"
    with Session(engine) as db:
        events = list(
            db.execute(
                sa.select(PeriodEvent.event_kind, PeriodEvent.actor_ref)
                .where(PeriodEvent.tenant_id == env.tenant_id, PeriodEvent.period_id == env.periods[2])
                .order_by(PeriodEvent.occurred_at, PeriodEvent.event_kind)
            )
        )
        assert {kind for kind, _ in events} == {"open", "soft_close", "lock"}
        assert {ref for _, ref in events} == {str(actor)}
    assert {"erp.period.opened", "erp.period.soft_closed", "erp.period.locked"} <= set(actions(engine, env))


def test_soft_close_refuses_with_a_pending_manual_journal_and_needs_a_review_ref(engine):
    env = seed(engine)
    prepare(engine, env)  # PREPARED, unresolved
    actor = env.parties["approver"]
    with pytest.raises(JournalRefused) as exc:
        run(engine, env, service.soft_close_period, period_id=env.periods[1], actor=actor, review_ref="R")
    assert exc.value.code == "pending_manual_journals"
    with pytest.raises(JournalRefused) as exc:
        run(engine, env, service.soft_close_period, period_id=env.periods[1], actor=actor, review_ref="")
    assert exc.value.code == "invalid_input"
    with Session(engine) as db:
        assert db.get(FiscalPeriod, env.periods[1]).status == PeriodStatus.OPEN


def test_lock_requires_soft_close_and_unknown_period_is_not_found(engine):
    env = seed(engine)
    with pytest.raises(JournalRefused):
        run(engine, env, service.lock_period, period_id=env.periods[1], actor=env.parties["approver"])
    with pytest.raises(JournalRefused) as exc:
        run(engine, env, service.open_period, period_id=uuid4(), actor=env.parties["approver"])
    assert exc.value.code == "not_found"


def test_service_never_exposes_period_reopen_or_account_creation():
    names = set(dir(service))
    assert "reopen_period" not in names
    assert not {n for n in names if n.startswith("create_")}
    assert InvalidPeriod  # accounting's own refusal type is what period commands map from


# --- review changes (B2-1 .. B2-6) ------------------------------------------


def test_another_modules_draft_is_not_held_and_cannot_be_requested(engine):
    env = seed(engine)
    foreign = legacy_draft(engine, env, document="dep-1", owner="finance")
    ours = legacy_draft(engine, env, document="leg-1")
    with Session(engine) as db:
        page = queries.held_journals(db, tenant_id=env.tenant_id, page=1)
        assert [j.id for j in page["journals"]] == [ours.id] and page["total"] == 1
    before = counts(engine, env)
    with pytest.raises(JournalRefused) as exc:
        run(
            engine,
            env,
            service.request_legacy_post,
            journal_id=foreign.id,
            actor=env.parties["preparer"],
            review_ref="R",
        )
    assert exc.value.code == "not_legacy_held"
    assert counts(engine, env) == before


def test_rejected_held_journal_can_be_re_requested_with_sod_and_audit_history(engine):
    env = seed(engine)
    held = legacy_draft(engine, env)
    first = run(
        engine, env, service.request_legacy_post, journal_id=held.id, actor=env.parties["preparer"], review_ref="R-1"
    )
    run(engine, env, service.reject, manual_id=first.manual_id, actor=env.parties["approver"], reason="missing support")
    with Session(engine) as db:
        assert queries.held_journals(db, tenant_id=env.tenant_id, page=1)["total"] == 1  # held again
    again = run(
        engine, env, service.request_legacy_post, journal_id=held.id, actor=env.parties["requester"], review_ref="R-2"
    )
    assert again.manual_id == first.manual_id and again.state == m.STATE_POST_REQUESTED
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, again, who="requester")
    assert exc.value.code == "sod_preparer_cannot_approve"
    approve(engine, env, again, who="approver")
    with Session(engine) as db:
        row = db.scalar(sa.select(ManualJournal).where(ManualJournal.id == first.manual_id))
        assert (row.state, row.evidence_ref, row.prepared_by_party_id) == (
            m.STATE_POSTED,
            "R-2",
            env.parties["requester"],
        )
        assert row.rejection_reason is None
        details = list(
            db.scalars(
                sa.select(AuditEvent.details).where(
                    AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == "erp.journal.post_requested"
                )
            )
        )
        assert any(d.get("prior_reason") == "missing support" and d.get("re_request") for d in details)
    assert "erp.journal.rejected" in actions(engine, env)


def test_rejected_manual_journal_stays_terminal(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    request(engine, env, outcome)
    run(engine, env, service.reject, manual_id=outcome.manual_id, actor=env.parties["approver"], reason="no")
    with pytest.raises(JournalRefused):
        request(engine, env, outcome, who="requester")
    journal_id = outcome.journal_id
    with pytest.raises(JournalRefused) as exc:
        run(
            engine,
            env,
            service.request_legacy_post,
            journal_id=journal_id,
            actor=env.parties["requester"],
            review_ref="R",
        )
    assert exc.value.code == "not_legacy_held"


def _raw_update(engine, manual_id, **values):
    with Session(engine) as db:
        try:
            db.execute(sa.update(ManualJournal).where(ManualJournal.id == manual_id).values(**values))
            db.flush()
        finally:
            db.rollback()


@pytest.mark.parametrize(
    "change",
    [
        {"prepared_by_party_id": "requester"},
        {"requested_by_party_id": "approver2"},
        {"approved_by_party_id": "approver2"},
        {"evidence_ref": "TAMPERED"},
    ],
)
def test_trigger_freezes_recorded_actors_and_evidence(engine, change):
    env = seed(engine)
    outcome = posted_flow(engine, env)
    values = {k: (env.parties[v] if k.endswith("party_id") else v) for k, v in change.items()}
    with pytest.raises(IntegrityError):
        _raw_update(engine, outcome.manual_id, **values)


def test_trigger_refuses_undeclared_state_transitions(engine):
    env = seed(engine)
    outcome = prepare(engine, env)  # PREPARED
    with pytest.raises(IntegrityError):
        _raw_update(
            engine,
            outcome.manual_id,
            state=m.STATE_POSTED,
            requested_by_party_id=env.parties["requester"],
            requested_at=NOW,
            approved_by_party_id=env.parties["approver"],
            approved_at=NOW,
        )


def test_downgrade_refuses_while_the_table_holds_evidence(engine):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    env = seed(engine)
    prepare(engine, env)
    module = importlib.import_module("erp_v2.migrations.versions.e2_0014_manual_journals")
    with engine.connect() as connection:
        transaction = connection.begin()
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="maker-checker evidence"):
                module.downgrade()
        transaction.rollback()
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count()).select_from(ManualJournal).where(ManualJournal.tenant_id == env.tenant_id)
            )
            == 1
        )


def test_control_account_journal_is_flagged_not_blocked(engine):
    env = seed(engine)
    cmd = PrepareJournal(
        uuid4(),
        DAY,
        JournalKind.STANDARD,
        "AP correction",
        "RECON-ITEM-9",
        (LineInput(env.accounts["2000"], debit=Decimal(10)), LineInput(env.accounts["1000"], credit=Decimal(10))),
    )
    outcome = run(engine, env, service.prepare_journal, actor=env.parties["preparer"], command=cmd)
    with Session(engine) as db:
        assert queries.detail(db, tenant_id=env.tenant_id, manual_id=outcome.manual_id)["control_accounts"] == ["2000"]
    request(engine, env, outcome)
    approve(engine, env, outcome)  # warning only: it still posts
    plain = prepare(engine, env)
    with Session(engine) as db:
        assert queries.detail(db, tenant_id=env.tenant_id, manual_id=plain.manual_id)["control_accounts"] == []


def test_held_drafts_are_counted_per_period_for_the_soft_close_screen(engine):
    env = seed(engine)
    legacy_draft(engine, env, document="a")
    legacy_draft(engine, env, document="b")
    legacy_draft(engine, env, document="c", owner="finance")
    with Session(engine) as db:
        assert queries.held_counts_by_period(db, tenant_id=env.tenant_id) == {env.periods[1]: 2}


# --- dual-control policy (D-9): default OFF ----------------------------------------------------------------------


def _stamp(engine, manual_id) -> bool:
    with Session(engine) as db:
        return db.scalar(sa.select(ManualJournal.dual_control).where(ManualJournal.id == manual_id))


def test_policy_off_one_person_prepares_requests_approves_and_reverses(engine):
    env = seed(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    outcome = prepare(engine, env, who="preparer")
    assert _stamp(engine, outcome.manual_id) is False  # the row takes the tenant policy at creation
    request(engine, env, outcome, who="preparer")
    assert approve(engine, env, outcome, who="preparer").state == m.STATE_POSTED  # the same person approves
    run(
        engine,
        env,
        service.request_reversal,
        manual_id=outcome.manual_id,
        actor=env.parties["preparer"],
        reason="booked twice",
        posting_date=DAY,
    )
    done = run(engine, env, service.approve_reversal, manual_id=outcome.manual_id, approver=env.parties["preparer"])
    assert done.state == m.STATE_REVERSED


def test_policy_on_stamps_the_row_and_the_refusals_still_fire(engine):
    env = seed(engine)  # the seed turns the policy ON
    outcome = prepare(engine, env)
    assert _stamp(engine, outcome.manual_id) is True
    request(engine, env, outcome, who="requester")
    for who, code in (("preparer", "sod_preparer_cannot_approve"), ("requester", "sod_requester_cannot_approve")):
        with pytest.raises(JournalRefused) as exc:
            approve(engine, env, outcome, who=who)
        assert exc.value.code == code


def test_a_row_keeps_the_stamp_it_was_created_with_when_the_policy_changes_later(engine):
    env = seed(engine)
    outcome = prepare(engine, env)  # created with the policy ON
    set_dual_control_engine(engine, env.tenant_id, False)
    request(engine, env, outcome, who="requester")
    with pytest.raises(JournalRefused) as exc:
        approve(engine, env, outcome, who="requester")
    assert exc.value.code == "sod_requester_cannot_approve"  # strict rows stay strict


def test_runtime_cannot_flip_the_stamp_of_a_manual_journal(engine):
    env = seed(engine)
    outcome = prepare(engine, env)
    with pytest.raises(IntegrityError):
        _raw_update(engine, outcome.manual_id, dual_control=False)


def test_check_post_sod_is_rewritten_not_dual_control_or_predicate(engine):
    env = seed(engine)
    for on, expect_error in ((True, True), (False, False)):
        outcome = prepare(engine, env)
        request(engine, env, outcome)
        with Session(engine) as db:
            # dual_control is frozen by trigger for updates, so exercise the CHECK through a fresh INSERT
            row = db.get(ManualJournal, outcome.manual_id)
            clone = ManualJournal(
                tenant_id=env.tenant_id,
                journal_id=uuid4(),
                dual_control=on,
                origin=m.ORIGIN_MANUAL,
                state=m.STATE_POSTED,
                evidence_ref="E",
                prepared_by_party_id=env.parties["preparer"],
                prepared_at=NOW,
                requested_by_party_id=env.parties["requester"],
                requested_at=NOW,
                approved_by_party_id=row.prepared_by_party_id,  # self-approval
                approved_at=NOW,
            )
            db.add(clone)
            if expect_error:
                with pytest.raises(IntegrityError) as exc:
                    db.flush()
                assert "ck_erp_manual_journals_post_sod" in str(exc.value)
            else:
                db.flush()  # policy off: the database accepts the same person
            db.rollback()


def test_check_reversal_sod_is_rewritten_not_dual_control_or_predicate(engine):
    env = seed(engine)
    for on, expect_error in ((True, True), (False, False)):
        with Session(engine) as db:
            clone = ManualJournal(
                tenant_id=env.tenant_id,
                journal_id=uuid4(),
                dual_control=on,
                origin=m.ORIGIN_MANUAL,
                state=m.STATE_POSTED,
                evidence_ref="E",
                prepared_by_party_id=env.parties["preparer"],
                prepared_at=NOW,
                requested_by_party_id=env.parties["requester"],
                requested_at=NOW,
                approved_by_party_id=env.parties["approver"],
                approved_at=NOW,
                reversal_requested_by_party_id=env.parties["requester"],
                reversal_approved_by_party_id=env.parties["requester"],  # reverser == reversal requester
            )
            db.add(clone)
            if expect_error:
                with pytest.raises(IntegrityError) as exc:
                    db.flush()
                assert "ck_erp_manual_journals_reversal_sod" in str(exc.value)
            else:
                db.flush()
            db.rollback()
