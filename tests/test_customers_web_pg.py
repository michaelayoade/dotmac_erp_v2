# ruff: noqa: S608  (test-only SQL built from constant table names and generated uuids)
"""Customer screen (F-F) against real PostgreSQL (hosted CI; ERP_TEST_POSTGRES_URL).

Real kernel, numbering, customers and billing migrations through e2_0035. The service runs as the NOBYPASSRLS runtime
role wherever it matters, so the W10 grants (SELECT, INSERT, column-limited UPDATE, no DELETE) are proven in use.
"""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_billing import models as bm
from dotmac_customers import service as customers_service
from dotmac_customers.contracts import AccountStatus, CreateCustomerAccount
from dotmac_customers.models import CustomerAccount
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, PartyType, Tenant
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dotmac_kernel.settings_resolver import SettingChangeContext, upsert_by_key
from dotmac_numbering.service import SeriesConfiguration, configure_series
from sqlalchemy.orm import Session

from erp_v2.ar import settings as ar_settings
from erp_v2.ar.routes import receivables_producer_feature
from erp_v2.assembly import assembly
from erp_v2.customers_web import checks, queries, service, terms
from erp_v2.customers_web import settings as cw_settings
from erp_v2.customers_web.models import EDITABLE_COLUMNS, TABLE
from erp_v2.customers_web.routes import customers_web_feature
from erp_v2.customers_web.service import CustomerRefused, FinanceInput

engine = bridge_pg.engine  # disposable-database fixture under its own name

TIN, EMAIL, PHONE, ADDRESS, CONTACT = (
    "TIN-0042-XYZ",
    "ap@customer.example",
    "+000 555 0100",
    "7 Example Road",
    "Pat Lee",
)
FULL = FinanceInput(
    trading_name="Acme Trading",
    tax_identification_number=TIN,
    payment_terms_code="net_30",
    contact_name=CONTACT,
    contact_email=EMAIL,
    contact_phone=PHONE,
    billing_address=ADDRESS,
)


@pytest.fixture(scope="module", autouse=True)
def registries():
    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))
    manifests = [receivables_producer_feature, customers_web_feature, FeatureManifest(name="kernel_stub")]
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


@pytest.fixture(autouse=True)
def _reference_runtime_stub(monkeypatch):
    """customers ``create_account`` lazily imports ``dotmac_kernel.db``, which needs a DSN and which a composed ERP
    process refuses (the service then refuses ``create_unavailable``). Stand in for the fixed command (it should import
    ``dotmac_kernel.transactions``) with the same savepoint helper, so the real command logic runs under test.
    """
    import sys

    from dotmac_kernel.transactions import conflict_savepoint

    monkeypatch.setitem(sys.modules, "dotmac_kernel.db", SimpleNamespace(conflict_savepoint=conflict_savepoint))


def _setting(db, domain, key, value, tenant_id):
    upsert_by_key(
        db, domain, key, value, tenant_id=tenant_id, changed_by=SettingChangeContext(actor_party_id=None, reason="test")
    )


def world(engine, *, series=True, terms_set=True):
    env = bridge_pg.seed(engine)
    with Session(engine) as db:
        party = Party(
            id=uuid4(),
            tenant_id=env.tenant_id,
            party_type=PartyType.person,
            display_name="preparer",
            email=f"prep-{env.tenant_id.hex[:6]}@acme.test",
        )
        db.add(party)
        db.flush()
        env.party_id = party.id
        if terms_set:
            _setting(db, ar_settings.DOMAIN, ar_settings.KEY_TERMS, {"net_30": 30, "net_14": 14}, env.tenant_id)
        if series:
            _setting(db, cw_settings.DOMAIN, cw_settings.KEY_SERIES_ACCOUNT_NUMBER, "CUST", env.tenant_id)
            configure_series(
                db, scope=env.scope, configuration=SeriesConfiguration(series_code="CUST", prefix="C-", min_digits=4)
            )
        db.commit()
    return env


@pytest.fixture()
def runtime(engine):
    """A NOBYPASSRLS runtime-role engine (app_user): the screen runs as this role in production."""
    role = f"cw_rt_{uuid4().hex[:12]}"
    admin = sa.create_engine(
        sa.make_url(os.environ["ERP_TEST_POSTGRES_URL"]), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOBYPASSRLS')
        c.exec_driver_sql(f'GRANT app_user TO "{role}"')
    rt = sa.create_engine(engine.url.set(username=role), hide_parameters=True)
    try:
        yield rt
    finally:
        rt.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


def act(rt, env, fn, **kw):
    """One request-like unit as the runtime role: call, then commit (also after a caught refusal)."""
    with Session(rt, expire_on_commit=False) as db:
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(env.tenant_id)})
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_id)
        try:
            return fn(db, tenant=tenant, party=party, **kw)
        finally:
            db.commit()


def refused(rt, env, fn, **kw):
    with pytest.raises(CustomerRefused) as info:
        act(rt, env, fn, **kw)
    return info.value.code


def create(rt, env, **over):
    kw = {"request_id": uuid4(), "display_name": "Acme Ltd", "finance": FULL, **over}
    return act(rt, env, service.create_customer, **kw)


def as_runtime(rt, env, fn):
    with Session(rt) as db:
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(env.tenant_id)})
        return fn(db)


def finance_row(engine, env, account_id):
    with Session(engine) as db:
        return queries.finance(db, env.tenant_id, account_id)


def audit_rows(engine, env, action=None):
    with Session(engine) as db:
        stmt = sa.select(AuditEvent).where(
            AuditEvent.tenant_id == env.tenant_id, AuditEvent.entity_type == "customer_account"
        )
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        return list(db.scalars(stmt.order_by(AuditEvent.created_at)))


# --- e2_0035 objects ----------------------------------------------------------------------------------------------


def test_table_forces_rls_with_the_exact_w10_grants(engine):
    with engine.connect() as c:
        assert c.scalar(sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": TABLE})
        grants = {
            p: c.scalar(sa.text(f"SELECT has_table_privilege('app_user', '{TABLE}', '{p}')"))
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
        assert grants == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False}
        columns = [
            r[0]
            for r in c.execute(
                sa.text(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{TABLE}'")
            )
        ]
        for column in columns:
            allowed = c.scalar(sa.text(f"SELECT has_column_privilege('app_user', '{TABLE}', '{column}', 'UPDATE')"))
            assert allowed is (column in EDITABLE_COLUMNS), column
    assert {"id", "tenant_id", "customer_account_id", "created_at"}.isdisjoint(EDITABLE_COLUMNS)


def _raw_row(c, tenant_id, account_id=None):
    row_id = uuid4()
    c.execute(
        sa.text(f"INSERT INTO {TABLE} (id, tenant_id, customer_account_id, trading_name) VALUES (:i, :t, :a, 'x')"),
        {"i": row_id, "t": tenant_id, "a": account_id or uuid4()},
    )
    return row_id


def test_triggers_refuse_identity_changes_and_delete_even_for_the_owner(engine):
    env = bridge_pg.seed(engine)
    with engine.begin() as c:
        row = _raw_row(c, env.tenant_id)
    with engine.connect() as c:
        for sql in (
            f"DELETE FROM {TABLE} WHERE id = '{row}'",
            f"UPDATE {TABLE} SET id = gen_random_uuid() WHERE id = '{row}'",
            f"UPDATE {TABLE} SET customer_account_id = gen_random_uuid() WHERE id = '{row}'",
            f"UPDATE {TABLE} SET tenant_id = gen_random_uuid() WHERE id = '{row}'",
            f"UPDATE {TABLE} SET created_at = now() - interval '1 day' WHERE id = '{row}'",
        ):
            with pytest.raises(sa.exc.DBAPIError):
                c.execute(sa.text(sql))
            c.rollback()
    with engine.begin() as c:  # an editable column is fine
        c.execute(sa.text(f"UPDATE {TABLE} SET trading_name = 'y', updated_at = now() WHERE id = '{row}'"))
    with engine.connect() as c:  # one row per customer in a tenant
        with pytest.raises(sa.exc.IntegrityError):
            c.execute(
                sa.text(
                    f"INSERT INTO {TABLE} (id, tenant_id, customer_account_id) "
                    f"SELECT gen_random_uuid(), tenant_id, customer_account_id FROM {TABLE} WHERE id = '{row}'"
                )
            )
        c.rollback()
        for column, size in (("tax_identification_number", 51), ("billing_address", 501)):
            with pytest.raises(sa.exc.DBAPIError):  # the length limits hold at the database
                c.execute(sa.text(f"UPDATE {TABLE} SET {column} = repeat('9', {size}) WHERE id = '{row}'"))
            c.rollback()


def test_rls_isolation_and_the_runtime_role_cannot_delete_or_move_rows(engine, runtime):
    a, b = bridge_pg.seed(engine), bridge_pg.seed(engine)
    with engine.begin() as c:
        row_a = _raw_row(c, a.tenant_id)
        _raw_row(c, b.tenant_id)
    seen = as_runtime(runtime, a, lambda db: {r[0] for r in db.execute(sa.text(f"SELECT tenant_id FROM {TABLE}"))})
    assert seen == {a.tenant_id}
    with Session(runtime) as db:
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a.tenant_id)})
        with pytest.raises(sa.exc.DBAPIError):  # cannot write another tenant's rows
            db.execute(
                sa.text(
                    f"INSERT INTO {TABLE} (id, tenant_id, customer_account_id) "
                    "VALUES (gen_random_uuid(), :t, gen_random_uuid())"
                ),
                {"t": b.tenant_id},
            )
        db.rollback()
    for sql in (
        f"DELETE FROM {TABLE} WHERE id = '{row_a}'",
        f"UPDATE {TABLE} SET customer_account_id = gen_random_uuid() WHERE id = '{row_a}'",
    ):
        with Session(runtime) as db:
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a.tenant_id)})
            with pytest.raises(sa.exc.DBAPIError):
                db.execute(sa.text(sql))
            db.rollback()


def test_downgrade_refuses_while_rows_exist(engine):
    from alembic import command

    from erp_v2.migrate import build_config

    env = bridge_pg.seed(engine)
    with engine.begin() as c:
        _raw_row(c, env.tenant_id)
    cfg = build_config(engine.url.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match="refusing to drop"):
        command.downgrade(cfg, "e2_0033_billing_gl_vat")
    with engine.connect() as c:
        assert "e2_0035_customer_finance" in {
            r[0] for r in c.execute(sa.text("SELECT version_num FROM alembic_version"))
        }


# --- the screen as the runtime role -------------------------------------------------------------------------------


def test_create_edit_suspend_activate_close_as_the_runtime_role(engine, runtime, monkeypatch):
    env = world(engine)
    monkeypatch.delattr(customers_service, "rename_account", raising=False)
    out = create(runtime, env)  # blank number: allocated from the series
    with Session(engine) as db:
        account = db.get(CustomerAccount, out.account_id)
        assert account.account_number.startswith("C-") and account.status == AccountStatus.ACTIVE
        assert account.display_name == "Acme Ltd"
    row = finance_row(engine, env, out.account_id)
    assert (row.trading_name, row.tax_identification_number, row.payment_terms_code) == ("Acme Trading", TIN, "net_30")
    assert (row.contact_name, row.contact_email, row.contact_phone, row.billing_address) == (
        CONTACT,
        EMAIL,
        PHONE,
        ADDRESS,
    )
    assert row.updated_by == env.party_id

    edited = FinanceInput(
        trading_name="Acme Trading",
        tax_identification_number="NEW-TIN-1",
        payment_terms_code="net_14",
        contact_name=CONTACT,
        contact_email=EMAIL,
        contact_phone=PHONE,
        billing_address="9 Other Road",
    )
    act(
        runtime,
        env,
        service.update_customer,
        account_id=out.account_id,
        display_name="Acme Ltd",
        finance=edited,
        notes="Pays late",
    )
    row = finance_row(engine, env, out.account_id)
    assert (row.tax_identification_number, row.payment_terms_code, row.billing_address) == (
        "NEW-TIN-1",
        "net_14",
        "9 Other Road",
    )
    with Session(engine) as db:
        assert queries.notes(db, env.tenant_id, out.account_id) == "Pays late"
        assert terms.code_for(db, env.tenant_id, out.account_id) == "net_14"

    for target, expected in ((AccountStatus.SUSPENDED, "SUSPENDED"), (AccountStatus.ACTIVE, "ACTIVE")):
        act(runtime, env, service.change_status, account_id=out.account_id, target=target)
        with Session(engine) as db:
            assert db.get(CustomerAccount, out.account_id).status.value == expected
    assert (
        refused(runtime, env, service.change_status, account_id=out.account_id, target=AccountStatus.CLOSED)
        == "close_unconfirmed"
    )
    act(runtime, env, service.change_status, account_id=out.account_id, target=AccountStatus.CLOSED, confirm_close=True)
    assert (
        refused(runtime, env, service.change_status, account_id=out.account_id, target=AccountStatus.ACTIVE)
        == "customer_closed"
    )
    # a closed customer's finance fields stay editable (legacy edited inactive customers); no delete exists anywhere
    act(
        runtime,
        env,
        service.update_customer,
        account_id=out.account_id,
        display_name="Acme Ltd",
        finance=edited,
        notes="Closed 1",
    )
    with Session(engine) as db:
        assert db.get(CustomerAccount, out.account_id).status == AccountStatus.CLOSED


def test_create_is_idempotent_and_a_changed_payload_or_a_taken_number_is_refused(engine, runtime):
    env = world(engine)
    request_id = uuid4()
    first = create(runtime, env, request_id=request_id, account_number="manual-1")
    again = create(runtime, env, request_id=request_id, account_number="manual-1")
    assert again.replayed is True and again.account_id == first.account_id
    assert len(audit_rows(engine, env, service.AUDIT_CREATED)) == 1  # a replay writes no second audit row
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(CustomerAccount)
                .where(CustomerAccount.tenant_id == env.tenant_id)
            )
            == 1
        )
        assert db.get(CustomerAccount, first.account_id).account_number == "MANUAL-1"  # customers normalises the case
    assert (
        refused(
            runtime,
            env,
            service.create_customer,
            request_id=request_id,
            display_name="Other Name",
            finance=FULL,
            account_number="manual-1",
        )
        == "idempotency_conflict"
    )
    assert (
        refused(
            runtime,
            env,
            service.create_customer,
            request_id=uuid4(),
            display_name="Dup",
            finance=FULL,
            account_number="MANUAL-1",
        )
        == "account_number_taken"
    )
    with Session(engine) as db:  # a refusal leaves neither an account nor a finance row behind
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(CustomerAccount)
                .where(CustomerAccount.tenant_id == env.tenant_id)
            )
            == 1
        )
        assert db.scalar(sa.text(f"SELECT count(*) FROM {TABLE} WHERE tenant_id = :t"), {"t": env.tenant_id}) == 1


def test_blank_numbers_take_consecutive_series_numbers_and_an_unset_series_refuses(engine, runtime):
    env = world(engine)
    one, two = create(runtime, env), create(runtime, env, display_name="Second")
    with Session(engine) as db:
        numbers = [db.get(CustomerAccount, i.account_id).account_number for i in (one, two)]
    assert numbers[0] != numbers[1] and all(n.startswith("C-") for n in numbers)
    bare = world(engine, series=False)
    assert (
        refused(runtime, bare, service.create_customer, request_id=uuid4(), display_name="X", finance=FULL)
        == "account_number_series_unset"
    )
    explicit = create(runtime, bare, account_number="EXPL-1")  # an explicit number needs no series
    assert explicit.replayed is False


def test_terms_must_be_a_configured_key_at_save(engine, runtime):
    env = world(engine)
    bad = FinanceInput(payment_terms_code="net_99")
    assert (
        refused(runtime, env, service.create_customer, request_id=uuid4(), display_name="X", finance=bad)
        == "payment_terms_unknown"
    )
    out = create(runtime, env)
    assert (
        refused(runtime, env, service.update_customer, account_id=out.account_id, display_name="Acme Ltd", finance=bad)
        == "payment_terms_unknown"
    )
    assert finance_row(engine, env, out.account_id).payment_terms_code == "net_30"  # the refusal saved nothing
    unconfigured = world(engine, terms_set=False)
    assert (
        refused(runtime, unconfigured, service.create_customer, request_id=uuid4(), display_name="X", finance=FULL)
        == "payment_terms_unknown"
    )


def test_a_rename_refuses_whole_until_a2_and_works_through_the_a2_command(engine, runtime, monkeypatch):
    env = world(engine)
    out = create(runtime, env)
    monkeypatch.delattr(customers_service, "rename_account", raising=False)
    changed = FinanceInput(trading_name="Should Not Save")
    assert (
        refused(
            runtime, env, service.update_customer, account_id=out.account_id, display_name="Renamed", finance=changed
        )
        == "rename_unavailable"
    )
    assert finance_row(engine, env, out.account_id).trading_name == "Acme Trading"  # atomic: nothing else saved
    with Session(engine) as db:
        assert db.get(CustomerAccount, out.account_id).display_name == "Acme Ltd"

    seen = {}

    def fake_rename_account(db, *, scope, account_id, display_name):  # the a2 command's contract (Starter, unreleased)
        seen.update(scope=scope, account_id=account_id, display_name=display_name)
        account = customers_service.get_account(db, scope=scope, account_id=account_id)
        account.display_name = display_name  # stands in for the owning module's own write
        db.flush()

    monkeypatch.setattr(customers_service, "rename_account", fake_rename_account, raising=False)
    act(runtime, env, service.update_customer, account_id=out.account_id, display_name="Renamed", finance=changed)
    assert seen["display_name"] == "Renamed" and seen["account_id"] == out.account_id
    with Session(engine) as db:
        assert db.get(CustomerAccount, out.account_id).display_name == "Renamed"
    assert finance_row(engine, env, out.account_id).trading_name == "Should Not Save"


def test_audit_events_carry_field_names_and_no_values(engine, runtime, monkeypatch):
    env = world(engine)
    monkeypatch.delattr(customers_service, "rename_account", raising=False)
    out = create(runtime, env, notes="Secret note text")
    act(
        runtime,
        env,
        service.update_customer,
        account_id=out.account_id,
        display_name="Acme Ltd",
        notes="Another note",
        finance=FinanceInput(
            trading_name="Acme Trading",
            tax_identification_number="CHANGED-TIN",
            payment_terms_code="net_14",
            contact_email="new@customer.example",
        ),
    )
    refused(
        runtime,
        env,
        service.update_customer,
        account_id=out.account_id,
        display_name="Acme Ltd",
        finance=FinanceInput(contact_email="not-an-email"),
    )
    act(runtime, env, service.change_status, account_id=out.account_id, target=AccountStatus.SUSPENDED)
    rows = audit_rows(engine, env)
    assert {r.action for r in rows} == {
        service.AUDIT_CREATED,
        service.AUDIT_UPDATED,
        service.AUDIT_STATUS,
        service.AUDIT_REFUSED,
    }
    blob = " ".join(repr(r.details) + str(r.entity_id) for r in rows)
    for value in (
        TIN,
        "CHANGED-TIN",
        EMAIL,
        "new@customer.example",
        PHONE,
        ADDRESS,
        CONTACT,
        "Acme Trading",
        "Secret note text",
        "Another note",
        "not-an-email",
    ):
        assert value not in blob, value
    updated = next(r for r in rows if r.action == service.AUDIT_UPDATED)
    assert updated.details["fields"] == [
        "notes",
        "tax_identification_number",
        "payment_terms_code",
        "contact_name",
        "contact_email",
        "contact_phone",
        "billing_address",
    ]
    assert next(r for r in rows if r.action == service.AUDIT_REFUSED).details == {
        "code": "invalid_email",
        "attempted": "update",
    }
    assert next(r for r in rows if r.action == service.AUDIT_STATUS).details == {"from": "ACTIVE", "to": "SUSPENDED"}
    assert all(r.actor_party_id == env.party_id for r in rows)


def test_a_missing_finance_row_is_created_on_edit_and_an_unchanged_save_writes_no_audit(engine, runtime):
    env = world(engine)
    with Session(engine) as db:  # a customer created by the owning module alone (an imported one before wave-4)
        account = customers_service.create_account(
            db,
            scope=TenantScope(env.tenant_id),
            command=CreateCustomerAccount("IMPORTED-1", "Imported Ltd", AccountStatus.ACTIVE),
        )
        db.commit()
        account_id = account.id
    assert finance_row(engine, env, account_id) is None
    act(
        runtime,
        env,
        service.update_customer,
        account_id=account_id,
        display_name="Imported Ltd",
        finance=FinanceInput(trading_name="Imported"),
    )
    assert finance_row(engine, env, account_id).trading_name == "Imported"
    before = len(audit_rows(engine, env, service.AUDIT_UPDATED))
    act(
        runtime,
        env,
        service.update_customer,
        account_id=account_id,
        display_name="Imported Ltd",
        finance=FinanceInput(trading_name="Imported"),
    )
    assert len(audit_rows(engine, env, service.AUDIT_UPDATED)) == before


def test_an_unknown_customer_is_refused_not_found_without_an_audit_row(engine, runtime):
    env = world(engine)
    assert (
        refused(runtime, env, service.update_customer, account_id=uuid4(), display_name="X", finance=FinanceInput())
        == "not_found"
    )
    assert refused(runtime, env, service.change_status, account_id=uuid4(), target=AccountStatus.ACTIVE) == "not_found"
    assert audit_rows(engine, env) == []


def test_the_billing_panel_lists_the_structural_link_only(engine, runtime):
    env = world(engine)
    mine, other = create(runtime, env), create(runtime, env, display_name="Other")
    with Session(engine) as db:
        for ref, currency in (
            (str(mine.account_id), "NGN"),
            (str(mine.account_id), "USD"),
            (str(other.account_id), "NGN"),
            ("not-a-customer-id", "NGN"),
        ):
            db.add(
                bm.BillingAccount(tenant_id=env.tenant_id, external_account_ref=ref, currency=currency, minor_units=2)
            )
        db.commit()
    with Session(engine) as db:
        listed = queries.billing_accounts(db, env.tenant_id, mine.account_id)
        assert [(a.external_account_ref, a.currency) for a in listed] == [
            (str(mine.account_id), "NGN"),
            (str(mine.account_id), "USD"),
        ]
        assert queries.open_item_count(db, env.tenant_id, listed) == 0
        assert queries.billing_accounts(db, env.tenant_id, uuid4()) == []


def test_the_terms_hook_uses_the_customers_code_and_a_draft_keeps_its_own(engine, runtime):
    env = world(engine)
    out = create(runtime, env)
    with Session(engine) as db:
        assert terms.code_for(db, env.tenant_id, out.account_id) == "net_30"
        assert terms.code_for(db, env.tenant_id, uuid4()) is None  # B-16 then falls back to the default code
    draft = uuid4()
    with engine.begin() as c:  # a B-16 draft that stored its own terms at creation (finding 3)
        c.execute(
            sa.text(
                "INSERT INTO erp_ar_drafts (id, tenant_id, kind, customer_account_id, currency, document_date, status, "
                "prepared_by, dual_control, request_id, payment_terms_code) VALUES (:i, :t, 'invoice', :c, 'NGN', "
                "'2026-01-12', 'draft', :p, false, :r, 'net_30')"
            ),
            {"i": draft, "t": env.tenant_id, "c": out.account_id, "p": uuid4(), "r": uuid4()},
        )
    act(
        runtime,
        env,
        service.update_customer,
        account_id=out.account_id,
        display_name="Acme Ltd",
        finance=FinanceInput(payment_terms_code="net_14"),
    )
    with Session(engine) as db:
        assert terms.code_for(db, env.tenant_id, out.account_id) == "net_14"
        assert (
            db.scalar(sa.text("SELECT payment_terms_code FROM erp_ar_drafts WHERE id = :i"), {"i": draft}) == "net_30"
        )


def test_the_gate_check_reports_the_series_and_wave_four_completeness(engine, runtime):
    env = world(engine, series=False)
    with Session(engine) as db:
        assert checks.ready(db, env.tenant_id).reasons == (
            f"customers_setup_incomplete:{cw_settings.KEY_SERIES_ACCOUNT_NUMBER}",
        )
        customers_service.create_account(
            db, scope=TenantScope(env.tenant_id), command=CreateCustomerAccount("NOFIN-1", "No finance row")
        )
        db.commit()
    with Session(engine) as db:
        assert checks.ready(db, env.tenant_id).reasons == (
            f"customers_setup_incomplete:{cw_settings.KEY_SERIES_ACCOUNT_NUMBER}",
            "customers_finance_missing:1",
        )
    complete = world(engine)
    create(runtime, complete)
    with Session(engine) as db:
        report = checks.ready(db, complete.tenant_id)
    assert report.ok and report.reasons == ()
