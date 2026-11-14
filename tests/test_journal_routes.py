"""Mounted-route security for B2 (journals, held legacy journals, periods).

Same posture as test_workflow_route_security: create_app mounts the real ERP facet and routers; no
authentication, permission, tenant-resolution or CSRF dependency is replaced. SQLite substitutes only
the database boundary, and the service/query layer is spied, so these tests prove admission, SoD
surfacing and the absence of any bulk path, not ledger mutation (see test_journals_pg for that).
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

from erp_v2.assembly import assembly
from erp_v2.journals import routes
from erp_v2.journals.service import JournalRefused

ID = uuid4()
# role needed -> (method, path)
PREPARE = [
    ("GET", "/finance/journals"),
    ("GET", "/finance/journals/new"),
    ("GET", "/finance/journals/held"),
    ("GET", f"/finance/journals/held/{ID}"),
    ("GET", f"/finance/journals/{ID}"),
    ("POST", "/finance/journals"),
    ("POST", f"/finance/journals/held/{ID}/request-post"),
    ("POST", f"/finance/journals/{ID}/request-post"),
    ("POST", f"/finance/journals/{ID}/request-reversal"),
]
APPROVE = [
    ("GET", "/finance/journal-approvals"),
    ("GET", f"/finance/journal-approvals/{ID}"),
    ("POST", f"/finance/journal-approvals/{ID}/approve"),
    ("POST", f"/finance/journal-approvals/{ID}/approve-reversal"),
    ("POST", f"/finance/journal-approvals/{ID}/reject"),
]
PERIODS = [
    ("GET", "/finance/periods"),
    ("POST", f"/finance/periods/{ID}/open"),
    ("POST", f"/finance/periods/{ID}/soft-close"),
    ("POST", f"/finance/periods/{ID}/lock"),
]
ROLE_FOR = {
    "prepare": ("finance_preparer", PREPARE),
    "approve": ("finance_approver", APPROVE),
    "periods": ("finance_approver", PERIODS),
}
SERVICE_NAMES = (
    "prepare_journal",
    "request_post",
    "request_legacy_post",
    "approve_and_post",
    "reject",
    "request_reversal",
    "approve_reversal",
    "open_period",
    "soft_close_period",
    "lock_period",
)


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


# (login name, roles held). Every journal-capable holder is also staff (access_cli requires it).
PEOPLE = {
    "admin": ("admin",),
    "staff": ("staff",),
    "preparer": ("staff", "finance_preparer"),
    "approver": ("staff", "finance_approver"),
    "treasury": ("staff", "treasury"),
    "preparer_without_staff": ("finance_preparer",),
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
        for slug in ("admin", "staff", "finance_preparer", "finance_approver", "treasury"):
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
        outcome = SimpleNamespace(manual_id=ID, journal_id=uuid4(), state="X", replayed=False)
        service = {name: Mock(return_value=outcome) for name in SERVICE_NAMES}
        service["open_period"].return_value = "OPEN"
        service["soft_close_period"].return_value = "SOFT_CLOSED"
        service["lock_period"].return_value = "LOCKED"
        for name, spy in service.items():
            monkeypatch.setattr(routes.service, name, spy)
        manual = SimpleNamespace(
            id=ID,
            state="POST_REQUESTED",
            origin="manual",
            evidence_ref="SUB-REPORT-1",
            legacy_source_key=None,
            prepared_by_party_id=parties["preparer"].id,
            prepared_at=date(2026, 1, 1),
            requested_by_party_id=parties["preparer"].id,
            requested_at=date(2026, 1, 1),
            approved_by_party_id=None,
            rejected_by_party_id=None,
            reversal_requested_by_party_id=None,
            reversal_approved_by_party_id=None,
        )
        journal = SimpleNamespace(
            number="MJ-1",
            status=SimpleNamespace(value="DRAFT"),
            posting_date=date(2026, 1, 10),
            description="Summary",
            currency_code="USD",
            exchange_rate=Decimal(1),
            total_debit=Decimal(100),
            total_credit=Decimal(100),
            source_owner="legacy-erp",
            source_document_kind="journal",
            source_document_id="a",
            source_version="1",
            id=ID,
        )
        account = SimpleNamespace(id=uuid4(), code="1000", name="Bank")
        line = SimpleNamespace(line_number=1, debit=Decimal(100), credit=Decimal(0), description=None)
        detail = {
            "manual": manual,
            "journal": journal,
            "lines": [(line, account)],
            "names": {parties["preparer"].id: "preparer"},
        }
        queries = {
            "register": Mock(return_value=[{"manual": manual, "journal": journal}]),
            "approval_queue": Mock(return_value=[{"manual": manual, "journal": journal}]),
            "detail": Mock(return_value=detail),
            "held_journals": Mock(
                return_value={"journals": [journal], "page": 1, "total": 1, "has_more": False, "max_page": 1000}
            ),
            "held_detail": Mock(return_value={"journal": journal, "lines": [(line, account)], "workflow": None}),
            "periods": Mock(
                return_value=[
                    SimpleNamespace(
                        id=ID,
                        name="P1",
                        is_adjustment=False,
                        start_date=date(2026, 1, 1),
                        end_date=date(2026, 1, 31),
                        status=SimpleNamespace(value="OPEN"),
                    )
                ]
            ),
            "posting_accounts": Mock(return_value=[account]),
        }
        for name, spy in queries.items():
            monkeypatch.setattr(routes.queries, name, spy)
        monkeypatch.setattr(routes.policy, "functional_currency", Mock(return_value="USD"))
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tenant, parties, tokens, service, queries)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


def _cases():
    for group, (role_holder, routes_) in ROLE_FOR.items():
        for method, path in routes_:
            yield pytest.param(group, role_holder, method, path, id=f"{method} {path.replace(str(ID), '{id}')}")


def _no_writes(browser: Browser) -> None:
    for spy in browser.service.values():
        spy.assert_not_called()


@pytest.mark.parametrize("group,holder,method,path", list(_cases()))
def test_anonymous_is_sent_to_login(browser, group, holder, method, path):
    token = browser.sign_in(None)
    response = browser.call(method, path, token)
    assert response.status_code == 302 and response.headers["location"].startswith("/login")
    _no_writes(browser)


@pytest.mark.parametrize("who", ["admin", "staff", "treasury", "preparer_without_staff"])
@pytest.mark.parametrize("group,holder,method,path", list(_cases()))
def test_admin_staff_and_other_roles_have_no_journal_or_period_access(browser, who, group, holder, method, path):
    token = browser.sign_in(who)
    response = browser.call(method, path, token, review_ref="R", reason="r")
    assert response.status_code == 403
    _no_writes(browser)


@pytest.mark.parametrize("group,holder,method,path", list(_cases()))
def test_each_route_needs_its_own_role_not_the_neighbouring_one(browser, group, holder, method, path):
    wrong = {"prepare": "approver", "approve": "preparer", "periods": "preparer"}[group]
    token = browser.sign_in(wrong)
    response = browser.call(method, path, token, review_ref="R", reason="r")
    assert response.status_code == 403
    _no_writes(browser)


@pytest.mark.parametrize("group,holder,method,path", list(_cases()))
def test_the_holder_of_the_role_is_admitted(browser, group, holder, method, path):
    who = {"prepare": "preparer", "approve": "approver", "periods": "approver"}[group]
    token = browser.sign_in(who)
    response = browser.call(
        method,
        path,
        token,
        review_ref="R",
        reason="r",
        posting_date="2026-01-31",
        request_id=str(uuid4()),
        kind="STANDARD",
        description="d",
        evidence_ref="E",
        account_1=str(uuid4()),
        debit_1="1",
        account_2=str(uuid4()),
        credit_2="1",
    )
    assert response.status_code in (200, 303), (response.status_code, response.text[:300])


@pytest.mark.parametrize("group,holder,method,path", [p for p in _cases() if "POST" in p.id])
@pytest.mark.parametrize("proof", ["missing", "mismatch", "foreign-origin"])
def test_post_routes_refuse_invalid_csrf_before_any_command(browser, group, holder, method, path, proof):
    who = {"prepare": "preparer", "approve": "approver", "periods": "approver"}[group]
    token = browser.sign_in(who)
    headers = {}
    if proof == "mismatch":
        headers["x-csrf-token"] = token + "invalid"
    elif proof == "foreign-origin":
        headers.update({"x-csrf-token": token, "origin": "https://other.example"})
    response = browser.client.post(path, headers=headers, data={"review_ref": "R"})
    assert response.status_code == 403
    _no_writes(browser)


def test_prepare_passes_the_acting_party_and_a_parsed_command(browser):
    token = browser.sign_in("preparer")
    request_id, account_a, account_b = uuid4(), uuid4(), uuid4()
    response = browser.call(
        "POST",
        "/finance/journals",
        token,
        request_id=str(request_id),
        posting_date="2026-01-31",
        kind="ADJUSTMENT",
        description="Option B",
        evidence_ref="SUB-REPORT-9",
        account_1=str(account_a),
        debit_1="100.50",
        account_2=str(account_b),
        credit_2="100.50",
    )
    assert response.status_code == 303 and response.headers["location"] == f"/finance/journals/{ID}"
    call = browser.service["prepare_journal"].call_args.kwargs
    assert call["actor"] == browser.parties["preparer"].id and call["tenant_id"] == browser.tenant.id
    command = call["command"]
    assert (command.request_id, command.evidence_ref, command.kind.value) == (request_id, "SUB-REPORT-9", "ADJUSTMENT")
    assert [(line.account_id, line.debit, line.credit) for line in command.lines] == [
        (account_a, Decimal("100.50"), Decimal(0)),
        (account_b, Decimal(0), Decimal("100.50")),
    ]
    for name, spy in browser.service.items():
        assert spy.called is (name == "prepare_journal")


@pytest.mark.parametrize(
    "path,name,key",
    [
        (f"/finance/journals/{ID}/request-post", "request_post", "actor"),
        (f"/finance/journal-approvals/{ID}/approve", "approve_and_post", "approver"),
        (f"/finance/journal-approvals/{ID}/approve-reversal", "approve_reversal", "approver"),
    ],
)
def test_command_routes_call_exactly_one_service_with_the_acting_party(browser, path, name, key):
    token = browser.sign_in("approver" if "approvals" in path else "preparer")
    response = browser.call("POST", path, token)
    assert response.status_code == 303
    spy = browser.service[name]
    spy.assert_called_once()
    assert spy.call_args.kwargs["manual_id"] == ID and spy.call_args.kwargs[key] in {
        p.id for p in browser.parties.values()
    }
    for other, other_spy in browser.service.items():
        assert other_spy.called is (other == name)


def test_held_journal_request_acts_on_the_path_journal_only_never_a_list(browser):
    token = browser.sign_in("preparer")
    other = uuid4()
    response = browser.call(
        "POST",
        f"/finance/journals/held/{ID}/request-post",
        token,
        review_ref="FIN-1",
        journal_id=str(other),
        journal_ids=f"{other},{uuid4()}",
        all="on",
    )
    assert response.status_code == 303
    browser.service["request_legacy_post"].assert_called_once()
    kwargs = browser.service["request_legacy_post"].call_args.kwargs
    assert kwargs["journal_id"] == ID and kwargs["review_ref"] == "FIN-1"
    assert not [r for r in routes.router.routes if "bulk" in r.path or r.path.endswith("/all")]


def test_sod_refusal_is_shown_and_not_redirected(browser):
    browser.service["approve_and_post"].side_effect = JournalRefused(
        "sod_preparer_cannot_approve", "the preparer cannot approve their own journal"
    )
    token = browser.sign_in("approver")
    response = browser.call("POST", f"/finance/journal-approvals/{ID}/approve", token)
    assert response.status_code == 400 and "cannot approve their own journal" in response.text
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_new_journal_page_shows_the_setting_currency_and_never_a_literal(browser):
    browser.sign_in("preparer")
    body = browser.client.get("/finance/journals/new").text
    assert "USD" in body and "NGN" not in body
    routes.policy.functional_currency.side_effect = routes.policy.FunctionalCurrencyError(
        "functional_currency_unset", "x"
    )
    body = browser.client.get("/finance/journals/new").text
    assert "not configured" in body and "<form" not in body.split("</h1>", 1)[1]


def test_period_page_is_read_only_for_currency_and_has_no_reopen_or_create(browser):
    browser.sign_in("approver")
    body = browser.client.get("/finance/periods").text
    assert (
        "read-only" in body
        and "/open" not in body
        and "reopen" not in body.lower().replace("not created or reopened", "")
    )
    assert not [
        r
        for r in routes.router.routes
        if "reopen" in r.path or (r.path.endswith("/finance/periods") and "POST" in r.methods)
    ]


def test_detail_pages_render_for_both_audiences(browser):
    browser.sign_in("preparer")
    assert "Request posting" not in browser.client.get(f"/finance/journals/{ID}").text  # state is POST_REQUESTED
    browser.sign_in("approver")
    body = browser.client.get(f"/finance/journal-approvals/{ID}").text
    assert "Approve and post" in body and "Reject" in body


def test_register_queue_held_and_period_pages_render(browser):
    browser.sign_in("preparer")
    assert "MJ-1" in browser.client.get("/finance/journals").text
    held = browser.client.get("/finance/journals/held").text
    assert "no bulk action" in held and "Review" in held
    assert "Request posting of this journal" in browser.client.get(f"/finance/journals/held/{ID}").text
    browser.sign_in("approver")
    assert "MJ-1" in browser.client.get("/finance/journal-approvals").text
    assert "Soft-close" in browser.client.get("/finance/periods").text
