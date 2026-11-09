"""Payroll forms exercise the released owner, including refusals and isolation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_kernel.models import Base, Party, Tenant
from dotmac_kernel.money import Currency
from dotmac_payroll import service
from dotmac_payroll.contracts import PayComponentInput, PayRuleInput
from dotmac_payroll.models import TENANT_MODELS, PayrollCalculation, PayrollCalculationLine, PayrollRun
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from erp_v2.web import payroll_actions as actions

NOW = datetime(2026, 1, 31, tzinfo=UTC)


@pytest.fixture
def payroll():
    engine = create_engine("sqlite://", execution_options={"schema_translate_map": {"mod_payroll": None}})
    Base.metadata.create_all(engine, tables=[Tenant.__table__, *(m.__table__ for m in TENANT_MODELS)])
    with Session(engine) as db:
        tenant = Tenant(slug="payroll", name="Payroll")
        other = Tenant(slug="other", name="Other")
        db.add_all([tenant, other])
        db.flush()
        party = Party(id=uuid4())
        base = service.create_pay_component(
            db,
            tenant_id=tenant.id,
            command=PayComponentInput(
                component_code="BASE",
                name="Base",
                kind="earning",
                expense_account_ref="ledger:salary",
                liability_account_ref=None,
                liability_destination_ref=None,
            ),
        )
        deduction = service.create_pay_component(
            db,
            tenant_id=tenant.id,
            command=PayComponentInput(
                component_code="TAX",
                name="Tax",
                kind="deduction",
                expense_account_ref=None,
                liability_account_ref="ledger:tax",
                liability_destination_ref="tax:authority",
            ),
        )
        structure = service.create_pay_structure(
            db, tenant_id=tenant.id, code="MONTHLY", name="Monthly", currency=Currency("NGN", 2)
        )
        revision = service.publish_pay_structure_revision(
            db,
            tenant_id=tenant.id,
            structure_id=structure.id,
            version=1,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            net_payable_account_ref="ledger:net",
            published_by_id=party.id,
            published_at=NOW,
            rules=tuple(
                PayRuleInput(
                    component_id=c.id,
                    sequence=i,
                    calculation_method="input",
                    fixed_amount=None,
                    rate=None,
                    basis_component_ids=(),
                    prorates=(i == 1),
                )
                for i, c in enumerate((base, deduction), 1)
            ),
        )
        service.assign_employee_pay_structure(
            db,
            tenant_id=tenant.id,
            employee_ref="employee:1",
            revision_id=revision.id,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            source_ref="people:assignment",
            source_version="1",
        )
        run = actions.create_draft(
            db,
            tenant=tenant,
            party=party,
            run_ref="JAN",
            period_start="2026-01-01",
            period_end="2026-01-31",
            currency_code="NGN",
            minor_units="2",
        )
        yield db, tenant, other, party, run
    engine.dispose()


def _calculate(db, tenant, run, **overrides):
    values = dict(
        run_id=run.id,
        employee_ref="employee:1",
        proration_factor="0.5",
        component_inputs="BASE | 1000.02 | compensation:1\nTAX | 50.00 | tax:1",
    )
    values.update(overrides)
    return actions.calculate_employee(db, tenant=tenant, **values)


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_calculation_uses_released_owner_and_evidence(payroll):
    db, tenant, _, party, run = payroll
    assert run.status == "draft" and run.created_by_id == party.id
    calculated = _calculate(db, tenant, run)
    assert calculated.gross_amount == Decimal("500.01")
    assert calculated.net_amount == Decimal("450.01")
    lines = db.scalars(select(PayrollCalculationLine).order_by(PayrollCalculationLine.sequence)).all()
    assert [line.evidence_ref for line in lines] == ["compensation:1", "tax:1"]
    assert run.status == "draft" and db.in_transaction()
    db.rollback()
    assert _count(db, PayrollCalculation) == 0


@pytest.mark.parametrize("factor", ["NaN", "Infinity", "1e-2", "-0.1", "1.1", "0.123456789"])
def test_invalid_proration_writes_no_rows(payroll, factor):
    db, tenant, _, _, run = payroll
    with pytest.raises(actions.PayrollActionError):
        _calculate(db, tenant, run, proration_factor=factor)
    assert _count(db, PayrollCalculation) == 0


@pytest.mark.parametrize(
    "inputs",
    [
        "BASE | 1000.001 | evidence",
        "BASE | NaN | evidence",
        "BASE | 100 | ",
        "BASE | 100 | e\nBASE | 100 | e",
        "UNKNOWN | 100 | e",
        "BASE,100,e",
        "BASE | 100 | e",
        "BASE | 10 | e\nTAX | 100 | tax:1",
    ],
)
def test_invalid_or_refused_input_has_no_partial_evidence(payroll, inputs):
    db, tenant, _, _, run = payroll
    with pytest.raises(actions.PayrollActionError):
        _calculate(db, tenant, run, component_inputs=inputs)
    assert _count(db, PayrollCalculation) == _count(db, PayrollCalculationLine) == 0
    assert _calculate(db, tenant, run).net_amount == Decimal("450.01")


def test_tenant_crossing_is_refused(payroll):
    db, tenant, other, _, run = payroll
    with pytest.raises(actions.PayrollActionError, match="not found"):
        _calculate(db, other, run)
    other_run = service.create_payroll_run(
        db,
        tenant_id=other.id,
        run_ref="JAN",
        period_start=run.period_start,
        period_end=run.period_end,
        currency=Currency("NGN", 2),
        created_by_id=uuid4(),
        created_at=NOW,
    )
    with pytest.raises(actions.PayrollActionError):
        _calculate(db, other, other_run)
    assert _count(db, PayrollCalculation) == 0
    assert _calculate(db, tenant, run).net_amount == Decimal("450.01")


def test_duplicate_employee_and_non_draft_refused(payroll):
    db, tenant, _, _, run = payroll
    _calculate(db, tenant, run)
    with pytest.raises(actions.PayrollActionError, match="already"):
        _calculate(db, tenant, run)
    run.status = "approved"
    db.flush()
    with pytest.raises(actions.PayrollActionError, match="draft"):
        _calculate(db, tenant, run)
    assert _count(db, PayrollCalculation) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        dict(period_end="2025-12-31"),
        dict(period_start="bad"),
        dict(minor_units="7"),
        dict(currency_code="123"),
        dict(run_ref=""),
        dict(run_ref="JAN"),
    ],
)
def test_invalid_or_duplicate_run_refused_and_session_reusable(payroll, overrides):
    db, tenant, _, party, _ = payroll
    values = dict(
        run_ref="OTHER", period_start="2026-01-01", period_end="2026-01-31", currency_code="NGN", minor_units="2"
    )
    values.update(overrides)
    with pytest.raises(actions.PayrollActionError):
        actions.create_draft(db, tenant=tenant, party=party, **values)
    assert _count(db, PayrollRun) == 1
    values.update(
        run_ref="VALID", period_start="2026-01-01", period_end="2026-01-31", currency_code="NGN", minor_units="2"
    )
    assert actions.create_draft(db, tenant=tenant, party=party, **values).status == "draft"


def test_service_failure_after_flush_rolls_back_savepoint(payroll, monkeypatch):
    db, tenant, _, _, run = payroll
    original = service.calculate_employee_payroll

    def fail_after_flush(*args, **kwargs):
        original(*args, **kwargs)
        raise service.PayrollRuleViolation("refused after flush")

    with monkeypatch.context() as patch:
        patch.setattr(service, "calculate_employee_payroll", fail_after_flush)
        with pytest.raises(actions.PayrollActionError, match="after flush"):
            _calculate(db, tenant, run)
    assert _count(db, PayrollCalculation) == _count(db, PayrollCalculationLine) == 0
    assert _calculate(db, tenant, run).net_amount == Decimal("450.01")
