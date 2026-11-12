"""Private, explicit historical payables cohort plan/apply operator boundary.

A checked snapshot file binds bytes held by the operator; the operator's restore
receipt must independently attest that the named source database came from those
bytes. This command creates no settlement or Accounting posting. Candidate code
must only be deployed against the released owner version containing admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer.identity_cli import _manifest, _safe_engine, _sha


class OperatorRefusal(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise OperatorRefusal(reason)


def _json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, OperatorRefusal):
            raise
        raise OperatorRefusal("invalid_json_document") from exc
    _require(isinstance(value, dict), "json_object_required")
    return value


def _uuid(value, reason: str) -> UUID:
    try:
        ident = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise OperatorRefusal(reason) from exc
    _require(str(ident) == value, reason)
    return ident


def _cohort(raw: bytes, manifest) -> tuple[UUID, tuple[UUID, ...]]:
    data = _json(raw)
    _require(
        set(data) == {"version", "tenant_id", "snapshot_sha256", "candidate_revision", "invoice_ids"},
        "invalid_cohort_schema",
    )
    _require(type(data["version"]) is int and data["version"] == 1, "invalid_cohort_version")
    _require(data["snapshot_sha256"] == manifest.snapshot_sha256, "cohort_snapshot_mismatch")
    _require(data["candidate_revision"] == manifest.candidate_revision, "cohort_candidate_mismatch")
    tenant = _uuid(data["tenant_id"], "invalid_tenant_id")
    _require(tuple(manifest.tenant_extent) == (str(tenant),), "tenant_extent_mismatch")
    rows = data["invoice_ids"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= 1000, "explicit_bounded_cohort_required")
    ids = tuple(_uuid(value, "invalid_invoice_id") for value in rows)
    _require(len(set(ids)) == len(ids), "duplicate_invoice_id")
    return tenant, tuple(sorted(ids, key=str))


def _reviewed(args) -> dict | None:
    if not args.apply:
        return None
    _require(
        args.reviewed_plan is not None and bool(re.fullmatch(r"[0-9a-f]{64}", args.reviewed_plan_sha256 or "")),
        "reviewed_plan_required",
    )
    raw = args.reviewed_plan.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == args.reviewed_plan_sha256, "reviewed_plan_digest_mismatch")
    return _json(raw)


def _command_json(command) -> dict:
    return dict(
        source_owner=command.source_owner,
        source_invoice_id=command.source_invoice_id,
        invoice_id=str(command.invoice_id),
        source_snapshot_sha256=command.source_snapshot_sha256,
        evidence_fingerprint=command.evidence_fingerprint,
    )


def _plan(verifier, scope, invoice_ids, session, bindings: dict) -> dict:
    commands, refusals = [], []
    for invoice_id in invoice_ids:
        try:
            evidence = verifier.evidence(scope=scope, invoice_id=invoice_id, destination=session)
            commands.append(_command_json(verifier.command(evidence)))
        except ValueError as exc:
            reason = getattr(exc, "reason", None)
            # Only deliberately named evidence failures enter private receipts.
            # Unexpected parser/driver errors are not converted to business facts.
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_:.-]*", reason):
                raise
            refusals.append(dict(invoice_id=str(invoice_id), reason=reason))
    return dict(
        version=1,
        mode="plan",
        bindings=bindings,
        production_ready=False,
        counts=dict(requested=len(invoice_ids), eligible=len(commands), refused=len(refusals)),
        commands=commands,
        refusals=refusals,
    )


def _apply(session, scope, verifier, plan: dict, reviewed: dict, admit, *, command_type=None) -> None:
    _require(plan == reviewed, "reviewed_plan_changed")
    _require(not plan["refusals"], "cohort_not_fully_eligible")
    if command_type is None:
        from dotmac_payables import HistoricalUnpaidInvoiceCommand

        command_type = HistoricalUnpaidInvoiceCommand
    for raw in plan["commands"]:
        command = command_type(**(raw | {"invoice_id": UUID(raw["invoice_id"])}))
        identity = json.dumps(
            {key: raw[key] for key in ("source_owner", "source_invoice_id", "source_snapshot_sha256")},
            sort_keys=True,
            separators=(",", ":"),
        )
        key = "erp.historical-payables:" + hashlib.sha256(identity.encode()).hexdigest()
        admit(
            session,
            scope=scope,
            command=command,
            verifier=verifier,
            idempotency_key=key,
            idempotency_expires_at=None,
            admitted_at=datetime.now(UTC),
        )


def _dependencies():
    # Called only after both operator DSNs have been removed from the environment.
    from dotmac_kernel.cache import TenantScope
    from dotmac_payables import HistoricalUnpaidInvoiceCommand, admit_historical_unpaid_invoice

    from erp_v2.importer.payables_history import HistoricalPayablesVerifier

    return TenantScope, HistoricalPayablesVerifier, admit_historical_unpaid_invoice, HistoricalUnpaidInvoiceCommand


def _installed_payables_version() -> str:
    return version("dotmac-payables")


def _expected_schema_heads() -> set[str]:
    from erp_v2.migrate import EXPECTED_STAMPED_HEADS

    return set(EXPECTED_STAMPED_HEADS)


def _destination_preflight(session, tenant: UUID) -> None:
    """Check the actual admission connection, then bind its transaction to scope."""
    posture = (
        session.execute(
            sa.text(
                "WITH RECURSIVE role_closure(roleid) AS ("
                " SELECT oid FROM pg_catalog.pg_roles WHERE rolname IN (current_user, session_user)"
                " UNION SELECT am.roleid FROM pg_catalog.pg_auth_members am"
                " JOIN role_closure c ON am.member=c.roleid)"
                " SELECT current_user AS current_role_name, session_user AS session_role_name,"
                " (SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname IN (current_user, session_user))"
                " AS identities_found,"
                " (SELECT count(*) FROM pg_catalog.pg_roles r JOIN role_closure c ON r.oid=c.roleid"
                " WHERE r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolreplication OR r.rolcreatedb"
                " OR r.rolname IN ('pg_execute_server_program','pg_read_server_files','pg_write_server_files'))"
                " AS privileged_roles,"
                " (SELECT count(*) FROM pg_catalog.pg_class r JOIN pg_catalog.pg_namespace n ON n.oid=r.relnamespace"
                " WHERE n.nspname IN ('public','mod_payables','mod_accounting')"
                " AND r.relowner IN (SELECT roleid FROM role_closure))"
                " + (SELECT count(*) FROM pg_catalog.pg_namespace n"
                " WHERE n.nspname IN ('public','mod_payables','mod_accounting')"
                " AND n.nspowner IN (SELECT roleid FROM role_closure))"
                " + (SELECT count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace"
                " WHERE n.nspname IN ('public','mod_payables','mod_accounting')"
                " AND p.proowner IN (SELECT roleid FROM role_closure))"
                " + (SELECT count(*) FROM pg_catalog.pg_database d WHERE d.datname=current_database()"
                " AND d.datdba IN (SELECT roleid FROM role_closure)) AS owned_objects"
            )
        )
        .mappings()
        .one()
    )
    _require(
        isinstance(posture["current_role_name"], str)
        and bool(posture["current_role_name"])
        and posture["current_role_name"] == posture["session_role_name"]
        and posture["identities_found"] == 1
        and posture["privileged_roles"] == 0
        and posture["owned_objects"] == 0,
        "destination_role_not_nonprivileged",
    )
    expected = _expected_schema_heads()
    _require(
        session.scalar(sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")) is True,
        "destination_schema_heads_mismatch",
    )
    actual = list(session.scalars(sa.text("SELECT version_num FROM public.alembic_version")))
    _require(
        "pa_0003_historical_invoice" in expected and len(actual) == len(set(actual)) and set(actual) == expected,
        "destination_schema_heads_mismatch",
    )
    selected = session.scalar(
        sa.text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant)}
    )
    observed = session.scalar(sa.text("SELECT public.app_current_tenant_id()"))
    _require(selected == str(tenant) and str(observed) == str(tenant), "destination_tenant_binding_mismatch")


def _authorized(args) -> bool:
    return not args.apply or (
        os.environ.get("ERP_IMPORT_APPROVED") == "1" and os.environ.get("ERP_IMPORT_APPLY_OK") == "1"
    )


def _run(args) -> dict:
    source_url = os.environ.pop("LEGACY_DATABASE_URL", "")
    destination_url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    _require(_authorized(args), "explicit_apply_authorization_required")
    _require(bool(source_url) and bool(destination_url), "operator_database_materials_required")
    _require(
        "DATABASE_URL" not in os.environ and "PLATFORM_DATABASE_URL" not in os.environ,
        "serving_database_materials_forbidden",
    )
    # Read each binding once. The held reviewed-plan bytes are both hashed and
    # parsed; no second path read can substitute another approved cohort.
    manifest_bytes, cohort_bytes = args.manifest.read_bytes(), args.cohort.read_bytes()
    manifest_data = _json(manifest_bytes)
    try:
        manifest = _manifest(json.dumps(manifest_data).encode())
    except ValueError as exc:
        raise OperatorRefusal("invalid_manifest") from exc
    _require(
        isinstance(manifest.candidate_revision, str)
        and bool(re.fullmatch(r"[0-9a-f]{40}", manifest.candidate_revision)),
        "full_candidate_revision_required",
    )
    snapshot_sha = _sha(args.snapshot_file)
    _require(snapshot_sha == manifest.snapshot_sha256, "snapshot_digest_mismatch")
    tenant, ids = _cohort(cohort_bytes, manifest)
    reviewed = _reviewed(args)
    bindings = dict(
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        cohort_sha256=hashlib.sha256(cohort_bytes).hexdigest(),
        snapshot_sha256=snapshot_sha,
        candidate_revision=manifest.candidate_revision,
        tenant_id=str(tenant),
        source_database=manifest.source_database,
        destination_database=manifest.destination_database,
        source_owner="legacy-erp",
    )
    _require(_installed_payables_version() == "0.1.0a3", "released_payables_version_required")
    scope_type, verifier_type, admit, command_type = _dependencies()
    scope = scope_type(tenant)
    source_engine = _safe_engine(source_url)
    try:
        destination_engine = _safe_engine(destination_url)
        try:
            with source_engine.connect() as source, source.begin():
                source.execute(sa.text("SET TRANSACTION READ ONLY"))
                # Refuse any source RLS filtering instead of accepting partial evidence.
                source.execute(sa.text("SET LOCAL row_security = off"))
                _require(
                    source.scalar(sa.text("SELECT current_database()")) == manifest.source_database,
                    "source_database_mismatch",
                )
                verifier = verifier_type(source, manifest)
                with Session(destination_engine) as session, session.begin():
                    if not args.apply:
                        session.execute(sa.text("SET TRANSACTION READ ONLY"))
                    _require(
                        session.scalar(sa.text("SELECT current_database()")) == manifest.destination_database,
                        "destination_database_mismatch",
                    )
                    _destination_preflight(session, tenant)
                    plan = _plan(verifier, scope, ids, session, bindings)
                    if args.apply:
                        _apply(session, scope, verifier, plan, reviewed, admit, command_type=command_type)
                    # Plan transaction has been forced read-only. Context exit
                    # commits no writes; apply exits once for the entire cohort.
                return (
                    plan
                    if not args.apply
                    else dict(
                        plan,
                        mode="apply",
                        admitted=len(plan["commands"]),
                        reviewed_plan_sha256=args.reviewed_plan_sha256,
                    )
                )
        finally:
            destination_engine.dispose()
    finally:
        source_engine.dispose()


def _receipt(path: Path) -> int:
    # Refuse symlinked parents as well as existing/symlinked final paths. Reserve
    # before any database action so an output collision cannot follow admission.
    _require(path.parent.resolve(strict=True) == Path(os.path.abspath(path.parent)), "receipt_symlink_forbidden")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.importer.payables_history_cli")
    for name in ("manifest", "snapshot-file", "cohort", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed-plan", type=Path)
    parser.add_argument("--reviewed-plan-sha256")
    args = parser.parse_args(argv)
    try:
        _require(_authorized(args), "explicit_apply_authorization_required")
        fd = _receipt(args.receipt)
    except Exception:
        print("historical payables refused: authorization or private receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = _run(args)
            code = 0 if payload["counts"]["refused"] == 0 else 3
        except OperatorRefusal as exc:
            payload, code = dict(production_ready=False, refusal=exc.reason), 2
        except Exception:
            payload, code = dict(production_ready=False, refusal="historical_payables_command_refused"), 2
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    counts = payload.get("counts", {})
    print(
        "historical payables: "
        + (
            f"requested={counts.get('requested', 0)} eligible={counts.get('eligible', 0)} "
            f"refused={counts.get('refused', 0)}"
            if counts
            else "command refused"
        )
        + "; production_ready=false"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
