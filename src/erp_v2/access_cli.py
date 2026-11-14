"""Operator plan/apply: named parties -> finance/payroll role grants.

Typed, reviewed input only (a JSON allowlist bound by sha256); role holders are
never taken from env files or defaults. Only the roles declared in
``erp_v2.access.NEW_ROLES`` are grantable: ``admin``, ``staff`` and anything else
refuse. Grants are additive (kernel ``ensure_grant``); nothing is revoked or
widened. DSN is environment-only (``MIGRATION_DATABASE_URL``); the receipt (0600,
created exclusively before any write) carries counts/codes, never identities.

    python -m erp_v2.access_cli plan  --allowlist A.json --receipt R.json
    ERP_ACCESS_APPLY=1 python -m erp_v2.access_cli apply --allowlist A.json \
        --receipt R.json --approved-allowlist-sha256 <sha256 of A.json> --operator-ref <who>

No access is widened: a party must already hold ``staff`` or ``admin`` (portal
entry) and must not end up with a conflicting role pair (``SOD_CONFLICT_PAIRS``)
unless the sha-bound allowlist lists that pair under ``sod_exceptions``. Every
inserted grant writes a DB audit event (``erp.access.role_granted``) in the same
transaction. There is no revoke path yet (required before the flip).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from erp_v2.access import AUDIT_ROLE_GRANTED, NEW_ROLES, SOD_CONFLICT_PAIRS

MAX_GRANTS = 200


class AccessRefusal(ValueError):
    """Sanitized refusal; ``code`` only."""


def _refuse(code: str):
    raise AccessRefusal(code)


@dataclass(frozen=True, slots=True)
class Allowlist:
    tenant_id: UUID = field(repr=False)
    grants: tuple[tuple[UUID, str], ...] = field(repr=False)
    sod_exceptions: frozenset[tuple[UUID, frozenset[str]]] = field(default=frozenset(), repr=False)


def _uuid(value: object, code: str) -> UUID:
    # Canonical dashed lowercase only: no alternate spellings of one identity.
    try:
        parsed = UUID(str(value))
    except ValueError:
        _refuse(code)
    if str(parsed) != value:
        _refuse(code)
    return parsed


def parse_allowlist(raw_bytes: bytes) -> Allowlist:
    try:
        raw = json.loads(raw_bytes)
    except ValueError:
        _refuse("invalid_allowlist_json")
    if (
        not isinstance(raw, dict)
        or not {"version", "tenant_id", "grants"} <= set(raw)
        or set(raw) - {"version", "tenant_id", "grants", "sod_exceptions"}
        or type(raw["version"]) is not int
        or raw["version"] != 1
    ):
        _refuse("invalid_allowlist_schema")
    tenant = _uuid(raw["tenant_id"], "invalid_tenant_id")
    rows = raw["grants"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_GRANTS:
        _refuse("explicit_bounded_grants_required")
    grants: list[tuple[UUID, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"party_id", "role"}:
            _refuse("invalid_grant_row")
        role = row["role"]
        if role in ("admin", "staff"):
            _refuse("role_not_grantable")
        if role not in NEW_ROLES:
            _refuse("unknown_role")
        grants.append((_uuid(row["party_id"], "invalid_party_id"), role))
    if len(set(grants)) != len(grants):
        _refuse("duplicate_grant")
    exceptions = set()
    for row in raw.get("sod_exceptions", []):
        if not isinstance(row, dict) or set(row) != {"party_id", "roles"} or not isinstance(row["roles"], list):
            _refuse("invalid_sod_exception")
        pair = frozenset(row["roles"])
        if pair not in SOD_CONFLICT_PAIRS or len(row["roles"]) != 2:
            _refuse("invalid_sod_exception")
        exceptions.add((_uuid(row["party_id"], "invalid_party_id"), pair))
    return Allowlist(tenant, tuple(sorted(grants, key=lambda g: (str(g[0]), g[1]))), frozenset(exceptions))


@dataclass(frozen=True, slots=True)
class AccessPlan:
    selected: int
    grant_inserts: int
    grant_replays: int
    role_inserts: int
    blockers: tuple[tuple[str, int], ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def receipt(self) -> dict[str, object]:
        return {
            "selected": self.selected,
            "grant_inserts": self.grant_inserts,
            "grant_replays": self.grant_replays,
            "role_inserts": self.role_inserts,
            "blockers": dict(self.blockers),
            "ready": self.ready,
        }


def _write_audit(db, *, tenant_id, party_id, role, allowlist_sha256, operator_ref) -> None:
    from dotmac_kernel.audit import write_audit_event

    write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_type="service",
        actor_id=operator_ref,
        actor_label="erp_v2.access_cli",
        source_application="dotmac_erp",
        action=AUDIT_ROLE_GRANTED,
        entity_type="party_role_grant",
        entity_id=str(party_id),
        is_success=True,
        details={"role": role, "allowlist_sha256": allowlist_sha256},
    )


def _observe(
    db, allow: Allowlist, *, write: bool, audit=None, allowlist_sha256: str = "", operator_ref: str = ""
) -> AccessPlan:
    from dotmac_kernel import identity_storage
    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    blockers: Counter[str] = Counter()
    tenant = db.scalar(select(Tenant).where(Tenant.id == allow.tenant_id).with_for_update())
    if tenant is None or not tenant.is_active:
        blockers["tenant_missing_or_inactive"] += 1
        return AccessPlan(len(allow.grants), 0, 0, 0, tuple(blockers.items()))
    party_ids = tuple(sorted({party for party, _ in allow.grants}, key=str))
    parties = {p.id: p for p in identity_storage.lock_parties(db, tenant_id=allow.tenant_id, party_ids=party_ids)}
    for party_id in party_ids:
        party = parties.get(party_id)
        if party is None:
            blockers["party_missing"] += 1
        elif not party.is_active:
            blockers["party_inactive"] += 1
        elif getattr(party.party_type, "value", party.party_type) != "person":
            blockers["party_not_a_person"] += 1
    roles = {r.slug: r for r in identity_storage.lock_roles(db, tenant_id=allow.tenant_id)}
    needed = sorted({role for _, role in allow.grants})
    missing = [slug for slug in needed if slug not in roles]
    grant_rows = identity_storage.lock_grants(db, tenant_id=allow.tenant_id, party_ids=tuple(parties))
    existing = {(g.party_id, g.role_id) for g in grant_rows}
    slug_by_id = {r.id: r.slug for r in roles.values()}
    held: dict[UUID, set[str]] = {party_id: set() for party_id in party_ids}
    for g in grant_rows:
        held.setdefault(g.party_id, set()).add(slug_by_id.get(g.role_id, ""))
    requested: dict[UUID, set[str]] = {}
    for party_id, role in allow.grants:
        requested.setdefault(party_id, set()).add(role)
    for party_id in party_ids:
        if party_id not in parties:
            continue
        if not held[party_id] & {"staff", "admin"}:
            blockers["party_lacks_portal_access"] += 1
        final = held[party_id] | requested[party_id]
        for pair in SOD_CONFLICT_PAIRS:
            if pair <= final and (party_id, pair) not in allow.sod_exceptions:
                blockers["sod_conflict"] += 1
    replays = sum(1 for party, role in allow.grants if role in roles and (party, roles[role].id) in existing)
    inserts = len(allow.grants) - replays
    if write and not blockers:
        now = datetime.now(UTC)
        roles.update(
            {
                r.slug: r
                for r in identity_storage.provision_missing_roles(
                    db,
                    tenant_id=allow.tenant_id,
                    catalog=tuple(roles.values()),
                    configured_names={slug: slug.replace("_", " ").title() for slug in missing},
                    missing_role_ids={slug: uuid4() for slug in missing},
                    created_at=now,
                    updated_at=now,
                )
            }
        )
        for party, role in allow.grants:
            already = (party, roles[role].id) in existing
            identity_storage.ensure_grant(
                db,
                tenant_id=allow.tenant_id,
                party_id=party,
                role_id=roles[role].id,
                new_grant_id=uuid4(),
                created_at=now,
                updated_at=now,
            )
            if not already:
                (audit or _write_audit)(
                    db,
                    tenant_id=allow.tenant_id,
                    party_id=party,
                    role=role,
                    allowlist_sha256=allowlist_sha256,
                    operator_ref=operator_ref,
                )
    return AccessPlan(len(allow.grants), inserts, replays, len(missing), tuple(sorted(blockers.items())))


def plan_grants(db, allow: Allowlist) -> AccessPlan:
    return _observe(db, allow, write=False)


def apply_grants(db, allow: Allowlist, *, allowlist_sha256: str, operator_ref: str, audit=None) -> AccessPlan:
    """Caller owns the transaction. Refused plans write nothing; each new grant is audited."""
    if not operator_ref or not operator_ref.strip() or len(operator_ref) > 120:
        _refuse("operator_ref_required")
    return _observe(
        db, allow, write=True, audit=audit, allowlist_sha256=allowlist_sha256, operator_ref=operator_ref.strip()
    )


def _run(args) -> dict:
    raw = args.allowlist.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    allow = parse_allowlist(raw)
    if args.mode == "apply" and args.approved_allowlist_sha256 != sha:
        _refuse("allowlist_review_binding_required")
    if args.mode == "apply" and not (args.operator_ref or "").strip():
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
                apply_grants(db, allow, allowlist_sha256=sha, operator_ref=args.operator_ref or "")
                if args.mode == "apply"
                else plan_grants(db, allow)
            )
            if args.mode == "plan" or not plan.ready:
                db.rollback()
            return {"mode": args.mode, "allowlist_sha256": sha, "access": plan.receipt()}
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.access_cli")
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--approved-allowlist-sha256")
    parser.add_argument("--operator-ref", help="who is applying (recorded in the audit event)")
    args = parser.parse_args(argv)
    try:
        if args.mode == "apply" and os.environ.get("ERP_ACCESS_APPLY") != "1":
            _refuse("explicit_access_apply_required")
        # Reserve the receipt first: existing evidence blocks BEFORE any write.
        fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception:
        print("access command refused: apply authorization or receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = _run(args)
            code = 0 if payload["access"]["ready"] else 3
        except AccessRefusal as exc:
            payload, code = {"ready": False, "refusal": str(exc)}, 2
        except Exception:
            payload, code = {"ready": False, "refusal": "access_command_refused"}, 2
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    print("access receipt written" if code == 0 else "access command refused; see private receipt")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
