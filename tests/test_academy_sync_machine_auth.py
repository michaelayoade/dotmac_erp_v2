"""The Academy receiver authenticates with kernel machine credentials only."""

from __future__ import annotations

import inspect
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from dotmac_app_sync import encode_envelope
from dotmac_kernel import models as kernel_models
from dotmac_kernel.machine_models import MachineCredential
from dotmac_kernel.machine_rotation import issue_credential, revoke_credential
from dotmac_kernel.secret_sources import clear_secret_source, install_secret_source
from dotmac_kernel.source_applications import (
    SourceApplicationRegistry,
    install_source_applications,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_academy_sync import _envelope

from erp_v2.academy_sync import routes
from erp_v2.academy_sync.contracts import SOURCE_APPLICATION
from erp_v2.machine_keys import MACHINE_KEY_ENV, install_machine_key_secret

SCOPE = routes.REQUIRED_SCOPE
PATH = "/integration/academy-sync/completions"


class _Source:
    def load(self):
        return {"machine_credential_hmac_key": "fixture-hmac-" + "k" * 32}


class _FakeRuntime:
    def __init__(self, session: Session) -> None:
        self._session = session

    @contextmanager
    def tenant_session(self, tenant_id):
        yield self._session


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    install_secret_source(_Source())
    install_source_applications(SourceApplicationRegistry([SOURCE_APPLICATION, "dotmac_other"]))
    engine = sa.create_engine(f"sqlite:///{tmp_path}/m.db")
    kernel_models.Tenant.__table__.create(engine)
    MachineCredential.__table__.create(engine)
    tenant_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(kernel_models.Tenant(id=tenant_id, slug="acme", name="Acme"))
        s.flush()
        monkeypatch.setattr(routes, "resolve_database_runtime", lambda: _FakeRuntime(s))
        monkeypatch.setenv("ACADEMY_SYNC_TENANT_ID", str(tenant_id))
        app = FastAPI()
        app.include_router(routes.router)
        yield TestClient(app), s, tenant_id
    clear_secret_source()


def _key(s, tenant_id, scopes=(SCOPE,), app=SOURCE_APPLICATION, label="academy", **kw):
    cred, raw = issue_credential(s, tenant_id=tenant_id, label=label, source_application=app, scopes=list(scopes), **kw)
    return cred, raw


def _post(client, key=None, body=b"{}"):
    headers = {"X-Api-Key": key} if key else {}
    return client.post(PATH, content=body, headers=headers)


def test_valid_key_in_scope_passes_authentication(ctx):
    client, s, tenant_id = ctx
    _, raw = _key(s, tenant_id)
    # Authentication passed: the refusal is now about the (empty) envelope,
    # not about credentials.
    response = _post(client, raw, body=b"{}")
    assert response.status_code == 422
    assert response.json() == {"status": "source_scope_refused"}


def test_valid_key_delivers_envelope_under_credential_application(ctx, monkeypatch):
    client, s, tenant_id = ctx
    _, raw = _key(s, tenant_id)
    seen = {}

    def fake_deliver(body, *, peer, **kwargs):
        seen["peer"] = peer
        raise routes.PeerMismatch("x")

    monkeypatch.setattr(routes, "deliver_authenticated", fake_deliver)
    body = encode_envelope(_envelope())
    response = _post(client, raw, body=body)
    assert response.status_code == 401 and response.json() == {"status": "peer_mismatch"}
    assert seen["peer"].application == SOURCE_APPLICATION
    assert seen["peer"].transport_subject.startswith("machine:")


@pytest.mark.parametrize(
    "scopes", [(), ("academy_sync.completion:read",), ("*",), ("academy_sync.*",), (SCOPE + ":x",)]
)
def test_wrong_scope_is_refused_opaquely(ctx, scopes):
    client, s, tenant_id = ctx
    _, raw = _key(s, tenant_id, scopes=scopes)
    response = _post(client, raw)
    assert response.status_code == 403
    assert response.json() == {"status": "forbidden"}
    assert SCOPE not in response.text


def test_unknown_missing_revoked_expired_inactive_keys_are_refused_identically(ctx):
    client, s, tenant_id = ctx
    _, revoked_raw = _key(s, tenant_id, label="revoked")
    revoke_credential(s, s.scalars(sa.select(MachineCredential)).one())
    _, expired_raw = _key(s, tenant_id, label="expired", expires_at=datetime.now(UTC) - timedelta(minutes=1))
    inactive, inactive_raw = _key(s, tenant_id, label="inactive")
    inactive.is_active = False
    s.flush()
    unattributed, unattributed_raw = _key(s, tenant_id, label="unattributed")
    unattributed.source_application = None
    s.flush()

    bodies = set()
    for raw in (None, "", "not-a-real-key", revoked_raw, expired_raw, inactive_raw, unattributed_raw):
        response = _post(client, raw)
        assert response.status_code == 401, raw
        bodies.add(response.text)
    assert bodies == {'{"status":"unauthorized"}'}


def test_credential_of_another_tenant_is_refused(ctx):
    client, s, _tenant_id = ctx
    other = uuid.uuid4()
    s.add(kernel_models.Tenant(id=other, slug="other", name="Other"))
    s.flush()
    _, raw = _key(s, other)
    # The fake runtime does not enforce RLS, so the route's own tenant check
    # must refuse a principal that does not belong to the configured tenant.
    assert _post(client, raw).status_code == 401


def test_no_verification_secret_is_a_deployment_fault_not_a_pass(ctx):
    client, s, tenant_id = ctx
    _, raw = _key(s, tenant_id)
    clear_secret_source()
    response = _post(client, raw)
    assert response.status_code == 503
    assert response.json() == {"status": "not_configured"}


def test_tenant_not_configured_is_closed(ctx, monkeypatch):
    client, s, tenant_id = ctx
    _, raw = _key(s, tenant_id)
    monkeypatch.delenv("ACADEMY_SYNC_TENANT_ID")
    assert _post(client, raw).status_code == 503


def test_shared_secret_path_is_gone(ctx, monkeypatch):
    client, _s, _tenant_id = ctx
    monkeypatch.setenv("ACADEMY_SYNC_WEBHOOK_SECRET", "legacy-secret-" + "z" * 32)
    body = b"{}"
    import hashlib
    import hmac

    sig = "sha256=" + hmac.new(b"legacy-secret-" + b"z" * 32, body, hashlib.sha256).hexdigest()
    response = client.post(PATH, content=body, headers={"X-Webhook-Signature-256": sig})
    assert response.status_code == 401
    assert response.json() == {"status": "unauthorized"}
    source = inspect.getsource(routes)
    for needle in ("WEBHOOK_SECRET", "x-webhook-signature", "import hmac", "import hashlib"):
        assert needle.lower() not in source.lower()


def test_machine_key_secret_installs_from_env_only_when_present(monkeypatch):
    clear_secret_source()
    assert install_machine_key_secret({}) is False
    assert install_machine_key_secret({MACHINE_KEY_ENV: "k" * 40}) is True
    clear_secret_source()


def test_materials_refuse_weak_or_reused_machine_key():
    from test_runtime_materials import valid_env

    from erp_v2.materials import runtime_materials_problems

    env = valid_env()
    assert runtime_materials_problems(env) == []
    env[MACHINE_KEY_ENV] = "short"
    assert any(MACHINE_KEY_ENV in p for p in runtime_materials_problems(env))
    env[MACHINE_KEY_ENV] = env["JWT_SECRET"]
    assert any("distinct" in p for p in runtime_materials_problems(env))
    env[MACHINE_KEY_ENV] = "m" * 40
    assert runtime_materials_problems(env) == []


def test_repo_has_no_shared_secret_reference():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    this = pathlib.Path(__file__).resolve()
    for path in [
        *root.joinpath("src").rglob("*.py"),
        *root.joinpath("docs").rglob("*.md"),
        *root.joinpath("deploy").rglob("*"),
    ]:
        if path.is_file() and path != this:
            text = path.read_text(errors="ignore")
            if "ACADEMY_SYNC_WEBHOOK_SECRET" in text:
                # Only the deployment doc may name it, as a removal note.
                assert path.name == "deployment.md", path
