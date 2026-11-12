"""Operator boundary tests and opt-in disposable hosted PostgreSQL qualification."""

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer import payables_history_cli as cli
from erp_v2.importer.snapshot import SnapshotManifest

TENANT, INVOICE = str(UUID(int=1)), str(UUID(int=2))
REVISION = "a" * 40
SHA = "b" * 64


def manifest():
    return SnapshotManifest(SHA, "source", "destination", REVISION, (TENANT,))


def cohort():
    return dict(version=1, tenant_id=TENANT, snapshot_sha256=SHA, candidate_revision=REVISION, invoice_ids=[INVOICE])


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"version": True}, "invalid_cohort_version"),
        ({"invoice_ids": []}, "explicit_bounded_cohort_required"),
        ({"invoice_ids": [INVOICE, INVOICE]}, "duplicate_invoice_id"),
        ({"invoice_ids": ["invalid"]}, "invalid_invoice_id"),
        ({"tenant_id": str(UUID(int=9))}, "tenant_extent_mismatch"),
        ({"snapshot_sha256": "c" * 64}, "cohort_snapshot_mismatch"),
        ({"candidate_revision": "d" * 40}, "cohort_candidate_mismatch"),
    ],
)
def test_cohort_bindings(change, reason):
    raw = cohort() | change
    with pytest.raises(cli.OperatorRefusal, match=reason):
        cli._cohort(json.dumps(raw).encode(), manifest())


def test_strict_json_refuses_duplicate_keys():
    with pytest.raises(cli.OperatorRefusal, match="duplicate_json_key"):
        cli._json(b'{"version":1,"version":2}')


class Evidence:
    def __init__(self, invoice_id=INVOICE):
        self.invoice_id = invoice_id


class Command:
    def __init__(self, ident=INVOICE):
        self.source_owner = "legacy-erp"
        self.source_invoice_id = ident
        self.invoice_id = UUID(ident)
        self.source_snapshot_sha256 = SHA
        self.evidence_fingerprint = "cv1:" + "c" * 64


class Verifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def evidence(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise cli.OperatorRefusal("source_paid")
        return Evidence(kwargs["invoice_id"])

    def command(self, item):
        return Command(str(item.invoice_id))


def test_plan_calls_evidence_only_and_retains_named_refusals(monkeypatch):
    scope = SimpleNamespace(tenant_id=UUID(TENANT))
    good = cli._plan(Verifier(), scope, (UUID(INVOICE),), object(), {"snapshot_sha256": SHA})
    assert good["counts"] == dict(requested=1, eligible=1, refused=0)
    assert good["commands"][0]["invoice_id"] == INVOICE
    assert good["production_ready"] is False
    bad = cli._plan(Verifier(True), scope, (UUID(INVOICE),), object(), {})
    assert bad["refusals"] == [dict(invoice_id=INVOICE, reason="source_paid")]
    assert bad["counts"]["refused"] == 1


@pytest.mark.parametrize("environment", [{}, {"ERP_IMPORT_APPROVED": "1"}, {"ERP_IMPORT_APPLY_OK": "1"}])
def test_missing_apply_authorization_precedes_file_and_database_use(monkeypatch, tmp_path, environment, capsys):
    for name in ("ERP_IMPORT_APPROVED", "ERP_IMPORT_APPLY_OK"):
        monkeypatch.delenv(name, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    receipt = tmp_path / "receipt.json"
    result = cli.main(
        [
            "--manifest",
            "missing",
            "--snapshot-file",
            "missing",
            "--cohort",
            "missing",
            "--receipt",
            str(receipt),
            "--apply",
        ]
    )
    assert result == 2
    assert not receipt.exists()
    assert INVOICE not in capsys.readouterr().out


def test_receipt_symlink_is_refused_without_overwriting(monkeypatch, tmp_path):
    victim = tmp_path / "existing.json"
    victim.write_text("untouched")
    receipt = tmp_path / "link.json"
    receipt.symlink_to(victim)
    assert (
        cli.main(
            ["--manifest", "missing", "--snapshot-file", "missing", "--cohort", "missing", "--receipt", str(receipt)]
        )
        == 2
    )
    assert victim.read_text() == "untouched"


def test_failure_receipt_is_private_and_does_not_expose_error(monkeypatch, tmp_path, capsys):
    def fail(args):
        raise ValueError("postgresql://user:secret@host/ " + INVOICE)

    monkeypatch.setattr(cli, "_run", fail)
    receipt = tmp_path / "receipt.json"
    assert (
        cli.main(
            ["--manifest", "missing", "--snapshot-file", "missing", "--cohort", "missing", "--receipt", str(receipt)]
        )
        == 2
    )
    text = receipt.read_text() + capsys.readouterr().out
    assert "secret" not in text and INVOICE not in text
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_reviewed_plan_bytes_read_once_and_digest_checked(monkeypatch, tmp_path):
    path = tmp_path / "reviewed.json"
    raw = b'{"version":1}'
    path.write_bytes(raw)
    args = SimpleNamespace(apply=True, reviewed_plan=path, reviewed_plan_sha256=hashlib.sha256(raw).hexdigest())
    seen = []
    original = Path.read_bytes

    def read(p):
        seen.append(p)
        return original(p)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert cli._reviewed(args) == {"version": 1}
    assert seen == [path]
    args.reviewed_plan_sha256 = "f" * 64
    with pytest.raises(cli.OperatorRefusal, match="reviewed_plan_digest_mismatch"):
        cli._reviewed(args)


def test_apply_rejects_changed_plan_before_any_owner_call():
    calls = []
    with pytest.raises(cli.OperatorRefusal, match="reviewed_plan_changed"):
        cli._apply(
            None, None, Verifier(), {"commands": [], "refusals": []}, {"changed": True}, lambda *a, **k: calls.append(k)
        )
    assert not calls


def test_apply_requires_complete_cohort_and_uses_same_commands():
    command = Command()
    plan = dict(commands=[cli._command_json(command)], refusals=[])
    calls = []
    cli._apply(
        object(),
        SimpleNamespace(tenant_id=UUID(TENANT)),
        Verifier(),
        plan,
        plan,
        lambda *a, **k: calls.append(k),
        command_type=lambda **kw: SimpleNamespace(**kw),
    )
    assert calls[0]["command"].evidence_fingerprint == command.evidence_fingerprint
    assert calls[0]["verifier"].__class__ is Verifier
    assert calls[0]["idempotency_expires_at"] is None
    assert calls[0]["admitted_at"].utcoffset().total_seconds() == 0
    plan["refusals"] = [dict(reason="source_paid")]
    with pytest.raises(cli.OperatorRefusal, match="cohort_not_fully_eligible"):
        cli._apply(None, None, None, plan, plan, lambda *a, **k: None)


@pytest.fixture
def operator(monkeypatch, tmp_path):
    snapshot = tmp_path / "snapshot.dump"
    snapshot.write_bytes(b"synthetic frozen snapshot bytes")
    actual_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    data = manifest().as_dict() | {"snapshot_sha256": actual_sha}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(data))
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(json.dumps(cohort() | {"snapshot_sha256": actual_sha}))
    args = SimpleNamespace(
        manifest=manifest_path,
        snapshot_file=snapshot,
        cohort=cohort_path,
        apply=False,
        reviewed_plan=None,
        reviewed_plan_sha256=None,
    )
    state = dict(
        sql=[],
        calls=[],
        committed=0,
        rolled_back=0,
        pending=[],
        source_database="source",
        destination_database="destination",
        posture=dict(
            current_role_name="historical_operator",
            session_role_name="historical_operator",
            identities_found=1,
            privileged_roles=0,
            owned_objects=0,
        ),
        schema_heads=["pa_0003_historical_invoice", "assembly_head"],
        transaction_tenant=None,
        tenant_observed=TENANT,
        planned=0,
        fail=False,
    )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        @contextmanager
        def begin(self):
            yield self

        def execute(self, statement):
            state["sql"].append(str(statement))

        def scalar(self, statement):
            state["sql"].append(str(statement))
            return state["source_database"]

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        @contextmanager
        def begin(self):
            try:
                yield self
            except Exception:
                state["rolled_back"] += 1
                state["pending"].clear()
                raise
            else:
                state["committed"] += 1

        def execute(self, statement):
            sql = str(statement)
            state["sql"].append(sql)
            if sql.startswith("WITH RECURSIVE role_closure"):
                return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: state["posture"]))
            assert sql == "SET TRANSACTION READ ONLY"

        def scalars(self, statement):
            state["sql"].append(str(statement))
            assert str(statement) == "SELECT version_num FROM public.alembic_version"
            return iter(state["schema_heads"])

        def scalar(self, statement, parameters=None):
            sql = str(statement)
            state["sql"].append(sql)
            if sql == "SELECT current_database()":
                return state["destination_database"]
            if sql == "SELECT to_regclass('public.alembic_version') IS NOT NULL":
                return True
            if sql == "SELECT set_config('app.current_tenant', :tenant, true)":
                state["transaction_tenant"] = parameters["tenant"]
                return parameters["tenant"]
            assert sql == "SELECT public.app_current_tenant_id()"
            return state["tenant_observed"]

    class FixedVerifier(Verifier):
        def __init__(self, source, binding):
            super().__init__()
            self.manifest = binding

        def evidence(self, **kwargs):
            assert state["transaction_tenant"] == str(kwargs["scope"].tenant_id) == TENANT
            state["planned"] += 1
            return super().evidence(**kwargs)

        def command(self, item):
            command = Command(str(item.invoice_id))
            command.source_snapshot_sha256 = self.manifest.snapshot_sha256
            return command

    def admit(session, **kw):
        assert state["transaction_tenant"] == str(kw["scope"].tenant_id) == TENANT
        state["calls"].append(kw)
        state["pending"].append(kw["command"])
        if state["fail"] or (state.get("fail_after") is not None and len(state["calls"]) >= state["fail_after"]):
            raise ValueError("private driver failure after owner write")

    def dependencies():
        assert "LEGACY_DATABASE_URL" not in cli.os.environ
        assert "MIGRATION_DATABASE_URL" not in cli.os.environ
        return (
            lambda tenant: SimpleNamespace(tenant_id=tenant),
            FixedVerifier,
            admit,
            lambda **kw: SimpleNamespace(**kw),
        )

    monkeypatch.setattr(cli, "_dependencies", dependencies)
    monkeypatch.setattr(cli, "_installed_payables_version", lambda: "0.1.0a3")
    monkeypatch.setattr(cli, "_expected_schema_heads", lambda: {"pa_0003_historical_invoice", "assembly_head"})
    monkeypatch.setattr(cli, "_safe_engine", lambda url: Engine())
    monkeypatch.setattr(cli, "Session", FakeSession)
    for name in ("DATABASE_URL", "PLATFORM_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("ERP_IMPORT_APPROVED", "1")
    monkeypatch.setenv("ERP_IMPORT_APPLY_OK", "1")

    def materials():
        monkeypatch.setenv("LEGACY_DATABASE_URL", "operator-source")
        monkeypatch.setenv("MIGRATION_DATABASE_URL", "operator-destination")

    materials()
    return args, state, materials


def review(operator, tmp_path):
    args, _state, materials = operator
    plan = cli._run(args)
    path = tmp_path / "reviewed.json"
    raw = json.dumps(plan).encode()
    path.write_bytes(raw)
    args.apply = True
    args.reviewed_plan = path
    args.reviewed_plan_sha256 = hashlib.sha256(raw).hexdigest()
    materials()
    return plan


def test_operator_plan_no_owner_calls_and_only_read_queries(operator):
    args, state, _ = operator
    plan = cli._run(args)
    assert plan["counts"]["eligible"] == 1
    assert not state["calls"] and not state["pending"]
    assert all(
        sql.startswith(("SET TRANSACTION READ ONLY", "SET LOCAL row_security = off", "SELECT", "WITH RECURSIVE"))
        for sql in state["sql"]
    )
    assert state["transaction_tenant"] == TENANT and state["planned"] == 1
    assert "SET LOCAL row_security = off" in state["sql"]
    posture = next(sql for sql in state["sql"] if sql.startswith("WITH RECURSIVE"))
    assert "current_user, session_user" in posture and "pg_catalog.pg_auth_members" in posture
    assert all(flag in posture for flag in ("rolsuper", "rolbypassrls", "rolcreaterole", "rolreplication"))


@pytest.mark.parametrize("installed", ["0.1.0a2", "0.1.0a4", "0.1.0a3+local"])
def test_unqualified_payables_runtime_refuses_before_database_access(operator, monkeypatch, installed):
    args, state, _ = operator
    monkeypatch.setattr(cli, "_installed_payables_version", lambda: installed)
    with pytest.raises(cli.OperatorRefusal, match="released_payables_version_required"):
        cli._run(args)
    assert not state["sql"] and not state["calls"]


@pytest.mark.parametrize(
    "change",
    [
        {"current_role_name": "different_role"},
        {"session_role_name": "privileged_login"},
        {"identities_found": 0},
        {"privileged_roles": 1},
        {"privileged_roles": None},
        {"owned_objects": 1},
    ],
)
@pytest.mark.parametrize("apply", [False, True])
def test_unsafe_destination_role_refuses_before_plan_or_owner(operator, tmp_path, change, apply):
    if apply:
        review(operator, tmp_path)
    args, state, _ = operator
    planned_before = state["planned"]
    state["posture"].update(change)
    with pytest.raises(cli.OperatorRefusal, match="destination_role_not_nonprivileged"):
        cli._run(args)
    assert state["planned"] == planned_before and not state["calls"]
    assert state["rolled_back"] == 1


@pytest.mark.parametrize(
    "heads",
    [
        ["pa_0002_optional_supplier_doc", "assembly_head"],
        ["pa_0003_historical_invoice"],
        ["pa_0003_historical_invoice", "assembly_head", "unknown_head"],
        ["pa_0003_historical_invoice", "assembly_head", "assembly_head"],
    ],
)
def test_destination_requires_complete_exact_schema_before_planning(operator, heads):
    args, state, _ = operator
    state["schema_heads"] = heads
    with pytest.raises(cli.OperatorRefusal, match="destination_schema_heads_mismatch"):
        cli._run(args)
    assert state["planned"] == 0 and not state["calls"]
    assert state["transaction_tenant"] is None


def test_destination_tenant_readback_must_match_bound_cohort(operator):
    args, state, _ = operator
    state["tenant_observed"] = str(UUID(int=999))
    with pytest.raises(cli.OperatorRefusal, match="destination_tenant_binding_mismatch"):
        cli._run(args)
    assert state["planned"] == 0 and not state["calls"] and state["rolled_back"] == 1


@pytest.mark.parametrize(
    "key,reason",
    [("source_database", "source_database_mismatch"), ("destination_database", "destination_database_mismatch")],
)
def test_actual_database_binding_refusal(operator, key, reason):
    args, state, _ = operator
    state[key] = "unbound"
    with pytest.raises(cli.OperatorRefusal, match=reason):
        cli._run(args)
    assert not state["calls"]


def test_snapshot_bytes_must_match_before_database_access(operator):
    args, state, _ = operator
    args.snapshot_file.write_bytes(b"changed snapshot")
    with pytest.raises(cli.OperatorRefusal, match="snapshot_digest_mismatch"):
        cli._run(args)
    assert not state["sql"]


def test_apply_owner_failure_rolls_back_whole_caller_transaction(operator, tmp_path):
    review(operator, tmp_path)
    args, state, _ = operator
    state["fail"] = True
    with pytest.raises(ValueError):
        cli._run(args)
    assert state["rolled_back"] == 1 and not state["pending"]


def test_apply_replays_identical_command_and_key_after_later_owner_state(operator, tmp_path):
    review(operator, tmp_path)
    args, state, materials = operator
    cli._run(args)
    first = state["calls"][-1]
    state["pending"].clear()  # stand-in for later owner settlement, outside immutable facts
    materials()
    cli._run(args)
    second = state["calls"][-1]
    assert first["idempotency_key"] == second["idempotency_key"]
    assert vars(first["command"]) == vars(second["command"])


def test_changed_current_command_refuses_before_owner_write(operator, tmp_path, monkeypatch):
    review(operator, tmp_path)
    args, state, _ = operator
    original = cli._plan

    def changed(*args, **kw):
        result = original(*args, **kw)
        result["commands"][0]["evidence_fingerprint"] = "cv1:" + "f" * 64
        return result

    monkeypatch.setattr(cli, "_plan", changed)
    with pytest.raises(cli.OperatorRefusal, match="reviewed_plan_changed"):
        cli._run(args)
    assert not state["calls"] and state["rolled_back"] == 1


def test_serving_materials_are_forbidden_and_operator_env_is_popped(operator, monkeypatch):
    args, state, _ = operator
    monkeypatch.setenv("DATABASE_URL", "forbidden-serving-material")
    with pytest.raises(cli.OperatorRefusal, match="serving_database_materials_forbidden"):
        cli._run(args)
    assert "LEGACY_DATABASE_URL" not in cli.os.environ
    assert "MIGRATION_DATABASE_URL" not in cli.os.environ
    assert not state["sql"]


def test_second_cohort_failure_rolls_back_earlier_effect_and_never_reports_success(operator, tmp_path, capsys):
    args, state, _ = operator
    raw = json.loads(args.cohort.read_bytes())
    raw["invoice_ids"].append(str(UUID(int=3)))
    args.cohort.write_text(json.dumps(raw))
    review(operator, tmp_path)
    committed_before = state["committed"]
    state["fail_after"] = 2
    receipt = tmp_path / "failed-apply.json"
    result = cli.main(
        [
            "--manifest",
            str(args.manifest),
            "--snapshot-file",
            str(args.snapshot_file),
            "--cohort",
            str(args.cohort),
            "--receipt",
            str(receipt),
            "--apply",
            "--reviewed-plan",
            str(args.reviewed_plan),
            "--reviewed-plan-sha256",
            args.reviewed_plan_sha256,
        ]
    )
    assert result == 2
    assert len(state["calls"]) == 2
    assert state["pending"] == []
    assert state["rolled_back"] == 1 and state["committed"] == committed_before
    payload = json.loads(receipt.read_bytes())
    assert payload == dict(production_ready=False, refusal="historical_payables_command_refused")
    output = capsys.readouterr().out
    assert "command refused" in output and INVOICE not in output


@pytest.fixture(scope="module")
def hosted_postgres():
    """Actual released migrations and roles, only on the disposable hosted CI service."""
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
    database, owner = f"ap_history_{suffix}", f"ap_owner_{suffix}"
    attributes = {
        "operator": "LOGIN",
        "superuser": "LOGIN SUPERUSER",
        "bypass": "LOGIN BYPASSRLS",
        "createrole": "LOGIN CREATEROLE",
        "replication": "LOGIN REPLICATION",
        "createdb": "LOGIN CREATEDB",
        "member": "LOGIN",
        "middle": "NOLOGIN",
        "danger": "NOLOGIN BYPASSRLS",
        "server_member": "LOGIN",
        "server_middle": "NOLOGIN",
        "routine_owner": "LOGIN",
    }
    names = {key: f"ap_{key}_{suffix}" for key in attributes}
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    engines, created_roles, created_database = {}, [], False
    try:
        with admin.connect() as connection:
            for role, options in (("app_admin", "LOGIN BYPASSRLS"), ("app_user", "LOGIN"), ("platform_api", "LOGIN")):
                if not connection.scalar(
                    sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}
                ):
                    connection.exec_driver_sql(f'CREATE ROLE "{role}" {options}')
            connection.exec_driver_sql(f'CREATE ROLE "{owner}" LOGIN BYPASSRLS CREATEROLE')
            created_roles.append(owner)
            connection.exec_driver_sql(f'GRANT app_admin, app_user, platform_api TO "{owner}" WITH ADMIN OPTION')
            connection.exec_driver_sql(f'CREATE DATABASE "{database}" OWNER "{owner}"')
            created_database = True
            for key, options in attributes.items():
                connection.exec_driver_sql(f'CREATE ROLE "{names[key]}" {options}')
                created_roles.append(names[key])
                if options.startswith("LOGIN"):
                    connection.exec_driver_sql(f'GRANT app_user TO "{names[key]}"')
            connection.exec_driver_sql(f'GRANT "{names["danger"]}" TO "{names["middle"]}"')
            connection.exec_driver_sql(f'GRANT "{names["middle"]}" TO "{names["member"]}"')
            connection.exec_driver_sql(f'GRANT pg_read_server_files TO "{names["server_middle"]}"')
            connection.exec_driver_sql(f'GRANT "{names["server_middle"]}" TO "{names["server_member"]}"')
        owner_url = admin_url.set(username=owner, database=database)
        schema_admin = sa.create_engine(admin_url.set(database=database), hide_parameters=True)
        try:
            with schema_admin.begin() as connection:
                connection.exec_driver_sql("ALTER SCHEMA public OWNER TO app_admin")
                connection.exec_driver_sql(f'GRANT USAGE, CREATE ON SCHEMA public TO "{owner}"')
        finally:
            schema_admin.dispose()
        upgrade_heads(owner_url.render_as_string(hide_password=False))
        engines["owner"] = sa.create_engine(owner_url, hide_parameters=True)
        engines["admin"] = sa.create_engine(admin_url.set(database=database), hide_parameters=True)
        for key, options in attributes.items():
            if options.startswith("LOGIN"):
                engines[key] = sa.create_engine(
                    admin_url.set(username=names[key], database=database), hide_parameters=True
                )
        with engines["owner"].begin() as connection:
            connection.exec_driver_sql("GRANT SELECT ON public.alembic_version TO app_user")
            connection.exec_driver_sql("CREATE TABLE public.ap_history_rls_probe (tenant_id uuid NOT NULL)")
            connection.exec_driver_sql("ALTER TABLE public.ap_history_rls_probe ENABLE ROW LEVEL SECURITY")
            connection.exec_driver_sql("ALTER TABLE public.ap_history_rls_probe FORCE ROW LEVEL SECURITY")
            connection.exec_driver_sql(
                "CREATE POLICY ap_history_probe_tenant ON public.ap_history_rls_probe "
                "USING (tenant_id=public.app_current_tenant_id())"
            )
            connection.exec_driver_sql("GRANT SELECT ON public.ap_history_rls_probe TO app_user")
            connection.execute(
                sa.text("INSERT INTO public.ap_history_rls_probe (tenant_id) VALUES (:one), (:two)"),
                {"one": TENANT, "two": str(UUID(int=999))},
            )
        with engines["admin"].begin() as connection:
            connection.exec_driver_sql(f'GRANT CREATE ON SCHEMA public TO "{names["routine_owner"]}"')
            connection.exec_driver_sql(
                "CREATE FUNCTION public.ap_history_owned_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            connection.exec_driver_sql(
                f'ALTER FUNCTION public.ap_history_owned_fn() OWNER TO "{names["routine_owner"]}"'
            )
        with engines["owner"].begin() as connection:
            for schema in ("ap", "ar"):
                connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
                connection.exec_driver_sql(f"CREATE TABLE {schema}.payment_allocation (invoice_id uuid NOT NULL)")
                connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA {schema} TO app_user")
                connection.exec_driver_sql(f"GRANT SELECT ON {schema}.payment_allocation TO app_user")
            connection.execute(sa.text("INSERT INTO ap.payment_allocation VALUES (:invoice)"), {"invoice": INVOICE})
            connection.execute(
                sa.text("INSERT INTO ar.payment_allocation VALUES (:invoice)"), {"invoice": str(UUID(int=999))}
            )
        yield engines, names
    finally:
        for engine in engines.values():
            engine.dispose()
        with admin.connect() as connection:
            if created_database:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            for role in reversed(created_roles):
                connection.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


def test_hosted_postgres_nonprivileged_preflight_enforces_rls_and_transaction_scope(hosted_postgres):
    engines, _names = hosted_postgres
    with Session(engines["operator"]) as session:
        for tenant in (UUID(TENANT), UUID(int=999)):
            with session.begin():
                session.execute(sa.text("SET TRANSACTION READ ONLY"))
                assert session.scalar(sa.text("SELECT public.app_current_tenant_id()")) is None
                assert list(session.scalars(sa.text("SELECT tenant_id FROM public.ap_history_rls_probe"))) == []
                cli._destination_preflight(session, tenant)
                assert list(session.scalars(sa.text("SELECT tenant_id FROM public.ap_history_rls_probe"))) == [tenant]
        with session.begin():
            assert session.scalar(sa.text("SELECT public.app_current_tenant_id()")) is None


@pytest.mark.parametrize(
    "role",
    [
        "superuser",
        "bypass",
        "createrole",
        "replication",
        "createdb",
        "member",
        "owner",
        "server_member",
        "routine_owner",
    ],
)
def test_hosted_postgres_rejects_privileged_roles_and_transitive_membership(hosted_postgres, role):
    engines, _names = hosted_postgres
    with Session(engines[role]) as session, session.begin():
        with pytest.raises(cli.OperatorRefusal, match="destination_role_not_nonprivileged"):
            cli._destination_preflight(session, UUID(TENANT))


def test_hosted_postgres_rejects_changed_role_even_when_current_role_is_unprivileged(hosted_postgres):
    engines, names = hosted_postgres
    with Session(engines["admin"]) as session, session.begin():
        session.execute(sa.text(f'SET LOCAL ROLE "{names["operator"]}"'))
        with pytest.raises(cli.OperatorRefusal, match="destination_role_not_nonprivileged"):
            cli._destination_preflight(session, UUID(TENANT))


@pytest.mark.parametrize("change", ["stale", "missing", "absent"])
def test_hosted_postgres_refuses_unqualified_schema_heads(hosted_postgres, change):
    engines, _names = hosted_postgres
    with engines["owner"].connect() as connection:
        original = list(connection.scalars(sa.text("SELECT version_num FROM public.alembic_version")))
    try:
        with engines["owner"].begin() as connection:
            if change == "absent":
                connection.exec_driver_sql("ALTER TABLE public.alembic_version RENAME TO ap_history_held_version")
            elif change == "missing":
                connection.execute(
                    sa.text("DELETE FROM public.alembic_version WHERE version_num=:head"), {"head": original[0]}
                )
            else:
                connection.execute(
                    sa.text(
                        "UPDATE public.alembic_version SET version_num='pa_0002_optional_supplier_doc' "
                        "WHERE version_num='pa_0003_historical_invoice'"
                    )
                )
        with Session(engines["operator"]) as session, session.begin():
            with pytest.raises(cli.OperatorRefusal, match="destination_schema_heads_mismatch"):
                cli._destination_preflight(session, UUID(TENANT))
    finally:
        with engines["owner"].begin() as connection:
            if change == "absent":
                connection.exec_driver_sql("ALTER TABLE public.ap_history_held_version RENAME TO alembic_version")
            else:
                connection.exec_driver_sql("DELETE FROM public.alembic_version")
                connection.execute(
                    sa.text("INSERT INTO public.alembic_version (version_num) VALUES (:head)"),
                    [{"head": head} for head in original],
                )


def test_hosted_postgres_ap_allocation_contract_ignores_ar_namesake(hosted_postgres):
    from erp_v2.importer.payables_history import _read

    engines, _names = hosted_postgres
    with engines["operator"].connect() as connection, connection.begin():
        connection.execute(sa.text("SET TRANSACTION READ ONLY"))
        connection.execute(sa.text("SET LOCAL row_security = off"))
        rows = _read(connection, "payment_allocation", key="invoice_id", value=INVOICE)
        assert len(rows) == 1 and str(rows[0]["invoice_id"]) == INVOICE
        assert _read(connection, "payment_allocation", key="invoice_id", value=str(UUID(int=999))) == []


def test_hosted_postgres_source_refuses_rls_filtering_instead_of_partial_rows(hosted_postgres):
    engines, _names = hosted_postgres
    with engines["operator"].connect() as connection, connection.begin() as transaction:
        connection.execute(sa.text("SET TRANSACTION READ ONLY"))
        connection.execute(sa.text("SET LOCAL row_security = off"))
        with pytest.raises(sa.exc.ProgrammingError) as refused:
            connection.execute(sa.text("SELECT tenant_id FROM public.ap_history_rls_probe")).all()
        assert refused.value.orig.sqlstate == "42501"
        transaction.rollback()
