"""Operator bindings and receipt hygiene without source credential access."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from erp_v2.importer import identity_cli as cli


def manifest(tenant):
    return cli._manifest(
        json.dumps(
            {
                "snapshot_sha256": "a" * 64,
                "source_database": "fixture_source",
                "destination_database": "fixture_destination",
                "candidate_revision": "b" * 40,
                "tenant_extent": [str(tenant)],
            }
        ).encode()
    )


def test_operator_permission_profile_uses_real_registry_without_admin_grants():
    plan = cli._migration_permissions()
    assert {role.code for role in plan.roles} == {"staff"}
    assert plan.roles[0].create_if_missing
    assert {(grant.role_code, grant.permission_code) for grant in plan.grants} == {("staff", "erp.web.access")}
    assert plan.is_declared("erp.web.access")


def test_allowlist_scope_is_explicit_and_snapshot_bound():
    tenant, credential, person, employee = (uuid4() for _ in range(4))
    data = {
        "version": 1,
        "tenant_id": str(tenant),
        "snapshot_sha256": "a" * 64,
        "candidate_revision": "b" * 40,
        "mappings": [
            {
                "credential_id": str(credential),
                "person_id": str(person),
                "party_id": str(person),
                "employee_id": str(employee),
            }
        ],
    }
    scope = cli._load_scope(json.dumps(data).encode(), manifest(tenant))
    assert scope.tenant_id == tenant
    assert scope.mappings[0].credential_id == credential
    data["snapshot_sha256"] = "c" * 64
    with pytest.raises(cli.OperatorRefusal, match="snapshot_mismatch"):
        cli._load_scope(json.dumps(data).encode(), manifest(tenant))


def test_extra_secret_field_is_rejected_not_serialized():
    data = {"version": 1, "tenant_id": str(uuid4()), "password_hash": "must-not-appear"}
    with pytest.raises(cli.OperatorRefusal) as exc:
        cli._load_scope(json.dumps(data).encode(), manifest(uuid4()))
    assert "must-not-appear" not in str(exc.value)


def arguments(tmp_path):
    return [
        "--manifest",
        str(tmp_path / "manifest.json"),
        "--snapshot-file",
        str(tmp_path / "snapshot.dump"),
        "--allowlist",
        str(tmp_path / "allowlist.json"),
        "--receipt",
        str(tmp_path / "receipt.json"),
    ]


def test_existing_receipt_blocks_before_any_database_action(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    path.write_text("original evidence")
    monkeypatch.setattr(cli, "_run", lambda args: pytest.fail("must not run"))
    assert cli.main(arguments(tmp_path)) == 2
    assert path.read_text() == "original evidence"


def test_apply_is_explicit_and_never_connects_on_missing_authorization(tmp_path, monkeypatch):
    monkeypatch.delenv("ERP_IDENTITY_IMPORT_APPROVED", raising=False)
    monkeypatch.setattr(cli, "_run", lambda args: pytest.fail("must not run"))
    assert cli.main([*arguments(tmp_path), "--apply"]) == 2
    assert not (tmp_path / "receipt.json").exists()


def test_unknown_error_receipt_has_no_exception_material_and_is_private(tmp_path, monkeypatch, capsys):
    def fail(args):
        raise ValueError("sensitive-hash-and-email")

    monkeypatch.setattr(cli, "_run", fail)
    assert cli.main(arguments(tmp_path)) == 2
    receipt = tmp_path / "receipt.json"
    assert json.loads(receipt.read_text()) == {"ready": False, "refusal": "identity_command_refused"}
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert "sensitive-hash-and-email" not in capsys.readouterr().out


@pytest.fixture
def source_rows():
    from erp_v2.importer.identity import IdentityMapping, IdentityScope

    engine = sa.create_engine("sqlite://", hide_parameters=True)
    metadata = sa.MetaData()
    people = sa.Table(
        "people",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid()),
        sa.Column("email", sa.String()),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("status", sa.String()),
    )
    credentials = sa.Table(
        "user_credentials",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("person_id", sa.Uuid()),
        sa.Column("password_hash", sa.String()),
        sa.Column("provider", sa.String()),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("must_change_password", sa.Boolean()),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("failed_login_attempts", sa.Integer()),
    )
    mfa = sa.Table(
        "mfa_methods",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("person_id", sa.Uuid()),
        sa.Column("enabled", sa.Boolean()),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("secret", sa.String()),
    )
    metadata.create_all(engine)
    tenant, person, credential, employee = (uuid4() for _ in range(4))
    scope = IdentityScope(tenant, (IdentityMapping(credential, person, person, employee),))
    with engine.begin() as connection:
        connection.execute(
            people.insert().values(
                id=person, organization_id=tenant, email="fixture@example.test", is_active=True, status="active"
            )
        )
        connection.execute(
            credentials.insert().values(
                id=credential,
                person_id=person,
                password_hash="fixture-material-not-for-output",  # noqa: S106 - invalid noncredential sentinel
                provider="local",
                is_active=True,
                must_change_password=False,
                locked_until=None,
                failed_login_attempts=0,
            )
        )
        yield SimpleNamespace(
            connection=connection,
            people=people,
            credentials=credentials,
            mfa=mfa,
            scope=scope,
            tenant=tenant,
            person=person,
        )
    engine.dispose()


def test_real_source_query_returns_only_explicit_supported_cohort(source_rows):
    records = cli._source_records(source_rows.connection, source_rows.scope, "main")
    assert len(records) == 1
    assert records[0].person_id == source_rows.person
    assert "fixture-material-not-for-output" not in repr(records)


@pytest.mark.parametrize(
    "change",
    [
        "tenant",
        "person-inactive",
        "person-status",
        "reset",
        "lock",
        "failed",
        "mfa",
        "missing-person",
        "duplicate-email",
    ],
)
def test_source_query_preserves_access_restrictions_by_refusing_cohort(source_rows, change):
    f = source_rows
    if change == "tenant":
        f.connection.execute(f.people.update().values(organization_id=uuid4()))
    elif change == "person-inactive":
        f.connection.execute(f.people.update().values(is_active=False))
    elif change == "person-status":
        f.connection.execute(f.people.update().values(status="archived"))
    elif change == "reset":
        f.connection.execute(f.credentials.update().values(must_change_password=True))
    elif change == "lock":
        f.connection.execute(f.credentials.update().values(locked_until=datetime.now(UTC) + timedelta(days=1)))
    elif change == "failed":
        f.connection.execute(f.credentials.update().values(failed_login_attempts=1))
    elif change == "mfa":
        f.connection.execute(
            f.mfa.insert().values(
                id=uuid4(),
                person_id=f.person,
                enabled=True,
                is_active=True,
                secret="must-not-be-selected",  # noqa: S106 - unread test sentinel
            )
        )
    elif change == "missing-person":
        f.connection.execute(f.people.delete())
    else:
        f.connection.execute(
            f.people.insert().values(
                id=uuid4(), organization_id=f.tenant, email="FIXTURE@example.test", is_active=True, status="active"
            )
        )
    with pytest.raises(cli.SourceEligibilityRefusal) as exc:
        cli._source_records(f.connection, f.scope, "main")
    assert exc.value.blockers
    assert "fixture-material-not-for-output" not in str(exc.value)
    assert "must-not-be-selected" not in str(exc.value)


def bound_files(tmp_path):
    tenant, credential, person, employee = (uuid4() for _ in range(4))
    snapshot = tmp_path / "snapshot.dump"
    snapshot.write_bytes(b"synthetic snapshot fixture")
    data = {
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "source_database": "fixture_source",
        "destination_database": "fixture_destination",
        "candidate_revision": "b" * 40,
        "tenant_extent": [str(tenant)],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(data))
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": str(tenant),
                "snapshot_sha256": data["snapshot_sha256"],
                "candidate_revision": data["candidate_revision"],
                "mappings": [
                    {
                        "credential_id": str(credential),
                        "person_id": str(person),
                        "party_id": str(person),
                        "employee_id": str(employee),
                    }
                ],
            }
        )
    )
    return SimpleNamespace(
        manifest=manifest_path,
        snapshot_file=snapshot,
        allowlist=allowlist,
        apply=True,
        source_schema="public",
        approved_allowlist_sha256=cli._sha(allowlist),
        approved_manifest_sha256=cli._sha(manifest_path),
    )


@pytest.mark.parametrize("field", ["source_database", "destination_database"])
def test_substituted_database_manifest_refuses_before_connection(tmp_path, monkeypatch, field):
    args = bound_files(tmp_path)
    data = json.loads(args.manifest.read_text())
    data[field] = "substituted_database"
    args.manifest.write_text(json.dumps(data))
    monkeypatch.setattr(cli, "_safe_engine", lambda url: pytest.fail("must not connect"))
    with pytest.raises(cli.OperatorRefusal, match="manifest_review_binding_required"):
        cli._run(args)


def test_input_digests_bind_the_single_read_bytes(tmp_path, monkeypatch):
    args = bound_files(tmp_path)
    args.apply = False
    monkeypatch.delenv("LEGACY_DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    original = type(args.manifest).read_bytes
    counts = {}

    def read_once(path):
        if path in (args.manifest, args.allowlist):
            counts[path] = counts.get(path, 0) + 1
            assert counts[path] == 1
        return original(path)

    monkeypatch.setattr(type(args.manifest), "read_bytes", read_once)
    with pytest.raises(cli.OperatorRefusal, match="operator_database_materials_required"):
        cli._run(args)
    assert counts == {args.manifest: 1, args.allowlist: 1}
