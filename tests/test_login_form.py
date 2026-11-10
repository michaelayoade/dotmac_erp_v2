"""Production HTTPS login submits the actual rendered CSRF proof."""

from contextlib import contextmanager
from html.parser import HTMLParser
from uuid import uuid4

import pytest
from dotmac_kernel.config import settings
from dotmac_kernel.deps import get_db
from dotmac_kernel.models import AuthSession, UserCredential
from dotmac_kernel.security import hash_password
from sqlalchemy import select
from test_workflow_route_security import browser as mounted_browser_fixture


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("type") == "hidden":
            self.values[values["name"]] = values.get("value", "")


@pytest.fixture()
def login_browser(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret", "synthetic-jwt-material-" * 3)
    monkeypatch.setattr(settings, "session_hash_secret", "synthetic-session-material-" * 3)
    monkeypatch.setattr(settings, "csrf_secret", "synthetic-csrf-material-" * 3)
    monkeypatch.setattr(settings, "platform_database_url", "postgresql://fixture.invalid/test")
    with contextmanager(mounted_browser_fixture.__wrapped__)(monkeypatch) as browser:
        browser.client.base_url = "https://acme.erp.test"
        db = next(browser.client.app.dependency_overrides[get_db]())
        UserCredential.__table__.create(db.get_bind(), checkfirst=True)
        db.add(
            UserCredential(
                id=uuid4(),
                tenant_id=browser.tenant.id,
                party_id=browser.party.id,
                password_hash=hash_password("synthetic-login-password"),
            )
        )
        db.commit()
        yield browser, db


def form(client):
    response = client.get("/login")
    assert response.status_code == 200
    values = Fields(response.text).values
    assert values["csrf_token"] == client.cookies.get("__Host-csrf_token")
    assert values["csrf_token"]
    return values


def test_https_login_form_and_errors_keep_submittable_proof(login_browser):
    browser, _ = login_browser
    client = browser.client
    values = form(client)
    response = client.post("/login", data=values)
    assert response.status_code == 200
    assert "Email and password are required" in response.text
    values = Fields(response.text).values
    assert values["csrf_token"] == client.cookies.get("__Host-csrf_token")
    response = client.post(
        "/login", data={**values, "email": "admin@acme.test", "password": "synthetic-wrong-password"}
    )
    assert response.status_code == 200
    assert "Invalid email or password" in response.text
    assert Fields(response.text).values["csrf_token"] == client.cookies.get("__Host-csrf_token")


def test_rendered_login_proof_issues_secure_session_and_rotates_for_logout(login_browser):
    browser, db = login_browser
    client = browser.client
    before = form(client)
    response = client.post(
        "/login", data={**before, "email": "admin@acme.test", "password": "synthetic-login-password"}
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert "access_token=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert len(db.scalars(select(AuthSession).where(AuthSession.party_id == browser.party.id)).all()) == 2
    refreshed = form(client)
    assert refreshed["csrf_token"] != before["csrf_token"]
    assert client.post("/logout", data={"csrf_token": before["csrf_token"]}).status_code == 403
    assert client.post("/logout", data={"csrf_token": refreshed["csrf_token"]}).status_code == 302
    assert len(db.scalars(select(AuthSession).where(AuthSession.party_id == browser.party.id)).all()) == 1


def test_login_refuses_missing_or_foreign_origin_proof(login_browser):
    browser, _ = login_browser
    client = browser.client
    values = form(client)
    response = client.post("/login", data={"email": "admin@acme.test", "password": "synthetic-login-password"})
    assert response.status_code == 403
    assert client.post("/login", data=values, headers={"Origin": "https://other.invalid"}).status_code == 403
