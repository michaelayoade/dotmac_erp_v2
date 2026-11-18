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
from dotmac_kernel.settings_models import DomainSetting, SettingDomain
from dotmac_kernel.settings_resolver import get_spec
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from erp_v2.assembly import assembly
from erp_v2.banking import routes as banking_routes
from erp_v2.web import (
    credit_note_routes,
    payroll_actions,
    payroll_run_routes,
    revenue_approval_routes,
    revenue_routes,
)

DOCUMENT_ID = uuid4()
WRITE_PATHS = (
    "/finance/supplier-invoices",
    f"/finance/supplier-invoices/{DOCUMENT_ID}/submit",
    "/payroll/runs",
    f"/payroll/runs/{DOCUMENT_ID}/calculate",
    "/finance/supplier-credit-notes",
    f"/finance/supplier-credit-notes/{DOCUMENT_ID}/submit",
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
    approve: Mock
    credit_approve: Mock
    credit_apply: Mock
    payroll: dict | None = None

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
        roles: dict[str, Role] = {}
        for role_name in (
            "admin",
            "staff",
            "unprivileged",
            "approver",
            "admin_approver",
            "payroll_approver",
            "payroll_finalizer",
        ):
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
            slugs = {
                "unprivileged": (),
                "approver": ("staff", "finance_approver"),  # portal entry needs staff/admin (F4)
                "admin_approver": ("admin", "staff"),  # admin must gain nothing (W14)
                "payroll_approver": ("staff", "payroll_approver"),
                "payroll_finalizer": ("staff", "payroll_finalizer"),
            }.get(role_name, (role_name,))
            for slug in slugs:
                if slug not in roles:
                    roles[slug] = Role(tenant_id=tenant.id, slug=slug, name=slug)
                    db.add(roles[slug])
                    db.flush()
                db.add(PartyRoleGrant(tenant_id=tenant.id, party_id=party.id, role_id=roles[slug].id))
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
        monkeypatch.setattr(credit_note_routes.services, "create_credit_note_draft", actions[4])
        monkeypatch.setattr(credit_note_routes.services, "submit_credit_note_draft", actions[5])
        credit_approve = Mock(
            return_value=SimpleNamespace(credit_note_id=DOCUMENT_ID, accounting_reference="j", replayed=False)
        )
        monkeypatch.setattr(credit_note_routes.services, "approve_and_post", credit_approve)
        credit_apply = Mock(return_value=SimpleNamespace(credit_note_id=DOCUMENT_ID, release_journal=None))
        monkeypatch.setattr(credit_note_routes.services, "apply_credit", credit_apply)
        approve = Mock(return_value=SimpleNamespace(invoice_id=DOCUMENT_ID, accounting_reference="j", replayed=False))
        monkeypatch.setattr(revenue_approval_routes.services, "approve_and_post", approve)
        payroll_mocks = {
            "batch_calculate": Mock(return_value=SimpleNamespace(calculated=3, skipped_existing=0)),
            "approve": Mock(return_value=SimpleNamespace(run_id=DOCUMENT_ID, replayed=False)),
            "finalize": Mock(return_value=SimpleNamespace(run_id=DOCUMENT_ID, replayed=False)),
            "cancel_run": Mock(return_value=SimpleNamespace(run_id=DOCUMENT_ID, replayed=False)),
        }
        for name, mock in payroll_mocks.items():
            monkeypatch.setattr(payroll_run_routes.services, name, mock)
        monkeypatch.setattr(
            payroll_run_routes, "_page", lambda *a, **k: HTMLResponse(f"page {k.get('step')} {k.get('error')}")
        )
        banking_read = Mock(return_value={"accounts": [], "statements": [], "bindings": [], "receipts": []})
        supplier_read = Mock(return_value=[])
        monkeypatch.setattr(banking_routes.queries, "workspace", banking_read)
        monkeypatch.setattr(revenue_routes.queries, "posting_accounts", supplier_read)
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(
                client,
                tenant,
                parties["admin"],
                tokens,
                actions,
                banking_read,
                supplier_read,
                approve,
                credit_approve,
                credit_apply,
                payroll_mocks,
            )
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
    if selected in (0, 4):
        assert "csrf_token" not in browser.actions[selected].call_args.kwargs["values"]
    if selected in (1, 3, 5):
        key = {1: "invoice_id", 3: "run_id", 5: "credit_note_id"}[selected]
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
        ("/finance/supplier-credit-notes/new", "admin", 200),
        ("/finance/supplier-credit-notes/new", None, 302),
        ("/finance/supplier-credit-notes/new", "staff", 403),
        (f"/finance/supplier-credit-notes/{DOCUMENT_ID}", None, 302),
        (f"/finance/supplier-credit-notes/{DOCUMENT_ID}", "unprivileged", 403),
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


@pytest.mark.parametrize(
    "key,default",
    [("timezone", "UTC"), ("date_format", "%Y-%m-%d"), ("datetime_format", "%Y-%m-%d %H:%M")],
)
def test_assembly_registers_browser_display_settings(key: str, default: str) -> None:
    # Cookie authentication resolves display settings before route dispatch.
    # Specs must come from production assembly imports, not fixture bootstrap.
    assert get_spec(SettingDomain.display, key).default == default


APPROVE_PATH = f"/finance/supplier-invoices/{DOCUMENT_ID}/approve"


def test_only_finance_approver_reaches_approve_and_post(browser: Browser) -> None:
    token = browser.sign_in("approver")
    response = browser.client.post(APPROVE_PATH, data={"csrf_token": token})
    assert response.status_code == 303
    browser.approve.assert_called_once()
    kwargs = browser.approve.call_args.kwargs
    assert kwargs["tenant"].id == browser.tenant.id and kwargs["invoice_id"] == DOCUMENT_ID
    assert str(kwargs["party"].id) != str(browser.party.id)


@pytest.mark.parametrize("role,expected", [(None, 302), ("admin", 403), ("staff", 403), ("admin_approver", 403)])
def test_approve_refused_for_admin_staff_and_anonymous(browser: Browser, role, expected) -> None:
    token = browser.sign_in(role)
    response = browser.client.post(APPROVE_PATH, data={"csrf_token": token})
    assert response.status_code == expected
    browser.approve.assert_not_called()


def test_approve_refuses_invalid_csrf(browser: Browser) -> None:
    browser.sign_in("approver")
    response = browser.client.post(APPROVE_PATH, data={}, headers={"origin": "https://other.example"})
    assert response.status_code == 403
    browser.approve.assert_not_called()


CREDIT_APPROVE_PATH = f"/finance/supplier-credit-notes/{DOCUMENT_ID}/approve"


def test_only_finance_approver_reaches_credit_note_approve_and_post(browser: Browser) -> None:
    token = browser.sign_in("approver")
    response = browser.client.post(
        CREDIT_APPROVE_PATH, data={"csrf_token": token, "evidence_ref": "FIN-EXC-1", "apply_amount": ""}
    )
    assert response.status_code == 303
    browser.credit_approve.assert_called_once()
    kwargs = browser.credit_approve.call_args.kwargs
    assert kwargs["tenant"].id == browser.tenant.id and kwargs["credit_note_id"] == DOCUMENT_ID
    assert kwargs["evidence_ref"] == "FIN-EXC-1" and kwargs["obligation_id"] is None
    browser.approve.assert_not_called()  # the invoice approver is a different route


@pytest.mark.parametrize("role,expected", [(None, 302), ("admin", 403), ("staff", 403), ("admin_approver", 403)])
def test_credit_note_approve_refused_for_admin_staff_and_anonymous(browser: Browser, role, expected) -> None:
    token = browser.sign_in(role)
    response = browser.client.post(CREDIT_APPROVE_PATH, data={"csrf_token": token, "evidence_ref": "x"})
    assert response.status_code == expected
    browser.credit_approve.assert_not_called()


CREDIT_APPLY_PATH = f"/finance/supplier-credit-notes/{DOCUMENT_ID}/apply"


def test_only_finance_approver_reaches_credit_note_apply(browser: Browser) -> None:
    token = browser.sign_in("approver")
    form = {"csrf_token": token, "obligation_id": str(DOCUMENT_ID), "evidence_ref": "FIN-1", "request_id": "r"}
    assert browser.client.post(CREDIT_APPLY_PATH, data=form).status_code == 303
    browser.credit_apply.assert_called_once()
    kwargs = browser.credit_apply.call_args.kwargs
    assert kwargs["credit_note_id"] == DOCUMENT_ID and kwargs["obligation_id"] == DOCUMENT_ID
    browser.credit_approve.assert_not_called()


@pytest.mark.parametrize("role,expected", [(None, 302), ("admin", 403), ("staff", 403), ("admin_approver", 403)])
def test_credit_note_apply_refused_for_admin_staff_and_anonymous(browser: Browser, role, expected) -> None:
    token = browser.sign_in(role)
    response = browser.client.post(CREDIT_APPLY_PATH, data={"csrf_token": token, "obligation_id": str(DOCUMENT_ID)})
    assert response.status_code == expected
    browser.credit_apply.assert_not_called()


def test_credit_note_apply_refuses_invalid_csrf(browser: Browser) -> None:
    browser.sign_in("approver")
    response = browser.client.post(CREDIT_APPLY_PATH, data={}, headers={"origin": "https://other.example"})
    assert response.status_code == 403
    browser.credit_apply.assert_not_called()


def test_credit_note_approve_refuses_invalid_csrf(browser: Browser) -> None:
    browser.sign_in("approver")
    response = browser.client.post(CREDIT_APPROVE_PATH, data={}, headers={"origin": "https://other.example"})
    assert response.status_code == 403
    browser.credit_approve.assert_not_called()


REENTRY_PATH = "/finance/supplier-invoices/legacy-reentry"


def test_legacy_reentry_is_admin_write_only_with_csrf(browser: Browser, monkeypatch) -> None:
    from erp_v2.web import legacy_reentry_routes

    create = Mock(return_value=SimpleNamespace(id=DOCUMENT_ID))
    monkeypatch.setattr(legacy_reentry_routes.service, "create_legacy_reentry_draft", create)
    for role, expected in ((None, 302), ("staff", 403), ("approver", 403)):
        token = browser.sign_in(role)
        assert browser.client.post(REENTRY_PATH, data={"csrf_token": token}).status_code == expected
    create.assert_not_called()
    browser.sign_in("admin")
    assert browser.client.post(REENTRY_PATH, data={}, headers={"origin": "https://other.example"}).status_code == 403
    create.assert_not_called()
    token = browser.sign_in("admin")
    response = browser.client.post(REENTRY_PATH, data={"csrf_token": token, "legacy_invoice_id": "x"})
    assert response.status_code == 303
    create.assert_called_once()
    assert "csrf_token" not in create.call_args.kwargs["values"]


PAYROLL_STEPS = (
    ("batch_calculate", f"/payroll/runs/{DOCUMENT_ID}/batch-calculate", "admin"),
    ("approve", f"/payroll/runs/{DOCUMENT_ID}/approve", "payroll_approver"),
    ("finalize", f"/payroll/runs/{DOCUMENT_ID}/finalize", "payroll_finalizer"),
    # cancel reuses the approve permission (payroll_approver): the withdrawal counterpart of approval
    ("cancel_run", f"/payroll/runs/{DOCUMENT_ID}/cancel", "payroll_approver"),
)


@pytest.mark.parametrize(("command", "path", "holder"), PAYROLL_STEPS)
def test_payroll_step_is_dormant_for_everyone_but_its_named_role(browser: Browser, command, path, holder) -> None:
    others = ("staff", "approver", "payroll_approver", "payroll_finalizer", "unprivileged")
    if command != "batch_calculate":  # batch calculation is the admin draft permission; approve/finalize never are
        others += ("admin", "admin_approver")
    for role in others:
        if role == holder:
            continue
        token = browser.sign_in(role)
        assert browser.client.post(path, data={"csrf_token": token}).status_code == 403, role
        assert browser.client.get(path).status_code == 403, role
    token = browser.sign_in(None)
    assert browser.client.post(path, headers={"x-csrf-token": token}, data={}).status_code == 302
    for mock in browser.payroll.values():
        mock.assert_not_called()


@pytest.mark.parametrize(("command", "path", "holder"), PAYROLL_STEPS)
def test_payroll_step_reaches_only_its_command_with_csrf(browser: Browser, command, path, holder) -> None:
    browser.sign_in(holder)
    bad = browser.client.post(path, data={}, headers={"origin": "https://other.example"})
    assert bad.status_code == 403
    for mock in browser.payroll.values():
        mock.assert_not_called()
    token = browser.sign_in(holder)
    response = browser.client.post(path, data={"csrf_token": token})
    assert response.status_code in (200, 303)
    browser.payroll[command].assert_called_once()
    kwargs = browser.payroll[command].call_args.kwargs
    assert kwargs["tenant"].id == browser.tenant.id and kwargs["run_id"] == DOCUMENT_ID
    if command != "batch_calculate":
        assert str(kwargs["party"].id) != str(browser.party.id)  # not the admin party
    for other, mock in browser.payroll.items():
        if other != command:
            mock.assert_not_called()


def test_a_refused_command_renders_its_reason_without_redirect(browser: Browser) -> None:
    from erp_v2.web.payroll_run_services import RunRefused

    browser.payroll["finalize"].side_effect = RunRefused("sod_approver_is_finalizer", "another person must finalize")
    token = browser.sign_in("payroll_finalizer")
    response = browser.client.post(PAYROLL_STEPS[2][1], data={"csrf_token": token})
    assert response.status_code == 200 and "another person must finalize" in response.text
    browser.payroll["finalize"].side_effect = RunRefused("not_found", "x")
    assert browser.client.post(PAYROLL_STEPS[2][1], data={"csrf_token": token}).status_code == 404
