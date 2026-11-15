"""Operator load of the census never-posted cohort that enables legacy AP re-entry.

The flip-window census classes legacy open invoices (R4/R6) that legacy NEVER posted. The
operator hands this command that list as a JSON file plus its sha256; the command refuses
unless the digest matches the bytes held, and writes the cohort once per tenant. Until a
cohort is loaded the re-entry route is off. Plan mode is read-only; apply needs
``--apply`` AND ERP_IMPORT_APPROVED=1 AND ERP_IMPORT_APPLY_OK=1. Reloading the identical
cohort is a no-op; a different one is refused (superseding needs a separate ruling).
The receipt holds counts and the digest, never invoice ids.

Cohort file: {"version": 1, "tenant_id": "<uuid>", "census_ref": "<text>",
"legacy_invoice_ids": ["<canonical uuid>", ...]}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.legacy_ap.models import LegacyNeverPostedCohort, LegacyNeverPostedMember

MAX_MEMBERS = 50_000


class CohortRefusal(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise CohortRefusal(reason)


def parse_cohort(raw: bytes, expected_sha256: str, tenant_id: UUID) -> tuple[str, tuple[UUID, ...]]:
    _require(bool(re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "")), "cohort_sha256_required")
    _require(hashlib.sha256(raw).hexdigest() == expected_sha256, "cohort_digest_mismatch")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        data = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, CohortRefusal):
            raise
        raise CohortRefusal("invalid_json_document") from exc
    _require(
        isinstance(data, dict) and set(data) == {"version", "tenant_id", "census_ref", "legacy_invoice_ids"},
        "invalid_cohort_schema",
    )
    _require(type(data["version"]) is int and data["version"] == 1, "invalid_cohort_version")
    _require(data["tenant_id"] == str(tenant_id), "cohort_tenant_mismatch")
    ref = data["census_ref"]
    _require(
        isinstance(ref, str) and bool(ref.strip()) and ref == ref.strip() and len(ref) <= 255, "census_ref_required"
    )
    rows = data["legacy_invoice_ids"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= MAX_MEMBERS, "bounded_cohort_required")
    ids = []
    for value in rows:
        try:
            ident = UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise CohortRefusal("invalid_legacy_invoice_id") from exc
        _require(str(ident) == value, "invalid_legacy_invoice_id")
        ids.append(ident)
    _require(len(set(ids)) == len(ids), "duplicate_legacy_invoice_id")
    return ref, tuple(sorted(ids, key=str))


def load_cohort(
    db: Session,
    *,
    tenant_id: UUID,
    census_ref: str,
    ids: tuple[UUID, ...],
    sha256: str,
    operator_ref: str,
    write: bool,
) -> dict:
    """Caller owns the transaction. ``write`` False validates only."""
    _require(bool(operator_ref and operator_ref.strip()) and len(operator_ref) <= 255, "operator_ref_required")
    db.execute(sa.text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_id)})
    _require(
        db.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM public.tenants WHERE id = :t)"), {"t": tenant_id}) is True,
        "tenant_unknown",
    )
    existing = db.scalar(sa.select(LegacyNeverPostedCohort).where(LegacyNeverPostedCohort.tenant_id == tenant_id))
    if existing is not None:
        _require(existing.cohort_sha256 == sha256, "different_cohort_already_loaded")
        return dict(status="already_loaded", members=existing.member_count, cohort_sha256=sha256)
    if write:
        cohort = LegacyNeverPostedCohort(
            id=uuid4(),
            tenant_id=tenant_id,
            cohort_sha256=sha256,
            census_ref=census_ref,
            member_count=len(ids),
            loaded_by=operator_ref.strip(),
        )
        db.add(cohort)
        db.flush()
        db.add_all(LegacyNeverPostedMember(tenant_id=tenant_id, cohort_id=cohort.id, legacy_invoice_id=i) for i in ids)
        db.flush()
    return dict(status="loaded" if write else "plan", members=len(ids), cohort_sha256=sha256)


def _authorized(args) -> bool:
    return not args.apply or (
        os.environ.get("ERP_IMPORT_APPROVED") == "1" and os.environ.get("ERP_IMPORT_APPLY_OK") == "1"
    )


def _run(args) -> dict:
    url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    _require(_authorized(args), "explicit_apply_authorization_required")
    _require(bool(url), "operator_database_materials_required")
    _require("DATABASE_URL" not in os.environ, "serving_database_materials_forbidden")
    try:
        tenant_id = UUID(args.tenant_id)
    except ValueError as exc:
        raise CohortRefusal("invalid_tenant_id") from exc
    _require(str(tenant_id) == args.tenant_id, "invalid_tenant_id")
    census_ref, ids = parse_cohort(args.cohort.read_bytes(), args.cohort_sha256, tenant_id)
    engine = sa.create_engine(url, echo=False, hide_parameters=True)
    try:
        _require(engine.dialect.name == "postgresql", "postgresql_required")
        with Session(engine) as db, db.begin():
            return load_cohort(
                db,
                tenant_id=tenant_id,
                census_ref=census_ref,
                ids=ids,
                sha256=args.cohort_sha256,
                operator_ref=args.operator_ref or "",
                write=args.apply,
            )
    finally:
        engine.dispose()


def _receipt(path: Path) -> int:
    _require(path.parent.resolve(strict=True) == Path(os.path.abspath(path.parent)), "receipt_symlink_forbidden")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.legacy_ap.cohort_cli")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--cohort", required=True, type=Path)
    parser.add_argument("--cohort-sha256", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--operator-ref")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        _require(_authorized(args), "explicit_apply_authorization_required")
        fd = _receipt(args.receipt)
    except Exception:
        print("legacy cohort refused: authorization or private receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload, code = _run(args), 0
        except CohortRefusal as exc:
            payload, code = dict(refusal=exc.reason), 2
        except Exception:
            payload, code = dict(refusal="legacy_cohort_command_refused"), 2
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("legacy cohort: " + (payload.get("status") or "refused"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
