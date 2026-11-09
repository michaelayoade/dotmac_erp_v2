"""Banking observation unit canaries. PostgreSQL RLS/races require hosted integration."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_banking.models import BankAccount, BankInstitution, BankStatement, BankStatementLine
from dotmac_kernel.machine_auth import MachinePrincipal
from dotmac_kernel.machine_models import MachineCredential
from dotmac_kernel.models import Base, Tenant
from pydantic import ValidationError
from sqlalchemy.orm import Session

from erp_v2.banking.contracts import BankingObservationEnvelope
from erp_v2.banking.models import BankingObservationReceipt, BankingProviderBinding
from erp_v2.banking.service import (
    MIRROR_SCOPE,
    WRITE_SCOPE,
    BankingObservationCollision,
    BankingObservationError,
    accept_observation,
    compare_observation,
)


def fixture_document(installation_id):
    # Matches dotmac-connector-mono plugin._transaction neutral wire exactly.
    return {
        "schema_version": "dotmac.io/product-observation/v1",
        "capability_id": "banking.transaction.observation.v1",
        "contract_version": 1,
        "source": {"installation_id": str(installation_id), "connector_key": "mono"},
        "provider_event_id": "txn-1",
        "event_type": "banking.transaction.observation.v1",
        "scope": {"kind": "tenant", "ref": "trusted-scope"},
        "observation": {
            "capability_id": "banking.transaction.observation.v1",
            "provider_account_id": "mono-account",
            "provider_transaction_id": "txn-1",
            "amount_minor": "12345",
            "currency": "NGN",
            "direction": "credit",
            "occurred_at": "2026-11-08T23:30:00Z",
            "arrival_mode": "poll",
            "balance_minor": "15000",
            "narration": "Settlement",
        },
    }


@pytest.fixture()
def banking():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_banking"))
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            MachineCredential.__table__,
            BankInstitution.__table__,
            BankAccount.__table__,
            BankStatement.__table__,
            BankStatementLine.__table__,
            BankingProviderBinding.__table__,
            BankingObservationReceipt.__table__,
        ],
    )
    with Session(engine) as db:
        tenant = Tenant(id=uuid4(), slug="banking", name="Banking")
        db.add(tenant)
        principal = MachinePrincipal(
            uuid4(), tenant.id, "Integrator", "dotmac-integrator", frozenset({WRITE_SCOPE, MIRROR_SCOPE})
        )
        institution = BankInstitution(
            id=uuid4(), tenant_id=tenant.id, code="TEST", name="Bank", country_code="NG", status="active"
        )
        db.add(institution)
        account = BankAccount(
            id=uuid4(),
            tenant_id=tenant.id,
            institution_id=institution.id,
            account_code="BANK",
            account_name="Current",
            account_identifier="test",
            account_type_code="current",
            currency_code="NGN",
            minor_units=2,
            cash_account_ref="GL:1",
            status="active",
        )
        db.add(account)
        binding = BankingProviderBinding(
            id=uuid4(),
            tenant_id=tenant.id,
            credential_id=principal.credential_id,
            source_application=principal.application,
            installation_id=uuid4(),
            provider_account_id="mono-account",
            account_id=account.id,
            scope_kind="tenant",
            scope_ref="trusted-scope",
            enabled=True,
        )
        db.add(binding)
        db.flush()
        yield db, principal, binding, account
    engine.dispose()


def accept(db, principal, document, key="delivery-1"):
    return accept_observation(
        db,
        principal=principal,
        envelope=BankingObservationEnvelope.model_validate(document),
        idempotency_key=key,
        correlation_id="correlation-1",
        received_at=datetime.now(UTC),
    )


def test_connector_fixture_replay_and_lagos_date(banking):
    db, principal, binding, _ = banking
    document = fixture_document(binding.installation_id)
    first = accept(db, principal, document)
    second = accept(db, principal, document)
    assert first.observation_id == second.observation_id and second.replayed
    line = db.scalar(sa.select(BankStatementLine))
    assert line.amount == Decimal("123.45")
    assert line.transaction_date == date(2026, 11, 9)
    assert db.scalar(sa.select(sa.func.count()).select_from(BankStatement)) == 1
    assert compare_observation(db, principal=principal, envelope=BankingObservationEnvelope.model_validate(document))[
        "agrees"
    ]


def test_payload_collision_and_changed_key_refused(banking):
    db, principal, binding, _ = banking
    document = fixture_document(binding.installation_id)
    accept(db, principal, document)
    with pytest.raises(BankingObservationCollision):
        accept(db, principal, document, key="different-delivery")
    document["observation"]["amount_minor"] = "12346"
    with pytest.raises(BankingObservationCollision):
        accept(db, principal, document)
    assert db.scalar(sa.select(sa.func.count()).select_from(BankStatement)) == 1


@pytest.mark.parametrize("identity", ["tenant_id", "credential_id", "application", "scopes"])
def test_principal_cannot_select_another_mapping(banking, identity):
    db, principal, binding, _ = banking
    value = frozenset() if identity == "scopes" else "other" if identity == "application" else uuid4()
    with pytest.raises(BankingObservationError):
        accept(db, replace(principal, **{identity: value}), fixture_document(binding.installation_id))
    assert db.scalar(sa.select(sa.func.count()).select_from(BankStatement)) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", "USD"),
        ("amount_minor", "0"),
        ("amount_minor", "999999999999999999"),
        ("provider_account_id", "other-account"),
    ],
)
def test_inadmissible_values_refuse_before_write(banking, field, value):
    db, principal, binding, _ = banking
    document = fixture_document(binding.installation_id)
    document["observation"][field] = value
    with pytest.raises(BankingObservationError):
        accept(db, principal, document)
    assert db.scalar(sa.select(sa.func.count()).select_from(BankStatement)) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("amount_minor", 123), ("amount_minor", "1.25"), ("amount_minor", "-1"), ("occurred_at", "2026-11-09T00:00:00")],
)
def test_wire_amount_and_timezone_are_exact(field, value):
    document = fixture_document(uuid4())
    document["observation"][field] = value
    with pytest.raises(ValidationError):
        BankingObservationEnvelope.model_validate(document)


def test_disabled_binding_only_allows_read_only_comparison(banking):
    db, principal, binding, _ = banking
    binding.enabled = False
    document = fixture_document(binding.installation_id)
    with pytest.raises(BankingObservationError):
        accept(db, principal, document)
    assert (
        compare_observation(db, principal=principal, envelope=BankingObservationEnvelope.model_validate(document))[
            "verdict"
        ]
        == "missing"
    )
    assert db.scalar(sa.select(sa.func.count()).select_from(BankStatement)) == 0


def test_caller_rollback_removes_receipt_and_statement(banking):
    db, principal, binding, _ = banking
    with pytest.raises(RuntimeError, match="cancel"):
        with db.begin_nested():
            accept(db, principal, fixture_document(binding.installation_id))
            raise RuntimeError("cancel")
    assert db.scalar(sa.select(sa.func.count()).select_from(BankStatement)) == 0
    assert db.scalar(sa.select(sa.func.count()).select_from(BankingObservationReceipt)) == 0


@pytest.mark.parametrize("model", [BankingProviderBinding, BankingObservationReceipt])
def test_foreign_keys_reference_actual_owner_unique_keys(model):
    # SQLite accepts a composite FK whose remote columns are not unique;
    # clean PostgreSQL genesis correctly refuses it. Check the released
    # owner metadata as well as exercising genesis on the test server.
    for foreign_key in model.__table__.foreign_key_constraints:
        target = foreign_key.referred_table
        referenced = {element.column.name for element in foreign_key.elements}
        unique_keys = [set(target.primary_key.columns.keys())]
        unique_keys.extend(
            set(constraint.columns.keys())
            for constraint in target.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        )
        assert referenced in unique_keys, (model.__name__, target.fullname, referenced)
