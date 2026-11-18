"""Mounted payee bank routes: permission matrix, CSRF, and actor wiring.

Same harness as test_workflow_route_security: the real facet, cookie auth, tenant resolution and
CSRF middleware run; SQLite and spy services substitute only the database and financial action.
Holders model the real grant shape: new-role holders must already be staff (access_cli refuses
otherwise), so "staff + role" is the realistic principal.
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

from erp_v2.access import NEW_ROLES
from erp_v2.assembly import assembly
from erp_v2.payees import routes as payee_routes

IID = uuid4()
BASE = "/finance/payee-bank-details"
PRINCIPALS = {
    "staff": ("staff",),
    "admin": ("admin",),
    "preparer": ("staff", "finance_preparer"),
    "approver": ("staff", "finance_approver"),
    "treasury": ("staff", "treasury"),
    # REVIEW-B8 R2: finance roles WITHOUT staff/erp.web.access (e.g. a leaver whose staff grant was removed)
    "preparer_nostaff": ("finance_preparer",),
    "approver_nostaff": ("finance_approver",),
    "treasury_nostaff": ("treasury",),
    "payroll": ("staff", "payroll_approver", "payroll_finalizer", "bank_reconciler"),
}
# route -> (method, path, principals that must be admitted, spied service function)
ACTIONS = {
    "propose": ("POST", BASE, {"preparer"}, "propose"),
    "approve": ("POST", f"{BASE}/{IID}/approve", {"approver"}, "approve"),
    "reject": ("POST", f"{BASE}/{IID}/reject", {"approver"}, "reject"),
    "revoke": ("POST", f"{BASE}/{IID}/revoke", {"approver"}, "revoke"),
    "reveal": ("POST", f"{BASE}/{IID}/reveal", {"approver"}, "reveal_account_number"),
}


@dataclass
class Browser:
    client: TestClient
    tenant: Tenant
    parties: dict[str, Party]
    tokens: dict[str, str]
    spies: dict[str, Mock]

    def sign_in(self, who: str | None) -> str:
        self.client.cookies.clear()
        if who is not None:
            self.client.cookies.set("access_token", self.tokens[who])
        assert self.client.get("/login").status_code == 200
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
        spies = {
            name: Mock(return_value=SimpleNamespace(id=IID))
            for name in ("propose", "approve", "reject", "revoke", "reveal_account_number")
        }
        spies["reveal_account_number"].return_value = "0000000000"
        for name, spy in spies.items():
            monkeypatch.setattr(payee_routes.service, name, spy)
        spies["list_instructions"] = Mock(return_value=[])
        monkeypatch.setattr(payee_routes.service, "list_instructions", spies["list_instructions"])
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tenant, parties, tokens, spies)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


def _post(browser, path, token, **data):
    return browser.client.post(path, data={"csrf_token": token, **data})


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("who", PRINCIPALS)
def test_each_action_is_admitted_only_for_its_named_role(browser, action, who):
    _, path, allowed, spy_name = ACTIONS[action]
    token = browser.sign_in(who)
    response = _post(browser, path, token, reason="r", note="n", payee_kind="supplier")
    if who in allowed:
        assert response.status_code in (200, 303)
        browser.spies[spy_name].assert_called_once()
        kwargs = browser.spies[spy_name].call_args.kwargs
        assert kwargs["tenant_id"] == browser.tenant.id
        actor = next(v for k, v in kwargs.items() if k.endswith("party_id"))
        assert actor == browser.parties[who].id
    else:
        assert response.status_code == 403, (action, who)
        for spy in browser.spies.values():
            spy.assert_not_called()


@pytest.mark.parametrize(
    "who,expected",
    [
        ("preparer", 200),
        ("approver", 200),
        ("treasury", 200),
        ("staff", 403),
        ("admin", 403),
        ("payroll", 403),
        (None, 302),
    ],
)
def test_masked_list_needs_the_read_permission_so_staff_and_admin_see_nothing(browser, who, expected):
    browser.sign_in(who)
    response = browser.client.get(BASE)
    assert response.status_code == expected
    assert response.headers.get("cache-control") == "no-store" if expected == 200 else True
    assert browser.spies["list_instructions"].called is (expected == 200)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("proof", ["missing", "mismatch", "foreign-origin"])
def test_invalid_csrf_never_reaches_the_service(browser, action, proof):
    _, path, allowed, _ = ACTIONS[action]
    who = sorted(allowed)[0]
    token = browser.sign_in(who)
    headers = {}
    if proof == "mismatch":
        headers["x-csrf-token"] = token + "x"
    elif proof == "foreign-origin":
        headers.update({"x-csrf-token": token, "origin": "https://other.example"})
    response = browser.client.post(path, headers=headers, data={"reason": "r"})
    assert response.status_code == 403
    for spy in browser.spies.values():
        spy.assert_not_called()


def test_treasury_cannot_approve_even_with_a_valid_session_and_csrf(browser):
    token = browser.sign_in("treasury")
    for action in ("approve", "reject", "revoke", "reveal"):
        assert _post(browser, ACTIONS[action][1], token, reason="r").status_code == 403
    browser.spies["approve"].assert_not_called()


def test_refusal_renders_code_without_calling_further_and_reveal_is_no_store(browser):
    token = browser.sign_in("approver")
    browser.spies["approve"].side_effect = payee_routes.service.PayeeBankError("same_person", "no self approval")
    refused = _post(browser, ACTIONS["approve"][1], token)
    assert refused.status_code == 400 and "same_person" in refused.text
    reveal = _post(browser, ACTIONS["reveal"][1], token, reason="verify")
    assert reveal.status_code == 200 and reveal.headers["cache-control"] == "no-store"
    assert browser.spies["reveal_account_number"].call_args.kwargs["reason"] == "verify"


def test_route_permission_table_matches_the_access_profile():
    from erp_v2 import access

    assert payee_routes.ROUTE_PERMISSIONS == {
        "list": access.PAYEE_BANK_READ,
        "propose": access.PAYEE_BANK_PROPOSE,
        "approve": access.PAYEE_BANK_APPROVE,
        "reject": access.PAYEE_BANK_APPROVE,
        "revoke": access.PAYEE_BANK_REVOKE,
        "reveal": access.PAYEE_BANK_READ_UNMASKED,
    }


@pytest.mark.parametrize("who", ["preparer_nostaff", "approver_nostaff", "treasury_nostaff"])
def test_finance_role_without_portal_access_is_refused_everywhere(browser, who):
    token = browser.sign_in(who)
    assert browser.client.get(BASE).status_code in (302, 403)
    for _, path, _, _ in ACTIONS.values():
        response = _post(browser, path, token, reason="r", note="n", payee_kind="supplier")
        assert response.status_code in (302, 403), (who, path)
    for spy in browser.spies.values():
        spy.assert_not_called()


def test_payee_routes_are_not_facet_entry_routes():
    refs = {r.module for r in assembly.web_facets[0].entry_routes}
    assert "erp_payee_bank" not in refs


def test_the_form_offers_the_statutory_kind_with_a_dest_reference_hint(browser):
    browser.sign_in("preparer")
    html = browser.client.get(BASE).text
    assert '<option value="statutory">' in html and "dest:PENSION_ER" in html and "dest:PAYE" in html


def test_finance_can_propose_a_statutory_instruction_through_the_form(browser):
    token = browser.sign_in("preparer")
    response = _post(
        browser,
        BASE,
        token,
        payee_kind="statutory",
        payee_ref="dest:PENSION_ER",
        bank_code="058",
        bank_name="Example Bank",
        account_number="0123456789",
        account_name="Example Authority",
        evidence_ref="letter-1",
    )
    assert response.status_code == 303
    kwargs = browser.spies["propose"].call_args.kwargs
    assert kwargs["payee_kind"] == "statutory" and kwargs["payee_ref"] == "dest:PENSION_ER"
    assert kwargs["proposer_party_id"] == browser.parties["preparer"].id
    assert kwargs["tenant_id"] == browser.tenant.id
