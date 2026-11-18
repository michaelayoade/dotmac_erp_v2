"""Per-tenant dual-control policy (D-9): the read helper, the spec parser and the operator gates (sqlite, no server).

Real PostgreSQL (CLI end to end, RLS, privileges, migration) is in tests/test_governance_policy_pg.py.
"""

import hashlib
import json
import os
import stat
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.models import Base, Tenant
from sqlalchemy.orm import Session

from erp_v2.governance import cli
from erp_v2.governance.models import GovernancePolicy
from erp_v2.governance.policy import dual_control

TENANT = uuid4()


def spec(**over):
    data = {"version": 1, "tenant_id": str(TENANT), "dual_control": True, "evidence_ref": "ruling"}
    data.update(over)
    return json.dumps(data).encode()


@pytest.fixture()
def db():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Tenant.__table__, GovernancePolicy.__table__])
    with Session(engine) as session:
        session.add_all([Tenant(id=TENANT, slug="acme", name="Acme"), Tenant(id=uuid4(), slug="b", name="B")])
        session.flush()
        yield session
    engine.dispose()


def test_default_is_off_and_the_policy_is_tenant_scoped(db):
    other = db.scalar(sa.select(Tenant.id).where(Tenant.slug == "b"))
    assert dual_control(db, TENANT) is False  # no row reads as off
    db.add(GovernancePolicy(tenant_id=TENANT, dual_control=True, set_by="ops", evidence_ref="e"))
    db.flush()
    assert dual_control(db, TENANT) is True and dual_control(db, other) is False
    db.get(GovernancePolicy, TENANT).dual_control = False
    db.flush()
    assert dual_control(db, TENANT) is False


def test_parse_ok_and_typed():
    parsed = cli.parse_spec(spec())
    assert parsed.tenant_id == TENANT and parsed.dual_control is True and parsed.evidence_ref == "ruling"
    assert cli.parse_spec(spec(dual_control=False)).dual_control is False


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"nope", "invalid_spec_json"),
        (b'{"version":1,"version":1}', "duplicate_json_key"),
        (spec(extra=1), "invalid_spec_schema"),
        (spec(version=2), "invalid_spec_schema"),
        (spec(version=True), "invalid_spec_schema"),
        (spec(dual_control="true"), "invalid_dual_control"),
        (spec(dual_control=1), "invalid_dual_control"),
        (spec(tenant_id="x"), "invalid_tenant_id"),
        (spec(tenant_id=str(TENANT).upper()), "invalid_tenant_id"),
        (spec(evidence_ref=" "), "evidence_ref_required"),
        (spec(evidence_ref=" padded "), "evidence_ref_required"),
        (spec(evidence_ref="x" * 256), "evidence_ref_required"),
        (spec(evidence_ref=None), "evidence_ref_required"),
        (b"[]", "invalid_spec_schema"),
    ],
)
def test_malformed_specs_refuse(raw, code):
    with pytest.raises(cli.GovernanceRefusal, match=code):
        cli.parse_spec(raw)


def test_plan_does_not_write_and_apply_writes_once_with_an_audit_callback(db):
    parsed = cli.parse_spec(spec())
    plan = cli.plan_policy(db, parsed)
    assert plan.receipt() == {"current": "unset", "requested": "on", "changes": True, "blockers": {}, "ready": True}
    assert db.get(GovernancePolicy, TENANT) is None
    seen = []
    applied = cli.apply_policy(
        db, parsed, spec_sha256="a" * 64, operator_ref="ops-1", audit=lambda _db, **kw: seen.append(kw)
    )
    assert applied.changes and db.get(GovernancePolicy, TENANT).dual_control is True
    assert [(k["previous"], k["operator_ref"], k["spec_sha256"]) for k in seen] == [("unset", "ops-1", "a" * 64)]
    again = cli.apply_policy(
        db, parsed, spec_sha256="a" * 64, operator_ref="ops-1", audit=lambda _db, **kw: seen.append(kw)
    )
    assert again.changes is False and len(seen) == 1  # replay: no write, no audit


def test_apply_requires_operator_ref_and_a_known_active_tenant(db):
    parsed = cli.parse_spec(spec())
    with pytest.raises(cli.GovernanceRefusal, match="operator_ref_required"):
        cli.apply_policy(db, parsed, spec_sha256="a" * 64, operator_ref="  ")
    assert db.get(GovernancePolicy, TENANT) is None
    missing = cli.parse_spec(spec(tenant_id=str(uuid4())))
    plan = cli.apply_policy(db, missing, spec_sha256="a" * 64, operator_ref="ops", audit=lambda *a, **k: None)
    assert plan.ready is False and dict(plan.blockers) == {"tenant_missing_or_inactive": 1}


def run_main(tmp_path, monkeypatch, *argv, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return cli.main([*argv, "--spec", str(tmp_path / "s.json"), "--receipt", str(tmp_path / "r.json")])


def test_apply_needs_the_explicit_env_and_an_existing_receipt_blocks(tmp_path, monkeypatch):
    (tmp_path / "s.json").write_bytes(spec())
    monkeypatch.delenv("ERP_GOVERNANCE_APPLY", raising=False)
    assert run_main(tmp_path, monkeypatch, "apply") == 2 and not (tmp_path / "r.json").exists()
    (tmp_path / "r.json").write_text("old")
    assert run_main(tmp_path, monkeypatch, "plan") == 2
    assert (tmp_path / "r.json").read_text() == "old"


def test_refusals_land_in_a_0600_receipt_without_secrets(tmp_path, monkeypatch):
    (tmp_path / "s.json").write_bytes(spec())
    monkeypatch.setenv("ERP_GOVERNANCE_APPLY", "1")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://u:SECRET@127.0.0.1:1/db")
    sha = hashlib.sha256((tmp_path / "s.json").read_bytes()).hexdigest()
    assert run_main(tmp_path, monkeypatch, "apply", "--approved-spec-sha256", "0" * 64, "--operator-ref", "o") == 2
    receipt = tmp_path / "r.json"
    assert stat.S_IMODE(os.stat(receipt).st_mode) == 0o600
    assert "spec_review_binding_required" in receipt.read_text() and "SECRET" not in receipt.read_text()
    receipt.unlink()
    assert run_main(tmp_path, monkeypatch, "apply", "--approved-spec-sha256", sha) == 2
    assert "operator_ref_required" in receipt.read_text()
    receipt.unlink()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:x@127.0.0.1:1/db")
    assert run_main(tmp_path, monkeypatch, "plan") == 2
    assert "serving_database_materials_forbidden" in receipt.read_text()
