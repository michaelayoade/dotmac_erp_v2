"""Mounted cash GL re-point routes: permission matrix and CSRF (same harness shape as the payee routes)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel import create_app
from dotmac_kernel.config import settings
from dotmac_kernel.deps import get_db
from dotmac_kernel.middleware import tenant as tenant_middleware
from dotmac_kernel.models import AuthSession, Base, Party, PartyRoleGrant, PartyType, Role, Tenant, TenantDomain
from dotmac_kernel.security import hash_token, issue_access_token
from dotmac_kernel.settings_models import DomainSetting
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from erp_v2.access import NEW_ROLES
from erp_v2.assembly import assembly
from erp_v2.cash_gl import routes as cash_routes

RID = uuid4()
BASE = "/finance/banking/cash-gl-repoints"
PRINCIPALS = {
    "staff": ("staff",),
    "admin": ("admin",),
    "preparer": ("staff", "finance_preparer"),
    "approver": ("staff", "finance_approver"),
    "treasury": ("staff", "treasury"),
    "reconciler": ("staff", "bank_reconciler"),
    "approver_nostaff": ("finance_approver",),
}
ACTIONS = {
    "propose": (BASE, {"preparer"}, "propose"),
    "approve": (f"{BASE}/{RID}/approve", {"approver"}, "approve"),
    "reject": (f"{BASE}/{RID}/reject", {"approver"}, "reject"),
}


@dataclass
class Browser:
    client: TestClient
    tokens: dict[str, str]
    spies: dict[str, Mock]

    def sign_in(self, who):
        self.client.cookies.clear()
        if who is not None:
            self.client.cookies.set("access_token", self.tokens[who])
        assert self.client.get("/login").status_code == 200
        return self.client.cookies.get("csrf_token")


@pytest.fixture()
def browser(monkeypatch):
    monkeypatch.setattr(settings, "csrf_enabled", True)
    monkeypatch.setattr(settings, "platform_root_domain", "erp.test")
    monkeypatch.setattr(settings, "trusted_hosts", "acme.erp.test")
    monkeypatch.setattr(settings, "rate_limit_requests", 10000)
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
        tokens = {}
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
            tokens[name] = token
        db.commit()

        @contextmanager
        def resolver_session():
            yield db

        def request_session():
            yield db

        monkeypatch.setattr(tenant_middleware, "resolver_session", resolver_session)
        app = create_app(replace(assembly, web_enabled=True, database_runtime=None, require_database_runtime=False))
        app.dependency_overrides[get_db] = request_session
        spies = {name: Mock(return_value=SimpleNamespace(id=RID)) for name in ("propose", "approve", "reject")}
        spies["list_requests"] = Mock(return_value=[])
        for name, spy in spies.items():
            monkeypatch.setattr(cash_routes.service, name, spy)
        monkeypatch.setattr(cash_routes, "_page", lambda *a, **k: cash_routes.HTMLResponse("ok"))
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tokens, spies)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


@pytest.mark.parametrize(
    "who,expected",
    [
        ("preparer", 200),
        ("approver", 200),
        ("staff", 403),
        ("admin", 403),
        ("treasury", 403),
        ("reconciler", 403),
        (None, 302),
    ],
)
def test_list_admits_the_pair_and_nobody_else(browser, who, expected):
    browser.sign_in(who)
    assert browser.client.get(BASE).status_code == expected


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("who", PRINCIPALS)
def test_each_action_is_admitted_only_for_its_named_role(browser, action, who):
    path, allowed, spy_name = ACTIONS[action]
    token = browser.sign_in(who)
    values = {"csrf_token": token, "reason": "r", "note": "n"}
    if action == "propose":
        values.update(bank_account_id="not-a-uuid", evidence_ref="e")  # refused before any DB access
    response = browser.client.post(path, data=values)
    if who in allowed:
        assert response.status_code in (200, 303)
        if action != "propose":
            browser.spies[spy_name].assert_called_once()
    else:
        assert response.status_code == 403, (action, who)
        for name in ("propose", "approve", "reject"):
            browser.spies[name].assert_not_called()


def test_missing_csrf_never_reaches_the_service(browser):
    browser.sign_in("approver")
    assert browser.client.post(ACTIONS["approve"][0], data={"note": "n"}).status_code == 403
    browser.spies["approve"].assert_not_called()
