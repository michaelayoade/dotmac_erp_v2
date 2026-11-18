"""Operator plan/apply: set a tenant's dual-control policy (D-9). There is no UI or runtime write path.

Michael's ruling (2026-11-17/18): no compulsory two-person. Every coded two-person check is a per-tenant
policy, default OFF; this command is the only writer of ``erp_governance_policies`` (the runtime role has
SELECT only). Typed, reviewed input only: a JSON spec bound by sha256.

    {"version": 1, "tenant_id": "<uuid>", "dual_control": true|false, "evidence_ref": "<who/why, <=255>"}

    python -m erp_v2.governance.cli plan  --spec S.json --receipt R.json
    ERP_GOVERNANCE_APPLY=1 python -m erp_v2.governance.cli apply --spec S.json --receipt R.json \
        --approved-spec-sha256 <sha256 of S.json> --operator-ref <who>

DSN is environment-only (``MIGRATION_DATABASE_URL``); the receipt (0600, created exclusively before any
write) carries the current and requested state and the sha, never the tenant id. Plan writes nothing; apply
upserts the one policy row and writes the ``erp.governance.dual_control_set`` audit event in the same
transaction. Re-applying the current value is a no-op (no write, no audit row). Turning the policy ON
affects rows and checks created or evaluated afterwards; rows already recorded keep the value they were
stamped with (see ``erp_v2.governance.policy``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

_EVIDENCE_MAX = 255


class GovernanceRefusal(ValueError):
    """Sanitized refusal; ``code`` only."""


def _refuse(code: str):
    raise GovernanceRefusal(code)


@dataclass(frozen=True, slots=True)
class PolicySpec:
    tenant_id: UUID
    dual_control: bool
    evidence_ref: str


def parse_spec(raw_bytes: bytes) -> PolicySpec:
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                _refuse("duplicate_json_key")
            out[key] = value
        return out

    try:
        raw = json.loads(raw_bytes, object_pairs_hook=unique)
    except GovernanceRefusal:
        raise
    except (ValueError, UnicodeDecodeError):
        _refuse("invalid_spec_json")
    if not isinstance(raw, dict) or set(raw) != {"version", "tenant_id", "dual_control", "evidence_ref"}:
        _refuse("invalid_spec_schema")
    if type(raw["version"]) is not int or raw["version"] != 1:
        _refuse("invalid_spec_schema")
    if type(raw["dual_control"]) is not bool:
        _refuse("invalid_dual_control")
    try:
        tenant = UUID(str(raw["tenant_id"]))
    except ValueError:
        _refuse("invalid_tenant_id")
    if str(tenant) != raw["tenant_id"]:
        _refuse("invalid_tenant_id")
    evidence = raw["evidence_ref"]
    if not isinstance(evidence, str) or not evidence.strip() or evidence != evidence.strip():
        _refuse("evidence_ref_required")
    if len(evidence) > _EVIDENCE_MAX:
        _refuse("evidence_ref_required")
    return PolicySpec(tenant, raw["dual_control"], evidence)


@dataclass(frozen=True, slots=True)
class PolicyPlan:
    current: str  # "unset" | "on" | "off"
    requested: str  # "on" | "off"
    blockers: tuple[tuple[str, int], ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def changes(self) -> bool:
        return self.ready and self.current != self.requested

    def receipt(self) -> dict[str, object]:
        return {
            "current": self.current,
            "requested": self.requested,
            "changes": self.changes,
            "blockers": dict(self.blockers),
            "ready": self.ready,
        }


def _label(value: bool | None) -> str:
    return "unset" if value is None else ("on" if value else "off")


def _write_audit(db, *, spec: PolicySpec, previous: str, spec_sha256: str, operator_ref: str) -> None:
    from dotmac_kernel.audit import write_audit_event

    from erp_v2.governance.policy import AUDIT_DUAL_CONTROL_SET

    write_audit_event(
        db,
        tenant_id=spec.tenant_id,
        actor_type="service",
        actor_id=operator_ref,
        actor_label="erp_v2.governance.cli",
        source_application="dotmac_erp",
        action=AUDIT_DUAL_CONTROL_SET,
        entity_type="governance_policy",
        entity_id=str(spec.tenant_id),
        is_success=True,
        details={
            "dual_control": spec.dual_control,
            "previous": previous,
            "spec_sha256": spec_sha256,
            "evidence_ref": spec.evidence_ref,
        },
    )


def _observe(
    db, spec: PolicySpec, *, write: bool, spec_sha256: str = "", operator_ref: str = "", audit=None
) -> PolicyPlan:
    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from erp_v2.governance.models import GovernancePolicy

    requested = _label(spec.dual_control)
    tenant = db.scalar(select(Tenant).where(Tenant.id == spec.tenant_id).with_for_update())
    if tenant is None or not tenant.is_active:
        return PolicyPlan("unset", requested, (("tenant_missing_or_inactive", 1),))
    row = db.scalar(select(GovernancePolicy).where(GovernancePolicy.tenant_id == spec.tenant_id).with_for_update())
    current = _label(None if row is None else row.dual_control)
    plan = PolicyPlan(current, requested, ())
    if write and plan.changes:
        now = datetime.now(UTC)
        if row is None:
            db.add(
                GovernancePolicy(
                    tenant_id=spec.tenant_id,
                    dual_control=spec.dual_control,
                    set_by=operator_ref,
                    set_at=now,
                    evidence_ref=spec.evidence_ref,
                )
            )
        else:
            row.dual_control = spec.dual_control
            row.set_by = operator_ref
            row.set_at = now
            row.evidence_ref = spec.evidence_ref
        db.flush()
        (audit or _write_audit)(db, spec=spec, previous=current, spec_sha256=spec_sha256, operator_ref=operator_ref)
    return plan


def plan_policy(db, spec: PolicySpec) -> PolicyPlan:
    return _observe(db, spec, write=False)


def apply_policy(db, spec: PolicySpec, *, spec_sha256: str, operator_ref: str, audit=None) -> PolicyPlan:
    """Caller owns the transaction. A refused plan writes nothing; a real change is audited once."""
    if not operator_ref or not operator_ref.strip() or len(operator_ref) > 120:
        _refuse("operator_ref_required")
    return _observe(db, spec, write=True, spec_sha256=spec_sha256, operator_ref=operator_ref.strip(), audit=audit)


def _run(args) -> dict:
    raw = args.spec.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    applying = args.mode == "apply"
    spec = parse_spec(raw)
    if applying and args.approved_spec_sha256 != sha:
        _refuse("spec_review_binding_required")
    if applying and not (args.operator_ref or "").strip():
        _refuse("operator_ref_required")
    url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    if not url:
        _refuse("operator_database_materials_required")
    if "DATABASE_URL" in os.environ:
        _refuse("serving_database_materials_forbidden")
    import sqlalchemy as sa
    from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
    from sqlalchemy.orm import Session

    from erp_v2.assembly import assembly

    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))

    engine = sa.create_engine(url, echo=False, hide_parameters=True)
    try:
        if engine.dialect.name != "postgresql":
            _refuse("postgresql_required")
        with Session(engine) as db, db.begin():
            plan = (
                apply_policy(db, spec, spec_sha256=sha, operator_ref=args.operator_ref or "")
                if applying
                else plan_policy(db, spec)
            )
            if not applying or not plan.ready:
                db.rollback()
            return {"mode": args.mode, "spec_sha256": sha, "policy": plan.receipt()}
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.governance.cli")
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--approved-spec-sha256")
    parser.add_argument("--operator-ref", help="who is applying (recorded in the audit event)")
    args = parser.parse_args(argv)
    try:
        if args.mode == "apply" and os.environ.get("ERP_GOVERNANCE_APPLY") != "1":
            _refuse("explicit_governance_apply_required")
        # Reserve the receipt first: existing evidence blocks BEFORE any write.
        fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception:
        print("governance command refused: apply authorization or receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = _run(args)
            code = 0 if payload["policy"]["ready"] else 3
        except GovernanceRefusal as exc:
            payload, code = {"ready": False, "refusal": str(exc)}, 2
        except Exception:
            payload, code = {"ready": False, "refusal": "governance_command_refused"}, 2
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    print("governance receipt written" if code == 0 else "governance command refused; see private receipt")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
