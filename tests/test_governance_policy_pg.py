"""Per-tenant dual-control policy (D-9, e2_0022) on real PostgreSQL: the CLI, privileges, RLS and the migration.

Runs only on the disposable hosted CI service (ERP_TEST_POSTGRES_URL). Covered here: the operator CLI plan/apply
(receipt 0600, no tenant id, audit event, replay is a no-op, refusals), the runtime role's read-only access under
forced RLS, every governed table's ``dual_control`` column default, and the migration itself (existing rows stay
strict across the upgrade; downgrade refuses to relax evidence). The per-flow policy-off/on behaviour lives next to
each flow's own PostgreSQL tests (journals, payees, reconciliation, payouts, cash GL, B1, B1b, B3, B4, access).
"""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyPerson, PartyType, Tenant
from sqlalchemy.orm import Session

from erp_v2.governance import cli
from erp_v2.governance.policy import AUDIT_DUAL_CONTROL_SET, dual_control

GOVERNED = (
    "erp_manual_journals",
    "payee_bank_instructions",
    "erp_bank_reconciliations",
    "erp_payroll_payout_batches",
    "erp_payroll_payout_lines",
    "erp_cash_account_repoints",
)


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
    database, owner = f"governance_{suffix}", f"governance_owner_{suffix}"
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


@pytest.fixture(scope="module")
def runtime_engine(engine):
    role = f"governance_rt_{uuid4().hex[:12]}"
    admin = sa.create_engine(
        sa.make_url(os.environ["ERP_TEST_POSTGRES_URL"]), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOBYPASSRLS')
        c.exec_driver_sql(f'GRANT app_user TO "{role}"')
    rt = sa.create_engine(engine.url.set(username=role), hide_parameters=True)
    try:
        yield rt
    finally:
        rt.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


def new_tenant(engine, *, active=True) -> object:
    tenant_id = uuid4()
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, slug=f"t{tenant_id.hex[:24]}", name="Governance", is_active=active))
        db.commit()
    return tenant_id


class Runner:
    def __init__(self, engine, tmp_path, monkeypatch):
        self.engine, self.tmp, self.mp, self.n = engine, tmp_path, monkeypatch, 0
        monkeypatch.delenv("DATABASE_URL", raising=False)

    def run(
        self, mode, tenant_id, value, *, apply_env=True, operator="ops-1", sha=None, evidence="Michael ruling v1525"
    ):
        self.n += 1
        spec = self.tmp / f"s{self.n}.json"
        spec.write_text(
            json.dumps({"version": 1, "tenant_id": str(tenant_id), "dual_control": value, "evidence_ref": evidence})
        )
        receipt = self.tmp / f"r{self.n}.json"
        self.mp.setenv("MIGRATION_DATABASE_URL", self.engine.url.render_as_string(hide_password=False))
        if apply_env:
            self.mp.setenv("ERP_GOVERNANCE_APPLY", "1")
        else:
            self.mp.delenv("ERP_GOVERNANCE_APPLY", raising=False)
        argv = [mode, "--spec", str(spec), "--receipt", str(receipt)]
        if mode == "apply":
            argv += ["--approved-spec-sha256", sha or hashlib.sha256(spec.read_bytes()).hexdigest()]
            if operator:
                argv += ["--operator-ref", operator]
        code = cli.main(argv)
        text = receipt.read_text() if receipt.exists() else ""
        if receipt.exists():
            assert stat.S_IMODE(os.stat(receipt).st_mode) == 0o600
        assert str(tenant_id) not in text and "postgresql" not in text
        return code, (json.loads(text) if text else {})


@pytest.fixture
def runner(engine, tmp_path, monkeypatch):
    return Runner(engine, tmp_path, monkeypatch)


def audits(engine, tenant_id):
    with Session(engine) as db:
        return list(
            db.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == AUDIT_DUAL_CONTROL_SET)
                .order_by(AuditEvent.created_at)
            )
        )


def policy_row(engine, tenant_id):
    with engine.connect() as c:
        return c.execute(
            sa.text("SELECT dual_control, set_by, evidence_ref FROM erp_governance_policies WHERE tenant_id = :t"),
            {"t": tenant_id},
        ).first()


def helper(engine, tenant_id) -> bool:
    with Session(engine) as db:
        return dual_control(db, tenant_id)


# --- the helper and the operator CLI ------------------------------------------------------------------------------


def test_missing_row_reads_as_off_and_the_default_is_false(engine):
    tenant = new_tenant(engine)
    assert helper(engine, tenant) is False
    with engine.connect() as c:
        default = c.execute(
            sa.text(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'erp_governance_policies' AND column_name = 'dual_control'"
            )
        ).one()
    assert default[0] == "false" and default[1] == "NO"


def test_plan_writes_nothing_and_reports_the_change(engine, runner):
    tenant = new_tenant(engine)
    code, receipt = runner.run("plan", tenant, True)
    assert code == 0 and receipt["mode"] == "plan"
    assert receipt["policy"] == {"current": "unset", "requested": "on", "changes": True, "blockers": {}, "ready": True}
    assert policy_row(engine, tenant) is None and audits(engine, tenant) == []


def test_apply_sets_the_policy_audits_once_and_replays_as_a_no_op(engine, runner):
    tenant = new_tenant(engine)
    code, receipt = runner.run("apply", tenant, True)
    assert code == 0 and receipt["policy"]["changes"] is True
    row = policy_row(engine, tenant)
    assert row.dual_control is True and row.set_by == "ops-1" and row.evidence_ref == "Michael ruling v1525"
    assert helper(engine, tenant) is True
    (event,) = audits(engine, tenant)
    assert event.entity_id == str(tenant) and event.actor_id == "ops-1" and event.is_success is True
    assert event.details["dual_control"] is True and event.details["previous"] == "unset"
    assert len(event.details["spec_sha256"]) == 64
    again_code, again = runner.run("apply", tenant, True)  # replay
    assert again_code == 0 and again["policy"]["changes"] is False
    assert len(audits(engine, tenant)) == 1


def test_switching_off_again_is_audited_with_the_previous_value(engine, runner):
    tenant = new_tenant(engine)
    assert runner.run("apply", tenant, True)[0] == 0
    assert runner.run("apply", tenant, False, evidence="Michael ruling 2026-11-18")[0] == 0
    assert helper(engine, tenant) is False and policy_row(engine, tenant).dual_control is False
    _first, second = audits(engine, tenant)
    assert second.details["previous"] == "on" and second.details["dual_control"] is False


def test_apply_gates_refuse_before_any_write(engine, runner):
    tenant = new_tenant(engine)
    assert runner.run("apply", tenant, True, apply_env=False)[0] == 2
    code, receipt = runner.run("apply", tenant, True, sha="0" * 64)
    assert code == 2 and receipt["refusal"] == "spec_review_binding_required"
    code, receipt = runner.run("apply", tenant, True, operator=None)
    assert code == 2 and receipt["refusal"] == "operator_ref_required"
    assert policy_row(engine, tenant) is None and audits(engine, tenant) == []


def test_unknown_or_inactive_tenant_is_a_blocker_and_writes_nothing(engine, runner):
    code, receipt = runner.run("apply", uuid4(), True)
    assert code == 3 and receipt["policy"]["blockers"] == {"tenant_missing_or_inactive": 1}
    inactive = new_tenant(engine, active=False)
    code, receipt = runner.run("apply", inactive, True)
    assert code == 3 and policy_row(engine, inactive) is None


def test_existing_receipt_blocks_before_any_write(engine, runner, tmp_path, monkeypatch):
    tenant = new_tenant(engine)
    spec, receipt = tmp_path / "s.json", tmp_path / "r.json"
    spec.write_text(json.dumps({"version": 1, "tenant_id": str(tenant), "dual_control": True, "evidence_ref": "e"}))
    receipt.write_text("old")
    monkeypatch.setenv("ERP_GOVERNANCE_APPLY", "1")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    argv = ["apply", "--spec", str(spec), "--receipt", str(receipt), "--operator-ref", "ops"]
    assert cli.main([*argv, "--approved-spec-sha256", hashlib.sha256(spec.read_bytes()).hexdigest()]) == 2
    assert receipt.read_text() == "old" and policy_row(engine, tenant) is None


# --- privileges and RLS ------------------------------------------------------------------------------------------


def test_runtime_role_can_only_read_its_own_tenants_policy(engine, runtime_engine):
    one, two = new_tenant(engine), new_tenant(engine)
    with engine.begin() as c:
        for tenant, value in ((one, True), (two, False)):
            c.execute(
                sa.text(
                    "INSERT INTO erp_governance_policies (tenant_id, dual_control, set_by, evidence_ref) "
                    "VALUES (:t, :v, 'ops', 'e')"
                ),
                {"t": tenant, "v": value},
            )
    with engine.connect() as c:
        flags = c.execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'erp_governance_policies'"
            )
        ).one()
        assert tuple(flags) == (True, True)
    with runtime_engine.connect() as c:
        privileges = {
            p: c.execute(sa.text("SELECT has_table_privilege('erp_governance_policies', :p)"), {"p": p}).scalar()
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
    assert privileges == {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False, "TRUNCATE": False}
    with runtime_engine.connect() as c, c.begin():
        c.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(one)})
        seen = c.execute(sa.text("SELECT tenant_id, dual_control FROM erp_governance_policies")).all()
    assert [(str(t), v) for t, v in seen] == [(str(one), True)]
    with runtime_engine.connect() as c, pytest.raises(sa.exc.DBAPIError):
        c.execute(sa.text("UPDATE erp_governance_policies SET dual_control = false"))


# --- the migration -----------------------------------------------------------------------------------------------


def test_every_governed_table_has_a_not_null_default_true_column_and_runtime_cannot_update_it(engine, runtime_engine):
    with engine.connect() as c:
        for table in GOVERNED:
            default, nullable = c.execute(
                sa.text(
                    "SELECT column_default, is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'dual_control'"
                ),
                {"t": table},
            ).one()
            assert (default, nullable) == ("true", "NO"), table
    with runtime_engine.connect() as c:
        # erp_manual_journals has a table-level UPDATE grant: its stamp is frozen by trigger instead (journals tests)
        for table in (t for t in GOVERNED if t != "erp_manual_journals"):
            assert (
                c.execute(sa.text("SELECT has_column_privilege(:t, 'dual_control', 'UPDATE')"), {"t": table}).scalar()
                is False
            ), table


def _alembic(engine):
    from erp_v2.migrate import build_config

    return build_config(engine.url.render_as_string(hide_password=False))


def _seed_pre_e2_0022_rows(engine, tenant, party):
    """Rows as the e2_0021 schema accepts them (no dual_control column yet)."""
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO erp_manual_journals (id, tenant_id, journal_id, origin, state, evidence_ref, "
                "prepared_by_party_id, prepared_at) VALUES (:i, :t, :j, 'manual', 'PREPARED', 'E', :p, now())"
            ),
            {"i": uuid4(), "t": tenant, "j": uuid4(), "p": party},
        )
        c.execute(
            sa.text(
                "INSERT INTO payee_bank_instructions (id, tenant_id, payee_kind, payee_ref, payee_label, "
                "account_number, account_last4, status, origin, proposed_by_party_id, proposed_at) "
                "VALUES (:i, :t, 'supplier', 'sup-1', 'x', '0123456789', '6789', 'proposed', 'manual', :p, now())"
            ),
            {"i": uuid4(), "t": tenant, "p": party},
        )


def test_existing_rows_stay_strict_across_the_upgrade_and_the_checks_are_rewritten(engine):
    tenant = new_tenant(engine)
    with Session(engine) as db:
        party = uuid4()
        db.add(Party(id=party, tenant_id=tenant, party_type=PartyType.person, display_name="p", email="p@x.io"))
        db.flush()
        db.add(PartyPerson(party_id=party, first_name="P", last_name="Q"))
        db.commit()
    with engine.begin() as c:  # earlier tests in this module recorded policy rows; downgrade refuses to drop them
        c.execute(sa.text("DELETE FROM erp_governance_policies"))
    config = _alembic(engine)
    command.downgrade(config, "e2_0021_payroll_payout_returns")
    try:
        with engine.connect() as c:
            assert not c.execute(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'erp_manual_journals' AND column_name = 'dual_control')"
                )
            ).scalar()
        _seed_pre_e2_0022_rows(engine, tenant, party)
    finally:
        command.upgrade(config, "heads")
    with engine.connect() as c:
        for table in ("erp_manual_journals", "payee_bank_instructions"):
            flags = (
                c.execute(sa.text(f"SELECT dual_control FROM {table} WHERE tenant_id = :t"), {"t": tenant})  # noqa: S608
                .scalars()
                .all()
            )
            assert flags == [True], table  # rows that existed before e2_0022 are strict
        for name, table in (
            ("ck_erp_manual_journals_post_sod", "erp_manual_journals"),
            ("ck_erp_manual_journals_reversal_sod", "erp_manual_journals"),
            ("ck_payee_bank_instructions_two_person", "payee_bank_instructions"),
            ("ck_erp_bank_reconciliations_approved", "erp_bank_reconciliations"),
            ("ck_erp_payroll_payout_batches_distinct_people", "erp_payroll_payout_batches"),
            ("ck_erp_cash_account_repoints_two_person", "erp_cash_account_repoints"),
        ):
            definition = c.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = :n AND conrelid = CAST(:t AS regclass)"
                ),
                {"n": name, "t": table},
            ).scalar()
            assert definition is not None and "NOT dual_control" in definition, name
        guard = c.execute(
            sa.text("SELECT prosrc FROM pg_proc WHERE proname = 'erp_payroll_payout_lines_return_guard'")
        ).scalar()
        assert "NEW.dual_control AND" in guard


def test_downgrade_refuses_while_a_row_was_recorded_with_the_policy_off(engine):
    tenant = new_tenant(engine)
    with Session(engine) as db:
        party = uuid4()
        db.add(Party(id=party, tenant_id=tenant, party_type=PartyType.person, display_name="p", email="p2@x.io"))
        db.flush()
        db.add(PartyPerson(party_id=party, first_name="P", last_name="Q"))
        db.commit()
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO erp_manual_journals (id, tenant_id, journal_id, dual_control, origin, state, "
                "evidence_ref, prepared_by_party_id, prepared_at) "
                "VALUES (:i, :t, :j, false, 'manual', 'PREPARED', 'E', :p, :now)"
            ),
            {"i": uuid4(), "t": tenant, "j": uuid4(), "p": party, "now": datetime.now(UTC)},
        )
    try:
        with pytest.raises(RuntimeError, match="refusing to downgrade"):
            command.downgrade(_alembic(engine), "e2_0021_payroll_payout_returns")
        with engine.connect() as c:  # still at the head: nothing was relaxed
            assert c.execute(sa.text("SELECT count(*) FROM erp_governance_policies")).scalar() == 0
            # still at the head (e2_0023, staff loan entries, follows e2_0022): nothing was relaxed
            assert "e2_0023_staff_loan_entries" in {
                r[0] for r in c.execute(sa.text("SELECT version_num FROM alembic_version"))
            }
    finally:
        with engine.begin() as c:
            c.execute(sa.text("DELETE FROM erp_manual_journals WHERE tenant_id = :t"), {"t": tenant})
