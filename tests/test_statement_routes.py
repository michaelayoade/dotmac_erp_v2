"""Mounted statement upload routes: real create_app, cookie auth, roles and CSRF.

SQLite substitutes only the database boundary; the statement service is a spy (its
logic and PostgreSQL behaviour are covered by test_statement_import*.py). This proves
admission: permission (admin/staff hold nothing, W14), CSRF on multipart, upload
validation before any service call, escaping of file content, and no-store headers.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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
from erp_v2.banking import statement_import as si
from erp_v2.banking import statement_routes as routes
from erp_v2.banking.statement_file import MAX_FILE_BYTES, parse_statement

ROLES = ("admin", "staff", "bank_reconciler", "treasury", "unprivileged")
CSV = b"date,type,amount,description,reference\n2026-01-15,credit,10.00,<script>alert(1)</script>,R1\n"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ACCOUNT_ID = uuid4()


def make_plan(data: bytes = CSV, blockers: tuple[str, ...] = ()) -> si.ImportPlan:
    parsed = parse_statement(data, "csv")
    return si.ImportPlan(
        parsed=parsed,
        account_code="OP1",
        currency_code="NGN",
        minor_units=2,
        statement_ref="REF-1",
        source_ref="s",
        evidence_ref="e",
        period_start=parsed.lines[0].transaction_date,
        period_end=parsed.lines[0].transaction_date,
        total_credits=parsed.lines[0].amount,
        total_debits=parsed.lines[0].amount * 0,
        duplicates=(),
        blockers=blockers,
    )


@dataclass
class Browser:
    client: TestClient
    tenant: Tenant
    parties: dict[str, Party]
    tokens: dict[str, str]
    preview: Mock
    do_import: Mock

    def sign_in(self, role: str | None) -> str:
        self.client.cookies.clear()
        if role is not None:
            self.client.cookies.set("access_token", self.tokens[role])
        assert self.client.get("/login").status_code == 200
        token = self.client.cookies.get("csrf_token")
        assert token
        return token

    def post(self, path, *, token, name="stmt.csv", content=CSV, ctype="text/csv", extra=None, headers=None):
        data = {"csrf_token": token, "account_id": str(ACCOUNT_ID), "expected_sha256": "a" * 64, **(extra or {})}
        files = {"file": (name, content, ctype)} if name is not None else None
        return self.client.post(path, data=data, files=files, headers=headers or {})


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
        tokens, parties = {}, {}
        for role_name in ROLES:
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

        monkeypatch.setattr(tenant_middleware, "resolver_session", resolver_session)
        app = create_app(replace(assembly, web_enabled=True, database_runtime=None, require_database_runtime=False))
        app.dependency_overrides[get_db] = request_session
        preview = Mock(side_effect=lambda **kw: make_plan(kw["data"]))
        do_import = Mock(return_value=SimpleNamespace(statement_id=uuid4()))
        monkeypatch.setattr(routes.service, "preview", preview)
        monkeypatch.setattr(routes.service, "import_statement", do_import)
        monkeypatch.setattr(routes, "_accounts", lambda db, tenant: [])
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tenant, parties, tokens, preview, do_import)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


PREVIEW, IMPORT = "/finance/banking/statements/preview", "/finance/banking/statements/import"


@pytest.mark.parametrize("path", [PREVIEW, IMPORT])
@pytest.mark.parametrize("role,expected", [(None, 302), ("admin", 403), ("staff", 403), ("treasury", 403)])
def test_only_the_bank_reconciler_role_is_admitted(browser: Browser, path, role, expected):
    token = browser.sign_in(role)
    response = browser.post(path, token=token)
    assert response.status_code == expected
    browser.preview.assert_not_called()
    browser.do_import.assert_not_called()


def test_form_page_is_dormant_for_admin_and_open_to_bank_reconciler(browser: Browser):
    browser.sign_in("admin")
    assert browser.client.get(IMPORT).status_code == 403
    browser.sign_in("bank_reconciler")
    response = browser.client.get(IMPORT)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("proof", ["missing", "mismatch", "forged", "foreign-origin"])
@pytest.mark.parametrize("path", [PREVIEW, IMPORT])
def test_multipart_upload_refuses_invalid_csrf_before_the_service(browser: Browser, path, proof):
    token = browser.sign_in("bank_reconciler")
    headers = {}
    form_token = token
    if proof == "missing":
        form_token = ""
    elif proof == "mismatch":
        headers["x-csrf-token"] = token + "x"
    elif proof == "forged":
        browser.client.cookies.set("csrf_token", "forged-proof")
        headers["x-csrf-token"] = "forged-proof"
    elif proof == "foreign-origin":
        headers.update({"x-csrf-token": token, "origin": "https://other.example"})
    response = browser.post(path, token=form_token, headers=headers)
    assert response.status_code == 403
    browser.preview.assert_not_called()
    browser.do_import.assert_not_called()


def test_preview_renders_escaped_content_and_import_form_with_hash(browser: Browser):
    token = browser.sign_in("bank_reconciler")
    response = browser.post(PREVIEW, token=token)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "<script>alert(1)</script>" not in response.text and "&lt;script&gt;" in response.text
    assert parse_statement(CSV, "csv").sha256 in response.text
    assert 'action="/finance/banking/statements/import"' in response.text
    kwargs = browser.preview.call_args.kwargs
    assert kwargs["tenant_id"] == browser.tenant.id and kwargs["data"] == CSV and kwargs["file_format"] == "csv"
    browser.do_import.assert_not_called()


def test_preview_with_blockers_offers_no_import(browser: Browser):
    browser.preview.side_effect = lambda **kw: make_plan(kw["data"], blockers=("1 line(s) already exist",))
    token = browser.sign_in("bank_reconciler")
    response = browser.post(PREVIEW, token=token)
    assert response.status_code == 200 and "already exist" in response.text
    assert 'action="/finance/banking/statements/import"' not in response.text


def test_import_passes_tenant_actor_hash_and_bytes_then_redirects(browser: Browser):
    token = browser.sign_in("bank_reconciler")
    response = browser.post(IMPORT, token=token, extra={"expected_sha256": "b" * 64, "date_format": "dmy"})
    assert response.status_code == 303 and response.headers["location"] == "/finance/banking"
    kwargs = browser.do_import.call_args.kwargs
    assert kwargs["tenant_id"] == browser.tenant.id
    assert kwargs["actor_party_id"] == browser.parties["bank_reconciler"].id
    assert kwargs["expected_sha256"] == "b" * 64 and kwargs["data"] == CSV
    assert kwargs["options"].account_id == ACCOUNT_ID and kwargs["options"].date_format == "dmy"
    assert kwargs["now"].tzinfo is not None and abs((datetime.now(UTC) - kwargs["now"]).total_seconds()) < 60


def test_service_refusal_is_shown_not_raised(browser: Browser):
    browser.do_import.side_effect = si.StatementImportRefused(["This exact file has already been imported"])
    token = browser.sign_in("bank_reconciler")
    response = browser.post(IMPORT, token=token)
    assert response.status_code == 400 and "already been imported" in response.text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "stmt.exe", "content": b"MZ", "ctype": "application/octet-stream"},
        {"name": "stmt.csv", "ctype": "application/x-msdownload"},
        {"name": "stmt.xlsx", "ctype": "text/csv"},
        {"name": "stmt.csv", "content": b"a" * (MAX_FILE_BYTES + 1)},
        {"name": None},
    ],
)
@pytest.mark.parametrize("path", [PREVIEW, IMPORT])
def test_upload_validation_refuses_before_the_service(browser: Browser, path, kwargs):
    token = browser.sign_in("bank_reconciler")
    response = browser.post(path, token=token, **kwargs)
    assert response.status_code in (400, 413)
    browser.preview.assert_not_called()
    browser.do_import.assert_not_called()


def test_bad_account_id_and_amount_inputs_are_refused(browser: Browser):
    token = browser.sign_in("bank_reconciler")
    assert browser.post(PREVIEW, token=token, extra={"account_id": "nope"}).status_code == 400
    assert browser.post(PREVIEW, token=token, extra={"opening_balance": "1e9"}).status_code == 400
    browser.preview.assert_not_called()


def test_xlsx_content_type_is_accepted_for_xlsx_only(browser: Browser):
    token = browser.sign_in("bank_reconciler")
    browser.post(PREVIEW, token=token, name="s.xlsx", content=b"PK\x03\x04", ctype=XLSX_TYPE)
    assert browser.preview.call_args.kwargs["file_format"] == "xlsx"
