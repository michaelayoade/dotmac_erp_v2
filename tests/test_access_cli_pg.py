"""Role grant/revoke CLI end-to-end on real PostgreSQL (real migrations, real kernel storage).

Runs only on the disposable hosted CI service (ERP_TEST_POSTGRES_URL). Required by the P0 review
before any non-throwaway apply: role provisioned when missing, grant + audit row, a blocked apply
writes nothing, replay writes no new audit rows, governed revoke removes the grant and audits it,
admin/staff are never revocable. The CLI is driven through ``main`` exactly as an operator would.
"""

import hashlib
import json
import os
import stat
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role, Tenant
from dual_control_support import set_dual_control, set_dual_control_engine
from sqlalchemy.orm import Session

from erp_v2 import access, access_cli


@pytest.fixture(scope="module")
def engine():
    raw_url = os.environ.get("ERP_TEST_POSTGRES_URL")
    if not raw_url:
        pytest.skip("ERP_TEST_POSTGRES_URL is required for hosted PostgreSQL qualification")
    admin_url = sa.make_url(raw_url)
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert admin_url.get_backend_name() == "postgresql"
    assert admin_url.host in {"localhost", "127.0.0.1", "::1"} and not admin_url.query
    assert admin_url.username == "postgres" and admin_url.database == "postgres"
    from erp_v2.migrate import upgrade_heads

    suffix = uuid4().hex
    database, owner = f"access_cli_{suffix}", f"access_owner_{suffix}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    owner_engine = None
    created_database = created_owner = False
    try:
        with admin.connect() as connection:
            for role, options in (("app_admin", "LOGIN BYPASSRLS"), ("app_user", "LOGIN"), ("platform_api", "LOGIN")):
                if not connection.scalar(
                    sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}
                ):
                    connection.exec_driver_sql(f'CREATE ROLE "{role}" {options}')
            connection.exec_driver_sql(f'CREATE ROLE "{owner}" LOGIN BYPASSRLS CREATEROLE')
            created_owner = True
            connection.exec_driver_sql(f'GRANT app_admin, app_user, platform_api TO "{owner}" WITH ADMIN OPTION')
            connection.exec_driver_sql(f'CREATE DATABASE "{database}" OWNER "{owner}"')
            created_database = True
        schema_admin = sa.create_engine(admin_url.set(database=database), hide_parameters=True)
        try:
            with schema_admin.begin() as connection:
                connection.exec_driver_sql("ALTER SCHEMA public OWNER TO app_admin")
                connection.exec_driver_sql(f'GRANT USAGE, CREATE ON SCHEMA public TO "{owner}"')
        finally:
            schema_admin.dispose()
        owner_url = admin_url.set(username=owner, database=database)
        upgrade_heads(owner_url.render_as_string(hide_password=False))
        owner_engine = sa.create_engine(owner_url, hide_parameters=True, pool_size=8)
        yield owner_engine
    finally:
        if owner_engine is not None:
            owner_engine.dispose()
        with admin.connect() as connection:
            if created_database:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            if created_owner:
                connection.exec_driver_sql(f'DROP ROLE "{owner}"')
        admin.dispose()


def seed(engine, *, parties=1, staff=True):
    tenant_id = uuid4()
    ids = [uuid4() for _ in range(parties)]
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, slug=f"t{tenant_id.hex[:24]}", name="Access"))
        db.flush()
        set_dual_control(db, tenant_id, True)  # existing separation-of-duties assertions run with the policy ON
        staff_role = Role(tenant_id=tenant_id, slug="staff", name="Staff")
        db.add(staff_role)
        db.flush()
        for n, pid in enumerate(ids):
            db.add(
                Party(
                    id=pid,
                    tenant_id=tenant_id,
                    party_type=PartyType.person,
                    display_name=f"P{n}",
                    email=f"p{n}-{pid.hex[:8]}@example.test",
                )
            )
            db.flush()
            db.add(PartyPerson(party_id=pid, first_name="Ada", last_name=f"T{n}"))
            db.flush()
            if staff:
                db.add(PartyRoleGrant(tenant_id=tenant_id, party_id=pid, role_id=staff_role.id))
        db.commit()
    return tenant_id, ids


class Runner:
    def __init__(self, engine, tmp_path, monkeypatch):
        self.engine, self.tmp, self.mp, self.n = engine, tmp_path, monkeypatch, 0
        monkeypatch.delenv("DATABASE_URL", raising=False)

    def run(self, mode, tenant_id, grants, *, apply_env=True, operator="ops-1", sha=None, action=None, **extra):
        self.n += 1
        allow = self.tmp / f"a{self.n}.json"
        data = {
            "version": 1,
            "tenant_id": str(tenant_id),
            "action": action or ("revoke" if mode.startswith("revoke") else "grant"),
            "grants": [{"party_id": str(p), "role": r} for p, r in grants],
        }
        data.update(extra)
        allow.write_text(json.dumps(data))
        receipt = self.tmp / f"r{self.n}.json"
        self.mp.setenv("MIGRATION_DATABASE_URL", self.engine.url.render_as_string(hide_password=False))
        if apply_env:
            self.mp.setenv("ERP_ACCESS_APPLY", "1")
        else:
            self.mp.delenv("ERP_ACCESS_APPLY", raising=False)
        argv = [mode, "--allowlist", str(allow), "--receipt", str(receipt)]
        if mode.endswith("apply"):
            argv += ["--approved-allowlist-sha256", sha or hashlib.sha256(allow.read_bytes()).hexdigest()]
            if operator:
                argv += ["--operator-ref", operator]
        code = access_cli.main(argv)
        text = receipt.read_text() if receipt.exists() else ""
        if receipt.exists():
            assert stat.S_IMODE(os.stat(receipt).st_mode) == 0o600
        for p, _ in grants:
            assert str(p) not in text and "postgresql" not in text
        return code, (json.loads(text) if text else {})


@pytest.fixture
def runner(engine, tmp_path, monkeypatch):
    return Runner(engine, tmp_path, monkeypatch)


def held(engine, tenant_id, party_id):
    with Session(engine) as db:
        return set(
            db.scalars(
                sa.select(Role.slug)
                .join(PartyRoleGrant, PartyRoleGrant.role_id == Role.id)
                .where(PartyRoleGrant.tenant_id == tenant_id, PartyRoleGrant.party_id == party_id)
            )
        )


def audits(engine, tenant_id, action=None):
    with Session(engine) as db:
        stmt = sa.select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        return list(db.scalars(stmt))


def role_count(engine, tenant_id):
    with Session(engine) as db:
        return db.scalar(sa.select(sa.func.count()).select_from(Role).where(Role.tenant_id == tenant_id))


def test_grant_apply_provisions_role_grants_and_audits_then_replays_quietly(engine, runner):
    tenant, (party,) = seed(engine)
    assert role_count(engine, tenant) == 1
    code, receipt = runner.run("plan", tenant, [(party, "treasury")])
    assert code == 0 and receipt["access"]["grant_inserts"] == 1 and role_count(engine, tenant) == 1
    assert held(engine, tenant, party) == {"staff"} and audits(engine, tenant) == []
    code, receipt = runner.run("apply", tenant, [(party, "treasury")])
    assert code == 0 and receipt["access"]["role_inserts"] == 1 and receipt["access"]["grant_inserts"] == 1
    assert held(engine, tenant, party) == {"staff", "treasury"} and role_count(engine, tenant) == 2
    events = audits(engine, tenant, access.AUDIT_ROLE_GRANTED)
    assert len(events) == 1 and events[0].actor_type == "service" and events[0].actor_id == "ops-1"
    assert events[0].entity_id == str(party) and events[0].details["role"] == "treasury"
    before = len(audits(engine, tenant))
    code, receipt = runner.run("apply", tenant, [(party, "treasury")])
    assert code == 0 and receipt["access"]["grant_replays"] == 1 and receipt["access"]["grant_inserts"] == 0
    assert len(audits(engine, tenant)) == before and role_count(engine, tenant) == 2


def test_blocked_apply_writes_nothing(engine, runner):
    tenant, (party,) = seed(engine, staff=False)  # no portal access -> blocker
    code, receipt = runner.run("apply", tenant, [(party, "treasury")])
    assert code == 3 and receipt["access"]["blockers"] == {"party_lacks_portal_access": 1}
    assert held(engine, tenant, party) == set() and role_count(engine, tenant) == 1
    assert audits(engine, tenant) == []
    # SoD conflict blocks the whole request, including the harmless grant
    tenant, (a, b) = seed(engine, parties=2)
    code, receipt = runner.run(
        "apply", tenant, [(a, "finance_preparer"), (a, "finance_approver"), (b, "bank_reconciler")]
    )
    assert code == 3 and receipt["access"]["blockers"] == {"sod_conflict": 1}
    assert held(engine, tenant, a) == {"staff"} and held(engine, tenant, b) == {"staff"}
    assert audits(engine, tenant) == [] and role_count(engine, tenant) == 1


def test_apply_gates_refuse_before_any_write(engine, runner):
    tenant, (party,) = seed(engine)
    assert runner.run("apply", tenant, [(party, "treasury")], apply_env=False)[0] == 2
    assert runner.run("apply", tenant, [(party, "treasury")], operator="")[0] == 2
    assert runner.run("apply", tenant, [(party, "treasury")], sha="0" * 64)[0] == 2
    assert runner.run("apply", tenant, [(party, "admin")])[0] == 2
    assert held(engine, tenant, party) == {"staff"} and role_count(engine, tenant) == 1
    assert audits(engine, tenant) == []


def test_revoke_removes_grant_audits_and_is_idempotent(engine, runner):
    tenant, (party,) = seed(engine)
    assert runner.run("apply", tenant, [(party, "treasury"), (party, "payroll_approver")])[0] == 0
    code, receipt = runner.run("revoke-plan", tenant, [(party, "treasury")])
    assert code == 0 and receipt["access"]["revokes"] == 1
    assert held(engine, tenant, party) == {"staff", "treasury", "payroll_approver"}
    assert audits(engine, tenant, access.AUDIT_ROLE_REVOKED) == []
    code, receipt = runner.run("revoke-apply", tenant, [(party, "treasury")])
    assert code == 0 and receipt["access"]["revokes"] == 1 and receipt["access"]["noops"] == 0
    assert held(engine, tenant, party) == {"staff", "payroll_approver"}
    events = audits(engine, tenant, access.AUDIT_ROLE_REVOKED)
    assert len(events) == 1 and events[0].actor_type == "service" and events[0].actor_id == "ops-1"
    assert events[0].entity_id == str(party) and events[0].details["role"] == "treasury"
    # replay: absent grant is a no-op with no new audit row; the role row itself is untouched
    code, receipt = runner.run("revoke-apply", tenant, [(party, "treasury")])
    assert code == 0 and receipt["access"]["revokes"] == 0 and receipt["access"]["noops"] == 1
    assert len(audits(engine, tenant, access.AUDIT_ROLE_REVOKED)) == 1
    assert role_count(engine, tenant) == 3


def test_revoke_multiple_parties_one_audit_per_removed_grant(engine, runner):
    tenant, (a, b) = seed(engine, parties=2)
    assert runner.run("apply", tenant, [(a, "treasury"), (b, "treasury"), (b, "payroll_approver")])[0] == 0
    code, receipt = runner.run(
        "revoke-apply", tenant, [(a, "treasury"), (b, "treasury"), (b, "payroll_approver"), (a, "payroll_approver")]
    )
    assert code == 0 and receipt["access"]["revokes"] == 3 and receipt["access"]["noops"] == 1
    assert held(engine, tenant, a) == {"staff"} and held(engine, tenant, b) == {"staff"}
    assert len(audits(engine, tenant, access.AUDIT_ROLE_REVOKED)) == 3


def test_revoke_never_removes_staff_or_admin_and_refuses_unknown_roles(engine, runner):
    tenant, (party,) = seed(engine)
    for role in ("staff", "admin", "no_such_role"):
        code, receipt = runner.run("revoke-apply", tenant, [(party, role)])
        assert code == 2 and "refusal" in receipt
    assert held(engine, tenant, party) == {"staff"}  # portal access (erp.web.access) intact
    assert audits(engine, tenant) == []


def test_revoke_gates_refuse_before_any_write(engine, runner):
    tenant, (party,) = seed(engine)
    assert runner.run("apply", tenant, [(party, "treasury")])[0] == 0
    assert runner.run("revoke-apply", tenant, [(party, "treasury")], apply_env=False)[0] == 2
    assert runner.run("revoke-apply", tenant, [(party, "treasury")], operator="")[0] == 2
    assert runner.run("revoke-apply", tenant, [(party, "treasury")], sha="0" * 64)[0] == 2
    assert runner.run("revoke-apply", tenant, [(party, "treasury")], sod_exceptions=[])[0] == 2
    assert held(engine, tenant, party) == {"staff", "treasury"}
    assert audits(engine, tenant, access.AUDIT_ROLE_REVOKED) == []
    # unknown party blocks and writes nothing
    code, receipt = runner.run("revoke-apply", tenant, [(party, "treasury"), (uuid4(), "treasury")])
    assert code == 3 and receipt["access"]["blockers"] == {"party_missing": 1}
    assert held(engine, tenant, party) == {"staff", "treasury"}


def test_revoke_is_tenant_scoped(engine, runner):
    tenant, (party,) = seed(engine)
    other, (other_party,) = seed(engine)
    assert runner.run("apply", tenant, [(party, "treasury")])[0] == 0
    assert runner.run("apply", other, [(other_party, "treasury")])[0] == 0
    code, _ = runner.run("revoke-apply", other, [(party, "treasury")])  # wrong tenant for this party
    assert code == 3
    assert held(engine, tenant, party) == {"staff", "treasury"}
    assert held(engine, other, other_party) == {"staff", "treasury"}


def test_revoke_never_leaves_a_forbidden_pair_or_removes_portal_access(engine, runner):
    tenant, (party,) = seed(engine)
    pair = [(party, "finance_preparer"), (party, "finance_approver")]
    waiver = [{"party_id": str(party), "roles": ["finance_preparer", "finance_approver"]}]
    assert runner.run("apply", tenant, pair, sod_exceptions=waiver)[0] == 0
    assert held(engine, tenant, party) == {"staff", "finance_preparer", "finance_approver"}
    assert runner.run("revoke-apply", tenant, [(party, "finance_approver")])[0] == 0
    remaining = held(engine, tenant, party)
    assert remaining == {"staff", "finance_preparer"}
    assert not any(p <= remaining for p in access.SOD_CONFLICT_PAIRS)
    assert runner.run("revoke-apply", tenant, [(party, "finance_preparer")])[0] == 0
    assert held(engine, tenant, party) == {"staff"}


def test_action_mismatch_refuses_in_both_directions_and_writes_nothing(engine, runner):
    tenant, (party,) = seed(engine)
    assert runner.run("apply", tenant, [(party, "treasury")], action="revoke")[0] == 2
    assert held(engine, tenant, party) == {"staff"} and role_count(engine, tenant) == 1
    assert runner.run("apply", tenant, [(party, "treasury")])[0] == 0
    # a reviewed GRANT list can never be run as a revoke, and a revoke list never as a grant
    code, receipt = runner.run("revoke-apply", tenant, [(party, "treasury")], action="grant")
    assert code == 2 and receipt["refusal"] == "allowlist_action_mismatch"
    assert runner.run("apply", tenant, [(party, "payroll_approver")], action="revoke")[0] == 2
    assert held(engine, tenant, party) == {"staff", "treasury"}
    assert audits(engine, tenant, access.AUDIT_ROLE_REVOKED) == []


def test_audit_failure_after_removals_rolls_everything_back_through_run(engine, runner, monkeypatch):
    tenant, (a, b) = seed(engine, parties=2)
    assert runner.run("apply", tenant, [(a, "treasury"), (b, "treasury")])[0] == 0
    audits_before = len(audits(engine, tenant))

    def boom(db, **kw):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(access_cli, "_write_revoke_audit", boom)
    code, receipt = runner.run("revoke-apply", tenant, [(a, "treasury"), (b, "treasury")])
    assert code == 2 and receipt["refusal"] == "access_command_refused"
    assert held(engine, tenant, a) == {"staff", "treasury"} and held(engine, tenant, b) == {"staff", "treasury"}
    assert len(audits(engine, tenant)) == audits_before
    assert audits(engine, tenant, access.AUDIT_ROLE_REVOKED) == []
    monkeypatch.undo()
    # the same revoke succeeds afterwards (nothing was left half-done)
    assert runner.run("revoke-apply", tenant, [(a, "treasury"), (b, "treasury")])[0] == 0
    assert held(engine, tenant, a) == {"staff"} and len(audits(engine, tenant, access.AUDIT_ROLE_REVOKED)) == 2


# --- dual-control policy (D-9): default OFF ----------------------------------------------------------------------


def test_policy_off_role_pair_grants_apply_with_a_counted_warning(engine, runner):
    tenant, (a,) = seed(engine)
    set_dual_control_engine(engine, tenant, False)
    code, receipt = runner.run("apply", tenant, [(a, "finance_approver"), (a, "treasury")])  # the non-waivable pair
    assert code == 0
    assert receipt["access"]["blockers"] == {} and receipt["access"]["warnings"] == {"sod_conflict": 1}
    assert held(engine, tenant, a) == {"staff", "finance_approver", "treasury"}
    assert len(audits(engine, tenant)) == 2  # both grants audited


def test_policy_on_the_pair_still_blocks_and_writes_nothing(engine, runner):
    tenant, (a,) = seed(engine)  # the seed turns the policy ON
    code, receipt = runner.run("apply", tenant, [(a, "finance_approver"), (a, "treasury")])
    assert code == 3 and receipt["access"]["blockers"] == {"sod_conflict": 1}
    assert receipt["access"]["warnings"] == {}
    assert held(engine, tenant, a) == {"staff"} and audits(engine, tenant) == []


def test_no_policy_row_reads_as_off(engine, runner):
    tenant, (a,) = seed(engine)
    with Session(engine) as db:
        db.execute(sa.text("DELETE FROM erp_governance_policies WHERE tenant_id = :t"), {"t": tenant})
        db.commit()
    code, receipt = runner.run("plan", tenant, [(a, "treasury"), (a, "bank_reconciler")])
    assert code == 0 and receipt["access"]["warnings"] == {"sod_conflict": 1}
