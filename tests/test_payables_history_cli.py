"""Operator boundary tests; no real owner imports or external database."""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

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
            state["sql"].append(str(statement))

        def scalar(self, statement):
            state["sql"].append(str(statement))
            return state["destination_database"]

    class FixedVerifier(Verifier):
        def __init__(self, source, binding):
            super().__init__()
            self.manifest = binding

        def command(self, item):
            command = Command(str(item.invoice_id))
            command.source_snapshot_sha256 = self.manifest.snapshot_sha256
            return command

    def admit(session, **kw):
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
    assert all(sql.startswith(("SET TRANSACTION READ ONLY", "SELECT")) for sql in state["sql"])


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
