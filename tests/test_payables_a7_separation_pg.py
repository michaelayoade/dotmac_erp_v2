"""Payables a7 separation policy (D-9) on PostgreSQL: the policy reaches the owner, the stamp is read back.

Skipped until ``dotmac-payables==0.1.0a7`` is the pinned version (published 24-26 Nov). Reuses the B3 world.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import test_b3_supplier_payments_pg as b3
from dotmac_payables.models import SupplierPayment
from dual_control_support import set_dual_control_engine
from sqlalchemy.orm import Session

from erp_v2.supplier_payments import separation
from erp_v2.supplier_payments import service as svc
from erp_v2.supplier_payments.service import AllocationRequest

pytestmark = pytest.mark.skipif(
    not separation.payables_separation_supported(),
    reason="TODO(payables-a7): dotmac-payables 0.1.0a7 is not pinned yet",
)

engine = b3.engine
registries = b3.registries


_ENGINE: list = []


def _stamp(payment_id) -> bool:
    with Session(_ENGINE[0]) as db:
        return db.get(SupplierPayment, payment_id).separation_enforced


@pytest.fixture(autouse=True)
def _remember_engine(engine):
    _ENGINE[:] = [engine]


def test_prepare_stamps_the_tenant_policy_on_the_row_on(engine):
    env = b3.world(engine)
    set_dual_control_engine(engine, env.tenant_id, True)
    created = b3.prepare(engine, env, b3.open_obligation(engine, env))
    assert _stamp(created.payment_id) is True


def test_prepare_stamps_the_tenant_policy_on_the_row_off_and_one_person_can_do_every_step(engine):
    env = b3.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    created = b3.prepare(engine, env, b3.open_obligation(engine, env))
    assert _stamp(created.payment_id) is False
    b3.approve(engine, env, created.payment_id, who="preparer")  # a7 default OFF: the owner no longer refuses
    b3.record(engine, env, created.payment_id, who="preparer")
    b3.reverse(engine, env, created.payment_id, who="preparer")
    assert b3.audits(engine, env, svc.AUDIT_SOD_STAMP_FLAGGED) == []  # the stamp always matched


def test_a_stamp_that_disagrees_with_the_policy_at_prepare_is_refused_and_nothing_is_saved(engine, monkeypatch):
    env = b3.world(engine)
    set_dual_control_engine(engine, env.tenant_id, True)
    oid = b3.open_obligation(engine, env)
    monkeypatch.setattr(svc.separation, "payables_kwargs", lambda db, tenant_id: {"separation_of_duties": False})
    before = b3.counts(engine, env)
    b3.refused(lambda: b3.prepare(engine, env, oid), "sod_stamp_mismatch")
    assert b3.counts(engine, env) == before


def test_a_relaxed_row_under_policy_on_is_flagged_and_still_enforced(engine):
    env = b3.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    created = b3.prepare(engine, env, b3.open_obligation(engine, env))  # stamped OFF
    set_dual_control_engine(engine, env.tenant_id, True)
    b3.approve(engine, env, created.payment_id)  # a different person: allowed, but the stamp is flagged
    (event,) = b3.audits(engine, env, svc.AUDIT_SOD_STAMP_FLAGGED)
    assert event.details == {"step": "approve", "verdict": "relaxed_under_policy_on", "stamp": False, "policy": True}
    b3.refused(lambda: b3.record(engine, env, created.payment_id, who="approver"), "sod_recorder_is_approver")


def test_a_stricter_row_under_policy_off_is_flagged(engine):
    env = b3.world(engine)
    set_dual_control_engine(engine, env.tenant_id, True)
    created = b3.prepare(engine, env, b3.open_obligation(engine, env))  # stamped ON
    set_dual_control_engine(engine, env.tenant_id, False)
    b3.approve(engine, env, created.payment_id)
    (event,) = b3.audits(engine, env, svc.AUDIT_SOD_STAMP_FLAGGED)
    assert event.details["verdict"] == "stricter_than_policy"


def test_the_saved_check_finds_a_relaxed_payment_created_while_the_policy_was_on(engine, monkeypatch):
    from erp_v2.governance import cli as governance_cli
    from erp_v2.governance import sod_verification as verify

    env = b3.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    with Session(engine) as db, db.begin():
        governance_cli.apply_policy(
            db,
            governance_cli.PolicySpec(env.tenant_id, True, "test evidence"),
            spec_sha256="0" * 64,
            operator_ref="test-operator",
        )
    # Drift: the policy is ON, but a relaxed payment was prepared (a bug the saved check exists to catch).
    monkeypatch.setattr(svc.separation, "payables_kwargs", lambda db, tenant_id: {"separation_of_duties": False})
    monkeypatch.setattr(svc, "_read_back_stamp", lambda *a, **k: None)
    created = b3.act(  # a real "now": the policy event above is stamped by the database clock
        engine,
        env,
        "preparer",
        svc.prepare_payment,
        request_id=uuid4(),
        bank_account_id=env.bank_id,
        value_date=b3.VALUE_DATE,
        allocations=[AllocationRequest(b3.open_obligation(engine, env), Decimal(215))],
        now=datetime.now(UTC),
    )
    with Session(engine) as db:
        report = verify.verify(db, env.tenant_id)
    assert report.payables_stamp_available is True
    assert [(f.source, f.entity_id) for f in report.findings] == [("supplier_payment", str(created.payment_id))]
