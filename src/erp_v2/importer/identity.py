"""Bounded, hash-preserving migration into released kernel identity storage.

Callers supply an explicit reviewed allowlist and own the transaction. Plans and
receipts contain counts/codes only; source records must never be serialized.
No reset, session, MFA, external-provider or administrator migration is provided.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from dotmac_kernel import identity_storage
from dotmac_kernel.identity import normalize_email
from dotmac_kernel.models import Party, PartyPerson, PartyType, Tenant, UserCredential
from dotmac_kernel.permission_provisioning import PermissionPlan
from dotmac_people.models import Employee
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

_PLAN_KEY = secrets.token_bytes(32)


class LegacyIdentity:
    """Immutable transient input; asdict and ordinary JSON conversion refuse."""

    __slots__ = ("credential_id", "email", "is_active", "password_hash", "person_id", "provider")

    def __init__(
        self, credential_id: UUID, person_id: UUID, email: str, password_hash: str, provider: str, is_active: bool
    ) -> None:
        values = (credential_id, person_id, email, password_hash, provider, is_active)
        for name, value in zip(
            ("credential_id", "person_id", "email", "password_hash", "provider", "is_active"), values, strict=True
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("immutable_identity_record")

    def __repr__(self) -> str:
        return "LegacyIdentity(<redacted>)"


@dataclass(frozen=True, slots=True)
class IdentityMapping:
    """Reviewed identity binding; Party UUID must equal source person UUID."""

    credential_id: UUID = field(repr=False)
    person_id: UUID = field(repr=False)
    party_id: UUID = field(repr=False)
    employee_id: UUID = field(repr=False)


@dataclass(frozen=True, slots=True)
class IdentityScope:
    tenant_id: UUID = field(repr=False)
    mappings: tuple[IdentityMapping, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class IdentityPlan:
    source_count: int
    selected_count: int
    preserved_out_of_scope: int
    credential_inserts: int
    credential_replays: int
    party_email_fills: int
    blockers: tuple[tuple[str, int], ...]
    _binding: str = field(repr=False)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def sanitized_receipt(self) -> dict[str, object]:
        return {
            "source_count": self.source_count,
            "selected_count": self.selected_count,
            "preserved_out_of_scope": self.preserved_out_of_scope,
            "credential_inserts": self.credential_inserts,
            "credential_replays": self.credential_replays,
            "party_email_fills": self.party_email_fills,
            "blockers": dict(self.blockers),
            "ready": self.ready,
        }


class IdentityEnrolmentRefused(ValueError):
    """A sanitized refusal: no source values, hashes or database diagnostics."""


def _kernel_hash(value: str) -> str | None:
    # Preserve raw PBKDF2 rounds/salt/digest; convert only the legacy
    # Passlib adapted-base64 envelope into a107's URL-safe padded envelope.
    # Bcrypt is deliberately unsupported. Bound verifier CPU cost.
    if not isinstance(value, str) or len(value) > 255:
        return None
    try:
        if value.startswith("$pbkdf2-sha256$"):
            _, scheme, rounds, salt, digest = value.split("$")
            if scheme != "pbkdf2-sha256":
                return None

            def decode(part: str) -> bytes:
                if not part or "=" in part or "+" in part or "-" in part or "_" in part:
                    raise ValueError("invalid_hash_encoding")
                raw = base64.b64decode(part.replace(".", "+") + "=" * (-len(part) % 4), validate=True)
                canonical = base64.b64encode(raw).decode().rstrip("=").replace("+", ".")
                if canonical != part:
                    raise ValueError("invalid_hash_encoding")
                return raw

            salt_bytes, digest_bytes = decode(salt), decode(digest)
        else:
            scheme, rounds, salt, digest = value.split("$")
            if scheme != "pbkdf2_sha256":
                return None
            salt_bytes = base64.b64decode(salt, altchars=b"-_", validate=True)
            digest_bytes = base64.b64decode(digest, altchars=b"-_", validate=True)
            if any(
                base64.urlsafe_b64encode(raw).decode() != encoded
                for raw, encoded in ((salt_bytes, salt), (digest_bytes, digest))
            ):
                return None
        if not rounds.isascii() or not rounds.isdecimal() or str(int(rounds)) != rounds:
            return None
        if not 1 <= int(rounds) <= 1_000_000:
            return None
        if not 8 <= len(salt_bytes) <= 128 or len(digest_bytes) != 32:
            return None
        salt_out = base64.urlsafe_b64encode(salt_bytes).decode()
        digest_out = base64.urlsafe_b64encode(digest_bytes).decode()
        return f"pbkdf2_sha256${rounds}${salt_out}${digest_out}"
    except (ValueError, TypeError, binascii.Error):
        return None


def _permission_safe(plan: PermissionPlan) -> bool:
    return (
        any(role.code == "staff" for role in plan.roles)
        and {g.permission_code for g in plan.grants if g.role_code == "staff"} == {"erp.web.access"}
        and plan.is_declared("erp.web.access")
    )


def _safe_logging(db: Session) -> None:
    if db.new or db.dirty or db.deleted:
        raise IdentityEnrolmentRefused("clean_caller_session_required")
    engine = db.get_bind().engine
    if not engine.hide_parameters or engine.echo or engine.logger.isEnabledFor(logging.INFO):
        raise IdentityEnrolmentRefused("safe_sql_logging_required")


def _binding(
    scope: IdentityScope, records: tuple[LegacyIdentity, ...], permissions: PermissionPlan, observations: list[object]
) -> str:
    # Internal equality fingerprint only. Never include it in receipts.
    document = (
        str(scope.tenant_id),
        observations,
        permissions.digest,
        sorted((str(m.credential_id), str(m.person_id), str(m.party_id), str(m.employee_id)) for m in scope.mappings),
        sorted(
            (str(r.credential_id), str(r.person_id), r.email, r.password_hash, r.provider, r.is_active) for r in records
        ),
    )
    return hmac.new(_PLAN_KEY, json.dumps(document, default=str).encode(), hashlib.sha256).hexdigest()


def _observe(
    db: Session, scope: IdentityScope, records: tuple[LegacyIdentity, ...], permissions: PermissionPlan
) -> IdentityPlan:
    blockers: Counter[str] = Counter()
    observations: list[object] = []
    selected = inserts = replays = email_fills = 0
    if not _permission_safe(permissions):
        blockers["unsafe_staff_permission_profile"] += 1
    tenant = db.scalar(select(Tenant).where(Tenant.id == scope.tenant_id).with_for_update())
    observations.append(None if tenant is None else (tenant.id, tenant.is_active))
    if tenant is None or not tenant.is_active:
        blockers["tenant_missing_or_inactive"] += 1
    mappings = scope.mappings
    roles = identity_storage.lock_roles(db, tenant_id=scope.tenant_id)
    observations.append(sorted((str(role.id), role.slug, role.name) for role in roles))
    staff_definition = next((role for role in permissions.roles if role.code == "staff"), None)
    if (
        mappings
        and not any(role.slug == "staff" for role in roles)
        and (staff_definition is None or not staff_definition.create_if_missing)
    ):
        blockers["staff_role_missing"] += 1
    for attr in ("credential_id", "person_id", "party_id", "employee_id"):
        if len({getattr(item, attr) for item in mappings}) != len(mappings):
            blockers["ambiguous_mapping"] += 1
    credential_counts = Counter(row.credential_id for row in records)
    person_counts = Counter(row.person_id for row in records)
    email_counts = Counter(normalize_email(row.email) for row in records)
    by_credential = {row.credential_id: row for row in records}
    for mapping in mappings:
        previous_blockers = sum(blockers.values())
        row = by_credential.get(mapping.credential_id)
        if row is None:
            blockers["source_mapping_missing"] += 1
            continue
        selected += 1
        if row.person_id != mapping.person_id or mapping.party_id != mapping.person_id:
            blockers["identity_uuid_mismatch"] += 1
        if credential_counts[row.credential_id] != 1 or person_counts[row.person_id] != 1:
            blockers["ambiguous_source_identity"] += 1
        if not row.email or row.email != row.email.strip() or email_counts[normalize_email(row.email)] != 1:
            blockers["ambiguous_source_email"] += 1
        if row.provider != "local":
            blockers["unsupported_provider"] += 1
        if row.is_active is not True:
            blockers["inactive_source"] += 1
        if _kernel_hash(row.password_hash) is None:
            blockers["unsupported_password_hash"] += 1
        parties = db.scalars(
            select(Party)
            .where(
                Party.tenant_id == scope.tenant_id,
                or_(Party.id == mapping.party_id, func.lower(Party.email) == normalize_email(row.email)),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        observations.append(sorted((str(p.id), p.party_type, p.email, p.is_active) for p in parties))
        if (
            len(parties) != 1
            or parties[0].id != mapping.party_id
            or parties[0].party_type != PartyType.person
            or not parties[0].is_active
            or (parties[0].email is not None and normalize_email(parties[0].email) != normalize_email(row.email))
        ):
            blockers["destination_party_mismatch"] += 1
        person = db.scalar(
            select(PartyPerson.party_id)
            .join(Party, Party.id == PartyPerson.party_id)
            .where(
                Party.tenant_id == scope.tenant_id,
                PartyPerson.party_id == mapping.party_id,
            )
            .with_for_update()
        )
        observations.append(person)
        if person is None:
            blockers["destination_person_missing"] += 1
        employees = db.scalars(
            select(Employee)
            .where(
                Employee.tenant_id == scope.tenant_id,
                or_(Employee.id == mapping.employee_id, Employee.party_id == mapping.party_id),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        observations.append(sorted((str(e.id), str(e.party_id), e.status) for e in employees))
        if (
            len(employees) != 1
            or employees[0].id != mapping.employee_id
            or employees[0].party_id != mapping.party_id
            or employees[0].status != "ACTIVE"
        ):
            blockers["destination_employee_mismatch"] += 1
        # UUID collisions are global, including another tenant; never repoint.
        credentials = db.scalars(
            select(UserCredential)
            .where(
                or_(
                    UserCredential.id == mapping.credential_id,
                    (UserCredential.tenant_id == scope.tenant_id) & (UserCredential.party_id == mapping.party_id),
                )
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        observations.append(
            sorted((str(c.id), str(c.tenant_id), str(c.party_id), c.password_hash) for c in credentials)
        )
        if not credentials:
            inserts += 1
        elif (
            len(credentials) == 1
            and credentials[0].id == mapping.credential_id
            and credentials[0].tenant_id == scope.tenant_id
            and credentials[0].party_id == mapping.party_id
            and credentials[0].password_hash == _kernel_hash(row.password_hash)
        ):
            replays += 1
        else:
            blockers["destination_credential_collision"] += 1
        if sum(blockers.values()) == previous_blockers and parties[0].email is None:
            email_fills += 1
    mapped_ids = {item.credential_id for item in mappings}
    return IdentityPlan(
        len(records),
        selected,
        sum(row.credential_id not in mapped_ids for row in records),
        inserts,
        replays,
        email_fills,
        tuple(sorted(blockers.items())),
        _binding(scope, records, permissions, observations),
    )


def plan_identity_enrolment(
    db: Session, scope: IdentityScope, records: Iterable[LegacyIdentity], *, permissions: PermissionPlan
) -> IdentityPlan:
    """Read and classify only. Caller supplies complete source cohort and scope."""
    _safe_logging(db)
    try:
        with db.no_autoflush:
            return _observe(db, scope, tuple(records), permissions)
    except Exception:
        raise IdentityEnrolmentRefused("identity_plan_failed") from None


def apply_identity_enrolment(
    db: Session,
    scope: IdentityScope,
    records: Iterable[LegacyIdentity],
    *,
    permissions: PermissionPlan,
    reviewed_plan: IdentityPlan,
) -> IdentityPlan:
    """Revalidate, fill NULL Party emails, insert credentials and grant staff; never commit.

    Requires an explicit caller transaction. The caller must roll back on any
    refusal and serialize migration writers (PostgreSQL SERIALIZABLE recommended).
    Only NULL Party emails may receive the eligible source person email. Existing
    email values and credentials are never overwritten, including after login rehash.
    """
    if not db.in_transaction():
        raise IdentityEnrolmentRefused("caller_transaction_required")
    _safe_logging(db)
    try:
        rows = tuple(records)
        with db.no_autoflush:
            observed = _observe(db, scope, rows, permissions)
        if not observed.ready:
            raise IdentityEnrolmentRefused("identity_plan_blocked")
        if observed != reviewed_plan:
            raise IdentityEnrolmentRefused("identity_plan_changed")
        if not scope.mappings:
            return observed
        now = datetime.now(UTC)
        roles = identity_storage.provision_missing_roles(
            db,
            tenant_id=scope.tenant_id,
            catalog=identity_storage.lock_roles(db, tenant_id=scope.tenant_id),
            configured_names={"staff": "Staff"},
            missing_role_ids={"staff": uuid4()},
            created_at=now,
            updated_at=now,
        )
        staff = next(role for role in roles if role.slug == "staff")
        source = {row.credential_id: row for row in rows}
        for mapping in scope.mappings:
            # The revalidated, HMAC-bound observation already locked this exact
            # Party and ruled out normalized-email collisions across the tenant.
            party = db.scalars(
                select(Party)
                .where(
                    Party.tenant_id == scope.tenant_id,
                    Party.id == mapping.party_id,
                )
                .with_for_update()
            ).one()
            if party.email is None:
                party.email = normalize_email(source[mapping.credential_id].email)
                db.flush()
            if db.scalar(select(UserCredential.id).where(UserCredential.id == mapping.credential_id)) is None:
                db.add(
                    UserCredential(
                        id=mapping.credential_id,
                        tenant_id=scope.tenant_id,
                        party_id=mapping.party_id,
                        password_hash=_kernel_hash(source[mapping.credential_id].password_hash),
                    )
                )
                db.flush()
            identity_storage.ensure_grant(
                db,
                tenant_id=scope.tenant_id,
                party_id=mapping.party_id,
                role_id=staff.id,
                new_grant_id=None,
                created_at=None,
                updated_at=None,
            )
        return observed
    except IdentityEnrolmentRefused:
        raise
    except Exception:
        raise IdentityEnrolmentRefused("identity_apply_failed_rollback_required") from None
