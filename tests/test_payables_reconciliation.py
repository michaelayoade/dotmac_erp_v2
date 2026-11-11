"""Synthetic historical evidence only; no product bootstrap or external database."""

from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer.payables_reconciliation import _money, _sum, reconcile_payables

T1, T2, OUTSIDE = (UUID(int=n) for n in (1, 2, 3))
I1, I2, I3, I4 = (UUID(int=n) for n in (11, 12, 13, 14))
SUPPLIER = UUID(int=21)


@pytest.fixture
def databases():
    source_engine = sa.create_engine("sqlite://")
    target_engine = sa.create_engine("sqlite://")
    with source_engine.connect() as source, target_engine.connect() as target:
        target.exec_driver_sql("ATTACH DATABASE ':memory:' AS mod_payables")
        metadata = sa.MetaData()
        sh = sa.Table(
            "supplier_invoice",
            metadata,
            sa.Column("organization_id", sa.Uuid),
            sa.Column("invoice_id", sa.Uuid),
            sa.Column("supplier_id", sa.Uuid),
            sa.Column("currency_code", sa.Text),
            sa.Column("status", sa.Text),
            sa.Column("invoice_type", sa.Text),
            sa.Column("total_amount", sa.Text),
            sa.Column("amount_paid", sa.Text),
            sa.Column("supplier_invoice_number", sa.Text),
        )
        sh.create(source)
        dh = sa.Table(
            "supplier_invoices",
            metadata,
            sa.Column("tenant_id", sa.Uuid),
            sa.Column("id", sa.Uuid),
            sa.Column("supplier_ref", sa.Text),
            sa.Column("currency_code", sa.Text),
            sa.Column("status", sa.Text),
            sa.Column("total_amount", sa.Text),
            sa.Column("supplier_document_number", sa.Text),
            schema="mod_payables",
        )
        dh.create(target)
        po = sa.Table(
            "payment_obligations",
            metadata,
            sa.Column("tenant_id", sa.Uuid),
            sa.Column("id", sa.Uuid),
            sa.Column("invoice_id", sa.Uuid),
            sa.Column("currency_code", sa.Text),
            sa.Column("outstanding_amount", sa.Text),
            schema="mod_payables",
        )
        po.create(target)
        # SQLite reflects UUID as generic CHAR. Preserve UUID metadata in these
        # fixtures while exercising real query compilation and execution.
        from erp_v2.importer import payables_reconciliation as module

        with pytest.MonkeyPatch.context() as patch:
            original = module._reflect

            def reflect(connection, name, schema=None):
                original(connection, name, schema)  # Still test unique source resolution.
                return {"supplier_invoice": sh, "supplier_invoices": dh, "payment_obligations": po}[name]

            patch.setattr(module, "_reflect", reflect)
            yield source, target, sh, dh, po
    source_engine.dispose()
    target_engine.dispose()


def add_source(db, ident=I1, tenant=T1, **changes):
    source, _, sh, _, _ = db
    values = dict(
        organization_id=tenant,
        invoice_id=ident,
        supplier_id=SUPPLIER,
        currency_code="NGN",
        status="POSTED",
        invoice_type="STANDARD",
        total_amount="10.0001",
        amount_paid="0",
        supplier_invoice_number=None,
    )
    values.update(changes)
    source.execute(sh.insert().values(**values))


def add_header(db, ident=I1, tenant=T1, **changes):
    _, target, _, dh, _ = db
    values = dict(
        tenant_id=tenant,
        id=ident,
        supplier_ref=str(SUPPLIER),
        currency_code="NGN",
        status="APPROVED",
        total_amount="10.0001",
        supplier_document_number=None,
    )
    values.update(changes)
    target.execute(dh.insert().values(**values))


def add_obligation(db, ident=I1, tenant=T1, number=31, **changes):
    _, target, _, _, po = db
    values = dict(
        tenant_id=tenant, id=UUID(int=number), invoice_id=ident, currency_code="NGN", outstanding_amount="10.0001"
    )
    values.update(changes)
    target.execute(po.insert().values(**values))


def report(db, scope=(T1,)):
    return reconcile_payables(db[0], db[1], tenant_extent=scope)


def kinds(result):
    return {finding.kind for finding in result.findings}


def test_missing_positive_and_zero_obligations_and_old_cohort(databases):
    db = databases
    for ident, status, paid in ((I1, "POSTED", "0"), (I2, "PAID", "10.0001"), (I3, "APPROVED", "0")):
        add_source(db, ident, status=status, amount_paid=paid)
        add_header(db, ident)
    result = report(db)
    assert dict(result.counts)["old_comparison_landed_headers"] == 2
    assert dict(result.counts)["old_comparison_monetary_mismatches"] == 1
    assert dict(result.counts)["landed_status_APPROVED"] == 1
    assert dict(result.counts)["recognized_source_headers"] == 3
    assert dict(result.counts)["missing_target_obligation"] == 3
    assert result.positions[1].source_residual == Decimal(0)
    assert not any(position.structurally_reconciled for position in result.positions)
    assert not result.reconciled


def test_multi_obligations_exact_subcent_and_null_document_preserved(databases):
    db = databases
    add_source(db)
    add_header(db)
    add_obligation(db, outstanding_amount="0.0001")
    add_obligation(db, number=32, outstanding_amount="10")
    result = report(db)
    assert result.reconciled
    assert result.positions[0].target_outstanding == Decimal("10.0001")
    assert result.positions[0].structurally_reconciled
    assert db[1].scalar(sa.select(db[3].c.supplier_document_number)) is None


@pytest.mark.parametrize("status", ["DRAFT", "SUBMITTED", "VOID"])
def test_recognized_header_requires_approved_state(databases, status):
    add_source(databases)
    add_header(databases, status=status)
    add_obligation(databases)
    result = report(databases)
    assert "recognized_header_status_mismatch" in kinds(result)
    assert not result.positions[0].structurally_reconciled


def test_tenant_currency_supplier_cannot_net_and_scope_includes_orphans(databases):
    db = databases
    add_source(db)
    add_source(db, tenant=T2)
    add_header(db, supplier_ref="different-supplier")
    add_header(db, tenant=T2)
    add_obligation(db, currency_code="USD")
    add_obligation(db, tenant=T2, number=32, outstanding_amount="0")
    add_obligation(db, ident=I3, number=33)
    add_source(db, I4, tenant=OUTSIDE)
    add_obligation(db, I4, tenant=OUTSIDE, number=34)
    result = report(db, (T2, T1))
    assert {
        "header_supplier_ref_mismatch",
        "source_obligation_currency_mismatch",
        "obligation_currency_mismatch",
        "outstanding_amount_mismatch",
        "orphan_target_obligation",
        "extra_target_obligation",
    } <= kinds(result)
    assert all(p.tenant_id != str(OUTSIDE) for p in result.positions)
    assert all(g.tenant_id != str(OUTSIDE) for g in result.aggregates)
    assert {(g.supplier_ref, g.currency_code) for g in result.aggregates} >= {
        (str(SUPPLIER), "NGN"),
        ("different-supplier", "USD"),
        ("<missing_header>", "NGN"),
    }


def test_missing_source_target_extras_and_cross_tenant_same_id(databases):
    db = databases
    add_source(db)
    add_header(db, tenant=T2)
    add_obligation(db, tenant=T2)
    add_header(db, I2)
    result = report(db, (T1, T2))
    assert {
        "missing_target_header",
        "missing_target_obligation",
        "extra_target_header",
        "extra_target_obligation",
        "header_tenant_mismatch",
        "obligation_tenant_mismatch",
    } <= kinds(result)
    assert not result.positions[0].landed


@pytest.mark.parametrize("status", ["ON_HOLD", "DISPUTED", "PENDING_APPROVAL", "unrecognized"])
def test_unknown_status_is_named(databases, status):
    add_source(databases, status=status)
    add_header(databases)
    assert "unknown_source_status" in kinds(report(databases))


@pytest.mark.parametrize("invoice_type", ["CREDIT_NOTE", "DEBIT_NOTE", "unrecognized"])
def test_nonstandard_type_is_named(databases, invoice_type):
    add_source(databases, invoice_type=invoice_type)
    add_header(databases)
    assert "unsupported_source_type" in kinds(report(databases))
    assert dict(report(databases).counts)["recognized_source_headers"] == 0


@pytest.mark.parametrize("bad", [None, "NaN", "Infinity", "-Infinity", "broken"])
def test_invalid_money_never_defaults_to_zero(databases, bad):
    db = databases
    add_source(db, amount_paid=bad)
    add_header(db)
    add_obligation(db, outstanding_amount=bad)
    result = report(db)
    assert {"invalid_source_money", "invalid_target_money"} <= kinds(result)
    assert result.positions[0].source_residual is None
    assert result.positions[0].target_outstanding is None
    assert not result.positions[0].structurally_reconciled
    assert result.aggregates[0].invalid_source_amounts == 1
    assert result.aggregates[0].invalid_target_amounts == 1


def test_precision_and_negative_residual_are_exact(databases):
    total = "123456789012345678901234567890.00001"
    paid = "123456789012345678901234567890.00002"
    add_source(databases, total_amount=total, amount_paid=paid)
    add_header(databases, total_amount=total)
    add_obligation(databases, outstanding_amount="0")
    with localcontext() as context:
        context.prec = 3
        result = report(databases)
        assert _sum([Decimal(total), Decimal(paid).copy_negate()]) == Decimal("-0.00001")
    assert result.positions[0].source_residual == Decimal("-0.00001")
    assert "outstanding_amount_mismatch" in kinds(result)


@pytest.mark.parametrize("value", [0.1, True, object()])
def test_unsupported_money_values(value):
    with pytest.raises(ValueError, match="unsupported"):
        _money(value)


def test_select_only_deterministic_frozen_and_connection_lifecycle(databases):
    db = databases
    for ident in (I2, I1):
        add_source(db, ident)
        add_header(db, ident)
        add_obligation(db, ident, number=ident.int + 100)
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().split()[0].upper())

    for connection in db[:2]:
        sa.event.listen(connection, "before_cursor_execute", capture)
    result = report(db, (T2, T1, T1))
    assert result == report(db, (T1, T2))
    assert result.tenant_extent == (T1, T2)
    assert set(statements) <= {"SELECT", "PRAGMA"}
    assert "SELECT" in statements
    assert all(connection.in_transaction() and not connection.closed for connection in db[:2])
    with pytest.raises(FrozenInstanceError):
        result.positions = ()
    with Session(bind=db[0]) as session:
        with pytest.raises(TypeError, match="Core Connections"):
            reconcile_payables(session, db[1], tenant_extent=(T1,))


@pytest.mark.parametrize("scope", [(), (str(T1),), [T1], None])
def test_mandatory_uuid_extent_before_query(databases, scope):
    with pytest.raises(ValueError, match="tenant_extent"):
        report(databases, scope)


def test_ambiguous_source_schema_refuses(databases):
    source = databases[0]
    source.exec_driver_sql("ATTACH DATABASE ':memory:' AS duplicate")
    source.exec_driver_sql("CREATE TABLE duplicate.supplier_invoice (invoice_id TEXT)")
    with pytest.raises(ValueError, match="source_table_resolution"):
        report(databases)
