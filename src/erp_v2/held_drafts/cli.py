"""Operator plan/apply for the FY2026 legacy held DRAFT journals (B-2). No UI, no runtime write path.

Michael's ruling (v1537): a rule-matched void counts as "reviewed against source". BANK_FEE drafts matched to
POSTED fee journals and bank statement lines on (bank GL, date, amount) are voided ``duplicate_of_posted_fee``;
AR drafts with a POSTED twin of equal amounts are voided ``already_posted_duplicate``; everything else goes to
the Finance queue. See ``erp_v2.held_drafts.rules`` for the exact rules and ``RULES_SHA256``.

    {"version": 1, "tenant_id": "<uuid>", "fiscal_year_start": "YYYY-MM-DD", "fiscal_year_end": "YYYY-MM-DD",
     "rules_sha256": "<RULES_SHA256 of the reviewed rules>", "evidence_ref": "<who/why, <=255>",
     "requester_party_id": "<uuid>", "approver_party_id": "<uuid>", "filed_vat_months": ["YYYY-MM", ...]}

    python -m erp_v2.held_drafts.cli plan  --spec S.json --receipt P.json --operator-ref <who>
    ERP_HELD_DRAFTS_APPLY=1 python -m erp_v2.held_drafts.cli apply --spec S.json --plan-receipt P.json \
        --approved-plan-sha256 <plan_sha256 in P.json> --receipt A.json --operator-ref <who>

DSNs are environment-only: ``MIGRATION_DATABASE_URL`` (destination) and ``LEGACY_DATABASE_URL`` (read-only legacy
snapshot, ``gl.journal_entry``). Plan writes nothing. Apply recomputes the plan and refuses on any drift, then
performs each decision through the governed paths (B2 legacy-held post; accounting a2 void) and records one
immutable decision row per draft. Receipts are 0600, created exclusively before any write, and carry journal ids
and sha256 evidence only (never the tenant id, DSNs or account codes). Voids are terminal by design (the owner
void has no undo); the only rollback is not to apply, and the migration downgrade refuses once decisions exist.
Dual control (per-tenant policy, default off): when on, the applying operator must differ from the planning
operator and the requester and approver parties must differ.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from erp_v2.held_drafts import rules
from erp_v2.held_drafts.service import ApplyInputs, HeldDraftsRefused, Scope

_EVIDENCE_MAX = 255
_REF_MAX = 120
_SPEC_KEYS = {
    "version",
    "tenant_id",
    "fiscal_year_start",
    "fiscal_year_end",
    "rules_sha256",
    "evidence_ref",
    "requester_party_id",
    "approver_party_id",
    "filed_vat_months",
}


def _refuse(code: str):
    raise HeldDraftsRefused(code)


def _uuid(value: object, code: str) -> UUID:
    try:
        parsed = UUID(str(value))
    except ValueError:
        _refuse(code)
    if str(parsed) != value:
        _refuse(code)
    return parsed


def _day(value: object, code: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        _refuse(code)
    if parsed.isoformat() != value:
        _refuse(code)
    return parsed


def parse_spec(raw_bytes: bytes) -> tuple[Scope, dict[str, Any]]:
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                _refuse("duplicate_json_key")
            out[key] = value
        return out

    try:
        raw = json.loads(raw_bytes, object_pairs_hook=unique)
    except HeldDraftsRefused:
        raise
    except (ValueError, UnicodeDecodeError):
        _refuse("invalid_spec_json")
    if not isinstance(raw, dict) or set(raw) != _SPEC_KEYS:
        _refuse("invalid_spec_schema")
    if type(raw["version"]) is not int or raw["version"] != 1:
        _refuse("invalid_spec_schema")
    tenant = _uuid(raw["tenant_id"], "invalid_tenant_id")
    start, end = (
        _day(raw["fiscal_year_start"], "invalid_fiscal_year"),
        _day(raw["fiscal_year_end"], "invalid_fiscal_year"),
    )
    if start >= end:
        _refuse("invalid_fiscal_year")
    if raw["rules_sha256"] != rules.RULES_SHA256:
        _refuse("rules_version_mismatch")
    evidence = raw["evidence_ref"]
    if (
        not isinstance(evidence, str)
        or not evidence.strip()
        or evidence != evidence.strip()
        or len(evidence) > _EVIDENCE_MAX
    ):
        _refuse("evidence_ref_required")
    months = raw["filed_vat_months"]
    if not isinstance(months, list) or any(
        not isinstance(m, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m) for m in months
    ):
        _refuse("invalid_filed_vat_months")
    requester = _uuid(raw["requester_party_id"], "invalid_requester_party_id")
    approver = _uuid(raw["approver_party_id"], "invalid_approver_party_id")
    return Scope(tenant, start, end, frozenset(months)), {"requester": requester, "approver": approver}


def _operator(value: str | None) -> str:
    if not value or not value.strip() or value != value.strip() or len(value) > _REF_MAX:
        _refuse("operator_ref_required")
    return value


def _bridge_lines(plan) -> list[dict[str, object]]:
    return [d.line() for d in sorted(plan.decisions, key=lambda d: str(d.journal_id))]


def _summary(plan, proof: dict[str, Any]) -> dict[str, Any]:
    from erp_v2.held_drafts import service

    return {
        "plan_sha256": plan.sha256,
        "rules_sha256": rules.RULES_SHA256,
        "scope_sha256": plan.scope_sha256,
        "held_total": proof["held_total"],
        "pending": len(plan.pending),
        "pre_fiscal_year_drafts_out_of_scope": plan.out_of_scope_before_fy,
        "outcomes": proof["counts"],
        "reasons": service.reason_counts(plan.decisions),
        "partition_proof": proof,
        "ready": bool(proof["ok"]),
    }


def _connect(args):
    import sqlalchemy as sa

    legacy_url = os.environ.pop("LEGACY_DATABASE_URL", "")
    destination_url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    if not legacy_url or not destination_url:
        _refuse("operator_database_materials_required")
    if "DATABASE_URL" in os.environ or "PLATFORM_DATABASE_URL" in os.environ:
        _refuse("serving_database_materials_forbidden")
    legacy = sa.create_engine(legacy_url, echo=False, hide_parameters=True)
    destination = sa.create_engine(destination_url, echo=False, hide_parameters=True)
    if legacy.dialect.name != "postgresql" or destination.dialect.name != "postgresql":
        _refuse("postgresql_required")
    return legacy, destination


def _run(args) -> dict[str, Any]:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from erp_v2.held_drafts import service

    applying = args.mode == "apply"
    lock_connection = None
    spec_bytes = args.spec.read_bytes()
    spec_sha = hashlib.sha256(spec_bytes).hexdigest()
    scope, parties = parse_spec(spec_bytes)
    operator = _operator(args.operator_ref)
    receipt: dict[str, Any] = {}
    receipt_sha = ""
    if applying:
        if os.environ.get("ERP_HELD_DRAFTS_APPLY") != "1":
            _refuse("explicit_held_drafts_apply_required")
        if not args.plan_receipt or not args.approved_plan_sha256:
            _refuse("plan_binding_required")
        receipt_bytes = args.plan_receipt.read_bytes()
        receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
        try:
            receipt = json.loads(receipt_bytes)
        except ValueError:
            _refuse("invalid_plan_receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("mode") != "plan"
            or receipt.get("plan_sha256") != args.approved_plan_sha256
            or receipt.get("spec_sha256") != spec_sha
            or receipt.get("rules_sha256") != rules.RULES_SHA256
            or not receipt.get("ready")
        ):
            _refuse("plan_binding_mismatch")
        planned_by = _operator(receipt.get("prepared_by"))
    # The operator DSNs leave the environment BEFORE the assembly is imported: a runtime-shaped process must not
    # carry them (erp_v2.database refuses), and nothing downstream may read them.
    legacy_engine, destination_engine = _connect(args)
    try:
        from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions

        from erp_v2.assembly import assembly
        from erp_v2.governance import policy as governance

        install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))
        with legacy_engine.connect() as legacy, legacy.begin():
            legacy.execute(sa.text("SET TRANSACTION READ ONLY"))
            legacy.execute(sa.text("SET LOCAL row_security = off"))
            with Session(destination_engine) as db:
                if not applying:
                    db.execute(sa.text("SET TRANSACTION READ ONLY"))
                tenant_ok = db.scalar(sa.text("SELECT is_active FROM tenants WHERE id = :t"), {"t": scope.tenant_id})
                if not tenant_ok:
                    _refuse("tenant_missing_or_inactive")
                dual = governance.dual_control(db, scope.tenant_id)
                if applying:
                    if dual and planned_by == operator:
                        _refuse("dual_control_same_operator")
                    if dual and parties["requester"] == parties["approver"]:
                        _refuse("dual_control_same_party")
                    # A dedicated connection holds the session lock for the whole apply (batches commit).
                    lock_connection = destination_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
                    if not lock_connection.scalar(
                        sa.text("SELECT pg_try_advisory_lock(hashtext('erp.held_drafts'), hashtext(:t))"),
                        {"t": str(scope.tenant_id)},
                    ):
                        _refuse("another_apply_in_progress")
                plan = service.build_plan(db, legacy, scope)
                proof = service.partition_proof(db, scope, plan.decisions)
                out = {
                    "mode": args.mode,
                    "spec_sha256": spec_sha,
                    ("applied_by" if applying else "prepared_by"): operator,
                    "dual_control": dual,
                    **_summary(plan, proof),
                }
                if not applying:
                    out["bridge_lines"] = _bridge_lines(plan)
                    db.rollback()
                    return out
                if plan.sha256 != args.approved_plan_sha256:
                    out.update(ready=False, refusal="plan_drift")
                    db.rollback()
                    return out
                if not proof["ok"]:
                    out.update(ready=False, refusal="partition_proof_failed")
                    db.rollback()
                    return out
                result = service.apply_plan(
                    db,
                    scope,
                    plan,
                    ApplyInputs(
                        plan_sha256=plan.sha256,
                        plan_receipt_sha256=receipt_sha,
                        planned_by=planned_by,
                        applied_by=operator,
                        requester_party_id=parties["requester"],
                        approver_party_id=parties["approver"],
                        dual_control=dual,
                    ),
                )
                final = service.build_plan(db, legacy, scope)
                final_proof = service.partition_proof(db, scope, final.decisions)
                check = service.status_check(db, scope)
                db.rollback()  # read-only from here; everything above was committed in batches
                out.update(
                    apply=result,
                    final_outcomes=final_proof["counts"],
                    final_partition_proof=final_proof,
                    status_check=check,
                    bridge_lines=_bridge_lines(final),
                    ready=bool(final_proof["ok"] and not check["mismatches"]),
                )
                return out
    finally:
        if lock_connection is not None:
            lock_connection.close()  # closing the connection releases the session advisory lock
        legacy_engine.dispose()
        destination_engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.held_drafts.cli")
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--plan-receipt", type=Path)
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument("--operator-ref", required=True, help="who is running this (recorded on every decision)")
    args = parser.parse_args(argv)
    try:
        if args.mode == "apply" and os.environ.get("ERP_HELD_DRAFTS_APPLY") != "1":
            _refuse("explicit_held_drafts_apply_required")
        # Reserve the receipt first: existing evidence blocks BEFORE any read of the databases or any write.
        fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception:
        print("held drafts command refused: apply authorization or receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = _run(args)
            code = 0 if payload.get("ready") else 3
        except HeldDraftsRefused as exc:
            payload, code = {"ready": False, "refusal": exc.code}, 2
        except Exception as exc:
            payload, code = (
                {"ready": False, "refusal": "held_drafts_command_refused", "error_type": type(exc).__name__},
                2,
            )
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    print("held drafts receipt written" if code == 0 else "held drafts command refused; see private receipt")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
