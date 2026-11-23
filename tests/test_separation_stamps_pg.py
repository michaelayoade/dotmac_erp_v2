"""D-9 on PostgreSQL: the policy reaches dotmac-payroll a7, ERP records what it passed, and the saved check finds
relaxed events created while the policy was ON (safeguards 1, 3 and 4 behaviour).

Reuses the B4 payroll world (real accounting, payroll and kernel migrations).
"""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
import sqlalchemy as sa
import test_b4_payroll_finalize_pg as b4
from dotmac_kernel.audit import AuditEvent
from dotmac_payroll import service as payroll_service
from dual_control_support import set_dual_control_engine
from sqlalchemy.orm import Session

from erp_v2.governance import cli as governance_cli
from erp_v2.governance import policy as governance_policy
from erp_v2.governance import sod_verification as verify
from erp_v2.web import payroll_run_services as svc

engine = b4.engine
_registries = b4._registries


def _spy(monkeypatch, name):
    calls = []
    real = getattr(payroll_service, name)

    def wrapper(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(payroll_service, name, wrapper)
    return calls


def _event(engine, tenant_id, action):
    with Session(engine) as db:
        return list(
            db.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == action)
                .order_by(AuditEvent.created_at)
            )
        )


def _set_policy_with_audit(engine, tenant_id, on: bool) -> None:
    # The seed writes the policy ON without an audit event; the CLI only audits a change, so start from OFF.
    if on:
        set_dual_control_engine(engine, tenant_id, False)
    spec = governance_cli.PolicySpec(tenant_id, on, "test evidence")
    with Session(engine) as db, db.begin():
        plan = governance_cli.apply_policy(
            db, spec, spec_sha256=hashlib.sha256(str(on).encode()).hexdigest(), operator_ref="test-operator"
        )
        assert plan.ready


def test_policy_on_is_passed_to_payroll_and_recorded_on_the_run_events(engine, monkeypatch):
    env = b4.world(engine)  # the seed turns the policy ON
    approve_calls = _spy(monkeypatch, "approve_payroll_run")
    finalize_calls = _spy(monkeypatch, "finalize_payroll_run")
    run_id = b4.ready_to_finalize(engine, env)
    b4.finalize(engine, env, run_id)
    assert [c["separation_of_duties"] for c in approve_calls] == [True]
    assert [c["separation_of_duties"] for c in finalize_calls] == [True]
    (approved,) = _event(engine, env.tenant_id, svc.AUDIT_APPROVED)
    (finalized,) = _event(engine, env.tenant_id, svc.AUDIT_FINALIZED_POSTED)
    assert approved.details["separation_enforced"] is True
    assert finalized.details["separation_enforced"] is True


def test_policy_off_is_passed_to_payroll_and_recorded_on_the_run_events(engine, monkeypatch):
    env = b4.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    approve_calls = _spy(monkeypatch, "approve_payroll_run")
    finalize_calls = _spy(monkeypatch, "finalize_payroll_run")
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    b4.approve(engine, env, run_id, party_index=0)
    b4.finalize(engine, env, run_id, party_index=0)
    assert [c["separation_of_duties"] for c in approve_calls] == [False]
    assert [c["separation_of_duties"] for c in finalize_calls] == [False]
    (approved,) = _event(engine, env.tenant_id, svc.AUDIT_APPROVED)
    (finalized,) = _event(engine, env.tenant_id, svc.AUDIT_FINALIZED_POSTED)
    assert approved.details["separation_enforced"] is False
    assert finalized.details["separation_enforced"] is False


def test_payroll_a7_enforces_when_erp_passes_true(engine):
    """The owner's own check is live: a direct call with True and the creator as approver refuses."""
    env = b4.world(engine)
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    with Session(engine) as db, pytest.raises(payroll_service.PayrollRuleViolation):
        payroll_service.approve_payroll_run(
            db,
            tenant_id=env.tenant_id,
            run_id=run_id,
            approved_by_id=env.party_ids[0],
            approved_at=b4.NOW,
            separation_of_duties=True,
        )


def test_saved_check_finds_a_relaxed_event_created_while_the_policy_was_on(engine, monkeypatch):
    env = b4.world(engine)
    _set_policy_with_audit(engine, env.tenant_id, True)
    # Drift: the policy reads ON in the timeline but the value ERP passed was OFF (a fault the check must surface).
    monkeypatch.setattr(governance_policy, "separation_of_duties", lambda db, tenant_id: False)
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    b4.approve(engine, env, run_id, party_index=0)
    monkeypatch.undo()
    with Session(engine) as db:
        report = verify.verify(db, env.tenant_id)
    assert not report.clean
    assert [(f.source, f.action) for f in report.findings] == [("payroll_event", svc.AUDIT_APPROVED)]
    assert report.findings[0].entity_id == str(run_id)
    assert report.on_windows == 1


def test_saved_check_is_clean_for_honest_stamps_and_for_events_while_off(engine):
    env = b4.world(engine)
    _set_policy_with_audit(engine, env.tenant_id, True)
    run_id = b4.ready_to_finalize(engine, env)  # ON: stamped True
    b4.finalize(engine, env, run_id)
    _set_policy_with_audit(engine, env.tenant_id, False)
    second = b4.new_run(engine, env, ref="RUN-2026-02", start=date(2026, 2, 1), end=date(2026, 2, 28))
    b4.batch(engine, env, second)
    b4.approve(engine, env, second, party_index=0)  # OFF: stamped False, outside any ON window
    with Session(engine) as db:
        report = verify.verify(db, env.tenant_id)
    assert report.clean, report.receipt()
    assert report.on_windows == 1


def test_saved_check_with_the_policy_never_set_has_no_windows_and_no_findings(engine):
    env = b4.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)  # no audit event: the CLI is the only audited writer
    with Session(engine) as db:
        report = verify.verify(db, env.tenant_id)
    assert report.clean and report.on_windows == 0
