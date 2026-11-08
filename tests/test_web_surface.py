"""Web surface: facet declaration + identity consumption proof.

Identity proof runs the real login/logout service against kernel
identity tables (Party/UserCredential/AuthSession) on sqlite: ERP
consumes kernel identity, it does not reimplement it. Facet mount
proof is a DSN-bound subprocess boot: create_app validates every
WebRouteRef against the registered surface, so a clean boot with
web_enabled True proves the facet graph resolves. Request-level
proof needs the Postgres test server (rehearsal gate).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import sqlalchemy as sa
from dotmac_kernel.models import AuthSession, Base, Party, PartyType, Tenant, UserCredential
from dotmac_kernel.security import hash_password, hash_token
from sqlalchemy.orm import Session

from erp_v2.assembly import assembly
from erp_v2.web import auth_service


def test_facet_declared_with_kernel_cookie_auth() -> None:
    assert len(assembly.web_facets) == 1
    facet = assembly.web_facets[0]
    assert facet.url_prefix == "/"
    assert facet.admission_permission == "erp.web.access"
    refs = {(r.module, r.surface, r.route_name) for r in facet.entry_routes}
    assert refs == {
        ("erp_web", "legacy", "login_page"),
        ("erp_web", "legacy", "login_submit"),
        ("erp_web", "legacy", "logout"),
        ("erp_web", "legacy", "home"),
        ("erp_web", "legacy", "people_home"),
        ("erp_web", "legacy", "leave_home"),
        ("erp_web", "legacy", "leave_apply"),
        ("erp_web", "legacy", "leave_decide"),
        ("erp_web", "legacy", "leave_cancel"),
        ("erp_web", "legacy", "finance_home"),
        ("erp_web", "legacy", "payables_home"),
    }
    assert assembly.authentication_profiles[0].code == "erp_tenant_session"
    assert {d.code for d in assembly.role_definitions} == {"admin", "staff"}


def test_web_mounts_only_with_bound_database() -> None:
    # This test process has no DSN: the shell stays API-only.
    assert assembly.web_enabled is False
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+psycopg://erp:x@127.0.0.1:1/erp",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dotmac_kernel import create_app; "
            "from erp_v2.assembly import assembly; "
            "assert assembly.web_enabled; create_app(assembly); print('WEB-BOOT-OK')",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "WEB-BOOT-OK" in proc.stdout
    assert "erp_web@0.0.0" in proc.stdout + proc.stderr


def test_login_logout_against_kernel_identity_tables() -> None:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            Party.__table__,
            UserCredential.__table__,
            AuthSession.__table__,
        ],
    )
    tenant_id, party_id = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as s:
        tenant = Tenant(id=tenant_id, slug="acme", name="Acme")
        s.add(tenant)
        s.add(
            Party(
                id=party_id,
                tenant_id=tenant_id,
                party_type=PartyType.person,
                display_name="Ada Lovelace",
                email="Ada@Example.com",
            )
        )
        s.add(
            UserCredential(
                tenant_id=tenant_id,
                party_id=party_id,
                password_hash=hash_password("s3cret-pass"),
            )
        )
        s.flush()
        assert auth_service.web_login(s, tenant, "ada@example.com", "wrong") is None
        assert auth_service.web_login(s, tenant, "nobody@example.com", "s3cret-pass") is None
        token = auth_service.web_login(s, tenant, "ADA@example.com", "s3cret-pass")
        assert token
        stored = s.scalar(sa.select(AuthSession))
        assert stored is not None and stored.token_hash == hash_token(token)
        auth_service.web_logout(s, tenant, token)
        assert s.scalar(sa.select(AuthSession)) is None
