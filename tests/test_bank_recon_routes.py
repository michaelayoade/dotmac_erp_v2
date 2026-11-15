"""Mounted-route security for B7 (bank reconciliation pages).

Same posture as test_journal_routes: create_app mounts the real ERP facet and routers; no
authentication, permission, tenant-resolution or CSRF dependency is replaced. SQLite substitutes only
the database boundary and the service/query layer is spied, so these tests prove admission, CSRF, SoD
surfacing, one-command-per-route and the absence of any bulk or ledger-writing path. Ledger/ledger-row
behaviour is proven on real PostgreSQL in test_bank_recon_pg.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
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

from erp_v2.access import PERMISSION_SPECS
from erp_v2.assembly import assembly
from erp_v2.banking import recon_routes as routes
from erp_v2.banking.recon_service import ReconRefused

ID = uuid4()
BASE = "/finance/banking/reconciliations"
APPROVALS = "/finance/banking/reconciliation-approvals"
PREPARE = [
    ("GET", BASE),
    ("POST", BASE),
    ("GET", f"{BASE}/{ID}"),
    ("POST", f"{BASE}/{ID}/match"),
    ("POST", f"{BASE}/{ID}/unmatch"),
    ("POST", f"{BASE}/{ID}/items"),
    ("POST", f"{BASE}/{ID}/items/{ID}/withdraw"),
    ("POST", f"{BASE}/{ID}/prepare"),
    ("POST", f"{BASE}/{ID}/cancel"),
]
APPROVE = [
    ("GET", APPROVALS),
    ("GET", f"{APPROVALS}/{ID}"),
    ("POST", f"{APPROVALS}/{ID}/approve"),
    ("POST", f"{APPROVALS}/{ID}/return"),
]
GROUPS = {"prepare": ("reconciler", PREPARE), "approve": ("approver", APPROVE)}
SERVICE_NAMES = (
    "open_reconciliation",
    "match",
    "unmatch",
    "add_items",
    "withdraw_item",
    "prepare",
    "cancel",
    "approve",
    "return_to_open",
)
PEOPLE = {
    "admin": ("admin",),
    "staff": ("staff",),
    "reconciler": ("staff", "bank_reconciler"),
    "approver": ("staff", "finance_approver"),
    "treasury": ("staff", "treasury"),
    "preparer_fin": ("staff", "finance_preparer"),
    "reconciler_without_staff": ("bank_reconciler",),
    "approver_without_staff": ("finance_approver",),
}


@dataclass
class Browser:
    client: TestClient
    tenant: Tenant
    parties: dict[str, Party]
    tokens: dict[str, str]
    service: dict[str, Mock]
    queries: dict[str, Mock]

    def sign_in(self, who: str | None) -> str:
        self.client.cookies.clear()
        if who is not None:
            self.client.cookies.set("access_token", self.tokens[who])
        assert self.client.get("/login").status_code == 200
        return self.client.cookies.get("csrf_token")

    def call(self, method: str, path: str, token: str, **data):
        if method == "GET":
            return self.client.get(path)
        return self.client.post(path, data={"csrf_token": token, **data})


def _workspace_data(party_id, status="open"):
    recon = SimpleNamespace(
        id=ID,
        status=status,
        period_end=date(2026, 1, 31),
        currency_code="NGN",
        opened_by_party_id=party_id,
        prepared_by_party_id=None,
        approved_by_party_id=None,
        return_reason=None,
        cancel_reason=None,
        statement_closing_balance=None,
        gl_balance=None,
        items_total=None,
        difference=None,
    )
    return {
        "recon": recon,
        "account": SimpleNamespace(account_code="OP1"),
        "statement": SimpleNamespace(statement_ref="JAN", period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)),
        "evaluation": SimpleNamespace(
            statement_closing=Decimal(1),
            gl_balance=Decimal(1),
            items_total=Decimal(0),
            difference=Decimal(0),
            issues=(),
        ),
        "items": [],
        "item_refs": set(),
        "bank_open": [],
        "gl_open": [],
        "bank_total": 0,
        "gl_total": 0,
        "bank_page": 1,
        "gl_page": 1,
        "page_size": 50,
        "bank_text": {},
        "gl_text": {},
        "matches": [],
        "members": {},
        "suggestions": [],
        "people": {},
        "item_types": (),
        "timing_types": ("deposit_in_transit", "outstanding_payment"),
        "unrecorded_types": ("unrecorded_bank_charge", "unrecorded_bank_receipt"),
    }


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
        for slug in ("admin", "staff", "finance_preparer", "finance_approver", "treasury", "bank_reconciler"):
            roles[slug] = Role(tenant_id=tenant.id, slug=slug, name=slug)
            db.add(roles[slug])
        db.flush()
        parties, tokens = {}, {}
        for name, held in PEOPLE.items():
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
        row = SimpleNamespace(id=ID, status="prepared")
        service = {name: Mock(return_value=row) for name in SERVICE_NAMES}
        service["withdraw_item"].return_value = True
        service["live_reconciliation_account"] = Mock(return_value=uuid4())
        for name, spy in service.items():
            monkeypatch.setattr(routes.svc, name, spy)
        recon = _workspace_data(parties["reconciler"].id)["recon"]
        queries = {
            "reconciliations": Mock(
                return_value=[
                    {
                        "recon": recon,
                        "account": SimpleNamespace(account_code="OP1"),
                    }
                ]
            ),
            "openable_statements": Mock(return_value=[]),
            "statement_account": Mock(return_value=uuid4()),
            "workspace": Mock(return_value=_workspace_data(parties["reconciler"].id)),
        }
        for name, spy in queries.items():
            monkeypatch.setattr(routes.queries, name, spy)
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tenant, parties, tokens, service, queries)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


def _cases():
    for group, (_holder, routes_) in GROUPS.items():
        for method, path in routes_:
            yield pytest.param(group, method, path, id=f"{method} {path.replace(str(ID), '{id}')}")


def _commands(browser: Browser) -> None:
    for name in SERVICE_NAMES:
        browser.service[name].assert_not_called()


@pytest.mark.parametrize("group,method,path", list(_cases()))
def test_anonymous_is_sent_to_login(browser, group, method, path):
    token = browser.sign_in(None)
    response = browser.call(method, path, token)
    assert response.status_code == 302 and response.headers["location"].startswith("/login")
    _commands(browser)


@pytest.mark.parametrize(
    "who", ["admin", "staff", "treasury", "preparer_fin", "reconciler_without_staff", "approver_without_staff"]
)
@pytest.mark.parametrize("group,method,path", list(_cases()))
def test_admin_staff_and_unrelated_roles_have_no_reconciliation_access(browser, who, group, method, path):
    token = browser.sign_in(who)
    response = browser.call(method, path, token, reason="r")
    assert response.status_code == 403
    _commands(browser)


@pytest.mark.parametrize("group,method,path", list(_cases()))
def test_each_route_needs_its_own_role_not_the_neighbouring_one(browser, group, method, path):
    wrong = {"prepare": "approver", "approve": "reconciler"}[group]
    token = browser.sign_in(wrong)
    response = browser.call(method, path, token, reason="r")
    assert response.status_code == 403
    _commands(browser)


@pytest.mark.parametrize("group,method,path", list(_cases()))
def test_the_holder_of_the_role_is_admitted(browser, group, method, path):
    token = browser.sign_in(GROUPS[group][0])
    response = browser.call(method, path, token, reason="r", statement_id=str(uuid4()), match_id=str(uuid4()))
    assert response.status_code in (200, 303), (response.status_code, response.text[:300])


@pytest.mark.parametrize("group,method,path", [p for p in _cases() if "POST" in p.id])
@pytest.mark.parametrize("proof", ["missing", "mismatch", "foreign-origin"])
def test_post_routes_refuse_invalid_csrf_before_any_command(browser, group, method, path, proof):
    token = browser.sign_in(GROUPS[group][0])
    headers = {}
    if proof == "mismatch":
        headers["x-csrf-token"] = token + "invalid"
    elif proof == "foreign-origin":
        headers.update({"x-csrf-token": token, "origin": "https://other.example"})
    response = browser.client.post(path, headers=headers, data={"reason": "r"})
    assert response.status_code == 403
    _commands(browser)


def test_permissions_are_declared_for_the_named_roles_only():
    wanted = {
        "erp.banking.reconciliation.prepare": ("bank_reconciler",),
        "erp.banking.reconciliation.approve": ("finance_approver",),
    }
    specs = {s.code: tuple(s.default_roles) for s in PERMISSION_SPECS}
    for code, roles in wanted.items():
        assert specs[code] == roles
        assert "admin" not in roles and "staff" not in roles


def test_match_passes_the_acting_party_and_the_selected_lines_only(browser):
    token = browser.sign_in("reconciler")
    bank_a, bank_b, gl_a = uuid4(), uuid4(), uuid4()
    response = browser.client.post(
        f"{BASE}/{ID}/match",
        data={"csrf_token": token, "bank_line": [str(bank_a), str(bank_b)], "gl_line": [str(gl_a)]},
    )
    assert response.status_code == 303 and response.headers["location"] == f"{BASE}/{ID}"
    call = browser.service["match"].call_args.kwargs
    assert call["actor"] == browser.parties["reconciler"].id and call["tenant_id"] == browser.tenant.id
    assert call["bank_line_ids"] == [bank_a, bank_b] and call["gl_line_ids"] == [gl_a]
    for name in SERVICE_NAMES:
        assert browser.service[name].called is (name == "match")


def test_bad_line_identifier_is_refused_without_a_command(browser):
    token = browser.sign_in("reconciler")
    response = browser.client.post(
        f"{BASE}/{ID}/match", data={"csrf_token": token, "bank_line": "not-a-uuid", "gl_line": str(uuid4())}
    )
    assert response.status_code == 400
    _commands(browser)


def test_items_route_builds_typed_requests_from_the_selection(browser):
    token = browser.sign_in("reconciler")
    gl_line, bank_line = uuid4(), uuid4()
    response = browser.client.post(
        f"{BASE}/{ID}/items",
        data={
            "csrf_token": token,
            "gl_line": str(gl_line),
            "bank_line": str(bank_line),
            "gl_item_type": "outstanding_payment",
            "bank_item_type": "unrecorded_bank_charge",
            "note": "cheque 17",
        },
    )
    assert response.status_code == 303
    requests = browser.service["add_items"].call_args.kwargs["requests"]
    assert {(r.side, r.ref_id, r.item_type, r.note) for r in requests} == {
        ("gl", gl_line, "outstanding_payment", "cheque 17"),
        ("bank", bank_line, "unrecorded_bank_charge", "cheque 17"),
    }


@pytest.mark.parametrize(
    "path,name,who",
    [
        (f"{BASE}/{ID}/prepare", "prepare", "reconciler"),
        (f"{BASE}/{ID}/cancel", "cancel", "reconciler"),
        (f"{APPROVALS}/{ID}/approve", "approve", "approver"),
        (f"{APPROVALS}/{ID}/return", "return_to_open", "approver"),
    ],
)
def test_command_routes_call_exactly_one_service_with_the_acting_party(browser, path, name, who):
    token = browser.sign_in(who)
    response = browser.call("POST", path, token, reason="because")
    assert response.status_code == 303
    spy = browser.service[name]
    spy.assert_called_once()
    assert spy.call_args.kwargs["reconciliation_id"] == ID
    assert spy.call_args.kwargs["actor"] == browser.parties[who].id
    for other in SERVICE_NAMES:
        assert browser.service[other].called is (other == name)


def test_sod_refusal_is_shown_and_not_redirected(browser):
    browser.service["approve"].side_effect = ReconRefused(
        "sod_preparer", "the preparer cannot approve their own reconciliation"
    )
    token = browser.sign_in("approver")
    response = browser.call("POST", f"{APPROVALS}/{ID}/approve", token)
    assert response.status_code == 400 and "cannot approve their own reconciliation" in response.text
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_zero_difference_refusal_is_shown(browser):
    browser.service["approve"].side_effect = ReconRefused("difference_nonzero", "unexplained difference 5.000000")
    token = browser.sign_in("approver")
    response = browser.call("POST", f"{APPROVALS}/{ID}/approve", token)
    assert response.status_code == 400 and "unexplained difference" in response.text


def test_there_is_no_bulk_or_ledger_posting_route():
    paths = [r.path for r in routes.router.routes]
    assert not [p for p in paths if "bulk" in p or p.endswith("/all") or "post" in p.rsplit("/", 1)[-1]]
    import erp_v2.banking.recon_service as service_module

    source = open(service_module.__file__).read()
    for forbidden in (
        "create_journal(",
        "post_journal(",
        "reverse_journal(",
        "record_cash_observation(",
        "import requests",
        "import httpx",
        "from dotmac_accounting import service",
        "from dotmac_banking.service",
    ):
        assert forbidden not in source, forbidden


def test_pages_are_no_store_and_escape_content(browser):
    token = browser.sign_in("reconciler")
    data = _workspace_data(browser.parties["reconciler"].id)
    data["statement"] = SimpleNamespace(
        statement_ref="<script>alert(1)</script>", period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)
    )
    browser.queries["workspace"].return_value = data
    response = browser.call("GET", f"{BASE}/{ID}", token)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "<script>alert(1)</script>" not in response.text and "&lt;script&gt;" in response.text
