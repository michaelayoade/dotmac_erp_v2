"""Customer screen (F-F): units (no database), route matrix, validation, source greps.

The real facet, cookie auth, tenant resolution and CSRF middleware run (same harness as the AR producer routes);
SQLite and spy services substitute only the database and the actions.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_customers.contracts import AccountStatus, Conflict
from dotmac_kernel import create_app
from dotmac_kernel.config import settings as kernel_settings
from dotmac_kernel.deps import get_db
from dotmac_kernel.middleware import tenant as tenant_middleware
from dotmac_kernel.models import AuthSession, Base, Party, PartyRoleGrant, PartyType, Role, Tenant, TenantDomain
from dotmac_kernel.security import hash_token, issue_access_token
from dotmac_kernel.settings_models import DomainSetting
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from erp_v2 import access
from erp_v2.access import NEW_ROLES
from erp_v2.assembly import assembly
from erp_v2.customers_web import models, routes, service
from erp_v2.customers_web.service import CustomerRefused, FinanceInput, Outcome

SRC = Path(__file__).resolve().parents[1] / "src" / "erp_v2"
ID = uuid4()
B = routes.BASE
TERMS = {"net_30", "net_14"}


# --- validation --------------------------------------------------------------------------------------------------


def _clean(**kw):
    return service.clean_finance(FinanceInput(**kw), terms=TERMS)


def _code(**kw):
    with pytest.raises(CustomerRefused) as info:
        _clean(**kw)
    return info.value.code


def test_terms_code_must_be_a_configured_key_and_blank_is_allowed():
    assert _clean(payment_terms_code="net_30").payment_terms_code == "net_30"
    assert _clean(payment_terms_code="  ").payment_terms_code is None
    assert _code(payment_terms_code="net_99") == "payment_terms_unknown"
    long_code = "x" * 41
    with pytest.raises(CustomerRefused) as info:
        service.clean_finance(FinanceInput(payment_terms_code=long_code), terms={long_code})
    assert info.value.code == "payment_terms_code_too_long"  # the column is 40, an ar.payment_terms key may be 80


def test_tin_is_trimmed_and_must_be_printable_but_its_format_is_not_checked():
    assert _clean(tax_identification_number="  12345678-0001 ").tax_identification_number == "12345678-0001"
    assert _clean(tax_identification_number="anything goes 1").tax_identification_number == "anything goes 1"
    assert _code(tax_identification_number="12\x00345") == "invalid_input"
    assert _code(tax_identification_number="a\nb") == "invalid_input"
    assert _code(tax_identification_number="9" * 51) == "invalid_input"


@pytest.mark.parametrize("email", ["nobody", "a@b", "a b@c.d", "@c.d", "a@.d"])
def test_email_needs_a_basic_shape(email):
    assert _code(contact_email=email) == "invalid_email"


def test_a_good_email_and_a_multiline_address_pass_and_blank_becomes_none():
    clean = _clean(contact_email=" ap@customer.example ", billing_address="1 Example Road\nLagos", trading_name="")
    assert clean.contact_email == "ap@customer.example"
    assert clean.billing_address == "1 Example Road\nLagos" and clean.trading_name is None
    assert _code(contact_name="x" * 201) == "invalid_input"
    assert _code(billing_address="x" * 501) == "invalid_input"


def test_display_name_is_required_and_bounded():
    with pytest.raises(CustomerRefused) as info:
        service._display_name("  ")
    assert info.value.code == "display_name_required"
    with pytest.raises(CustomerRefused) as info:
        service._display_name("n" * 201)
    assert info.value.code == "invalid_input"
    assert service._display_name("  Acme Ltd ") == "Acme Ltd"


def test_a_new_customer_starts_active_or_prospect_only(monkeypatch):
    for status in (AccountStatus.SUSPENDED, AccountStatus.CLOSED):
        tenant = SimpleNamespace(id=uuid4())
        party = SimpleNamespace(id=uuid4(), tenant_id=tenant.id)
        db = Mock()
        db.begin_nested.return_value.__enter__ = Mock()
        db.begin_nested.return_value.__exit__ = Mock(return_value=False)
        monkeypatch.setattr(service, "_audit", Mock())
        with pytest.raises(CustomerRefused) as info:
            service.create_customer(
                db,
                tenant=tenant,
                party=party,
                request_id=uuid4(),
                display_name="X",
                finance=FinanceInput(),
                status=status,
            )
        assert info.value.code == "invalid_input"


def test_an_actor_from_another_tenant_is_refused_without_an_audit_row(monkeypatch):
    audit = Mock()
    monkeypatch.setattr(service, "_audit", audit)
    with pytest.raises(CustomerRefused) as info:
        service.change_status(
            Mock(),
            tenant=SimpleNamespace(id=uuid4()),
            party=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
            account_id=uuid4(),
            target=AccountStatus.ACTIVE,
        )
    assert info.value.code == "actor_tenant"
    audit.assert_not_called()


def test_create_refuses_with_a_named_reason_when_customers_imports_the_kernel_reference_runtime(monkeypatch):
    """customers a1/a2 ``create_account`` imports ``dotmac_kernel.db``; a composed ERP process refuses that import."""
    tenant = SimpleNamespace(id=uuid4())
    party = SimpleNamespace(id=uuid4(), tenant_id=tenant.id)
    db = Mock()
    db.begin_nested.return_value.__enter__ = Mock()
    db.begin_nested.return_value.__exit__ = Mock(return_value=False)
    audit = Mock()
    monkeypatch.setattr(service, "_audit", audit)
    monkeypatch.setattr(service, "valid_terms", lambda db_, tenant_id: set())

    def operation_runner(db_, *, operation, **kw):
        operation(db_)

    monkeypatch.setattr(service, "execute_once", operation_runner)
    monkeypatch.setattr(service, "_number", lambda *a, **k: "C-1")
    monkeypatch.setattr(
        service.customers_service,
        "create_account",
        Mock(side_effect=RuntimeError("dotmac_kernel.db refuses to construct the reference runtime: sealed")),
    )
    with pytest.raises(CustomerRefused) as info:
        service.create_customer(
            db, tenant=tenant, party=party, request_id=uuid4(), display_name="X", finance=FinanceInput()
        )
    assert info.value.code == "create_unavailable"
    assert audit.call_args.kwargs["action"] == service.AUDIT_REFUSED
    monkeypatch.setattr(service.customers_service, "create_account", Mock(side_effect=RuntimeError("something else")))
    with pytest.raises(RuntimeError, match="something else"):
        service.create_customer(
            db, tenant=tenant, party=party, request_id=uuid4(), display_name="X", finance=FinanceInput()
        )


# --- rename is behind customers a2 -------------------------------------------------------------------------------


def test_rename_refuses_with_a_named_reason_until_a2_is_pinned(monkeypatch):
    monkeypatch.delattr(service.customers_service, "rename_account", raising=False)
    assert service.rename_available() is False
    with pytest.raises(CustomerRefused) as info:
        service._rename(Mock(), uuid4(), uuid4(), "New Name")
    assert info.value.code == "rename_unavailable"


def test_rename_calls_the_a2_command_when_it_exists(monkeypatch):
    spy = Mock()
    monkeypatch.setattr(service.customers_service, "rename_account", spy, raising=False)
    tenant_id, account_id = uuid4(), uuid4()
    assert service.rename_available() is True
    service._rename(Mock(), tenant_id, account_id, "New Name")
    spy.assert_called_once()
    assert spy.call_args.kwargs["account_id"] == account_id and spy.call_args.kwargs["display_name"] == "New Name"
    assert spy.call_args.kwargs["scope"].tenant_id == tenant_id


# --- status machine (CLOSED terminal) ----------------------------------------------------------------------------


def _status_world(monkeypatch, before):
    tenant = SimpleNamespace(id=uuid4())
    party = SimpleNamespace(id=uuid4(), tenant_id=tenant.id, email="p@x.test")
    db = Mock()
    db.begin_nested.return_value.__enter__ = Mock()
    db.begin_nested.return_value.__exit__ = Mock(return_value=False)
    audit = Mock()
    monkeypatch.setattr(service, "_audit", audit)
    monkeypatch.setattr(service, "_account", lambda db_, tenant_id, account_id: SimpleNamespace(status=before))
    return db, tenant, party, audit


def test_close_needs_confirmation_and_closed_is_terminal(monkeypatch):
    db, tenant, party, audit = _status_world(monkeypatch, AccountStatus.ACTIVE)
    transition = Mock()
    monkeypatch.setattr(service.customers_service, "transition_account", transition)
    with pytest.raises(CustomerRefused) as info:
        service.change_status(db, tenant=tenant, party=party, account_id=uuid4(), target=AccountStatus.CLOSED)
    assert info.value.code == "close_unconfirmed"
    transition.assert_not_called()
    assert audit.call_args.kwargs["action"] == service.AUDIT_REFUSED

    db, tenant, party, audit = _status_world(monkeypatch, AccountStatus.CLOSED)
    transition.side_effect = Conflict("terminal customer lifecycle transition")
    with pytest.raises(CustomerRefused) as info:
        service.change_status(db, tenant=tenant, party=party, account_id=uuid4(), target=AccountStatus.ACTIVE)
    assert info.value.code == "customer_closed"


def test_a_status_change_audits_from_and_to_only_and_a_no_op_writes_nothing(monkeypatch):
    db, tenant, party, audit = _status_world(monkeypatch, AccountStatus.ACTIVE)
    monkeypatch.setattr(service.customers_service, "transition_account", Mock())
    service.change_status(db, tenant=tenant, party=party, account_id=uuid4(), target=AccountStatus.SUSPENDED)
    assert audit.call_args.kwargs["details"] == {"from": "ACTIVE", "to": "SUSPENDED"}
    audit.reset_mock()
    service.change_status(db, tenant=tenant, party=party, account_id=uuid4(), target=AccountStatus.ACTIVE)
    audit.assert_not_called()


# --- source greps ------------------------------------------------------------------------------------------------

CUSTOMER_MODELS = {"CustomerAccount", "CustomerProfile", "CustomerPartyReference"}
CUSTOMER_WRITE_COMMANDS = {
    "create_account",
    "set_profile",
    "link_party_reference",
    "transition_account",
    "rename_account",
}


def _py_files():
    for path in SRC.rglob("*.py"):
        parts = path.relative_to(SRC).parts
        if parts[0] in {"migrations", "importer"}:  # the importer writes as the migration role (its own plan)
            continue
        yield path


def test_no_erp_code_writes_mod_customers_except_through_customers_service_commands():
    offenders = []
    for path in _py_files():
        source = path.read_text()
        tree = ast.parse(source)
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name in CUSTOMER_MODELS:  # building a row of the owning module
                    offenders.append(f"{path}: {name}(...)")
                arg_names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
                if name in {"update", "delete", "insert", "add", "add_all", "merge", "bulk_save_objects"} and (
                    arg_names & CUSTOMER_MODELS
                ):
                    offenders.append(f"{path}: {name}({sorted(arg_names & CUSTOMER_MODELS)})")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                text = node.value.lower()
                if ("mod_customers" in text or "customer_accounts" in text or "customer_profiles" in text) and any(
                    verb in text for verb in ("update ", "insert ", "delete ")
                ):
                    offenders.append(f"{path}: SQL text")
    assert not offenders, offenders


def test_the_only_customer_commands_called_are_the_owning_modules_service_functions():
    source = (SRC / "customers_web" / "service.py").read_text()
    called = {
        n.func.attr
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "customers_service"
    }
    assert called <= CUSTOMER_WRITE_COMMANDS | {"get_account"}, called
    assert {"create_account", "transition_account", "set_profile"} <= called


def test_the_screen_never_deletes_and_never_touches_ar_sources():
    for path in (SRC / "customers_web").glob("*.py"):
        tree = ast.parse(path.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        assert "delete" not in names, path


def test_no_json_or_free_metadata_columns_on_the_finance_table():
    assert not any("JSON" in repr(c.type).upper() for c in models.CustomerFinance.__table__.columns)
    assert set(models.EDITABLE_COLUMNS) <= {c.name for c in models.CustomerFinance.__table__.columns}
    immutable = {"id", "tenant_id", "customer_account_id", "created_at"}
    assert not immutable & set(models.EDITABLE_COLUMNS)


# --- permissions --------------------------------------------------------------------------------------------------


def test_the_manage_permission_is_finance_preparer_only_and_the_profile_is_v7():
    declared = {spec.code: set(spec.default_roles) for spec in access.PERMISSION_SPECS}
    assert declared[routes.MANAGE] == {"finance_preparer"}
    assert routes.MANAGE == "erp.finance.customers.manage"
    assert access.PROFILE_VERSION == 7
    grants = {(g.role_code, g.permission_code) for p in access.ROLE_GRANT_PROFILES for g in p.grants}
    assert ("finance_preparer", routes.MANAGE) in grants
    assert not {r for r, c in grants if c == routes.MANAGE} - {"finance_preparer"}
    names = list(access._NEW_PERMISSIONS)
    assert names.index(routes.MANAGE) == names.index("erp.finance.receivables.post") + 1  # one hunk after B-16


# --- route matrix ------------------------------------------------------------------------------------------------

PRINCIPALS = {
    "staff": ("staff",),
    "admin": ("admin",),
    "preparer": ("staff", "finance_preparer"),
    "approver": ("staff", "finance_approver"),
    "preparer_nostaff": ("finance_preparer",),
}
FORM = {
    "request_id": str(uuid4()),
    "display_name": "Acme Ltd",
    "account_number": "",
    "status": "ACTIVE",
    "target": "SUSPENDED",
    "trading_name": "Acme",
}
# action -> (path, principals admitted, spied service function)
ACTIONS = {
    "customer_create": (f"{B}/new", {"preparer"}, "create_customer"),
    "customer_save": (f"{B}/{ID}/edit", {"preparer"}, "update_customer"),
    "customer_status": (f"{B}/{ID}/status", {"preparer"}, "change_status"),
}
SPIED = sorted({spy for _, _, spy in ACTIONS.values()})
GETS = {
    "customers_list": (B, {"staff", "admin", "preparer", "approver"}),
    "customer_detail": (f"{B}/{ID}", {"staff", "admin", "preparer", "approver"}),
    "customer_new": (f"{B}/new", {"preparer"}),
    "customer_edit": (f"{B}/{ID}/edit", {"preparer"}),
}


def test_the_route_permission_map_matches_the_spec():
    assert routes.ROUTE_PERMISSIONS == {
        "customers_list": "erp.web.access",
        "customer_new": routes.MANAGE,
        "customer_create": routes.MANAGE,
        "customer_detail": "erp.web.access",
        "customer_edit": routes.MANAGE,
        "customer_save": routes.MANAGE,
        "customer_status": routes.MANAGE,
    }
    served = {r.name for r in routes.router.routes}
    assert served == set(routes.ROUTE_PERMISSIONS)


class Browser:
    def __init__(self, client, tenant, parties, tokens, spies):
        self.client, self.tenant, self.parties, self.tokens, self.spies = client, tenant, parties, tokens, spies

    def sign_in(self, who):
        self.client.cookies.clear()
        if who is not None:
            self.client.cookies.set("access_token", self.tokens[who])
        assert self.client.get("/login").status_code == 200
        token = self.client.cookies.get("csrf_token")
        assert token
        return token


@pytest.fixture()
def browser(monkeypatch):
    monkeypatch.setattr(kernel_settings, "csrf_enabled", True)
    monkeypatch.setattr(kernel_settings, "platform_root_domain", "erp.test")
    monkeypatch.setattr(kernel_settings, "trusted_hosts", "acme.erp.test")
    monkeypatch.setattr(kernel_settings, "rate_limit_requests", 10000)
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[m.__table__ for m in (Tenant, TenantDomain, Party, Role, PartyRoleGrant, AuthSession, DomainSetting)],
    )
    with Session(engine, expire_on_commit=False) as db:
        tenant = Tenant(id=uuid4(), slug="acme", name="Acme")
        db.add(tenant)
        db.flush()
        roles = {}
        for slug in ("admin", "staff", *NEW_ROLES):
            roles[slug] = Role(tenant_id=tenant.id, slug=slug, name=slug)
            db.add(roles[slug])
        db.flush()
        parties, tokens = {}, {}
        for name, held in PRINCIPALS.items():
            party = Party(
                id=uuid4(),
                tenant_id=tenant.id,
                party_type=PartyType.person,
                display_name=name,
                email=f"{name}@acme.test",
            )
            db.add(party)
            db.flush()
            for slug in held:
                db.add(PartyRoleGrant(tenant_id=tenant.id, party_id=party.id, role_id=roles[slug].id))
            token, expires_at = issue_access_token(party.id, tenant.id)
            db.add(
                AuthSession(tenant_id=tenant.id, party_id=party.id, token_hash=hash_token(token), expires_at=expires_at)
            )
            parties[name], tokens[name] = party, token
        db.commit()

        @contextmanager
        def resolver_session():
            yield db

        def request_session():
            yield db

        monkeypatch.setattr(tenant_middleware, "resolver_session", resolver_session)
        app = create_app(replace(assembly, web_enabled=True, database_runtime=None, require_database_runtime=False))
        app.dependency_overrides[get_db] = request_session
        spies = {name: Mock(return_value=Outcome(ID)) for name in SPIED}
        for name, spy in spies.items():
            monkeypatch.setattr(routes.service, name, spy)
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tenant, parties, tokens, spies)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture(autouse=True)
def _known_account(monkeypatch):
    account = SimpleNamespace(
        id=ID, account_number="C-1", display_name="Acme Ltd", status=SimpleNamespace(value="ACTIVE")
    )
    monkeypatch.setattr(routes.queries, "account", lambda db, tenant_id, account_id: account)
    monkeypatch.setattr(routes.queries, "finance", lambda db, tenant_id, account_id: None)
    monkeypatch.setattr(routes.queries, "notes", lambda db, tenant_id, account_id: None)
    monkeypatch.setattr(routes.queries, "listing", lambda db, tenant_id, **kw: ([], 0))
    monkeypatch.setattr(routes.queries, "billing_accounts", lambda db, tenant_id, account_id: [])
    monkeypatch.setattr(routes.queries, "open_item_count", lambda db, tenant_id, accounts: 0)
    monkeypatch.setattr(routes.service, "valid_terms", lambda db, tenant_id: {"net_30"})
    monkeypatch.setattr(routes.settings, "series_code", lambda db, tenant_id: "CUST")


def _post(browser, path, token, **data):
    return browser.client.post(path, data={"csrf_token": token, **FORM, **data})


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("who", PRINCIPALS)
def test_each_write_is_admitted_only_for_the_manage_permission_with_the_authenticated_actor(browser, action, who):
    path, allowed, spy_name = ACTIONS[action]
    token = browser.sign_in(who)
    response = _post(browser, path, token)
    if who in allowed:
        assert response.status_code == 303, (action, who, response.status_code)
        spy = browser.spies[spy_name]
        spy.assert_called_once()
        kwargs = spy.call_args.kwargs
        assert kwargs["tenant"].id == browser.tenant.id
        assert kwargs["party"].id == browser.parties[who].id  # the guard's party, never request data
    else:
        assert response.status_code in (302, 403), (action, who, response.status_code)
        for spy in browser.spies.values():
            spy.assert_not_called()


@pytest.mark.parametrize("route", GETS)
@pytest.mark.parametrize("who", PRINCIPALS)
def test_reads_need_portal_access_and_the_form_pages_need_manage(browser, route, who):
    path, allowed = GETS[route]
    browser.sign_in(who)
    response = browser.client.get(path)
    if who in allowed:
        assert response.status_code == 200, (route, who, response.status_code)
    else:
        assert response.status_code in (302, 403), (route, who, response.status_code)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("proof", ["missing", "mismatch", "foreign-origin"])
def test_invalid_csrf_never_reaches_the_service(browser, action, proof):
    path, allowed, _ = ACTIONS[action]
    token = browser.sign_in(sorted(allowed)[0])
    headers = {}
    if proof == "mismatch":
        headers["x-csrf-token"] = token + "x"
    elif proof == "foreign-origin":
        headers.update({"x-csrf-token": token, "origin": "https://other.example"})
    assert browser.client.post(path, headers=headers, data=FORM).status_code == 403
    for spy in browser.spies.values():
        spy.assert_not_called()


def test_a_refusal_is_rendered_with_its_code_and_the_entered_values_survive(browser):
    token = browser.sign_in("preparer")
    browser.spies["create_customer"].side_effect = CustomerRefused("payment_terms_unknown", "not configured")
    response = _post(browser, f"{B}/new", token, trading_name="Kept Trading Name")
    assert response.status_code == 400
    assert "payment_terms_unknown" in response.text and "Kept Trading Name" in response.text


def test_a_missing_request_id_is_a_refusal_not_a_server_error(browser):
    token = browser.sign_in("preparer")
    response = browser.client.post(f"{B}/new", data={"csrf_token": token, "display_name": "X"})
    assert response.status_code == 400 and "invalid_input" in response.text
    browser.spies["create_customer"].assert_not_called()


def test_the_edit_form_says_when_rename_is_unavailable(browser, monkeypatch):
    monkeypatch.setattr(routes.service, "rename_available", lambda: False)
    browser.sign_in("preparer")
    assert "Renaming is not available yet" in browser.client.get(f"{B}/{ID}/edit").text
    monkeypatch.setattr(routes.service, "rename_available", lambda: True)
    assert "Renaming is not available yet" not in browser.client.get(f"{B}/{ID}/edit").text


def test_a_close_post_passes_the_confirmation_flag(browser):
    token = browser.sign_in("preparer")
    _post(browser, f"{B}/{ID}/status", token, target="CLOSED", confirm="close")
    assert browser.spies["change_status"].call_args.kwargs["confirm_close"] is True
    _post(browser, f"{B}/{ID}/status", token, target="CLOSED")
    assert browser.spies["change_status"].call_args.kwargs["confirm_close"] is False
    assert browser.spies["change_status"].call_args.kwargs["target"] is AccountStatus.CLOSED


def test_the_feature_is_in_the_assembly_with_its_audit_actions_and_setting_domain():
    (feature,) = [m for m in assembly.modules if getattr(m, "name", "") == "erp_customers_web"]
    assert set(service.AUDIT_ACTIONS) <= set(feature.audit_actions)
    assert "customers_web" in feature.setting_domains


def test_ids_are_uuids():
    assert isinstance(ID, UUID)


def test_migration_revision_chain_and_head():
    from erp_v2.migrate import EXPECTED_HEADS
    from erp_v2.migrations.versions import e2_0035_customer_finance as migration

    assert migration.revision == "e2_0035_customer_finance"
    assert migration.down_revision == "e2_0033_billing_gl_vat"  # e2_0034 stays reserved for the WHT follow-up
    assert "e2_0035_customer_finance" in EXPECTED_HEADS
    assert "e2_0033_billing_gl_vat" not in EXPECTED_HEADS and "e2_0032_ar_producer" not in EXPECTED_HEADS
