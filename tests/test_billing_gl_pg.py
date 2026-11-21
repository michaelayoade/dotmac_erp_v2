"""B-15 live billing -> GL mapper on real PostgreSQL: real migrations (e2_0031), the real billing commands (the a11
lane), the real accounting commands through the posting bridge. The mapper runs as the RUNTIME role (``app_user``,
W4), so the column-limited grants are exercised, not assumed.

Hosted CI only (ERP_TEST_POSTGRES_URL), like the other hosted PG tests.
"""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from billing_gl_support import (
    allocate,
    at,
    deallocate,
    new_account,
    new_invoice,
    positions,
    receipt,
    refund,
    reverse,
)
from billing_gl_support import receipt as make_receipt
from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, PostedLedgerLine
from dotmac_billing.contracts import SettlementFundingLane
from dotmac_billing.models import BillingAccount
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Tenant
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dotmac_kernel.settings_models import SettingValueType
from dotmac_kernel.settings_resolver import SettingChangeContext, SettingSpec, register_specs, upsert_by_key
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_journals_pg import seed
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.billing_gl import cli, settings
from erp_v2.billing_gl.feature import billing_gl_feature
from erp_v2.billing_gl.models import FactPosting, FactWindow
from erp_v2.journals import service as journals
from erp_v2.journals.routes import journals_web_feature
from erp_v2.journals.service import JournalRefused

RUN_AT = datetime(2026, 1, 20, 12, tzinfo=UTC)
D = Decimal


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


@pytest.fixture(autouse=True, scope="module")
def _registries():
    manifests = [journals_web_feature, billing_gl_feature, FeatureManifest(name="kernel_stub")]
    install_audit_actions(AuditActionRegistry.from_manifests(manifests))
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))
    # The ar.* accounts are registered by B-14; register identical stand-ins only when B-14 is absent.
    from dotmac_kernel.settings_resolver import get_spec

    for key in (settings.PREPAYMENT_KEY, settings.PREPAID_FUNDING_KEY):
        try:
            get_spec(settings.AR_DOMAIN, key)
        except KeyError:
            register_specs(
                [
                    SettingSpec(
                        domain=settings.AR_DOMAIN,
                        key=key,
                        value_type=SettingValueType.string,
                        default=None,
                        label=key,
                        inherits=False,
                    )
                ]
            )


def world(engine, *, floor="2025-12-01", **config):
    env = seed(engine, currency="NGN")
    with Session(engine) as db:
        cats = {
            c: AccountCategory(tenant_id=env.tenant_id, code=c, name=c, account_class=k)
            for c, k in (("LIA2", AccountClass.LIABILITY), ("AST2", AccountClass.ASSET))
        }
        db.add_all(cats.values())
        db.flush()
        for code, name, cat, normal in (
            ("2100", "Customer prepayments", "LIA2", NormalBalance.CREDIT),
            ("2150", "Prepaid funding", "LIA2", NormalBalance.CREDIT),
            ("1010", "Bank cash", "AST2", NormalBalance.DEBIT),
            ("1440", "Output VAT", "LIA2", NormalBalance.CREDIT),
            ("1455", "Deferred output VAT", "LIA2", NormalBalance.CREDIT),
        ):
            row = Account(
                tenant_id=env.tenant_id,
                category_id=cats[cat].id,
                code=code,
                name=name,
                kind=AccountKind.POSTING,
                normal_balance=normal,
            )
            db.add(row)
            db.flush()
            env.accounts[code] = row.id
        db.commit()
    configure(
        engine,
        env,
        **{
            "history_floor": floor,
            "enabled": True,
            "cash_account": {"integrator": str(env.accounts["1010"]), "opening_refund": str(env.accounts["1010"])},
            "tax_accounts": {"standard": {"current": str(env.accounts["1440"]), "deferral": str(env.accounts["1455"])}},
            "ar": {settings.PREPAYMENT_KEY: "Customer prepayments"},
            **config,
        },
    )
    return env


def configure(engine, env, **values):
    ctx = SettingChangeContext(reason="test")
    with Session(engine) as db:
        for key, value in values.items():
            if key == "ar":
                for ar_key, ar_value in value.items():
                    upsert_by_key(db, settings.AR_DOMAIN, ar_key, ar_value, tenant_id=env.tenant_id, changed_by=ctx)
            else:
                upsert_by_key(db, settings.DOMAIN, key, value, tenant_id=env.tenant_id, changed_by=ctx)
        db.commit()


@contextmanager
def runtime_session(engine, env):
    """A session as the RUNTIME role with the tenant set, exactly how the scheduled mapper connects.

    Pinned to ONE connection: ``SET ROLE`` is session state, so a pool must never hand the role to another user.
    The mapper's advisory lock takes its own connection from the engine, as in production.
    """
    with engine.connect() as conn, Session(bind=conn) as db:
        db.execute(sa.text("SET ROLE app_user"))
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(env.tenant_id)})
        try:
            yield db
        finally:
            db.rollback()
            conn.execute(sa.text("RESET ROLE"))
            conn.commit()


def run(engine, env, *, now=RUN_AT, apply=True, enable_once=False):
    with runtime_session(engine, env) as db:
        return cli.run_once(db, env.tenant_id, apply=apply, enable_once=enable_once, now=now)


def seed_billing(engine, env, fn):
    """Run billing commands as the owner on a billing session and commit."""
    with Session(engine) as db:
        result = fn(db, TenantScope(env.tenant_id))
        ident = result.id  # read before commit expires the instance; later steps need only the id
        db.commit()
        return SimpleNamespace(id=ident)


def with_lines(engine, env, invoice, *, pre_tax="100.00", tax="20.00", treatment="standard"):
    """B-16 normally writes erp_ar_line_accounts at issue; here the (stub) table is filled the same way."""
    with Session(engine) as db:
        db.execute(
            sa.text(
                "INSERT INTO erp_ar_line_accounts (id, tenant_id, billing_document_id, line_number, "
                "revenue_account_id, pre_tax, tax, treatment_code) VALUES (:i, :t, :d, 1, :r, :p, :x, :c)"
            ),
            {
                "i": uuid4(),
                "t": env.tenant_id,
                "d": invoice.id,
                "r": env.accounts["4000"],
                "p": pre_tax,
                "x": tax,
                "c": treatment,
            },
        )
        db.commit()
    return invoice


def gl(engine, env) -> dict[str, Decimal]:
    with Session(engine) as db:
        rows = db.execute(
            sa.select(PostedLedgerLine.account_code, sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit))
            .where(PostedLedgerLine.tenant_id == env.tenant_id)
            .group_by(PostedLedgerLine.account_code)
        ).all()
    return {code: D(total) for code, total in rows}


def rows(engine, env, **where):
    with Session(engine) as db:
        query = sa.select(FactPosting).where(FactPosting.tenant_id == env.tenant_id)
        for key, value in where.items():
            query = query.where(getattr(FactPosting, key) == value)
        return list(db.scalars(query.order_by(FactPosting.occurred_at)))


def dispositions(engine, env) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows(engine, env):
        out[row.disposition] = out.get(row.disposition, 0) + 1
    return out


def tie(engine, env, accounts):
    with Session(engine) as db:
        pos = positions(db, env.tenant_id, accounts)
    balances = gl(engine, env)
    assert balances.get("1400", D(0)) == pos["receivable"]
    assert -balances.get("2100", D(0)) == pos["available_credit"]
    return pos, balances


# ------------------------------------------------------------------- the T2 lane


def test_receipt_allocation_refund_reversal_tie_exactly_at_every_fact(engine):
    env = world(engine)
    with Session(engine) as db:
        account = new_account(db, TenantScope(env.tenant_id))
        db.commit()
        account_id = account.id
    accounts = [account_id]

    def acct(db):
        from dotmac_billing.models import BillingAccount

        return db.get(BillingAccount, account_id)

    invoice = with_lines(
        engine, env, seed_billing(engine, env, lambda db, sc: new_invoice(db, sc, acct(db), when=at(0)))
    )
    report = run(engine, env)
    assert report["posted"] == 1 and report["windows_closed"] >= 1, report["blocked_by_reason"]
    pos, _ = tie(engine, env, accounts)
    assert pos["receivable"] == D("120.00")

    settlement = seed_billing(engine, env, lambda db, sc: receipt(db, sc, acct(db), "500.00", when=at(1)))
    run(engine, env)
    pos, bal = tie(engine, env, accounts)
    assert pos["available_credit"] == D("500.00") and bal["1010"] == D("500.00")

    alloc = seed_billing(engine, env, lambda db, sc: allocate(db, sc, settlement, invoice, "50.00", when=at(2)))
    run(engine, env)
    pos, bal = tie(engine, env, accounts)
    assert (pos["receivable"], pos["available_credit"]) == (D("70.00"), D("450.00"))

    seed_billing(engine, env, lambda db, sc: deallocate(db, sc, alloc, "20.00", when=at(3)))
    run(engine, env)
    pos, bal = tie(engine, env, accounts)
    assert (pos["receivable"], pos["available_credit"]) == (D("90.00"), D("470.00"))

    refunded = seed_billing(engine, env, lambda db, sc: refund(db, sc, settlement, "100.00", when=at(4)))
    run(engine, env)
    pos, bal = tie(engine, env, accounts)
    assert pos["available_credit"] == D("370.00") and bal["1010"] == D("400.00")

    seed_billing(engine, env, lambda db, sc: reverse(db, sc, refunded, when=at(5)))
    run(engine, env)
    pos, bal = tie(engine, env, accounts)
    assert pos["available_credit"] == D("470.00") and bal["1010"] == D("500.00")
    assert dispositions(engine, env) == {"POSTED": 6}
    # Every journal is the bridge's: owner billing, one per fact, approval reference carries the mapping digest.
    with Session(engine) as db:
        journals_n = db.scalar(
            sa.select(sa.func.count())
            .select_from(JournalEntry)
            .where(JournalEntry.tenant_id == env.tenant_id, JournalEntry.source_owner == "billing")
        )
    assert journals_n == 6


def test_replay_redelivery_and_dry_run_are_no_ops(engine):
    env = world(engine)
    with Session(engine) as db:
        account = new_account(db, TenantScope(env.tenant_id))
        db.commit()
        account_id = account.id
    seed_billing(
        engine,
        env,
        lambda db, sc: receipt(db, sc, db.get(type(account), account_id), "40.00", when=at(1)),
    )
    first = run(engine, env, apply=False)  # dry run: reports, writes nothing
    assert first["posted"] == 1 and rows(engine, env) == [] and gl(engine, env) == {}
    applied = run(engine, env)
    assert applied["posted"] == 1
    snapshot = (gl(engine, env), len(rows(engine, env)))
    again = run(engine, env)
    assert again["posted"] == 0 and again["blocked"] == 0
    assert (gl(engine, env), len(rows(engine, env))) == snapshot
    # Forcing a re-page of the same window (redelivery) re-reads every fact and writes nothing new.
    with Session(engine) as db:
        db.execute(
            sa.text("UPDATE erp_billing_fact_windows SET closed_at = NULL WHERE tenant_id = :t"), {"t": env.tenant_id}
        )
        db.commit()
    run(engine, env)
    assert (gl(engine, env), len(rows(engine, env))) == snapshot


def test_late_committed_fact_in_a_closed_window_triggers_repage(engine):
    env = world(engine)
    with Session(engine) as db:
        account = new_account(db, TenantScope(env.tenant_id))
        db.commit()
        account_id = account.id
    model = type(account)
    seed_billing(engine, env, lambda db, sc: receipt(db, sc, db.get(model, account_id), "10.00", when=at(1)))
    first = run(engine, env)
    assert first["windows_closed"] >= 1 and first["windows_mismatched"] == 0
    # A fact whose business time is inside the already-closed day is committed afterwards.
    seed_billing(engine, env, lambda db, sc: receipt(db, sc, db.get(model, account_id), "15.00", when=at(2)))
    second = run(engine, env)
    assert second["windows_mismatched"] == 1 and second["posted"] == 1
    assert gl(engine, env)["1010"] == D("25.00")
    with Session(engine) as db:
        window = db.scalars(
            sa.select(FactWindow).where(FactWindow.tenant_id == env.tenant_id, FactWindow.count > 0)
        ).one()
    assert window.count == 2 and window.closed_at is not None


# ------------------------------------------------- blocking, healing, period close


def test_block_is_per_account_and_a_config_fix_heals_without_staff(engine):
    env = world(engine)
    scope_t = TenantScope(env.tenant_id)
    with Session(engine) as db:
        a, b = new_account(db, scope_t), new_account(db, scope_t)
        db.commit()
        ids, model = (a.id, b.id), type(a)
    # A: a prepaid_funding receipt (T6: account unset) then an invoice behind it; B: an ordinary receipt.
    seed_billing(
        engine,
        env,
        lambda db, sc: receipt(
            db, sc, db.get(model, ids[0]), "75.00", when=at(1), lane=SettlementFundingLane.PREPAID_FUNDING
        ),
    )
    with_lines(
        engine, env, seed_billing(engine, env, lambda db, sc: new_invoice(db, sc, db.get(model, ids[0]), when=at(2)))
    )
    seed_billing(engine, env, lambda db, sc: receipt(db, sc, db.get(model, ids[1]), "30.00", when=at(3)))
    report = run(engine, env)
    assert dispositions(engine, env) == {"BLOCKED": 1, "WAITING": 1, "POSTED": 1}
    assert report["blocked_by_reason"] == {"BLOCKED:prepaid_funding_unmapped": 1, "WAITING:waiting_predecessor": 1}
    assert gl(engine, env)["1010"] == D("30.00")  # B kept moving
    with Session(engine) as db:
        assert journals.__name__  # the guard below uses the period functions
    # While a fact is BLOCKED the period cannot soft-close (B15.6).
    period = env.periods[1]
    with Session(engine) as db, pytest.raises(JournalRefused) as refused:
        journals.soft_close_period(
            db,
            tenant_id=env.tenant_id,
            period_id=period,
            actor=env.parties["approver"],
            review_ref="REVIEW-1",
            now=RUN_AT,
        )
    assert refused.value.code == "billing_facts_incomplete"
    # The fix is configuration only: set the prepaid-funding account; the next run heals both.
    configure(engine, env, ar={settings.PREPAID_FUNDING_KEY: str(env.accounts["2150"])})
    healed = run(engine, env)
    closing = datetime(2026, 2, 2, 12, tzinfo=UTC)  # the period can only close after its end plus settle
    run(engine, env, now=closing)
    assert dispositions(engine, env) == {"POSTED": 3} and healed["blocked"] == 0 and healed["waiting"] == 0
    bal = gl(engine, env)
    assert bal["1010"] == D("105.00") and -bal["2150"] == D("75.00")
    with Session(engine) as db:
        status = journals.soft_close_period(
            db,
            tenant_id=env.tenant_id,
            period_id=period,
            actor=env.parties["approver"],
            review_ref="REVIEW-1",
            now=closing,
        )
        db.commit()
    assert status.lower().startswith("soft")


def test_non_functional_currency_blocks_and_nothing_posts(engine):
    env = world(engine)
    with Session(engine) as db:
        usd = new_account(db, TenantScope(env.tenant_id), currency="USD")
        db.commit()
        usd_id, model = usd.id, type(usd)
    with Session(engine) as db:
        sc = TenantScope(env.tenant_id)
        from billing_gl_support import money
        from dotmac_billing.contracts import AcceptSettlementV1
        from dotmac_billing.service import accept_settlement

        accept_settlement(
            db,
            scope=sc,
            command=AcceptSettlementV1(
                scope=sc,
                billing_account_id=usd_id,
                source_system="integrator",
                source_settlement_key=f"s-{uuid4().hex}",
                source_version="1",
                amount=money("9.00", "USD"),
                occurred_at=at(1),
                observed_at=at(1),
                confirmation_evidence="provider_settled",
                funding_lane=SettlementFundingLane.AVAILABLE_CREDIT,
            ),
            accepted_confirmation_evidence=frozenset({"provider_settled"}),
        )
        db.commit()
    report = run(engine, env)
    assert report["blocked_by_reason"] == {"BLOCKED:billing_fx_unsupported": 1} and gl(engine, env) == {}
    assert model is not None


def test_closed_period_policy_next_open_period_and_hold(engine):
    env = world(engine)
    scope = TenantScope(env.tenant_id)
    with Session(engine) as db:
        accounting.open_period(db, scope=scope, period_id=env.periods[2], actor_ref="seed", occurred_at=RUN_AT)
        from test_journals_pg import close_period

        db.commit()
    from test_journals_pg import close_period

    close_period(engine, env, 1)  # January is now soft-closed; February is open
    with Session(engine) as db:
        account = new_account(db, scope)
        db.commit()
        account_id, model = account.id, type(account)
    seed_billing(engine, env, lambda db, sc: receipt(db, sc, db.get(model, account_id), "12.00", when=at(1)))
    run(engine, env, now=datetime(2026, 2, 10, 12, tzinfo=UTC))
    with Session(engine) as db:
        line = db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.tenant_id == env.tenant_id)).first()
        journal = db.get(JournalEntry, line.journal_id)
    assert line.posting_date.month == 2  # next open period, never silently January
    assert "original date 2026-01-10" in journal.description

    held = world(engine, closed_period_policy="hold")
    close_period(engine, held, 1)
    with Session(engine) as db:
        other = new_account(db, TenantScope(held.tenant_id))
        db.commit()
        other_id, other_model = other.id, type(other)
    seed_billing(engine, held, lambda db, sc: receipt(db, sc, db.get(other_model, other_id), "12.00", when=at(1)))
    report = run(engine, held, now=datetime(2026, 2, 10, 12, tzinfo=UTC))
    assert report["blocked_by_reason"] == {"BLOCKED:period_closed": 1} and gl(engine, held) == {}


def test_disabled_mapper_is_idle_and_enable_once_dispositions_without_posting(engine):
    env = world(engine, enabled=False)
    with Session(engine) as db:
        account = new_account(db, TenantScope(env.tenant_id))
        db.commit()
        account_id, model = account.id, type(account)
    seed_billing(engine, env, lambda db, sc: make_receipt(db, sc, db.get(model, account_id), "5.00", when=at(1)))
    assert run(engine, env)["posted"] == 0 and rows(engine, env) == []
    once = run(engine, env, enable_once=True)
    assert once["would_post"] == 1 and once["posted"] == 0
    assert rows(engine, env) == [] and gl(engine, env) == {}  # nothing posted; the receipt stays undispositioned
    configure(engine, env, enabled=True)
    assert run(engine, env)["posted"] == 1


# ------------------------------------------------------ triggers, grants, RLS


def _one_posted_row(engine, env):
    with Session(engine) as db:
        account = new_account(db, TenantScope(env.tenant_id))
        db.commit()
        account_id, model = account.id, type(account)
    seed_billing(engine, env, lambda db, sc: receipt(db, sc, db.get(model, account_id), "8.00", when=at(1)))
    run(engine, env)
    return rows(engine, env)[0]


def test_triggers_refuse_digest_change_delete_and_terminal_updates_even_for_the_owner(engine):
    env = world(engine)
    row = _one_posted_row(engine, env)
    assert row.disposition == "POSTED" and row.map_digest and row.journal_id
    statements = (
        ("UPDATE erp_billing_fact_postings SET fact_digest = :d WHERE id = :i", {"d": "f" * 64, "i": row.id}),
        (
            "UPDATE erp_billing_fact_postings SET disposition = 'BLOCKED', journal_id = NULL WHERE id = :i",
            {"i": row.id},
        ),
        ("DELETE FROM erp_billing_fact_postings WHERE id = :i", {"i": row.id}),
        ("DELETE FROM erp_billing_fact_windows WHERE tenant_id = :t", {"t": env.tenant_id}),
        (
            "UPDATE erp_billing_fact_windows SET occurred_to = occurred_to + interval '1 day' WHERE tenant_id = :t",
            {"t": env.tenant_id},
        ),
    )
    for sql, params in statements:
        with Session(engine) as db, pytest.raises(DBAPIError):
            db.execute(sa.text(sql), params)
            db.commit()
    with Session(engine) as db, pytest.raises(DBAPIError) as digest:
        db.execute(
            sa.text("UPDATE erp_billing_fact_postings SET fact_digest = :d WHERE id = :i"),
            {"d": "e" * 64, "i": row.id},
        )
    assert "billing_fact_digest_changed" in str(digest.value)


def test_runtime_role_grants_are_column_limited_and_rls_isolates(engine):
    env = world(engine)
    other = world(engine)
    row = _one_posted_row(engine, env)
    with runtime_session(engine, env) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(FactPosting)) == 1
        for sql in (
            "DELETE FROM erp_billing_fact_postings",
            "UPDATE erp_billing_fact_postings SET fact_digest = repeat('a', 64)",
            "UPDATE erp_billing_fact_postings SET effect_kind = 'x'",
            "DELETE FROM erp_billing_fact_windows",
            "UPDATE erp_billing_fact_windows SET occurred_from = occurred_from",
        ):
            with pytest.raises(DBAPIError, match="permission denied"), db.begin_nested():
                db.execute(sa.text(sql))
            # The savepoint rollback keeps the role and tenant; only a full rollback would reset them.
        # Another tenant's rows are invisible, and a cross-tenant insert is refused by the policy.
        with pytest.raises(DBAPIError, match="row-level security"), db.begin_nested():
            db.execute(
                sa.text(
                    "INSERT INTO erp_billing_fact_postings (id, tenant_id, fact_id, fact_version, effect_kind, "
                    "fact_digest, billing_account_id, occurred_at, disposition, attempts) VALUES "
                    "(gen_random_uuid(), :t, gen_random_uuid(), 1, 'allocation', repeat('a', 64), "
                    "gen_random_uuid(), now(), 'BLOCKED', 0)"
                ),
                {"t": other.tenant_id},
            )
        db.rollback()
        db.execute(sa.text("SET ROLE app_user"))
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(other.tenant_id)})
        assert db.scalar(sa.select(sa.func.count()).select_from(FactPosting)) == 0
    assert row.billing_account_id is not None
    assert isinstance(Tenant, type)


def test_changed_fact_digest_is_an_integrity_alarm_and_downgrade_refuses_with_rows(engine):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from dotmac_billing import AccountingFactCursorV1, AccountingFactIndexEntryV1

    from erp_v2.billing_gl.mapper import BillingFactDigestChanged, Mapper

    env = world(engine)
    row = _one_posted_row(engine, env)
    entry = AccountingFactIndexEntryV1(
        AccountingFactCursorV1(row.occurred_at, row.billing_account_id, 1, row.fact_version, row.fact_id),
        uuid4(),
        row.effect_kind,
        "dotmac-billing",
        f"settlement:{uuid4()}",
        "d" * 64,  # not the digest the row was dispositioned with
    )
    with Session(engine) as db:
        mapper = Mapper(db, env.tenant_id, now=RUN_AT, commit=lambda: None)
        with pytest.raises(BillingFactDigestChanged):
            mapper.process(entry)
        assert mapper.report.integrity_alarms == 1
    migration = importlib.import_module("erp_v2.migrations.versions.e2_0031_billing_fact_postings")
    with engine.connect() as conn:
        with (
            Operations.context(MigrationContext.configure(conn)),
            pytest.raises(RuntimeError, match="refusing to drop"),
        ):
            migration.downgrade()
        conn.rollback()


# ------------------------------------------------ review findings 1-4 (architect, 2026-11-19)


def test_retry_pages_every_blocked_fact_sharing_one_timestamp(engine, monkeypatch):
    """#72 stamps every opening fact with one occurred_at; a slice can hold more than one page of pending facts."""
    from erp_v2.billing_gl import mapper

    monkeypatch.setattr(mapper, "PAGE", 2)
    env = world(engine)
    scope = TenantScope(env.tenant_id)
    with Session(engine) as db:
        ids = [new_account(db, scope).id for _ in range(5)]
        db.commit()
    for account_id in ids:
        seed_billing(
            engine,
            env,
            lambda db, sc, a=account_id: receipt(
                db, sc, db.get(BillingAccount, a), "10.00", when=at(1), lane=SettlementFundingLane.PREPAID_FUNDING
            ),
        )
    assert run(engine, env)["blocked"] == 5
    configure(engine, env, ar={settings.PREPAID_FUNDING_KEY: str(env.accounts["2150"])})
    healed = run(engine, env)
    assert healed["blocked"] == 0 and dispositions(engine, env) == {"POSTED": 5}  # none stranded past page one


def test_a_backdated_fact_is_found_by_the_rolling_rescan_and_staleness_refuses_period_close(engine):
    env = world(engine)
    scope = TenantScope(env.tenant_id)
    with Session(engine) as db:
        account_id = new_account(db, scope).id
        db.commit()
    configure(engine, env, resummarize_days=1, rescan_batch=0)  # no recent recheck and no rolling rescan
    run(engine, env)
    # A fact backdated into an already closed, long-since-unchecked window is invisible while the rescan is off ...
    seed_billing(engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, account_id), "7.00", when=at(30)))
    assert run(engine, env)["posted"] == 0 and gl(engine, env) == {}
    # ... but the period cannot be closed on that basis: days not rescanned within rescan_days count as missing.
    from erp_v2.billing_gl.checks import facts_incomplete

    later = datetime(2026, 2, 2, 12, tzinfo=UTC)  # past the period end plus settle
    with Session(engine) as db:
        assert facts_incomplete(db, env.tenant_id, date(2026, 1, 1), date(2026, 1, 31), now=later) is True
    # The rolling rescan (a batch per run) finds it, so nothing is silently never mapped.
    configure(engine, env, rescan_batch=200)
    found = run(engine, env, now=later)
    assert found["posted"] == 1 and gl(engine, env)["1010"] == D("7.00")
    with Session(engine) as db:
        assert facts_incomplete(db, env.tenant_id, date(2026, 1, 1), date(2026, 1, 31), now=later) is False


def test_days_without_a_window_row_block_period_close_and_the_gate(engine):
    from erp_v2.billing_gl.checks import facts_incomplete, ready

    env = world(engine)
    with Session(engine) as db:
        assert facts_incomplete(db, env.tenant_id, date(2026, 1, 1), date(2026, 1, 31), now=RUN_AT) is True
        report = ready(db, env.tenant_id, now=RUN_AT, settle_seconds=300, seal_at=None, history_floor=date(2025, 12, 1))
    assert report["missing_windows"] > 0 and report["ready"] is False
    run(engine, env)
    mapped = {"standard": {"current": str(env.accounts["1440"]), "deferral": str(env.accounts["1455"])}}
    with Session(engine) as db:
        done = ready(
            db,
            env.tenant_id,
            now=RUN_AT,
            settle_seconds=300,
            seal_at=None,
            history_floor=date(2025, 12, 1),
            max_floor=date(2025, 12, 1),
            tax_accounts=mapped,
        )
        late = ready(
            db,
            env.tenant_id,
            now=RUN_AT,
            settle_seconds=300,
            seal_at=None,
            history_floor=date(2026, 1, 1),
            max_floor=date(2025, 12, 1),
        )
    assert done["missing_windows"] == 0 and done["ready"] is True, done
    assert late["floor_too_late"] is True and late["ready"] is False  # a late floor cannot hide earlier days


def test_runtime_role_cannot_insert_a_terminal_row_that_the_mapper_did_not_write(engine):
    env = world(engine)
    base = (
        "INSERT INTO erp_billing_fact_postings (id, tenant_id, fact_id, fact_version, effect_kind, fact_digest, "
        "billing_account_id, occurred_at, disposition, journal_id, attempts) VALUES "
        "(gen_random_uuid(), :t, gen_random_uuid(), :v, :k, repeat('a', 64), gen_random_uuid(), now(), :d, :j, 1)"
    )
    cases = (
        {"v": 1, "k": "settlement_accepted", "d": "POSTED", "j": uuid4()},  # fake journal
        {"v": 1, "k": "settlement_accepted", "d": "SKIP", "j": None},  # SKIP only for opening kinds
        {"v": 4, "k": "opening_cash_applied", "d": "SKIP", "j": None},  # V4 but not an import/unwind kind
        {"v": 4, "k": "opening_cash_applied", "d": "NO_GL", "j": None},  # NO_GL only for V1
    )
    with runtime_session(engine, env) as db:
        for case in cases:
            with pytest.raises(DBAPIError, match=r"integrity|POSTED|SKIP|NO_GL"), db.begin_nested():
                db.execute(sa.text(base), {"t": env.tenant_id, **case})
        # A pending row (what a BLOCKED fact is) remains insertable.
        db.execute(sa.text(base), {"t": env.tenant_id, "v": 1, "k": "allocation", "d": "BLOCKED", "j": None})


def test_two_runs_cannot_overlap_the_second_exits_without_work(engine):
    env = world(engine)
    scope = TenantScope(env.tenant_id)
    with Session(engine) as db:
        account_id = new_account(db, scope).id
        db.commit()
    seed_billing(engine, env, lambda db, sc: receipt(db, sc, db.get(BillingAccount, account_id), "5.00", when=at(1)))
    import hashlib

    key = int.from_bytes(hashlib.sha256(f"billing_gl:{env.tenant_id}".encode()).digest()[:8], "big", signed=True)
    with engine.connect() as holder:
        assert holder.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        blocked = run(engine, env)
        holder.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    assert blocked["locked"] is True and blocked["posted"] == 0 and gl(engine, env) == {}
    assert run(engine, env)["posted"] == 1  # the lock was released: the next run proceeds


def test_the_period_guard_does_not_apply_to_a_tenant_whose_mapper_was_never_used(engine):
    from erp_v2.billing_gl.checks import facts_incomplete

    env = world(engine, enabled=False)
    with Session(engine) as db:
        assert facts_incomplete(db, env.tenant_id, date(2026, 1, 1), date(2026, 1, 31), now=RUN_AT) is False


# ------------------------------------------------ re-review R1-R3 (architect, 2026-11-20)


def test_period_close_waits_for_the_period_end_plus_settle(engine):
    """R1: days between the settle horizon and the period end have no window yet, so they count as missing."""
    from erp_v2.billing_gl.checks import facts_incomplete

    env = world(engine)
    mid_month = datetime(2026, 1, 25, 12, tzinfo=UTC)
    run(engine, env, now=mid_month)
    with Session(engine) as db:
        assert facts_incomplete(db, env.tenant_id, date(2026, 1, 1), date(2026, 1, 31), now=mid_month) is True
        # Every existing window is fresh and closed; only the not-yet-complete days remain.
        assert db.scalar(sa.select(sa.func.count()).select_from(FactWindow).where(FactWindow.closed_at.is_(None))) == 0


def test_enable_once_rescans_every_stale_window(engine):
    """R2: while the mapper is idle the rolling batch does not run; --enable-once must heal ALL stale windows."""
    env = world(engine, enabled=False)
    run(engine, env, enable_once=True)
    later = datetime(2026, 2, 10, 12, tzinfo=UTC)
    report = run(engine, env, now=later, enable_once=True)
    with Session(engine) as db:
        stale = db.scalar(
            sa.select(sa.func.count())
            .select_from(FactWindow)
            .where(FactWindow.tenant_id == env.tenant_id, FactWindow.rechecked_at < later - timedelta(days=1))
        )
    assert stale == 0 and report["windows_remaining"] == 0


def test_the_lock_connection_is_not_idle_in_transaction_during_a_run(engine, monkeypatch):
    from erp_v2.billing_gl import mapper

    env = world(engine)
    seen = {}
    original = mapper.Mapper._run

    def probe(self):
        with engine.connect() as other:
            seen["states"] = other.execute(
                sa.text(
                    "SELECT state FROM pg_stat_activity WHERE query ILIKE '%pg_try_advisory_lock%' "
                    "AND pid <> pg_backend_pid()"
                )
            ).all()
        return original(self)

    monkeypatch.setattr(mapper.Mapper, "_run", probe)
    run(engine, env)
    assert seen["states"] and all(row[0] == "idle" for row in seen["states"])


def test_a_direct_commit_inside_the_run_stays_tenant_scoped_without_any_manual_rescope(engine, monkeypatch):
    """L3 (B15 re-chain review): the scope is armed by the kernel ``tenant_scope`` listener at the start of EVERY
    transaction, so a direct ``db.commit()`` (not through the mapper's ``self.commit``) followed by a read is still
    scoped. The session below starts with NO tenant set and runs as the RLS runtime role: an unscoped read would see
    zero rows (RLS fails closed)."""
    import erp_v2.billing_gl.mapper as mapper_module

    env = world(engine)
    seen: list[tuple[str | None, int]] = []

    def observe(db):
        tenant = db.execute(sa.text("SELECT current_setting('app.current_tenant', true)")).scalar_one()
        visible = db.execute(sa.text("SELECT count(*) FROM mod_accounting.accounts")).scalar_one()
        seen.append((tenant, visible))

    class Probe:
        def __init__(self, db, tenant_id, *, now, commit, enable_once):
            self.db = db

        def run(self):
            observe(self.db)
            self.db.commit()  # a direct commit: no ``self.commit``, no manual rescope
            observe(self.db)
            self.db.rollback()
            observe(self.db)
            self.db.commit()
            observe(self.db)
            from erp_v2.billing_gl.mapper import Report

            return Report()

    monkeypatch.setattr(mapper_module, "Mapper", Probe)
    with engine.connect() as conn, Session(bind=conn) as db:
        db.execute(sa.text("SET ROLE app_user"))
        db.commit()  # end that transaction: the run starts with no tenant scope at all
        assert db.execute(sa.text("SELECT current_setting('app.current_tenant', true)")).scalar_one() in (None, "")
        assert db.execute(sa.text("SELECT count(*) FROM mod_accounting.accounts")).scalar_one() == 0  # fails closed
        db.rollback()
        try:
            cli.run_once(db, env.tenant_id, apply=True, enable_once=False, now=RUN_AT)
            # after the block the session is usable and unscoped again (nothing leaks onto the connection)
            assert db.execute(sa.text("SELECT current_setting('app.current_tenant', true)")).scalar_one() in (None, "")
        finally:
            db.rollback()
            conn.execute(sa.text("RESET ROLE"))
            conn.commit()
    assert len(seen) == 4
    assert all(tenant == str(env.tenant_id) and visible > 0 for tenant, visible in seen), seen
