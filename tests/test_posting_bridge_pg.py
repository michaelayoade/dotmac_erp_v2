"""Posting bridge against real PostgreSQL, real accounting migrations and commands.

Runs only on the disposable hosted CI service (ERP_TEST_POSTGRES_URL), like the other
hosted qualification tests. The owner role used here bypasses RLS; tenant scoping is
asserted through explicit tenant filters and per-test tenants.
"""

import os
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
    NormalBalance,
    PeriodCloseEvidence,
    PeriodStatus,
)
from dotmac_accounting.models import (
    Account,
    AccountCategory,
    FiscalPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
    PostedLedgerLine,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.idempotency_models import IdempotencyRecord
from dotmac_kernel.models import Tenant
from sqlalchemy.orm import Session

from erp_v2.posting import bridge
from erp_v2.posting.rules import PostingRefused

NOW = datetime(2026, 1, 15, tzinfo=UTC)
FP = "a" * 64


@pytest.fixture(scope="module")
def engine():
    raw_url = os.environ.get("ERP_TEST_POSTGRES_URL")
    if not raw_url:
        pytest.skip("ERP_TEST_POSTGRES_URL is required for hosted PostgreSQL qualification")
    admin_url = sa.make_url(raw_url)
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert admin_url.get_backend_name() == "postgresql"
    assert admin_url.host in {"localhost", "127.0.0.1", "::1"} and not admin_url.query
    assert admin_url.username == "postgres" and admin_url.database == "postgres"
    from erp_v2.migrate import upgrade_heads

    suffix = uuid4().hex
    database, owner = f"post_bridge_{suffix}", f"post_owner_{suffix}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    owner_engine = None
    created_database = created_owner = False
    try:
        with admin.connect() as connection:
            for role, options in (("app_admin", "LOGIN BYPASSRLS"), ("app_user", "LOGIN"), ("platform_api", "LOGIN")):
                if not connection.scalar(
                    sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}
                ):
                    connection.exec_driver_sql(f'CREATE ROLE "{role}" {options}')
            connection.exec_driver_sql(f'CREATE ROLE "{owner}" LOGIN BYPASSRLS CREATEROLE')
            created_owner = True
            connection.exec_driver_sql(f'GRANT app_admin, app_user, platform_api TO "{owner}" WITH ADMIN OPTION')
            connection.exec_driver_sql(f'CREATE DATABASE "{database}" OWNER "{owner}"')
            created_database = True
        schema_admin = sa.create_engine(admin_url.set(database=database), hide_parameters=True)
        try:
            with schema_admin.begin() as connection:
                connection.exec_driver_sql("ALTER SCHEMA public OWNER TO app_admin")
                connection.exec_driver_sql(f'GRANT USAGE, CREATE ON SCHEMA public TO "{owner}"')
        finally:
            schema_admin.dispose()
        owner_url = admin_url.set(username=owner, database=database)
        upgrade_heads(owner_url.render_as_string(hide_password=False))
        owner_engine = sa.create_engine(owner_url, hide_parameters=True, pool_size=8)
        yield owner_engine
    finally:
        if owner_engine is not None:
            owner_engine.dispose()
        with admin.connect() as connection:
            if created_database:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            if created_owner:
                connection.exec_driver_sql(f'DROP ROLE "{owner}"')
        admin.dispose()


def seed(engine) -> SimpleNamespace:
    tenant_id = uuid4()
    scope = TenantScope(tenant_id)
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, slug=f"t{tenant_id.hex[:24]}", name="Bridge"))
        db.flush()
        # Direct owner rows: accounting a1's create_* helpers import the kernel's reference
        # runtime (dotmac_kernel.db), which a composed ERP process must not construct.
        categories = {
            code: AccountCategory(tenant_id=tenant_id, code=code, name=code, account_class=klass)
            for code, klass in (("EXP", AccountClass.EXPENSE), ("LIA", AccountClass.LIABILITY))
        }
        db.add_all(categories.values())
        db.flush()
        rows = {}
        for code, category, balance in (
            ("5000", "EXP", NormalBalance.DEBIT),
            ("2000", "LIA", NormalBalance.CREDIT),
            ("2040", "LIA", NormalBalance.CREDIT),
        ):
            rows[code] = Account(
                tenant_id=tenant_id,
                category_id=categories[category].id,
                code=code,
                name=f"Account {code}",
                kind=AccountKind.POSTING,
                normal_balance=balance,
            )
        db.add_all(rows.values())
        year = FiscalYear(
            tenant_id=tenant_id, code="FY26", name="FY 2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
        )
        db.add(year)
        db.flush()
        period = FiscalPeriod(
            tenant_id=tenant_id,
            fiscal_year_id=year.id,
            period_number=1,
            name="Jan",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        db.add(period)
        db.flush()
        accounts = {code: row.id for code, row in rows.items()}
        db.commit()
        return SimpleNamespace(tenant_id=tenant_id, scope=scope, accounts=accounts, period_id=period.id)


def entry(ref, debit="0", credit="0"):
    return SimpleNamespace(
        account_ref=str(ref), debit=Decimal(debit), credit=Decimal(credit), description="d", dimension_refs=()
    )


def consequence(env, *, amount="100", document="doc-1", owner="payables", kind="supplier_invoice", credit_ref=None):
    return SimpleNamespace(
        source=SimpleNamespace(owner=owner, document_kind=kind, document_id=document, version="1", fingerprint=FP),
        posting_date=date(2026, 1, 10),
        description="Bridge test",
        currency_code="NGN",
        exchange_rate=Decimal(1),
        entries=(
            entry(env.accounts["5000"], debit=amount),
            entry(credit_ref or env.accounts["2000"], credit=amount),
        ),
        fingerprint=FP,
    )


def post(engine, env, c, *, commit=True):
    with Session(engine) as db:
        result = bridge.post_consequence(
            db, scope=env.scope, consequence=c, approval_ref="appr-1", actor="p-1", now=NOW
        )
        if commit:
            db.commit()
        return result


def counts(engine, env) -> dict[str, int]:
    with Session(engine) as db:

        def n(model):
            return db.scalar(sa.select(sa.func.count()).select_from(model).where(model.tenant_id == env.tenant_id))

        return {
            "journals": n(JournalEntry),
            "lines": n(JournalLine),
            "ledger": n(PostedLedgerLine),
            "idempotency": n(IdempotencyRecord),
        }


def soft_close(engine, env):
    with Session(engine) as db:
        accounting.soft_close_period(
            db,
            scope=env.scope,
            period_id=env.period_id,
            evidence=PeriodCloseEvidence((CloseCheck("CHECK1", True, "evidence-1", "e" * 64),)),
            actor_ref="test",
            approval_reference="close-1",
            occurred_at=NOW,
        )
        db.commit()


POSTED = {"journals": 1, "lines": 2, "ledger": 2, "idempotency": 2}
EMPTY = {"journals": 0, "lines": 0, "ledger": 0, "idempotency": 0}


def test_post_writes_journal_lines_ledger_and_idempotency_rows(engine):
    env = seed(engine)
    result = post(engine, env, consequence(env))
    assert result.replayed is False and len(result.evidence_fingerprint) == 64
    assert counts(engine, env) == POSTED
    with Session(engine) as db:
        journal = db.get(JournalEntry, result.journal_id)
        assert journal.status.value == "POSTED" and journal.source_owner == "payables"
        assert journal.approval_reference == "appr-1" and journal.posted_by == "p-1"


def test_replay_after_period_soft_closes_returns_same_journal_and_writes_nothing(engine):
    env = seed(engine)
    first = post(engine, env, consequence(env))
    soft_close(engine, env)
    again = post(engine, env, consequence(env))
    assert again.replayed is True and again.journal_id == first.journal_id
    assert again.evidence_fingerprint == first.evidence_fingerprint
    assert counts(engine, env) == POSTED


def test_declared_code_2040_resolves_alongside_uuid_refs(engine):
    env = seed(engine)
    c = consequence(env, owner="payroll", kind="payroll_run", credit_ref="2040")
    result = post(engine, env, c)
    with Session(engine) as db:
        posted = set(
            db.scalars(sa.select(PostedLedgerLine.account_id).where(PostedLedgerLine.journal_id == result.journal_id))
        )
    assert posted == {env.accounts["5000"], env.accounts["2040"]}


def test_undeclared_code_form_refuses_and_writes_nothing(engine):
    env = seed(engine)
    with pytest.raises(PostingRefused) as exc:
        post(engine, env, consequence(env, credit_ref="2040"))  # payables declares UUID refs only
    assert exc.value.code == "account_ref_form_refused" and counts(engine, env) == EMPTY


def refuse_closed_period(engine, env):
    soft_close(engine, env)
    return consequence(env), "period_closed"


def refuse_inactive_account(engine, env):
    with Session(engine) as db:
        db.execute(sa.update(Account).where(Account.id == env.accounts["2000"]).values(is_active=False))
        db.commit()
    return consequence(env), "account_not_postable"


def refuse_unknown_account(engine, env):
    return consequence(env, credit_ref=uuid4()), "account_unknown"


def refuse_unbalanced(engine, env):
    c = consequence(env)
    c.entries = (entry(env.accounts["5000"], debit="100"), entry(env.accounts["2000"], credit="99"))
    return c, "unbalanced"


@pytest.mark.parametrize(
    "scenario", [refuse_closed_period, refuse_inactive_account, refuse_unknown_account, refuse_unbalanced]
)
def test_refusal_leaves_no_bridge_rows_and_the_callers_prior_write_survives(engine, scenario):
    env = seed(engine)
    c, code = scenario(engine, env)
    with Session(engine) as db:
        db.add(
            AccountCategory(
                tenant_id=env.tenant_id, code="CALLER", name="Caller write", account_class=AccountClass.EXPENSE
            )
        )
        db.flush()
        with pytest.raises(PostingRefused) as exc:
            bridge.post_consequence(db, scope=env.scope, consequence=c, approval_ref="appr-1", actor="p-1", now=NOW)
        assert exc.value.code == code
        db.commit()  # the caller catches the refusal and still commits
    assert counts(engine, env) == EMPTY
    with Session(engine) as db:
        assert db.scalar(
            sa.select(AccountCategory.id).where(
                AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "CALLER"
            )
        )


def test_same_source_with_a_different_request_refuses_and_changes_nothing(engine):
    env = seed(engine)
    post(engine, env, consequence(env, amount="100"))
    with pytest.raises(PostingRefused) as exc:
        post(engine, env, consequence(env, amount="250"))
    assert exc.value.code == "accounting_refused"
    assert counts(engine, env) == POSTED


def test_two_concurrent_posts_of_one_source_yield_one_journal(engine):
    env = seed(engine)
    holder = Session(engine)
    winner = bridge.post_consequence(
        holder, scope=env.scope, consequence=consequence(env), approval_ref="appr-1", actor="p-1", now=NOW
    )
    outcome: dict[str, object] = {}

    def loser():
        try:
            outcome["result"] = post(engine, env, consequence(env))
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=loser)
    thread.start()
    time.sleep(1.0)  # the second writer is now waiting on the first's uncommitted source identity
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and "error" not in outcome, outcome
    assert outcome["result"].journal_id == winner.journal_id and outcome["result"].replayed is True
    assert counts(engine, env)["journals"] == 1
