"""Private operator plan/apply for explicit snapshot-bound staff enrolment.

DSNs are environment-only; receipts contain metadata/counts/codes, never source
identities or credential material. A restore receipt must independently prove
that the named read-only source database came from the checked snapshot file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session


class OperatorRefusal(ValueError):
    pass


def _refuse(code: str):
    raise OperatorRefusal(code)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(raw: bytes):
    from erp_v2.importer.snapshot import SnapshotManifest

    data = json.loads(raw)
    required = {"snapshot_sha256", "source_database", "destination_database", "candidate_revision", "tenant_extent"}
    if not isinstance(data, dict) or set(data) != required:
        _refuse("invalid_manifest")
    if not re.fullmatch(r"[a-f0-9]{64}", str(data["snapshot_sha256"])):
        _refuse("invalid_snapshot_digest")
    if not all(
        isinstance(data[key], str) and data[key].strip() == data[key] and data[key]
        for key in ("source_database", "destination_database")
    ):
        _refuse("invalid_database_binding")
    if data["source_database"] == data["destination_database"]:
        _refuse("distinct_databases_required")
    if not isinstance(data["tenant_extent"], list) or len(data["tenant_extent"]) != 1:
        _refuse("single_tenant_extent_required")
    return SnapshotManifest(
        data["snapshot_sha256"],
        data["source_database"],
        data["destination_database"],
        data["candidate_revision"],
        tuple(data["tenant_extent"]),
    )


def _migration_permissions():
    from dotmac_kernel.permission_provisioning import PermissionPlan, RoleDefinition, RoleGrant, RoleGrantProfile

    from erp_v2.assembly import assembly

    # Explicit operator-only role bootstrap; runtime's closed creation policy
    # and admin grants are neither inherited nor changed by this migration.
    return PermissionPlan.from_manifests(
        assembly.modules,
        roles=(RoleDefinition("staff", create_if_missing=True),),
        profiles=(
            RoleGrantProfile(
                code="erp.identity-enrolment",
                version=1,
                grants=(RoleGrant("staff", "erp.web.access"),),
            ),
        ),
    )


def _load_scope(raw_bytes: bytes, manifest):
    from erp_v2.importer.identity import IdentityMapping, IdentityScope

    raw = json.loads(raw_bytes)
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "tenant_id",
        "snapshot_sha256",
        "candidate_revision",
        "mappings",
    }:
        _refuse("invalid_allowlist_schema")
    if raw["version"] != 1 or raw["snapshot_sha256"] != manifest.snapshot_sha256:
        _refuse("allowlist_snapshot_mismatch")
    if raw["candidate_revision"] != manifest.candidate_revision:
        _refuse("allowlist_candidate_mismatch")
    tenant = UUID(raw["tenant_id"])
    if set(manifest.tenant_extent) != {str(tenant)}:
        _refuse("tenant_extent_mismatch")
    if not isinstance(raw["mappings"], list) or not 1 <= len(raw["mappings"]) <= 1000:
        _refuse("explicit_bounded_cohort_required")
    mappings = []
    for row in raw["mappings"]:
        if not isinstance(row, dict) or set(row) != {"credential_id", "person_id", "party_id", "employee_id"}:
            _refuse("invalid_identity_mapping")
        mappings.append(IdentityMapping(**{key: UUID(value) for key, value in row.items()}))
    return IdentityScope(tenant, tuple(mappings))


def _safe_engine(url: str):
    engine = sa.create_engine(url, echo=False, hide_parameters=True, isolation_level="SERIALIZABLE")
    if engine.dialect.name != "postgresql":
        engine.dispose()
        _refuse("postgresql_required")
    if engine.logger.isEnabledFor(logging.INFO):
        engine.dispose()
        _refuse("unsafe_sql_logging")
    return engine


class SourceEligibilityRefusal(OperatorRefusal):
    def __init__(self, blockers):
        super().__init__("source_eligibility_blocked")
        self.blockers = dict(blockers)


def _source_records(connection, scope, schema: str):
    from dotmac_kernel.identity import normalize_email

    from erp_v2.importer.identity import LegacyIdentity

    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", schema):
        _refuse("invalid_source_schema")
    metadata = sa.MetaData()
    credentials = sa.Table("user_credentials", metadata, schema=schema, autoload_with=connection)
    people = sa.Table("people", metadata, schema=schema, autoload_with=connection)
    mfa = sa.Table("mfa_methods", metadata, schema=schema, autoload_with=connection)
    if not {
        "id",
        "person_id",
        "provider",
        "password_hash",
        "is_active",
        "must_change_password",
        "locked_until",
        "failed_login_attempts",
    } <= set(credentials.c.keys()):
        _refuse("source_credential_shape_mismatch")
    if not {"id", "email", "organization_id", "is_active", "status"} <= set(people.c.keys()):
        _refuse("source_person_shape_mismatch")
    if not {"person_id", "enabled", "is_active"} <= set(mfa.c.keys()):
        _refuse("source_mfa_shape_mismatch")
    query = (
        sa.select(
            sa.type_coerce(credentials.c.id, sa.Uuid()).label("credential_id"),
            sa.type_coerce(credentials.c.person_id, sa.Uuid()).label("person_id"),
            sa.type_coerce(people.c.organization_id, sa.Uuid()).label("organization_id"),
            people.c.email,
            people.c.is_active.label("person_active"),
            people.c.status.label("person_status"),
            credentials.c.password_hash,
            credentials.c.provider,
            credentials.c.is_active,
            credentials.c.must_change_password,
            credentials.c.locked_until,
            credentials.c.failed_login_attempts,
        )
        .select_from(credentials.outerjoin(people, credentials.c.person_id == people.c.id))
        .where(sa.type_coerce(credentials.c.id, sa.Uuid()).in_([row.credential_id for row in scope.mappings]))
        .order_by(credentials.c.id)
    )
    records = []
    blockers = Counter()
    now = datetime.now(UTC)
    for row in connection.execute(query):
        row_blockers = []
        if row.organization_id != scope.tenant_id:
            row_blockers.append("source_tenant_mismatch")
        if row.person_active is not True or row.person_status != "active":
            row_blockers.append("source_person_inactive")
        if row.must_change_password not in (None, False):
            row_blockers.append("source_password_change_required")
        if row.failed_login_attempts != 0:
            row_blockers.append("source_failed_attempt_state")
        if row.locked_until is not None:
            if row.locked_until.tzinfo is None or row.locked_until.utcoffset() is None:
                row_blockers.append("source_lock_state_unverified")
            elif row.locked_until > now:
                row_blockers.append("source_account_locked")
        if connection.scalar(
            sa.select(sa.func.count())
            .select_from(mfa)
            .where(
                sa.type_coerce(mfa.c.person_id, sa.Uuid()) == row.person_id,
                mfa.c.enabled.is_(True),
                mfa.c.is_active.is_(True),
            )
        ):
            # No factor secret is selected or copied, and migration must not
            # turn a protected source account into password-only login.
            row_blockers.append("source_mfa_requires_supported_flow")
        email = row.email or ""
        matches = connection.scalar(
            sa.select(sa.func.count())
            .select_from(people)
            .where(
                sa.func.lower(sa.func.trim(people.c.email)) == normalize_email(email),
            )
        )
        if not email or matches != 1:
            row_blockers.append("ambiguous_source_email")
        if row_blockers:
            blockers.update(row_blockers)
            continue
        records.append(
            LegacyIdentity(row.credential_id, row.person_id, email, row.password_hash, row.provider, row.is_active)
        )
    if blockers:
        raise SourceEligibilityRefusal(blockers)
    return tuple(records)


def _run(args) -> dict:
    # Read once: the reviewed digest must bind the exact bytes parsed,
    # not a second filesystem read that can race a scope replacement.
    manifest_bytes = args.manifest.read_bytes()
    allowlist_bytes = args.allowlist.read_bytes()
    manifest = _manifest(manifest_bytes)
    if not re.fullmatch(r"[a-f0-9]{40}", manifest.candidate_revision):
        _refuse("full_candidate_revision_required")
    if _sha(args.snapshot_file) != manifest.snapshot_sha256:
        _refuse("snapshot_digest_mismatch")
    scope = _load_scope(allowlist_bytes, manifest)
    allowlist_sha = hashlib.sha256(allowlist_bytes).hexdigest()
    if args.apply and args.approved_allowlist_sha256 != allowlist_sha:
        _refuse("allowlist_review_binding_required")
    if args.apply and args.approved_manifest_sha256 != hashlib.sha256(manifest_bytes).hexdigest():
        _refuse("manifest_review_binding_required")
    # Remove owner/source materials before importing the serving assembly.
    source_url = os.environ.pop("LEGACY_DATABASE_URL", "")
    destination_url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    if not source_url or not destination_url:
        _refuse("operator_database_materials_required")
    if "DATABASE_URL" in os.environ or "PLATFORM_DATABASE_URL" in os.environ:
        _refuse("serving_database_materials_forbidden")
    from erp_v2.importer.identity import apply_identity_enrolment, plan_identity_enrolment

    permissions = _migration_permissions()
    source = _safe_engine(source_url)
    try:
        destination = _safe_engine(destination_url)
        try:
            with source.connect() as source_connection, source_connection.begin():
                source_connection.execute(sa.text("SET TRANSACTION READ ONLY"))
                if source_connection.scalar(sa.text("SELECT current_database()")) != manifest.source_database:
                    _refuse("source_database_mismatch")
                records = _source_records(source_connection, scope, "public")
                with Session(destination) as db, db.begin():
                    if db.scalar(sa.text("SELECT current_database()")) != manifest.destination_database:
                        _refuse("destination_database_mismatch")
                    plan = plan_identity_enrolment(db, scope, records, permissions=permissions)
                    payload = {
                        "mode": "apply" if args.apply else "plan",
                        "bindings": {
                            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                            "snapshot_sha256": manifest.snapshot_sha256,
                            "allowlist_sha256": allowlist_sha,
                            "candidate_revision": manifest.candidate_revision,
                        },
                        "identity": plan.sanitized_receipt(),
                    }
                    if args.apply:
                        apply_identity_enrolment(db, scope, records, permissions=permissions, reviewed_plan=plan)
                    else:
                        db.rollback()
                    return payload
        finally:
            destination.dispose()
    finally:
        source.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.importer.identity_cli")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--snapshot-file", required=True, type=Path)
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-allowlist-sha256")
    parser.add_argument("--approved-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if args.apply and os.environ.get("ERP_IDENTITY_IMPORT_APPROVED") != "1":
            _refuse("explicit_identity_apply_required")
        # Reserve receipt first: existing evidence must block BEFORE mutation.
        fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception:
        print("identity command refused: apply authorization or receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = _run(args)
            code = 0 if payload["identity"]["ready"] else 3
        except SourceEligibilityRefusal as exc:
            payload = {"ready": False, "refusal": "source_eligibility_blocked", "blockers": exc.blockers}
            code = 2
        except OperatorRefusal as exc:
            payload = {"ready": False, "refusal": str(exc)}
            code = 2
        except Exception:
            # Never print DBAPI/row/parser diagnostics containing credentials.
            payload = {"ready": False, "refusal": "identity_command_refused"}
            code = 2
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    print("identity receipt written" if code == 0 else "identity command refused; see private receipt")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
