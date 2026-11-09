"""Mounted workflow security using released kernel cookie auth, roles and CSRF.

SQLite substitutes only the database boundaries. create_app mounts the real ERP
facet and routers; no authentication, permission, tenant-resolution or CSRF
middleware/dependency is replaced. Financial actions and workspace read models
are spies: these tests prove admission, not ledger mutation, Postgres RLS or
production database startup posture. The TestClient deliberately runs without
lifespan, which would require the dedicated Postgres server.
"""

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

from erp_v2.assembly import assembly
from erp_v2.banking import routes as banking_routes
from erp_v2.web import payroll_actions, revenue_routes

DOCUMENT_ID = uuid4()
WRITE_PATHS = (
    "/finance/supplier-invoices",
    f"/finance/supplier-invoices/{DOCUMENT_ID}/submit",
    "/payroll/runs",
    f"/payroll/runs/{DOCUMENT_ID}/calculate",
)


@dataclass
class Browser:
    client: TestClient
    tenant: Tenant
    party: Party
    tokens: dict[str, str]
    actions: tuple[Mock, ...]
    banking_read: Mock
    supplier_read: Mock

    def sign_in(self, role: str | None) -> str:
        self.client.cookies.clear()
        if role is not None:
            self.client.cookies.set("access_token", self.tokens[role])
        # Obtain a signed, session-bound token from the mounted login route.
        response = self.client.get("/login")
        assert response.status_code == 200
        token = self.client.cookies.get("csrf_token")
        assert token
        return token


@pytest.fixture()
def browser(monkeypatch):
    monkeypatch.setattr(settings, "csrf_enabled", True)
    monkeypatch.setattr(settings, "platform_root_domain", "erp.test")
    monkeypatch.setattr(settings, "trusted_hosts", "acme.erp.test")
    monkeypatch.setattr(settings, "rate_limit_requests", 10000)
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            model.__table__ for model in (Tenant, TenantDomain, Party, Role, PartyRoleGrant, AuthSession, DomainSetting)
        ],
    )
    with Session(engine, expire_on_commit=False) as db:
        tenant = Tenant(id=uuid4(), slug="acme", name="Acme")
        db.add(tenant)
        db.flush()
        tokens = {}
        parties = {}
        for role_name in ("admin", "staff", "unprivileged"):
            party = Party(
                id=uuid4(),
                tenant_id=tenant.id,
                party_type=PartyType.person,
                display_name=role_name,
                email=f"{role_name}@acme.test",
            )
            db.add(party)
            db.flush()
            parties[role_name] = party
            if role_name != "unprivileged":
                role = Role(tenant_id=tenant.id, slug=role_name, name=role_name)
                db.add(role)
                db.flush()
                db.add(PartyRoleGrant(tenant_id=tenant.id, party_id=party.id, role_id=role.id))
            token, expires_at = issue_access_token(party.id, tenant.id)
            db.add(
                AuthSession(tenant_id=tenant.id, party_id=party.id, token_hash=hash_token(token), expires_at=expires_at)
            )
            tokens[role_name] = token
        db.commit()

        @contextmanager
        def resolver_session():
            yield db

        def request_session():
            yield db

        # Preserve resolver SQL and the tenant middleware; replace its DB boundary.
        monkeypatch.setattr(tenant_middleware, "resolver_session", resolver_session)
        app = create_app(replace(assembly, web_enabled=True, database_runtime=None, require_database_runtime=False))
        app.dependency_overrides[get_db] = request_session
        actions = tuple(Mock(return_value=SimpleNamespace(id=DOCUMENT_ID)) for _ in WRITE_PATHS)
        monkeypatch.setattr(revenue_routes.services, "create_supplier_draft", actions[0])
        monkeypatch.setattr(revenue_routes.services, "submit_supplier_draft", actions[1])
        monkeypatch.setattr(payroll_actions, "create_draft", actions[2])
        monkeypatch.setattr(payroll_actions, "calculate_employee", actions[3])
        banking_read = Mock(return_value={"accounts": [], "statements": [], "bindings": [], "receipts": []})
        supplier_read = Mock(return_value=[])
        monkeypatch.setattr(banking_routes.queries, "workspace", banking_read)
        monkeypatch.setattr(revenue_routes.queries, "posting_accounts", supplier_read)
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tenant, parties["admin"], tokens, actions, banking_read, supplier_read)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_authorized_workflow_reaches_only_its_financial_action(browser: Browser, path: str) -> None:
    token = browser.sign_in("admin")
    response = browser.client.post(path, data={"csrf_token": token, "run_ref": "SECURITY-PROOF"})
    assert response.status_code == 303
    selected = WRITE_PATHS.index(path)
    for index, action in enumerate(browser.actions):
        if index == selected:
            action.assert_called_once()
            assert action.call_args.kwargs["tenant"].id == browser.tenant.id
            if index != 3:  # calculate uses tenant/run context, not an actor argument.
                assert action.call_args.kwargs["party"].id == browser.party.id
        else:
            action.assert_not_called()
    if selected == 0:
        assert "csrf_token" not in browser.actions[0].call_args.kwargs["values"]
    if selected in (1, 3):
        key = "invoice_id" if selected == 1 else "run_id"
        assert browser.actions[selected].call_args.kwargs[key] == DOCUMENT_ID


@pytest.mark.parametrize("path", WRITE_PATHS)
@pytest.mark.parametrize("role,expected", [(None, 302), ("staff", 403)])
def test_workflow_refuses_anonymous_or_missing_write_permission(browser: Browser, path, role, expected) -> None:
    token = browser.sign_in(role)
    response = browser.client.post(path, headers={"x-csrf-token": token}, data={"run_ref": "SECURITY-PROOF"})
    assert response.status_code == expected
    if role is None:
        assert response.headers["location"].startswith("/login?next=")
    else:
        assert "location" not in response.headers
    for action in browser.actions:
        action.assert_not_called()


@pytest.mark.parametrize("path", WRITE_PATHS)
@pytest.mark.parametrize("proof", ["missing", "mismatch", "forged", "foreign-origin"])
def test_workflow_refuses_invalid_csrf_before_financial_action(browser: Browser, path, proof) -> None:
    token = browser.sign_in("admin")
    headers = {}
    if proof == "mismatch":
        headers["x-csrf-token"] = token + "invalid"
    elif proof == "forged":
        # Matching attacker-controlled cookie/header is insufficient: signature required.
        browser.client.cookies.clear()
        browser.client.cookies.set("access_token", browser.tokens["admin"])
        browser.client.cookies.set("csrf_token", "forged-proof")
        headers["x-csrf-token"] = "forged-proof"
    elif proof == "foreign-origin":
        headers.update({"x-csrf-token": token, "origin": "https://other.example"})
    response = browser.client.post(path, headers=headers, data={"run_ref": "SECURITY-PROOF"})
    assert response.status_code == 403
    for action in browser.actions:
        action.assert_not_called()


@pytest.mark.parametrize(
    "path,role,expected",
    [
        ("/finance/banking", "staff", 200),
        ("/finance/banking", None, 302),
        ("/finance/banking", "unprivileged", 403),
        ("/finance/supplier-invoices/new", "admin", 200),
        ("/finance/supplier-invoices/new", None, 302),
        ("/finance/supplier-invoices/new", "staff", 403),
    ],
)
def test_mounted_read_pages_enforce_their_permission(browser: Browser, path, role, expected) -> None:
    browser.sign_in(role)
    response = browser.client.get(path)
    assert response.status_code == expected
    read = browser.banking_read if path == "/finance/banking" else browser.supplier_read
    if expected == 200:
        read.assert_called_once()
        assert read.call_args.kwargs["tenant_id"] == browser.tenant.id
    else:
        read.assert_not_called()
    for action in browser.actions:
        action.assert_not_called()
