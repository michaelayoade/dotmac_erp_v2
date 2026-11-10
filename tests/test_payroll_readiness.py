"""Selected-run evidence never confers approval authority or changes payroll."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from test_payroll_actions import _calculate
from test_payroll_actions import payroll as payroll
from test_workflow_route_security import browser as browser

from erp_v2.web import payroll_readiness, payroll_routes


def test_selected_run_is_read_only_and_policy_still_blocks(payroll):
    db, tenant, _, _, run = payroll
    calculation = _calculate(db, tenant, run)
    before = (run.status, calculation.gross_amount, calculation.net_amount)
    statements = []

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", record_statement)
    try:
        packet = payroll_readiness.approval_readiness(db, tenant_id=tenant.id, run_id=run.id)
    finally:
        event.remove(db.bind, "before_cursor_execute", record_statement)
    assert packet["calculation_count"] == 1 and packet["line_count"] == 2
    assert packet["totals"]["net_amount"] == Decimal("450.01")
    assert packet["calculation_evidence_ready"] is True
    assert packet["approval_authorized"] is False and packet["approval_blockers"]
    assert before == (run.status, calculation.gross_amount, calculation.net_amount)
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert not db.new and not db.dirty and not db.deleted


def test_missing_and_foreign_run_are_indistinguishable(payroll):
    db, tenant, other, _, run = payroll
    _calculate(db, tenant, run)
    assert payroll_readiness.approval_readiness(db, tenant_id=other.id, run_id=run.id) is None
    assert payroll_readiness.approval_readiness(db, tenant_id=tenant.id, run_id=uuid4()) is None


def test_empty_run_never_has_ready_evidence(payroll):
    db, tenant, _, _, run = payroll
    packet = payroll_readiness.approval_readiness(db, tenant_id=tenant.id, run_id=run.id)
    assert packet["calculation_count"] == packet["line_count"] == 0
    assert packet["calculation_evidence_ready"] is False
    assert "No employee calculations" in packet["evidence_blockers"][0]


def test_calculation_without_component_lines_is_blocked(payroll):
    db, tenant, _, _, run = payroll
    _calculate(db, tenant, run)
    for line in db.scalars(select(payroll_readiness.PayrollCalculationLine)):
        db.delete(line)
    db.flush()
    packet = payroll_readiness.approval_readiness(db, tenant_id=tenant.id, run_id=run.id)
    assert packet["calculation_evidence_ready"] is False
    assert any("no component lines" in item for item in packet["evidence_blockers"])


def test_read_does_not_autoflush_pending_changes(payroll):
    db, tenant, _, _, run = payroll
    _calculate(db, tenant, run)
    run.run_ref = "PENDING"
    flushes = []

    def record_flush(session, flush_context, instances):
        flushes.append(True)

    event.listen(db, "before_flush", record_flush)
    try:
        payroll_readiness.approval_readiness(db, tenant_id=tenant.id, run_id=run.id)
    finally:
        event.remove(db, "before_flush", record_flush)
    assert not flushes and run in db.dirty


def test_missing_line_reference_and_disagreeing_totals_block_without_repair(payroll):
    db, tenant, _, _, run = payroll
    calculation = _calculate(db, tenant, run)
    line = db.scalar(select(payroll_readiness.PayrollCalculationLine))
    line.evidence_ref = ""
    line.amount += Decimal("1")
    db.flush()
    packet = payroll_readiness.approval_readiness(db, tenant_id=tenant.id, run_id=run.id)
    assert packet["calculation_evidence_ready"] is False
    assert any("lack an evidence reference" in item for item in packet["evidence_blockers"])
    assert any("disagree" in item for item in packet["evidence_blockers"])
    assert calculation.gross_amount == Decimal("500.01")
    assert packet["totals"]["gross_amount"] == calculation.gross_amount
    assert not db.dirty


@pytest.mark.parametrize("role,expected", [(None, 302), ("staff", 403), ("unprivileged", 403), ("admin", 404)])
def test_mounted_readiness_requires_payroll_manage(browser, monkeypatch, role, expected):
    read = Mock(return_value=None)
    monkeypatch.setattr(payroll_routes.payroll_readiness, "approval_readiness", read)
    browser.sign_in(role)
    run_id = uuid4()
    response = browser.client.get(f"/payroll/runs/{run_id}/approval-readiness")
    assert response.status_code == expected
    if role == "admin":
        read.assert_called_once()
        assert read.call_args.kwargs == {"tenant_id": browser.tenant.id, "run_id": run_id}
    else:
        read.assert_not_called()
    for action in browser.actions:
        action.assert_not_called()


def test_authorized_populated_packet_renders_privately(browser, monkeypatch):
    run_id = uuid4()
    packet = {
        "run": SimpleNamespace(
            run_ref="REVIEW-RUN",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            currency_code="NGN",
            status="draft",
        ),
        "calculation_count": 1,
        "line_count": 2,
        "totals": {
            field: Decimal("10.00")
            for field in ("gross_amount", "deduction_amount", "net_amount", "employer_liability_amount")
        },
        "calculation_evidence_ready": True,
        "evidence_blockers": [],
        "approval_authorized": False,
        "approval_blockers": ["Expected employee roster completeness has not been verified."],
    }
    read = Mock(return_value=packet)
    monkeypatch.setattr(payroll_readiness, "approval_readiness", read)
    browser.sign_in("admin")
    response = browser.client.get(f"/payroll/runs/{run_id}/approval-readiness")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "REVIEW-RUN" in response.text and "Approval authorization: blocked" in response.text
    assert "Expected employee roster completeness" in response.text
    read.assert_called_once()
    assert read.call_args.kwargs == {"tenant_id": browser.tenant.id, "run_id": run_id}
    for action in browser.actions:
        action.assert_not_called()
