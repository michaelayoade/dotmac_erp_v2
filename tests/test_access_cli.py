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
    data = {"version": 1, "tenant_id": str(TENANT), "action": "grant", "grants": default if grants is None else grants}
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


def test_apply_without_operator_ref_refuses_before_database(tmp_path, monkeypatch):
    allow, receipt = tmp_path / "a.json", tmp_path / "r.json"
    allow.write_bytes(doc())
    monkeypatch.setenv("ERP_ACCESS_APPLY", "1")
    sha = hashlib.sha256(allow.read_bytes()).hexdigest()
    rc = cli.main(["apply", "--allowlist", str(allow), "--receipt", str(receipt), "--approved-allowlist-sha256", sha])
    assert rc == 2 and "operator_ref_required" in receipt.read_text()


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


STAFF = SimpleNamespace(id=uuid4(), slug="staff")


def staff_grant(pid):
    return SimpleNamespace(party_id=pid, role_id=STAFF.id)


class Store:
    """Scripted kernel identity_storage + session (parties default to staff holders)."""

    def __init__(self, monkeypatch, parties, roles=(), grants=(), staff=True):
        self.parties, self.roles, self.existing = parties, [STAFF, *roles], list(grants)
        if staff:
            self.existing += [staff_grant(p.id) for p in parties]
        self.audited = []
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


def apply(store, allow, **over):
    args = dict(allowlist_sha256="d" * 64, operator_ref="ops-1", audit=lambda db, **kw: store.audited.append(kw))
    args.update(over)
    return cli.apply_grants(store.db(), allow, **args)


def test_plan_writes_nothing_and_apply_is_additive_and_audited(monkeypatch):
    store = Store(monkeypatch, [party(P1)])
    allow = cli.parse_allowlist(doc())
    plan = cli.plan_grants(store.db(), allow)
    assert plan.ready and plan.role_inserts == 1 and plan.grant_inserts == 1
    assert store.ensured == [] and store.provisioned == [] and store.audited == []
    applied = apply(store, allow)
    assert applied.ready and store.provisioned == ["treasury"] and len(store.ensured) == 1
    assert [(a["party_id"], a["role"], a["operator_ref"]) for a in store.audited] == [(P1, "treasury", "ops-1")]


def test_apply_requires_operator_ref(monkeypatch):
    store = Store(monkeypatch, [party(P1)])
    with pytest.raises(cli.AccessRefusal, match="operator_ref_required"):
        apply(store, cli.parse_allowlist(doc()), operator_ref=" ")
    assert store.ensured == []


def test_party_without_portal_access_refuses(monkeypatch):
    store = Store(monkeypatch, [party(P1)], staff=False)
    plan = apply(store, cli.parse_allowlist(doc()))
    assert dict(plan.blockers) == {"party_lacks_portal_access": 1}
    assert store.ensured == [] and store.audited == []


def role(slug):
    return SimpleNamespace(id=uuid4(), slug=slug)


def test_sod_pair_in_one_request_refuses_unless_excepted(monkeypatch):
    pair = [{"party_id": str(P1), "role": "treasury"}, {"party_id": str(P1), "role": "bank_reconciler"}]
    store = Store(monkeypatch, [party(P1)])
    plan = apply(store, cli.parse_allowlist(doc(pair)))
    assert dict(plan.blockers) == {"sod_conflict": 1} and store.ensured == []
    allowed = doc(pair, sod_exceptions=[{"party_id": str(P1), "roles": ["treasury", "bank_reconciler"]}])
    store2 = Store(monkeypatch, [party(P1)])
    assert apply(store2, cli.parse_allowlist(allowed)).ready and len(store2.ensured) == 2


def test_approver_treasury_pair_is_never_waivable(monkeypatch):
    pair = [{"party_id": str(P1), "role": "finance_approver"}, {"party_id": str(P1), "role": "treasury"}]
    waiver = [{"party_id": str(P1), "roles": ["treasury", "finance_approver"]}]
    with pytest.raises(cli.AccessRefusal, match="sod_pair_not_waivable"):
        cli.parse_allowlist(doc(pair, sod_exceptions=waiver))
    store = Store(monkeypatch, [party(P1)])
    plan = apply(store, cli.parse_allowlist(doc(pair)))
    assert dict(plan.blockers) == {"sod_conflict": 1} and store.ensured == []


def test_sod_pair_against_existing_role_refuses(monkeypatch):
    existing = role("payroll_approver")
    store = Store(
        monkeypatch,
        [party(P1)],
        roles=[existing],
        grants=[SimpleNamespace(party_id=P1, role_id=existing.id)],
    )
    plan = apply(store, cli.parse_allowlist(doc([{"party_id": str(P1), "role": "payroll_finalizer"}])))
    assert dict(plan.blockers) == {"sod_conflict": 1}


@pytest.mark.parametrize(
    "extra",
    [
        {"sod_exceptions": [{"party_id": str(P1), "roles": ["treasury", "staff"]}]},
        {"sod_exceptions": [{"party_id": str(P1), "roles": ["treasury"]}]},
        {"sod_exceptions": [{"party_id": str(P1)}]},
    ],
)
def test_invalid_sod_exceptions_refuse(extra):
    with pytest.raises(cli.AccessRefusal, match="invalid_sod_exception"):
        cli.parse_allowlist(doc(**extra))


def test_boolean_version_and_noncanonical_uuids_refuse():
    with pytest.raises(cli.AccessRefusal, match="invalid_allowlist_schema"):
        cli.parse_allowlist(doc(version=True))
    with pytest.raises(cli.AccessRefusal, match="invalid_party_id"):
        cli.parse_allowlist(doc([{"party_id": str(P1).upper(), "role": "treasury"}]))
    with pytest.raises(cli.AccessRefusal, match="invalid_party_id"):
        cli.parse_allowlist(doc([{"party_id": P1.hex, "role": "treasury"}]))
    with pytest.raises(cli.AccessRefusal, match="invalid_tenant_id"):
        cli.parse_allowlist(doc(tenant_id=str(TENANT).upper()))


def test_replay_counts_existing_grant(monkeypatch):
    held = role("treasury")
    store = Store(monkeypatch, [party(P1)], roles=[held], grants=[SimpleNamespace(party_id=P1, role_id=held.id)])
    plan = apply(store, cli.parse_allowlist(doc()))
    assert (plan.grant_replays, plan.grant_inserts, plan.role_inserts) == (1, 0, 0)
    assert store.audited == []  # a replay writes no new audit event


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
    plan = apply(store, cli.parse_allowlist(doc()))
    assert not plan.ready and dict(plan.blockers) == {blocker: 1}
    assert store.ensured == [] and store.provisioned == []


def test_inactive_tenant_refuses(monkeypatch):
    store = Store(monkeypatch, [party(P1)])
    plan = cli.apply_grants(
        store.db(active=False), cli.parse_allowlist(doc()), allowlist_sha256="d" * 64, operator_ref="ops"
    )
    assert dict(plan.blockers) == {"tenant_missing_or_inactive": 1} and store.ensured == []


# ---- governed revoke -------------------------------------------------------------------------


def rdoc(grants=None, **over):
    return doc(grants, **{"action": "revoke", **over})


def test_action_is_required_and_direction_bound():
    assert cli.parse_allowlist(doc()).grants and cli.parse_allowlist(rdoc(), revoke=True).grants
    for raw, flag in ((rdoc(), False), (doc(), True)):
        with pytest.raises(cli.AccessRefusal, match="allowlist_action_mismatch"):
            cli.parse_allowlist(raw, revoke=flag)
    for bad in ({"action": "delete"}, {"action": None}):
        with pytest.raises(cli.AccessRefusal, match="invalid_allowlist_schema"):
            cli.parse_allowlist(doc(**bad))
    missing = json.loads(doc())
    del missing["action"]
    with pytest.raises(cli.AccessRefusal, match="invalid_allowlist_schema"):
        cli.parse_allowlist(json.dumps(missing).encode())


def test_reviewed_revoke_list_run_with_apply_refuses_before_database(tmp_path, monkeypatch):
    allow, receipt = tmp_path / "a.json", tmp_path / "r.json"
    allow.write_bytes(rdoc())
    monkeypatch.setenv("ERP_ACCESS_APPLY", "1")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://u:x@127.0.0.1:1/db")
    sha = hashlib.sha256(allow.read_bytes()).hexdigest()
    argv = ["--allowlist", str(allow), "--receipt", str(receipt), "--approved-allowlist-sha256", sha]
    assert cli.main(["apply", *argv, "--operator-ref", "o"]) == 2
    assert "allowlist_action_mismatch" in receipt.read_text()
    allow.write_bytes(doc())
    receipt2 = tmp_path / "r2.json"
    sha = hashlib.sha256(allow.read_bytes()).hexdigest()
    argv = ["--allowlist", str(allow), "--receipt", str(receipt2), "--approved-allowlist-sha256", sha]
    assert cli.main(["revoke-apply", *argv, "--operator-ref", "o"]) == 2
    assert "allowlist_action_mismatch" in receipt2.read_text()


def revoke_store(monkeypatch, parties, held_slugs=()):
    """Store whose grants for P1 are the named roles (plus staff); records remove_grants calls."""
    from contextlib import nullcontext

    from dotmac_kernel import identity_storage as s
    from dotmac_kernel import transactions

    roles = [SimpleNamespace(id=uuid4(), slug=slug) for slug in held_slugs]
    grants = [SimpleNamespace(id=uuid4(), tenant_id=TENANT, party_id=P1, role_id=r.id) for r in roles]
    store = Store(monkeypatch, parties, roles=roles, grants=grants)
    store.removed = []
    monkeypatch.setattr(transactions, "conflict_savepoint", lambda db: nullcontext())
    monkeypatch.setattr(
        s, "remove_grants", lambda db, tenant_id, party_id, expected: store.removed.append((party_id, expected))
    )
    return store


def revoke(store, allow, **over):
    args = dict(allowlist_sha256="d" * 64, operator_ref="ops-1", audit=lambda db, **kw: store.audited.append(kw))
    args.update(over)
    return cli.apply_revokes(store.db(), allow, **args)


def test_revoke_allowlist_refuses_admin_staff_unknown_and_sod_exceptions():
    for role in ("admin", "staff", "nope"):
        with pytest.raises(cli.AccessRefusal):
            cli.parse_allowlist(rdoc([{"party_id": str(P1), "role": role}]), revoke=True)
    with pytest.raises(cli.AccessRefusal, match="invalid_allowlist_schema"):
        cli.parse_allowlist(rdoc(sod_exceptions=[]), revoke=True)


def test_revoke_plan_writes_nothing_and_apply_removes_and_audits(monkeypatch):
    store = revoke_store(monkeypatch, [party(P1)], held_slugs=("treasury",))
    allow = cli.parse_allowlist(rdoc(), revoke=True)
    plan = cli.plan_revokes(store.db(), allow)
    assert plan.ready and plan.revokes == 1 and plan.noops == 0
    assert store.removed == [] and store.audited == []
    applied = revoke(store, allow)
    assert applied.ready and applied.revokes == 1
    assert len(store.removed) == 1 and store.removed[0][0] == P1 and len(store.removed[0][1]) == 1
    assert len(store.audited) == 1 and store.audited[0]["role"] == "treasury"
    assert store.audited[0]["operator_ref"] == "ops-1"


def test_revoke_of_absent_grant_is_a_noop(monkeypatch):
    store = revoke_store(monkeypatch, [party(P1)])
    applied = revoke(store, cli.parse_allowlist(rdoc(), revoke=True))
    assert applied.ready and applied.revokes == 0 and applied.noops == 1
    assert store.removed == [] and store.audited == []


def test_revoke_never_touches_staff_and_inactive_party_is_allowed(monkeypatch):
    store = revoke_store(monkeypatch, [party(P1, active=False)], held_slugs=("treasury",))
    revoke(store, cli.parse_allowlist(rdoc(), revoke=True))
    removed_ids = {g.id for _, facts in store.removed for g in facts}
    assert STAFF.id not in {g.role_id for _, facts in store.removed for g in facts}
    assert len(removed_ids) == 1


def test_revoke_missing_party_and_inactive_tenant_refuse(monkeypatch):
    store = revoke_store(monkeypatch, [])
    plan = revoke(store, cli.parse_allowlist(rdoc(), revoke=True))
    assert not plan.ready and dict(plan.blockers) == {"party_missing": 1} and store.removed == []
    store = revoke_store(monkeypatch, [party(P1)], held_slugs=("treasury",))
    inactive = SimpleNamespace(scalar=lambda stmt: SimpleNamespace(is_active=False))
    plan = cli.apply_revokes(
        inactive, cli.parse_allowlist(rdoc(), revoke=True), allowlist_sha256="d" * 64, operator_ref="ops-1"
    )
    assert not plan.ready and store.removed == []


def test_revoke_requires_operator_ref(monkeypatch):
    store = revoke_store(monkeypatch, [party(P1)], held_slugs=("treasury",))
    with pytest.raises(cli.AccessRefusal, match="operator_ref_required"):
        revoke(store, cli.parse_allowlist(rdoc(), revoke=True), operator_ref=" ")


def test_revoke_cli_gates(tmp_path, monkeypatch):
    allow = tmp_path / "a.json"
    allow.write_bytes(rdoc())
    monkeypatch.delenv("ERP_ACCESS_APPLY", raising=False)
    assert cli.main(["revoke-apply", "--allowlist", str(allow), "--receipt", str(tmp_path / "r1.json")]) == 2
    assert not (tmp_path / "r1.json").exists()
    monkeypatch.setenv("ERP_ACCESS_APPLY", "1")
    sha = hashlib.sha256(allow.read_bytes()).hexdigest()
    receipt = tmp_path / "r2.json"
    assert (
        cli.main(
            ["revoke-apply", "--allowlist", str(allow), "--receipt", str(receipt), "--approved-allowlist-sha256", sha]
        )
        == 2
    )
    assert "operator_ref_required" in receipt.read_text()
    receipt = tmp_path / "r3.json"
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://u:SECRET@127.0.0.1:1/db")
    assert (
        cli.main(
            [
                "revoke-apply",
                "--allowlist",
                str(allow),
                "--receipt",
                str(receipt),
                "--approved-allowlist-sha256",
                "0" * 64,
                "--operator-ref",
                "o",
            ]
        )
        == 2
    )
    assert (
        "allowlist_review_binding_required" in receipt.read_text() and stat.S_IMODE(os.stat(receipt).st_mode) == 0o600
    )
    receipt = tmp_path / "r4.json"
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://u:x@127.0.0.1:1/db")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:x@127.0.0.1:1/db")
    assert cli.main(["revoke-plan", "--allowlist", str(allow), "--receipt", str(receipt)]) == 2
    assert "serving_database_materials_forbidden" in receipt.read_text()
