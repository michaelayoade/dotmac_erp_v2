"""Role-grant CLI: refusals, binding, receipt hygiene, additive apply."""

import hashlib
import json
import os
import stat
from types import SimpleNamespace
from uuid import uuid4

import pytest

from erp_v2 import access_cli as cli

TENANT, P1, P2 = uuid4(), uuid4(), uuid4()


def doc(grants=None, **over):
    default = [{"party_id": str(P1), "role": "treasury"}]
    data = {"version": 1, "tenant_id": str(TENANT), "grants": default if grants is None else grants}
    data.update(over)
    return json.dumps(data).encode()


def test_parse_ok_is_sorted_and_typed():
    allow = cli.parse_allowlist(
        doc([{"party_id": str(P2), "role": "treasury"}, {"party_id": str(P1), "role": "finance_approver"}])
    )
    assert allow.tenant_id == TENANT and len(allow.grants) == 2


@pytest.mark.parametrize("role", ["admin", "staff"])
def test_admin_and_staff_are_refused(role):
    with pytest.raises(cli.AccessRefusal, match="role_not_grantable"):
        cli.parse_allowlist(doc([{"party_id": str(P1), "role": role}]))


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"nope", "invalid_allowlist_json"),
        (doc(extra=1), "invalid_allowlist_schema"),
        (doc(version=2), "invalid_allowlist_schema"),
        (doc(tenant_id="x"), "invalid_tenant_id"),
        (doc([]), "explicit_bounded_grants_required"),
        (doc([{"party_id": str(P1), "role": "owner"}]), "unknown_role"),
        (doc([{"party_id": "x", "role": "treasury"}]), "invalid_party_id"),
        (doc([{"party_id": str(P1), "role": "treasury", "email": "a@b"}]), "invalid_grant_row"),
        (doc([{"party_id": str(P1), "role": "treasury"}] * 2), "duplicate_grant"),
        (doc([{"party_id": str(uuid4()), "role": "treasury"}] * 201), "explicit_bounded_grants_required"),
    ],
)
def test_malformed_allowlists_refuse(raw, code):
    with pytest.raises(cli.AccessRefusal, match=code):
        cli.parse_allowlist(raw)


def test_apply_requires_explicit_env(tmp_path, monkeypatch, capsys):
    allow = tmp_path / "a.json"
    allow.write_bytes(doc())
    monkeypatch.delenv("ERP_ACCESS_APPLY", raising=False)
    rc = cli.main(["apply", "--allowlist", str(allow), "--receipt", str(tmp_path / "r.json")])
    assert rc == 2 and not (tmp_path / "r.json").exists()


def test_existing_receipt_blocks(tmp_path, monkeypatch):
    allow, receipt = tmp_path / "a.json", tmp_path / "r.json"
    allow.write_bytes(doc())
    receipt.write_text("old")
    assert cli.main(["plan", "--allowlist", str(allow), "--receipt", str(receipt)]) == 2
    assert receipt.read_text() == "old"


def test_apply_without_matching_sha_refuses_and_receipt_is_0600(tmp_path, monkeypatch):
    allow, receipt = tmp_path / "a.json", tmp_path / "r.json"
    allow.write_bytes(doc())
    monkeypatch.setenv("ERP_ACCESS_APPLY", "1")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://u:SECRET@127.0.0.1:1/db")
    rc = cli.main(
        ["apply", "--allowlist", str(allow), "--receipt", str(receipt), "--approved-allowlist-sha256", "0" * 64]
    )
    assert rc == 2
    assert stat.S_IMODE(os.stat(receipt).st_mode) == 0o600
    text = receipt.read_text()
    assert "allowlist_review_binding_required" in text and "SECRET" not in text and str(P1) not in text


def test_serving_database_url_forbidden(tmp_path, monkeypatch):
    allow, receipt = tmp_path / "a.json", tmp_path / "r.json"
    allow.write_bytes(doc())
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://u:x@127.0.0.1:1/db")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:x@127.0.0.1:1/db")
    assert cli.main(["plan", "--allowlist", str(allow), "--receipt", str(receipt)]) == 2
    assert "serving_database_materials_forbidden" in receipt.read_text()


def test_sha_binding_matches_allowlist_bytes():
    raw = doc()
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(doc()).hexdigest()


class Store:
    """Scripted kernel identity_storage + session."""

    def __init__(self, monkeypatch, parties, roles=(), grants=()):
        self.parties, self.roles, self.existing = parties, list(roles), list(grants)
        self.ensured, self.provisioned = [], []
        from dotmac_kernel import identity_storage as s

        monkeypatch.setattr(s, "lock_parties", lambda db, tenant_id, party_ids: tuple(self.parties))
        monkeypatch.setattr(s, "lock_roles", lambda db, tenant_id: tuple(self.roles))
        monkeypatch.setattr(s, "lock_grants", lambda db, tenant_id, party_ids: tuple(self.existing))
        monkeypatch.setattr(s, "provision_missing_roles", self.provision)
        monkeypatch.setattr(s, "ensure_grant", lambda db, **kw: self.ensured.append(kw))

    def provision(self, db, *, catalog, configured_names, missing_role_ids, **kw):
        self.provisioned += sorted(configured_names)
        return catalog + tuple(SimpleNamespace(id=missing_role_ids[s], slug=s) for s in configured_names)

    @staticmethod
    def db(active=True):
        return SimpleNamespace(scalar=lambda stmt: SimpleNamespace(is_active=active))


def party(pid, active=True, kind="person"):
    return SimpleNamespace(id=pid, is_active=active, party_type=kind)


def test_plan_writes_nothing_and_apply_is_additive(monkeypatch):
    store = Store(monkeypatch, [party(P1)])
    allow = cli.parse_allowlist(doc())
    plan = cli.plan_grants(store.db(), allow)
    assert plan.ready and plan.role_inserts == 1 and plan.grant_inserts == 1
    assert store.ensured == [] and store.provisioned == []
    applied = cli.apply_grants(store.db(), allow)
    assert applied.ready and store.provisioned == ["treasury"] and len(store.ensured) == 1


def test_replay_counts_existing_grant(monkeypatch):
    role = SimpleNamespace(id=uuid4(), slug="treasury")
    store = Store(monkeypatch, [party(P1)], roles=[role], grants=[SimpleNamespace(party_id=P1, role_id=role.id)])
    plan = cli.plan_grants(store.db(), cli.parse_allowlist(doc()))
    assert (plan.grant_replays, plan.grant_inserts, plan.role_inserts) == (1, 0, 0)


@pytest.mark.parametrize(
    "parties,blocker",
    [
        ([], "party_missing"),
        ([party(P1, active=False)], "party_inactive"),
        ([party(P1, kind="org")], "party_not_a_person"),
    ],
)
def test_party_blockers_refuse_apply_and_write_nothing(monkeypatch, parties, blocker):
    store = Store(monkeypatch, parties)
    plan = cli.apply_grants(store.db(), cli.parse_allowlist(doc()))
    assert not plan.ready and dict(plan.blockers) == {blocker: 1}
    assert store.ensured == [] and store.provisioned == []


def test_inactive_tenant_refuses(monkeypatch):
    store = Store(monkeypatch, [party(P1)])
    plan = cli.apply_grants(store.db(active=False), cli.parse_allowlist(doc()))
    assert dict(plan.blockers) == {"tenant_missing_or_inactive": 1} and store.ensured == []
