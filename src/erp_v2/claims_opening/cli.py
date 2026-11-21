"""Operator CLI: ``python -m erp_v2.claims_opening {plan|apply|verify}`` (S8 section 4).

Shape of ``legacy_ap/settled_cli`` and ``importer/ar_opening_cli``: env-only DSNs (LEGACY_DATABASE_URL read only,
MIGRATION_DATABASE_URL; both popped before use), private 0600 receipts reserved BEFORE any database action, dry run by
default, every step bound to the snapshot sha, the cutoff receipt sha and the reviewed plan sha. ``plan`` runs READ
ONLY on both sides. ``apply`` needs ``--reviewed-plan-sha256`` equal to the re-computed plan, ERP_IMPORT_APPROVED=1 and
ERP_IMPORT_APPLY_OK=1, and runs every admission plus the ONE reclass journal in ONE transaction. ``verify`` re-derives.

Receipts carry counts, sums, digests and (the apply receipt only) the E37 register rows: never names.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.claims_opening import core, reclass
from erp_v2.claims_opening.extract import ExtractRefused
from erp_v2.claims_opening.reasons import ClaimsRefusal
from erp_v2.migration_ledger.common import LedgerRefused

REQUIRED_EXPENSES_VERSION = "0.1.0a2"
REQUIRED_EXPENSES_HEAD = "ex_0002_claim_liabilities"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ClaimsRefusal(reason)


def _installed_version() -> str:
    return version("dotmac-expenses")


def _stamped_heads(db: Session) -> set[str]:
    return set(db.scalars(sa.text("SELECT version_num FROM public.alembic_version")))


def _authorized(args) -> bool:
    return args.mode != "apply" or (
        os.environ.get("ERP_IMPORT_APPROVED") == "1" and os.environ.get("ERP_IMPORT_APPLY_OK") == "1"
    )


def _receipt_fd(path: Path) -> int:
    _require(path.parent.resolve(strict=True) == Path(os.path.abspath(path.parent)), "receipt_symlink_forbidden")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def _write(fd: int, data: bytes) -> str:
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(data).hexdigest()


def load_cutoff_receipt(path: Path | None, sha: str | None, manifest) -> tuple[date, str]:
    """The importer's itemised file (its binding names the snapshot and the history cutoff). -> (cutoff, file sha)."""
    from erp_v2.legacy_ap.transform_manifest import TransformManifest, TransformManifestError

    _require(path is not None and bool(_HEX64.match(sha or "")), "prerequisite_receipt_missing")
    try:
        loaded = TransformManifest.load(path, sha)
    except TransformManifestError as exc:
        raise ClaimsRefusal("prerequisite_receipt_mismatch") from exc
    _require(loaded.snapshot_sha256 == manifest.snapshot_sha256 and loaded.history_cutoff is not None,
             "prerequisite_receipt_mismatch")  # fmt: skip
    return loaded.history_cutoff, sha


def _run(args) -> dict:
    from erp_v2.importer.identity_cli import _manifest, _safe_engine
    from erp_v2.importer.payables_history_cli import OperatorRefusal, _destination_preflight

    source_url = os.environ.pop("LEGACY_DATABASE_URL", "")
    destination_url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    _require(_authorized(args), "explicit_apply_authorization_required")
    _require(
        bool(destination_url) and (args.mode == "verify" or bool(source_url)), "operator_database_materials_required"
    )
    _require(
        "DATABASE_URL" not in os.environ and "PLATFORM_DATABASE_URL" not in os.environ,
        "serving_database_materials_forbidden",
    )
    tenant = UUID(args.tenant_id)
    _require(str(tenant) == args.tenant_id, "invalid_tenant_id")
    window = args.window_id
    _require(bool(window) and window == window.strip() and len(window) <= 120, "invalid_window_id")
    operator = args.operator_ref
    _require(bool(operator) and operator == operator.strip() and len(operator) <= 120, "operator_ref_required")
    capture = date.fromisoformat(args.capture_date)
    if args.mode == "apply":
        _require(bool(_HEX64.match(args.reviewed_plan_sha256 or "")), "reviewed_plan_required")
    _require(_installed_version() == REQUIRED_EXPENSES_VERSION, "released_expenses_version_required")
    manifest = _manifest(args.snapshot_manifest.read_bytes())
    _require(manifest.snapshot_sha256 == args.snapshot_sha256, "snapshot_digest_mismatch")
    _require(tuple(manifest.tenant_extent) == (str(tenant),), "tenant_extent_mismatch")
    cutoff, cutoff_sha = load_cutoff_receipt(args.cutoff_receipt, args.cutoff_receipt_sha256, manifest)
    bindings = dict(
        tenant_id=str(tenant),
        snapshot_sha256=manifest.snapshot_sha256,
        candidate_revision=manifest.candidate_revision,
        cutoff_receipt_sha256=cutoff_sha,
        history_cutoff=cutoff.isoformat(),
        operator_ref=operator,
        expenses_version=REQUIRED_EXPENSES_VERSION,
    )
    if args.mode == "apply":
        from erp_v2.claims_opening import audit as vocabulary

        vocabulary.install()
    dest_engine = _safe_engine(destination_url)
    try:
        if args.mode == "verify":
            with Session(dest_engine) as db, db.begin():
                db.execute(sa.text("SET TRANSACTION READ ONLY"))
                _preflight(db, tenant, _destination_preflight, OperatorRefusal)
                body = None
                if args.apply_receipt is not None:
                    body = json.loads(args.apply_receipt.read_bytes())
                return core.verify(db, tenant, window, body)
        from erp_v2.claims_opening.verifier import ClaimOpeningVerifier

        source_engine = _safe_engine(source_url)
        try:
            with source_engine.connect() as source, source.begin():
                source.execute(sa.text("SET TRANSACTION READ ONLY"))
                source.execute(sa.text("SET LOCAL row_security = off"))
                _require(
                    source.scalar(sa.text("SELECT current_database()")) == manifest.source_database,
                    "source_database_mismatch",
                )
                with Session(dest_engine) as db, db.begin():
                    if args.mode == "plan":
                        db.execute(sa.text("SET TRANSACTION READ ONLY"))
                    _require(
                        db.scalar(sa.text("SELECT current_database()")) == manifest.destination_database,
                        "destination_database_mismatch",
                    )
                    _preflight(db, tenant, _destination_preflight, OperatorRefusal)
                    verifier = ClaimOpeningVerifier(source, manifest, cutoff, operator)
                    _require(verifier._opening_posted(db.connection(), tenant), "prerequisite_opening_not_posted")
                    now = datetime.now(UTC)
                    if args.mode == "plan":
                        if reclass.posted_reclass(db, tenant) is None:
                            _require(core.owner_state_empty(db, tenant), "claims_owner_state_not_empty")
                        plan = core.build_plan(
                            db, source, verifier, tenant, window_id=window, capture_date=capture, bindings=bindings,
                            admitted_at=now,
                        )  # fmt: skip
                        return dict(plan=plan)
                    result = core.apply_plan(
                        db, source, verifier, tenant, window_id=window, capture_date=capture, bindings=bindings,
                        reviewed_digest=args.reviewed_plan_sha256, now=now,
                    )  # fmt: skip
                    # derived inside the transaction: a fragment that is not exact rolls the whole apply back
                    items = result["admitted_items"]
                    result["attribution"] = core.attribution_2045(items)
                    dr_2040 = core.journal_dr(db, tenant, result["journal_id"], result["accounts"]["2040"])
                    result["fragment"] = core.fragment_2040(items, result["items"], dr_2040)
                    return result
        finally:
            source_engine.dispose()
    finally:
        dest_engine.dispose()


def _preflight(db: Session, tenant: UUID, preflight, operator_refusal) -> None:
    try:
        preflight(db, tenant, ())
    except operator_refusal as exc:
        raise ClaimsRefusal(exc.reason) from exc
    _require(REQUIRED_EXPENSES_HEAD in _stamped_heads(db), "released_expenses_version_required")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.claims_opening")
    parser.add_argument("mode", choices=("plan", "apply", "verify"))
    for name in ("tenant-id", "window-id", "capture-date", "operator-ref"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--snapshot-manifest", type=Path)
    parser.add_argument("--snapshot-sha256")
    parser.add_argument("--cutoff-receipt", type=Path)
    parser.add_argument("--cutoff-receipt-sha256")
    parser.add_argument("--reviewed-plan-sha256")
    parser.add_argument("--plan-out", type=Path, help="private per-claim plan file (plan mode)")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--attribution-out", type=Path, help="the 2045 gl_attribution receipt (apply)")
    parser.add_argument("--fragment-out", type=Path, help="the claims-2040-fragment (apply)")
    parser.add_argument("--apply-receipt", type=Path, help="verify: the apply receipt to compare counts with")
    args = parser.parse_args(argv)
    try:
        _require(_authorized(args), "explicit_apply_authorization_required")
        if args.mode != "verify":
            _require(
                args.snapshot_manifest is not None and args.snapshot_sha256 is not None, "snapshot_binding_required"
            )
            _require(args.cutoff_receipt is not None, "prerequisite_receipt_missing")
        if args.mode == "plan":
            _require(args.plan_out is not None, "plan_out_required")
        if args.mode == "apply":
            _require(args.attribution_out is not None and args.fragment_out is not None, "outputs_required")
        outputs = (
            ("receipt", args.receipt),
            ("plan", args.plan_out),
            ("attr", args.attribution_out),
            ("frag", args.fragment_out),
        )
        fds = {k: _receipt_fd(p) for k, p in outputs if p}
    except Exception:
        print("claims opening refused: authorization or private output path")
        return 2
    code = 2
    try:
        result = _run(args)
        if args.mode == "plan":
            plan = result["plan"]
            _write(fds.pop("plan"), core.dumps(plan))
            payload = core.public_receipt(plan, mode="plan")
            code = 0
        elif args.mode == "verify":
            payload, code = dict(kind="claims-opening-verify", **result), 0 if result["ok"] else 3
        else:
            attr_sha = _write(fds.pop("attr"), core.dumps(result["attribution"]))
            frag_sha = _write(fds.pop("frag"), core.dumps(result["fragment"]))
            payload = core.apply_receipt(result, mode="apply", attribution_sha256=attr_sha, fragment_sha256=frag_sha)
            code = 0
    except (ClaimsRefusal, ExtractRefused, LedgerRefused) as exc:
        payload = dict(production_ready=False, refusal=getattr(exc, "reason", None) or getattr(exc, "code", "refused"))
    except Exception:
        payload = dict(production_ready=False, refusal="claims_opening_command_refused")
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt_sha = _write(fds.pop("receipt"), data)
    for fd in fds.values():
        os.close(fd)
    print(f"claims opening {args.mode}: " + ("ok" if code == 0 else "not ok") + f"; receipt sha256={receipt_sha}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
