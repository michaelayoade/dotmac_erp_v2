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
    assert sorted((f.source, f.action, f.kind) for f in report.findings) == [
        ("payroll_event", svc.AUDIT_APPROVED, "relaxed"),
        ("payroll_event", svc.AUDIT_BATCH_CALCULATED, "relaxed"),
    ]
    assert {f.entity_id for f in report.findings} == {str(run_id)}
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


# --- stamp-or-policy across a policy flip (D-9 MED-1) --------------------------------------------------------


def test_batch_event_records_the_policy_in_force(engine):
    env = b4.world(engine)  # ON
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    (batch_event,) = _event(engine, env.tenant_id, svc.AUDIT_BATCH_CALCULATED)
    assert batch_event.details["separation_enforced"] is True


def test_policy_turned_off_after_batch_still_refuses_the_creator_approving(engine):
    env = b4.world(engine)  # ON at batch
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    set_dual_control_engine(engine, env.tenant_id, False)
    b4.refusal(engine, env, svc.approve, run_id, "sod_creator_is_approver", 0)
    assert b4.status(engine, run_id) == "draft"
    b4.approve(engine, env, run_id, party_index=1)  # another person still may


def test_policy_turned_off_after_approve_still_refuses_the_approver_finalizing(engine):
    env = b4.world(engine)  # ON at batch and approve
    run_id = b4.ready_to_finalize(engine, env)  # approved by party 1
    set_dual_control_engine(engine, env.tenant_id, False)
    b4.refusal(engine, env, svc.finalize, run_id, "sod_approver_is_finalizer", 1)
    b4.refusal(engine, env, svc.finalize, run_id, "sod_creator_is_finalizer", 0)
    assert b4.status(engine, run_id) == "approved"
    b4.finalize(engine, env, run_id, party_index=2)
    assert b4.status(engine, run_id) == "finalized"


def test_policy_off_throughout_is_never_tightened_by_the_stamps(engine):
    env = b4.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    b4.approve(engine, env, run_id, party_index=0)
    b4.finalize(engine, env, run_id, party_index=0)
    assert b4.status(engine, run_id) == "finalized"


def test_policy_turned_on_after_an_off_batch_enforces_from_the_live_value(engine):
    env = b4.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    set_dual_control_engine(engine, env.tenant_id, True)
    b4.refusal(engine, env, svc.approve, run_id, "sod_creator_is_approver", 0)


# --- the saved check refuses rather than reporting clean on missing evidence (D-9 MED-2) -----------------------


def test_saved_check_refuses_when_the_policy_is_on_with_no_audit_event(engine):
    env = b4.world(engine)  # the seed writes the policy ON without any dual_control_set event
    with Session(engine) as db, pytest.raises(verify.TimelineInconsistent):
        verify.verify(db, env.tenant_id)


def test_saved_check_refuses_when_the_timeline_says_on_but_the_row_is_off(engine):
    env = b4.world(engine)
    _set_policy_with_audit(engine, env.tenant_id, True)
    set_dual_control_engine(engine, env.tenant_id, False)  # changed outside the audited CLI
    with Session(engine) as db, pytest.raises(verify.TimelineInconsistent):
        verify.verify(db, env.tenant_id)


def test_saved_check_refuses_when_the_row_was_changed_long_after_the_last_event(engine):
    env = b4.world(engine)
    _set_policy_with_audit(engine, env.tenant_id, True)
    with Session(engine) as db, db.begin():
        db.execute(
            sa.text("UPDATE erp_governance_policies SET set_at = set_at + interval '2 hours' WHERE tenant_id = :t"),
            {"t": env.tenant_id},
        )
    with Session(engine) as db, pytest.raises(verify.TimelineInconsistent):
        verify.verify(db, env.tenant_id)


def test_saved_check_counts_an_in_window_payroll_event_without_the_key_as_a_finding(engine):
    from dotmac_kernel.audit import write_audit_event

    env = b4.world(engine)
    _set_policy_with_audit(engine, env.tenant_id, True)
    run_id = b4.new_run(engine, env)
    with Session(engine) as db, db.begin():
        write_audit_event(
            db,
            tenant_id=env.tenant_id,
            actor_type="service",
            actor_id="legacy",
            source_application="dotmac_erp",
            action=svc.AUDIT_APPROVED,
            entity_type="payroll_run",
            entity_id=str(run_id),
            is_success=True,
            details={"created_by": "x"},  # no separation_enforced key
        )
    with Session(engine) as db:
        report = verify.verify(db, env.tenant_id)
    assert [(f.kind, f.action) for f in report.findings] == [("unstamped", svc.AUDIT_APPROVED)]


# --- command exit codes: 0 clean, 3 findings, 2 refusal -------------------------------------------------------


def _main(engine, monkeypatch, capsys, tenant, *, url=True, serving_dsn=False):
    if url:
        monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    else:
        monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    if serving_dsn:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://serving.invalid/db")
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    code = verify.main(["--tenant-id", str(tenant)])
    return code, capsys.readouterr().out


def test_main_exit_0_when_clean(engine, monkeypatch, capsys):
    env = b4.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    code, out = _main(engine, monkeypatch, capsys, env.tenant_id)
    assert code == 0 and '"clean": true' in out


def test_main_exit_3_on_findings(engine, monkeypatch, capsys):
    env = b4.world(engine)
    _set_policy_with_audit(engine, env.tenant_id, True)
    monkeypatch.setattr(governance_policy, "separation_of_duties", lambda db, tenant_id: False)
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    monkeypatch.undo()
    code, out = _main(engine, monkeypatch, capsys, env.tenant_id)
    assert code == 3 and '"relaxed"' in out


def test_main_exit_2_when_the_timeline_is_inconsistent(engine, monkeypatch, capsys):
    env = b4.world(engine)  # policy ON, no event
    code, out = _main(engine, monkeypatch, capsys, env.tenant_id)
    assert code == 2 and "policy_timeline_inconsistent" in out


@pytest.mark.parametrize("case", ["no_dsn", "serving_dsn", "bad_tenant"])
def test_main_exit_2_on_refused_inputs(engine, monkeypatch, capsys, case):
    env = b4.world(engine)
    tenant = "not-a-uuid" if case == "bad_tenant" else env.tenant_id
    code, out = _main(engine, monkeypatch, capsys, tenant, url=case != "no_dsn", serving_dsn=case == "serving_dsn")
    assert code == 2 and "refusal" in out


def test_main_runs_in_a_read_only_transaction(engine, monkeypatch, capsys):
    env = b4.world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    seen = []

    def spy(db, tenant_id):
        seen.append(db.scalar(sa.text("SHOW transaction_read_only")))
        return verify.Report((), False, 0)

    monkeypatch.setattr(verify, "verify", spy)
    code, _ = _main(engine, monkeypatch, capsys, env.tenant_id)
    assert code == 0 and seen == ["on"]
