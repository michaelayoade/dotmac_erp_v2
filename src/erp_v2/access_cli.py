"""Operator plan/apply: named parties -> finance/payroll role grants.

Typed, reviewed input only (a JSON allowlist bound by sha256); role holders are
never taken from env files or defaults. Only the roles declared in
``erp_v2.access.NEW_ROLES`` are grantable: ``admin``, ``staff`` and anything else
refuse. Grants are additive (kernel ``ensure_grant``); nothing is revoked or
widened. DSN is environment-only (``MIGRATION_DATABASE_URL``); the receipt (0600,
created exclusively before any write) carries counts/codes, never identities.

    python -m erp_v2.access_cli plan  --allowlist A.json --receipt R.json
    ERP_ACCESS_APPLY=1 python -m erp_v2.access_cli apply --allowlist A.json \
        --receipt R.json --approved-allowlist-sha256 <sha256 of A.json>
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

from erp_v2.access import NEW_ROLES

MAX_GRANTS = 200


class AccessRefusal(ValueError):
    """Sanitized refusal; ``code`` only."""


def _refuse(code: str):
    raise AccessRefusal(code)


@dataclass(frozen=True, slots=True)
class Allowlist:
    tenant_id: UUID = field(repr=False)
    grants: tuple[tuple[UUID, str], ...] = field(repr=False)


def parse_allowlist(raw_bytes: bytes) -> Allowlist:
    try:
        raw = json.loads(raw_bytes)
    except ValueError:
        _refuse("invalid_allowlist_json")
    if not isinstance(raw, dict) or set(raw) != {"version", "tenant_id", "grants"} or raw["version"] != 1:
        _refuse("invalid_allowlist_schema")
    try:
        tenant = UUID(str(raw["tenant_id"]))
    except ValueError:
        _refuse("invalid_tenant_id")
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
        try:
            party = UUID(str(row["party_id"]))
        except ValueError:
            _refuse("invalid_party_id")
        grants.append((party, role))
    if len(set(grants)) != len(grants):
        _refuse("duplicate_grant")
    return Allowlist(tenant, tuple(sorted(grants, key=lambda g: (str(g[0]), g[1]))))


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


def _observe(db, allow: Allowlist, *, write: bool) -> AccessPlan:
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
    existing = {
        (g.party_id, g.role_id)
        for g in identity_storage.lock_grants(db, tenant_id=allow.tenant_id, party_ids=tuple(parties))
    }
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
            identity_storage.ensure_grant(
                db,
                tenant_id=allow.tenant_id,
                party_id=party,
                role_id=roles[role].id,
                new_grant_id=uuid4(),
                created_at=now,
                updated_at=now,
            )
    return AccessPlan(len(allow.grants), inserts, replays, len(missing), tuple(sorted(blockers.items())))


def plan_grants(db, allow: Allowlist) -> AccessPlan:
    return _observe(db, allow, write=False)


def apply_grants(db, allow: Allowlist) -> AccessPlan:
    """Caller owns the transaction. Refused plans write nothing."""
    return _observe(db, allow, write=True)


def _run(args) -> dict:
    raw = args.allowlist.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    allow = parse_allowlist(raw)
    if args.mode == "apply" and args.approved_allowlist_sha256 != sha:
        _refuse("allowlist_review_binding_required")
    url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    if not url:
        _refuse("operator_database_materials_required")
    if "DATABASE_URL" in os.environ:
        _refuse("serving_database_materials_forbidden")
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    engine = sa.create_engine(url, echo=False, hide_parameters=True)
    try:
        if engine.dialect.name != "postgresql":
            _refuse("postgresql_required")
        with Session(engine) as db, db.begin():
            plan = apply_grants(db, allow) if args.mode == "apply" else plan_grants(db, allow)
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
