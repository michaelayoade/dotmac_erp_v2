"""Synthetic identity migration proofs against real released kernel owners."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, replace
from datetime import date
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.models import (
    AuthSession,
    Base,
    Party,
    PartyPerson,
    PartyRoleGrant,
    PartyType,
    Role,
    Tenant,
    UserCredential,
)
from dotmac_kernel.permission_provisioning import PermissionDefinition, PermissionPlan, RoleDefinition, RoleGrant
from dotmac_kernel.security import verify_password
from dotmac_people.models import Employee
from sqlalchemy.orm import Session

from erp_v2.importer.identity import (
    IdentityEnrolmentRefused,
    IdentityMapping,
    IdentityScope,
    LegacyIdentity,
    apply_identity_enrolment,
    plan_identity_enrolment,
)
from erp_v2.web.auth_service import web_login


def _pbkdf2(*, passlib=False):
    salt = b"synthetic-identity-salt"
    digest = hashlib.pbkdf2_hmac("sha256", b"synthetic-password", salt, 210000)
    if passlib:

        def encode(value):
            return base64.b64encode(value).decode().rstrip("=").replace("+", ".")

        return f"$pbkdf2-sha256$210000${encode(salt)}${encode(digest)}"

    def encode(value):
        return base64.urlsafe_b64encode(value).decode()

    return f"pbkdf2_sha256$210000${encode(salt)}${encode(digest)}"


def _replace_source(row, **changes):
    values = {name: getattr(row, name) for name in LegacyIdentity.__slots__}
    return LegacyIdentity(**(values | changes))


@pytest.fixture
def cohort():
    engine = sa.create_engine(
        "sqlite://", hide_parameters=True, execution_options={"schema_translate_map": {"mod_people": None}}
    )
    tables = [
        Tenant.__table__,
        Party.__table__,
        PartyPerson.__table__,
        Role.__table__,
        PartyRoleGrant.__table__,
        UserCredential.__table__,
        AuthSession.__table__,
        Employee.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as db:
        tenant = Tenant(id=uuid4(), slug="synthetic", name="Synthetic")
        person, credential, employee = uuid4(), uuid4(), uuid4()
        db.add(tenant)
        db.add(
            Party(
                id=person,
                tenant_id=tenant.id,
                party_type=PartyType.person,
                display_name="Synthetic",
                email="staff@example.test",
                is_active=True,
            )
        )
        db.add(PartyPerson(party_id=person, first_name="Synthetic", last_name="Staff"))
        db.add(
            Employee(
                id=employee,
                tenant_id=tenant.id,
                party_id=person,
                employee_code="TEST-1",
                date_of_joining=date(2020, 1, 1),
                status="ACTIVE",
            )
        )
        db.flush()
        source = LegacyIdentity(credential, person, "STAFF@example.test", _pbkdf2(passlib=True), "local", True)
        scope = IdentityScope(tenant.id, (IdentityMapping(credential, person, person, employee),))
        grant = RoleGrant("staff", "erp.web.access")
        permissions = PermissionPlan(
            definitions=(PermissionDefinition("erp.web.access", "Access", "erp_web"),),
            roles=(RoleDefinition("staff", create_if_missing=True),),
            profiles=(),
            grants=(grant,),
        )
        yield db, tenant, source, scope, permissions


def _plan(cohort, rows=None, scope=None, permissions=None):
    db, _, row, default_scope, default_permissions = cohort
    return plan_identity_enrolment(
        db, scope or default_scope, rows or (row,), permissions=permissions or default_permissions
    )


def _apply(cohort, rows=None, scope=None, permissions=None, plan=None):
    db, _, row, default_scope, default_permissions = cohort
    return apply_identity_enrolment(
        db,
        scope or default_scope,
        rows or (row,),
        permissions=permissions or default_permissions,
        reviewed_plan=plan or _plan(cohort, rows, scope, permissions),
    )


def test_passlib_encoding_conversion_login_and_no_rewind(cohort):
    db, tenant, row, _scope, _ = cohort
    plan = _plan(cohort)
    assert plan.ready and plan.credential_inserts == 1
    _apply(cohort, plan=plan)
    stored = db.scalar(sa.select(UserCredential))
    assert stored.id == row.credential_id and stored.party_id == row.person_id
    assert verify_password("synthetic-password", stored.password_hash)
    assert stored.password_hash == _pbkdf2()
    replay = _plan(cohort)
    assert replay.credential_replays == 1
    _apply(cohort, plan=replay)
    assert len(db.scalars(sa.select(UserCredential)).all()) == 1
    assert [r.slug for r in db.scalars(sa.select(Role))] == ["staff"]
    assert len(db.scalars(sa.select(PartyRoleGrant)).all()) == 1
    assert web_login(db, tenant, row.email, "synthetic-password")
    upgraded = stored.password_hash
    assert upgraded.startswith("$argon2id$")
    assert dict(_plan(cohort).blockers) == {"destination_credential_collision": 1}
    with pytest.raises(IdentityEnrolmentRefused, match="identity_plan_blocked"):
        _apply(cohort)
    assert stored.password_hash == upgraded
    # This service never commits the caller's transaction.
    db.rollback()
    assert db.scalar(sa.select(UserCredential)) is None


@pytest.mark.parametrize(
    "change,code",
    [
        ({"provider": "google"}, "unsupported_provider"),
        ({"provider": "LOCAL"}, "unsupported_provider"),
        ({"is_active": False}, "inactive_source"),
        ({"password_hash": "$2b$12$synthetic"}, "unsupported_password_hash"),
        ({"password_hash": "$pbkdf2-sha256$9999999999$bad$bad"}, "unsupported_password_hash"),
        ({"password_hash": "pbkdf2_sha256$210000$bad$bad"}, "unsupported_password_hash"),
        ({"person_id": uuid4()}, "identity_uuid_mismatch"),
        ({"email": "other@example.test"}, "destination_party_mismatch"),
    ],
)
def test_selected_invalid_rows_block_all_mutations(cohort, change, code):
    db = cohort[0]
    rows = (_replace_source(cohort[2], **change),)
    assert code in dict(_plan(cohort, rows).blockers)
    with pytest.raises(IdentityEnrolmentRefused):
        _apply(cohort, rows)
    assert db.scalar(sa.select(UserCredential)) is None
    assert db.scalar(sa.select(Role)) is None


def test_explicit_scope_and_duplicate_ambiguity(cohort):
    extra = _replace_source(
        cohort[2], credential_id=uuid4(), person_id=uuid4(), email="excluded@example.test", is_active=False
    )
    plan = _plan(cohort, (cohort[2], extra))
    assert plan.ready and plan.preserved_out_of_scope == 1
    assert "ambiguous_source_identity" in dict(_plan(cohort, (cohort[2], cohort[2])).blockers)
    assert "ambiguous_source_email" in dict(
        _plan(cohort, (cohort[2], _replace_source(extra, email=cohort[2].email))).blockers
    )
    mapping = cohort[3].mappings[0]
    assert "ambiguous_mapping" in dict(_plan(cohort, scope=replace(cohort[3], mappings=(mapping, mapping))).blockers)
    assert "source_mapping_missing" in dict(
        _plan(cohort, scope=replace(cohort[3], mappings=(replace(mapping, credential_id=uuid4()),))).blockers
    )


def test_cross_tenant_and_credential_collisions(cohort):
    db, _, row, scope, _ = cohort
    other = Tenant(id=uuid4(), slug="other", name="Other")
    db.add(other)
    db.flush()
    assert "destination_party_mismatch" in dict(_plan(cohort, scope=replace(scope, tenant_id=other.id)).blockers)
    db.add(UserCredential(id=row.credential_id, tenant_id=other.id, party_id=uuid4(), password_hash=_pbkdf2()))
    db.flush()
    assert "destination_credential_collision" in dict(_plan(cohort).blockers)
    with pytest.raises(IdentityEnrolmentRefused):
        _apply(cohort)


def test_no_admin_escalation_and_receipt_redaction(cohort):
    _, _, row, _, permissions = cohort
    unsafe = replace(permissions, grants=(*permissions.grants, RoleGrant("staff", "finance.write")))
    assert "unsafe_staff_permission_profile" in dict(_plan(cohort, permissions=unsafe).blockers)
    with pytest.raises(IdentityEnrolmentRefused):
        _apply(cohort, permissions=unsafe)
    text = json.dumps(_plan(cohort).sanitized_receipt()) + repr(row) + repr(cohort[3])
    assert row.email not in text and row.password_hash not in text and str(row.person_id) not in text


def test_plan_drift_refuses_before_mutation(cohort):
    db = cohort[0]
    reviewed = _plan(cohort)
    db.scalar(sa.select(Party)).is_active = False
    db.flush()
    with pytest.raises(IdentityEnrolmentRefused):
        _apply(cohort, plan=reviewed)
    assert db.scalar(sa.select(UserCredential)) is None


def test_source_records_refuse_generic_serialization(cohort):
    row = cohort[2]
    with pytest.raises(TypeError):
        asdict(row)
    with pytest.raises(TypeError):
        json.dumps(row)
    assert not hasattr(row, "__dict__")
    with pytest.raises(AttributeError):
        row.password_hash = "replacement"  # noqa: S105 - synthetic immutability assertion


def test_same_counts_source_change_is_not_reviewed(cohort):
    reviewed = _plan(cohort)
    rows = (_replace_source(cohort[2], email=cohort[2].email.lower()),)
    assert _plan(cohort, rows).sanitized_receipt() == reviewed.sanitized_receipt()
    with pytest.raises(IdentityEnrolmentRefused, match="identity_plan_changed"):
        _apply(cohort, rows, plan=reviewed)


def test_planning_refuses_unsafe_sql_logging(cohort):
    engine = cohort[0].get_bind().engine
    engine.hide_parameters = False
    with pytest.raises(IdentityEnrolmentRefused, match="safe_sql_logging_required"):
        _plan(cohort)


def test_existing_admin_grant_is_preserved_without_escalation(cohort):
    db, tenant, row, _, _ = cohort
    role = Role(id=uuid4(), tenant_id=tenant.id, slug="admin", name="Admin")
    existing = PartyRoleGrant(id=uuid4(), tenant_id=tenant.id, party_id=row.person_id, role_id=role.id)
    db.add_all((role, existing))
    db.flush()
    _apply(cohort)
    grants = db.scalars(sa.select(PartyRoleGrant)).all()
    assert len(grants) == 2 and any(grant.id == existing.id for grant in grants)
    roles = {item.id: item.slug for item in db.scalars(sa.select(Role))}
    assert sorted(roles[grant.role_id] for grant in grants) == ["admin", "staff"]


def test_source_iterable_errors_are_sanitized(cohort):
    secret = cohort[2].password_hash

    def failed_source():
        raise RuntimeError(secret)
        yield  # pragma: no cover

    with pytest.raises(IdentityEnrolmentRefused) as error:
        _apply(cohort, failed_source(), plan=_plan(cohort))
    assert secret not in str(error.value)
    assert error.value.__suppress_context__


def test_database_failure_is_sanitized_and_caller_can_roll_back(cohort, monkeypatch):
    from dotmac_kernel import identity_storage

    db, _, row, _, _ = cohort
    secret = row.password_hash

    def failure(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(identity_storage, "ensure_grant", failure)
    with pytest.raises(IdentityEnrolmentRefused) as error:
        _apply(cohort)
    assert secret not in str(error.value)
    assert error.value.__suppress_context__
    db.rollback()
    assert db.scalar(sa.select(UserCredential)) is None
    assert db.scalar(sa.select(PartyRoleGrant)) is None


def test_pending_caller_changes_are_not_flushed(cohort):
    db = cohort[0]
    db.scalar(sa.select(Party)).is_active = False
    with pytest.raises(IdentityEnrolmentRefused, match="clean_caller_session_required"):
        _plan(cohort)
    assert db.dirty


def test_missing_role_creation_requires_explicit_definition(cohort):
    permissions = replace(cohort[4], roles=(RoleDefinition("staff"),))
    assert "staff_role_missing" in dict(_plan(cohort, permissions=permissions).blockers)
    with pytest.raises(IdentityEnrolmentRefused):
        _apply(cohort, permissions=permissions)


def test_null_email_plan_is_read_only_and_apply_replays_without_overwrite(cohort):
    db, _, row, scope, _ = cohort
    party = db.scalar(sa.select(Party).where(Party.id == scope.mappings[0].party_id))
    party.email = None
    db.flush()
    reviewed = _plan(cohort)
    assert reviewed.ready and reviewed.party_email_fills == 1
    assert reviewed.sanitized_receipt()["party_email_fills"] == 1
    assert party.email is None
    assert db.scalar(sa.select(UserCredential)) is None
    assert db.scalar(sa.select(PartyRoleGrant)) is None
    _apply(cohort, plan=reviewed)
    assert party.email == row.email.lower()
    assert party.id == row.person_id and party.is_active
    replay = _plan(cohort)
    assert replay.ready and replay.party_email_fills == 0 and replay.credential_replays == 1
    _apply(cohort, plan=replay)
    assert party.email == row.email.lower()
    assert len(db.scalars(sa.select(UserCredential)).all()) == 1
    assert len(db.scalars(sa.select(PartyRoleGrant)).all()) == 1


def test_matching_nonnull_email_is_preserved_exactly(cohort):
    db = cohort[0]
    party = db.scalar(sa.select(Party))
    party.email = "Staff@Example.Test"
    db.flush()
    plan = _plan(cohort)
    assert plan.ready and plan.party_email_fills == 0
    _apply(cohort, plan=plan)
    assert party.email == "Staff@Example.Test"


@pytest.mark.parametrize("existing", ["different@example.test", ""])
def test_nonnull_email_conflicts_are_never_filled(cohort, existing):
    db = cohort[0]
    party = db.scalar(sa.select(Party))
    party.email = existing
    db.flush()
    plan = _plan(cohort)
    assert "destination_party_mismatch" in dict(plan.blockers)
    assert plan.party_email_fills == 0
    with pytest.raises(IdentityEnrolmentRefused, match="identity_plan_blocked"):
        _apply(cohort, plan=plan)
    assert party.email == existing
    assert db.scalar(sa.select(UserCredential)) is None


@pytest.mark.parametrize("party_type", [PartyType.person, PartyType.organization])
def test_null_email_fill_refuses_other_party_email_owner(cohort, party_type):
    db, tenant, row, _, _ = cohort
    party = db.scalar(sa.select(Party))
    party.email = None
    db.add(
        Party(
            id=uuid4(),
            tenant_id=tenant.id,
            party_type=party_type,
            display_name="Other synthetic identity",
            email=row.email.lower(),
            is_active=True,
        )
    )
    db.flush()
    plan = _plan(cohort)
    assert "destination_party_mismatch" in dict(plan.blockers)
    assert plan.party_email_fills == 0
    with pytest.raises(IdentityEnrolmentRefused, match="identity_plan_blocked"):
        _apply(cohort, plan=plan)
    assert party.email is None
    assert db.scalar(sa.select(UserCredential)) is None
    assert db.scalar(sa.select(PartyRoleGrant)) is None


def test_null_email_observation_is_bound_to_reviewed_plan(cohort):
    db, _, row, _, _ = cohort
    party = db.scalar(sa.select(Party))
    party.email = None
    db.flush()
    reviewed = _plan(cohort)
    party.email = row.email.lower()
    db.flush()
    with pytest.raises(IdentityEnrolmentRefused, match="identity_plan_changed"):
        _apply(cohort, plan=reviewed)
    assert db.scalar(sa.select(UserCredential)) is None


def test_null_email_fill_is_rolled_back_with_failed_enrolment(cohort, monkeypatch):
    from dotmac_kernel import identity_storage

    db = cohort[0]
    party = db.scalar(sa.select(Party))
    party.email = None
    db.flush()
    transaction = db.begin_nested()

    def failure(*args, **kwargs):
        raise RuntimeError("synthetic provisioning failure")

    monkeypatch.setattr(identity_storage, "ensure_grant", failure)
    with pytest.raises(IdentityEnrolmentRefused, match="identity_apply_failed_rollback_required"):
        _apply(cohort)
    transaction.rollback()
    db.refresh(party)
    assert party.email is None
    assert db.scalar(sa.select(UserCredential)) is None
    assert db.scalar(sa.select(PartyRoleGrant)) is None
    assert db.scalar(sa.select(Role)) is None


def test_ineligible_selected_source_cannot_fill_null_email(cohort):
    db = cohort[0]
    party = db.scalar(sa.select(Party))
    party.email = None
    db.flush()
    rows = (_replace_source(cohort[2], is_active=False),)
    plan = _plan(cohort, rows)
    assert "inactive_source" in dict(plan.blockers) and plan.party_email_fills == 0
    with pytest.raises(IdentityEnrolmentRefused, match="identity_plan_blocked"):
        _apply(cohort, rows, plan=plan)
    assert party.email is None
