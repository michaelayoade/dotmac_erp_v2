"""Synthetic fixtures for hosted CI only; no product bootstrap or live data."""

from dataclasses import FrozenInstanceError, asdict
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer import payroll_reconciliation as module

T1, T2, OUTSIDE = (UUID(int=n) for n in (1, 2, 3))
RUN, SLIP, EARNING, DEDUCTION, COMPONENT = (UUID(int=n) for n in (11, 12, 13, 14, 15))


@pytest.fixture
def databases():
    source_engine, target_engine = sa.create_engine("sqlite://"), sa.create_engine("sqlite://")
    with source_engine.connect() as source, target_engine.connect() as target:
        source.exec_driver_sql("ATTACH DATABASE ':memory:' AS payroll")
        target.exec_driver_sql("ATTACH DATABASE ':memory:' AS mod_payroll")
        metadata = sa.MetaData()
        tables = {}

        def table(name, columns, schema, connection):
            tables[name] = sa.Table(name, metadata, *(sa.Column(n, t) for n, t in columns), schema=schema)
            tables[name].create(connection)

        uuid = sa.Uuid
        text = sa.Text
        table(
            "payroll_entry",
            [
                ("organization_id", uuid),
                ("entry_id", uuid),
                ("entry_number", text),
                ("start_date", text),
                ("end_date", text),
                ("currency_code", text),
                ("status", text),
                ("employee_count", sa.Integer),
                ("total_gross_pay", text),
                ("total_deductions", text),
                ("total_net_pay", text),
            ],
            "payroll",
            source,
        )
        table(
            "salary_slip",
            [
                ("organization_id", uuid),
                ("slip_id", uuid),
                ("payroll_entry_id", uuid),
                ("start_date", text),
                ("end_date", text),
                ("currency_code", text),
                ("gross_pay", text),
                ("total_deduction", text),
                ("net_pay", text),
                ("amount_paid", text),
                ("employee_id", text),
                ("employee_name", text),
                ("bank_account_number", text),
                ("tax_identification_number", text),
            ],
            "payroll",
            source,
        )
        table(
            "salary_component",
            [
                ("organization_id", uuid),
                ("component_id", uuid),
                ("component_code", text),
                ("component_type", text),
            ],
            "payroll",
            source,
        )
        table(
            "pay_components",
            [
                ("tenant_id", uuid),
                ("id", uuid),
                ("component_code", text),
                ("kind", text),
            ],
            "mod_payroll",
            target,
        )
        for name in ("salary_slip_earning", "salary_slip_deduction"):
            table(
                name,
                [
                    ("line_id", uuid),
                    ("slip_id", uuid),
                    ("component_id", uuid),
                    ("display_order", sa.Integer),
                    ("amount", text),
                    ("year_to_date", text),
                    ("statistical_component", sa.Boolean),
                    ("do_not_include_in_total", sa.Boolean),
                    ("component_name", text),
                ],
                "payroll",
                source,
            )
        table(
            "payroll_runs",
            [
                ("tenant_id", uuid),
                ("id", uuid),
                ("run_ref", text),
                ("period_start", text),
                ("period_end", text),
                ("currency_code", text),
                ("status", text),
            ],
            "mod_payroll",
            target,
        )
        table(
            "payroll_calculations",
            [
                ("tenant_id", uuid),
                ("id", uuid),
                ("run_id", uuid),
                ("employee_ref", text),
                ("gross_amount", text),
                ("deduction_amount", text),
                ("net_amount", text),
                ("employer_liability_amount", text),
            ],
            "mod_payroll",
            target,
        )
        table(
            "payroll_calculation_lines",
            [
                ("tenant_id", uuid),
                ("id", uuid),
                ("calculation_id", uuid),
                ("component_id", uuid),
                ("component_code", text),
                ("component_kind", text),
                ("sequence", sa.Integer),
                ("amount", text),
            ],
            "mod_payroll",
            target,
        )
        table(
            "payroll_liabilities",
            [
                ("tenant_id", uuid),
                ("id", uuid),
                ("run_id", uuid),
                ("calculation_id", uuid),
                ("currency_code", text),
            ],
            "mod_payroll",
            target,
        )
        yield source, target, tables
    source_engine.dispose()
    target_engine.dispose()


def seed(db, tenant=T1, *, paid="0", ytd="999999999999999999999999.123456789"):
    source, target, tables = db
    source.execute(
        tables["payroll_entry"]
        .insert()
        .values(
            organization_id=tenant,
            entry_id=RUN,
            entry_number="historical-run",
            start_date="2020-01-01",
            end_date="2020-01-31",
            currency_code="NGN",
            status="POSTED",
            employee_count=1,
            total_gross_pay="10.0001",
            total_deductions="2.0001",
            total_net_pay="8",
        )
    )
    source.execute(
        tables["salary_slip"]
        .insert()
        .values(
            organization_id=tenant,
            slip_id=SLIP,
            payroll_entry_id=RUN,
            start_date="2020-01-01",
            end_date="2020-01-31",
            currency_code="NGN",
            gross_pay="10.0001",
            total_deduction="2.0001",
            net_pay="8",
            amount_paid=paid,
            employee_id="PRIVATE_EMPLOYEE",
            employee_name="PRIVATE_NAME",
            bank_account_number="PRIVATE_BANK",
            tax_identification_number="PRIVATE_TAX",
        )
    )
    target.execute(
        tables["payroll_runs"]
        .insert()
        .values(
            tenant_id=tenant,
            id=RUN,
            run_ref="historical-run",
            period_start="2020-01-01",
            period_end="2020-01-31",
            currency_code="NGN",
            status="finalized",
        )
    )
    target.execute(
        tables["payroll_calculations"]
        .insert()
        .values(
            tenant_id=tenant,
            id=SLIP,
            run_id=RUN,
            employee_ref="PRIVATE_EMPLOYEE",
            employer_liability_amount="0",
            gross_amount="10.0001",
            deduction_amount="2.0001",
            net_amount="8",
        )
    )
    for name, ident, sequence, amount, component, code, kind in (
        ("salary_slip_earning", EARNING, 1, "10.0001", COMPONENT, "BASIC", "earning"),
        ("salary_slip_deduction", DEDUCTION, 2, "2.0001", UUID(int=16), "TAX", "deduction"),
    ):
        source.execute(
            tables["salary_component"]
            .insert()
            .values(
                organization_id=tenant,
                component_id=component,
                component_code=code,
                component_type=kind.upper(),
            )
        )
        target.execute(
            tables["pay_components"]
            .insert()
            .values(
                tenant_id=tenant,
                id=component,
                component_code=code,
                kind=kind,
            )
        )
        source.execute(
            tables[name]
            .insert()
            .values(
                line_id=ident,
                slip_id=SLIP,
                component_id=component,
                display_order=sequence,
                amount=amount,
                year_to_date=ytd,
                statistical_component=False,
                do_not_include_in_total=False,
                component_name="PRIVATE_COMPONENT",
            )
        )
        target.execute(
            tables["payroll_calculation_lines"]
            .insert()
            .values(
                tenant_id=tenant,
                id=ident,
                calculation_id=SLIP,
                component_id=component,
                sequence=sequence,
                component_code=code,
                component_kind=kind,
                amount=amount,
            )
        )


def report(db, extent=(T1,)):
    return module.reconcile_payroll(db[0], db[1], tenant_extent=extent)


def kinds(result):
    return {finding.kind for finding in result.findings}


def test_exact_transfer_retains_ytd_and_obligation_gaps_without_private_data(databases):
    seed(databases, paid="8")
    result = report(databases)
    assert kinds(result) == {
        "ytd_preservation_gap",
        "historical_obligation_evidence_unknown",
        "missing_target_liability_evidence",
    }
    assert dict(result.counts)["matched_earning_lines"] == 1
    assert dict(result.counts)["matched_deduction_lines"] == 1
    assert dict(result.counts)["ytd_preservation_gap"] == 2
    aggregate = result.aggregates[0]
    assert aggregate.source_gross == aggregate.target_gross == Decimal("10.0001")
    assert aggregate.source_net_outstanding == Decimal(0)
    assert aggregate.obligation_evidence == "unknown_requires_owner_reconciliation"
    assert not result.transfer_matches
    output = repr(asdict(result))
    for private in ("PRIVATE_", str(SLIP), str(EARNING), str(DEDUCTION), "999999999999"):
        assert private not in output
    with pytest.raises(FrozenInstanceError):
        result.counts = ()


def test_missing_is_not_zero_and_child_extras_survive(databases):
    seed(databases)
    _, target, tables = databases
    target.execute(tables["payroll_calculations"].delete())
    target.execute(tables["payroll_runs"].delete())
    result = report(databases)
    assert {"missing_target_run", "missing_target_calculation", "orphan_target_line"} <= kinds(result)
    assert result.aggregates[0].target_net is None
    assert dict(result.counts)["source_slips"] == 1
    assert dict(result.counts)["target_calculations"] == 0


def test_mismatched_identity_period_status_components_and_totals(databases):
    seed(databases)
    source, target, tables = databases
    source.execute(tables["payroll_entry"].update().values(total_net_pay="7", employee_count=2))
    source.execute(tables["salary_slip"].update().values(net_pay="7"))
    target.execute(
        tables["payroll_runs"].update().values(period_start="2020-01-02", status="draft", currency_code="USD")
    )
    target.execute(tables["payroll_calculation_lines"].update().values(amount="0", component_id=UUID(int=99)))
    result = report(databases)
    assert {
        "source_slip_arithmetic_mismatch",
        "source_run_slip_count_mismatch",
        "calculation_amount_mismatch",
        "run_field_mismatch",
        "line_field_mismatch",
        "line_amount_mismatch",
    } <= kinds(result)
    assert {f.field for f in result.findings if f.kind == "run_field_mismatch"} == {
        "period_start",
        "currency_code",
        "status",
    }


def test_absent_and_extra_line_counts_and_source_header_totals(databases):
    seed(databases)
    source, target, tables = databases
    source.execute(tables["payroll_entry"].update().values(total_gross_pay="12"))
    target.execute(
        tables["payroll_calculation_lines"].delete().where(tables["payroll_calculation_lines"].c.id == EARNING)
    )
    target.execute(
        tables["payroll_calculation_lines"]
        .insert()
        .values(
            tenant_id=T1,
            id=UUID(int=90),
            calculation_id=SLIP,
            component_id=COMPONENT,
            sequence=3,
            amount="10.0001",
        )
    )
    result = report(databases)
    assert {"missing_target_line", "extra_target_line", "source_run_total_mismatch"} <= kinds(result)
    assert dict(result.counts)["matched_earning_lines"] == 0


def test_tenant_scope_bounds_source_children_and_target_orphans(databases):
    seed(databases, tenant=OUTSIDE)
    result = report(databases)
    assert result.aggregates == ()
    assert result.findings == ()
    assert dict(result.counts)["source_earning_lines"] == 0
    target, tables = databases[1:]
    target.execute(
        tables["payroll_runs"]
        .insert()
        .values(
            tenant_id=T1,
            id=RUN,
            run_ref="other",
            period_start="2020-01-01",
            period_end="2020-01-31",
            currency_code="NGN",
            status="finalized",
        )
    )
    assert "extra_target_run" in kinds(report(databases))


def test_liability_presence_never_certifies_settlement(databases):
    seed(databases)
    _, target, tables = databases
    target.execute(
        tables["payroll_liabilities"]
        .insert()
        .values(
            tenant_id=T1,
            id=UUID(int=70),
            run_id=RUN,
            calculation_id=SLIP,
            currency_code="NGN",
        )
    )
    result = report(databases)
    assert "missing_target_liability_evidence" not in kinds(result)
    assert "historical_obligation_evidence_unknown" in kinds(result)
    assert result.aggregates[0].target_liabilities == 1
    assert result.aggregates[0].source_net_outstanding == Decimal(8)


def test_invalid_money_not_hidden_by_aggregate_and_float_storage_refused(databases, monkeypatch):
    seed(databases)
    source, _, tables = databases
    source.execute(tables["salary_slip"].update().values(gross_pay="NaN", amount_paid=None))
    result = report(databases)
    assert result.aggregates[0].source_gross is None
    assert result.aggregates[0].source_net_outstanding is None
    assert "invalid_source_money" in kinds(result)
    original = module._reflect

    def reflect_with_float(connection, name, schema=None):
        reflected = original(connection, name, schema)
        if name == "salary_slip":
            reflected.c.net_pay.type = sa.Float()
        return reflected

    monkeypatch.setattr(module, "_reflect", reflect_with_float)
    with pytest.raises(ValueError, match="unsupported_float_money_column"):
        report(databases)


def test_select_only_no_private_columns_and_no_transaction_control(databases):
    seed(databases)
    observed = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        observed.append(statement.lower())

    def no_transaction_control(*args):
        pytest.fail("diagnostic changed transaction state")

    for connection in databases[:2]:
        sa.event.listen(connection, "before_cursor_execute", observe)
        sa.event.listen(connection, "commit", no_transaction_control)
        sa.event.listen(connection, "rollback", no_transaction_control)
    try:
        report(databases)
        assert all(sql.lstrip().startswith(("select", "pragma")) for sql in observed)
        data_selects = [sql for sql in observed if sql.lstrip().startswith("select") and "sqlite_master" not in sql]
        for sql in data_selects:
            assert all(
                private not in sql
                for private in (
                    "employee_name",
                    "bank_account_number",
                    "tax_identification_number",
                    "year_to_date",
                )
            )
    finally:
        for connection in databases[:2]:
            sa.event.remove(connection, "before_cursor_execute", observe)
            sa.event.remove(connection, "commit", no_transaction_control)
            sa.event.remove(connection, "rollback", no_transaction_control)


@pytest.mark.parametrize("extent", [(), [], (str(T1),), (None,)])
def test_refuses_unbounded_scope_before_reflection(databases, extent):
    with pytest.raises(ValueError, match="nonempty tuple of UUIDs"):
        report(databases, extent)


def test_refuses_orm_sessions(databases):
    with Session(bind=databases[0]) as session:
        with pytest.raises(TypeError, match="Core Connections"):
            module.reconcile_payroll(session, databases[1], tenant_extent=(T1,))


def test_money_is_exact_under_low_decimal_precision():
    with localcontext() as context:
        context.prec = 2
        assert module._sum([Decimal("123456789.0001"), Decimal("-0.00001")]) == Decimal("123456789.00009")
    for value in (0.1, True, None, "Infinity", "NaN"):
        with pytest.raises(ValueError):
            module._money(value)


def test_employee_identity_is_compared_without_reporting_staff_identifiers(databases):
    seed(databases)
    _, target, tables = databases
    target.execute(tables["payroll_calculations"].update().values(employee_ref="PRIVATE_DIFFERENT_EMPLOYEE"))
    result = report(databases)
    assert dict(result.counts)["employee_identity_mismatch"] == 1
    assert "PRIVATE_" not in repr(asdict(result))
    finding = next(f for f in result.findings if f.kind == "employee_identity_mismatch")
    assert finding.run_id == str(RUN)
    assert finding.field == ""


@pytest.mark.parametrize("field,value", [("component_code", "WRONG"), ("component_kind", "information")])
def test_component_line_code_and_actual_kind_are_preserved(databases, field, value):
    seed(databases)
    _, target, tables = databases
    target.execute(tables["payroll_calculation_lines"].update().values(**{field: value}))
    result = report(databases)
    assert any(f.kind == "line_field_mismatch" and f.field == field for f in result.findings)
    assert "line_component_snapshot_mismatch" in kinds(result)
    assert "line_amount_mismatch" not in kinds(result)


def test_changed_target_component_kind_is_detected_even_if_line_agrees(databases):
    seed(databases)
    _, target, tables = databases
    target.execute(tables["pay_components"].update().values(kind="information"))
    target.execute(tables["payroll_calculation_lines"].update().values(component_kind="information"))
    result = report(databases)
    assert "component_field_mismatch" in kinds(result)
    assert any(f.kind == "line_field_mismatch" and f.field == "component_kind" for f in result.findings)


def test_real_reflection_preserves_hex_uuid_scope_and_source_child_cohort(databases):
    seed(databases)
    reflected = module._reflect(databases[0], "salary_slip")
    assert not isinstance(reflected.c.organization_id.type, sa.Uuid)
    result = report(databases)
    assert dict(result.counts)["source_runs"] == 1
    assert dict(result.counts)["source_slips"] == 1
    assert dict(result.counts)["source_earning_lines"] == 1
    assert dict(result.counts)["matched_calculations"] == 1
    assert report(databases, (OUTSIDE,)).aggregates == ()


def test_matching_transferred_lines_must_also_match_stored_calculation_totals(databases):
    seed(databases)
    source, target, tables = databases
    source.execute(tables["salary_slip_earning"].update().values(amount="9"))
    target.execute(
        tables["payroll_calculation_lines"]
        .update()
        .where(
            tables["payroll_calculation_lines"].c.id == EARNING,
        )
        .values(amount="9")
    )
    result = report(databases)
    assert "line_amount_mismatch" not in kinds(result)
    assert any(f.kind == "target_line_total_mismatch" and f.field == "gross_amount" for f in result.findings)


def test_empty_line_cohorts_are_checked_against_stored_employer_amount(databases):
    seed(databases)
    _, target, tables = databases
    target.execute(tables["payroll_calculations"].update().values(employer_liability_amount="3", net_amount="7"))
    result = report(databases)
    assert any(
        f.kind == "target_line_total_mismatch" and f.field == "employer_liability_amount" for f in result.findings
    )
    assert "target_calculation_arithmetic_mismatch" in kinds(result)
    target.execute(tables["payroll_calculation_lines"].delete())
    assert "target_calculation_without_lines" in kinds(report(databases))


@pytest.mark.parametrize("flag", ["statistical_component", "do_not_include_in_total"])
def test_legacy_exclusion_flags_are_explicit_preservation_gaps(databases, flag):
    seed(databases)
    source, _, tables = databases
    source.execute(tables["salary_slip_earning"].update().values(**{flag: True}))
    result = report(databases)
    assert any(f.kind == "source_line_flag_preservation_gap" and f.field == flag for f in result.findings)


def test_information_lines_are_excluded_and_employer_lines_sum_separately(databases):
    seed(databases)
    _, target, tables = databases
    for number, kind, amount in ((81, "information", "999"), (82, "employer_liability", "3.00001")):
        target.execute(
            tables["payroll_calculation_lines"]
            .insert()
            .values(
                tenant_id=T1,
                id=UUID(int=number),
                calculation_id=SLIP,
                component_id=COMPONENT,
                component_code="EXTRA",
                component_kind=kind,
                sequence=number,
                amount=amount,
            )
        )
    target.execute(tables["payroll_calculations"].update().values(employer_liability_amount="3.00001"))
    result = report(databases)
    assert "extra_target_line" in kinds(result)
    assert "target_line_total_mismatch" not in kinds(result)
