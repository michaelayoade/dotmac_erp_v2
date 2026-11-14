"""Statement import command on SQLite (logic). Real PostgreSQL: test_statement_import_pg.py."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_banking.models import BankAccount, BankInstitution, BankStatement, BankStatementLine
from dotmac_kernel.models import Base, Tenant
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp_v2.banking import statement_import as si
from erp_v2.banking.models import BankingObservationReceipt

NOW = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
FILE_A = (
    b"date,type,amount,description,reference\n"
    b"2026-01-15,credit,300.50,Deposit,TRF-1\n"
    b"2026-01-16,debit,10.00,Fee,TRF-2\n"
)
FILE_OVERLAP = (
    b"date,type,amount,description,reference\n"
    b"2026-01-16,debit,10.00,Fee again,TRF-2\n"
    b"2026-01-20,credit,5.00,New,TRF-3\n"
)


@pytest.fixture()
def env():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("ATTACH DATABASE ':memory:' AS mod_banking"))
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            BankInstitution.__table__,
            BankAccount.__table__,
            BankStatement.__table__,
            BankStatementLine.__table__,
            BankingObservationReceipt.__table__,
        ],
    )
    with Session(engine) as db:
        tenants = [Tenant(id=uuid4(), slug=f"t{i}", name=f"T{i}") for i in range(2)]
        db.add_all(tenants)
        db.flush()

        def account(tenant, code, status="active", currency="NGN", minor=2):
            inst = BankInstitution(
                id=uuid4(), tenant_id=tenant.id, code=f"I{code}", name="Bank", country_code="NG", status="active"
            )
            db.add(inst)
            db.flush()
            acct = BankAccount(
                id=uuid4(),
                tenant_id=tenant.id,
                institution_id=inst.id,
                account_code=code,
                account_name=code,
                account_identifier=f"id-{code}",
                account_type_code="current",
                currency_code=currency,
                minor_units=minor,
                cash_account_ref="GL:1",
                status=status,
            )
            db.add(acct)
            db.flush()
            return acct

        a1 = account(tenants[0], "OP1")
        a2 = account(tenants[0], "OP2")
        closed = account(tenants[0], "CL", status="closed")
        foreign = account(tenants[1], "FX")
        db.commit()
        yield db, tenants[0], a1, a2, closed, foreign
    engine.dispose()


def run(db, tenant, account, data, *, key=None, audit=None, expected=None, **opts):
    import hashlib

    return si.import_statement(
        db,
        tenant_id=tenant.id,
        actor_party_id=uuid4(),
        data=data,
        file_format="csv",
        expected_sha256=expected or hashlib.sha256(data).hexdigest(),
        options=si.ImportOptions(account_id=account.id, **opts),
        now=NOW,
        audit=audit or Mock(),
    )


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def refusal(db, tenant, account, data, **kwargs) -> str:
    with pytest.raises(si.StatementImportRefused) as exc:
        run(db, tenant, account, data, **kwargs)
    return str(exc.value)


def test_preview_writes_nothing_and_reports_totals(env):
    db, tenant, a1, *_ = env
    plan = si.preview(db, tenant_id=tenant.id, data=FILE_A, file_format="csv", options=si.ImportOptions(a1.id), now=NOW)
    assert plan.importable and len(plan.parsed.lines) == 2
    assert plan.total_credits == Decimal("300.50") and plan.total_debits == Decimal("10.00")
    assert (plan.period_start, plan.period_end) == (date(2026, 1, 15), date(2026, 1, 16))
    assert count(db, BankStatement) == 0 and count(db, BankStatementLine) == 0


def test_import_creates_statement_with_provenance_and_audit(env):
    db, tenant, a1, *_ = env
    audit = Mock()
    result = run(db, tenant, a1, FILE_A, audit=audit)
    statement = db.get(BankStatement, result.statement_id)
    assert statement.tenant_id == tenant.id and statement.account_id == a1.id
    assert statement.status == "imported" and statement.total_lines == 2
    assert statement.source_ref == f"erp:statement-upload:sha256:{result.sha256}"
    assert statement.evidence_ref == f"upload:sha256:{result.sha256}"
    assert statement.total_credits == Decimal("300.50") and statement.total_debits == Decimal("10.00")
    assert count(db, BankStatementLine) == 2
    audit.assert_called_once()
    assert audit.call_args.kwargs["sha256"] == result.sha256 and audit.call_args.kwargs["line_count"] == 2


def test_import_never_touches_the_mono_receipts_or_posts(env):
    db, tenant, a1, *_ = env
    run(db, tenant, a1, FILE_A)
    assert count(db, BankingObservationReceipt) == 0


def test_same_file_cannot_import_twice_even_to_another_account(env):
    db, tenant, a1, a2, *_ = env
    run(db, tenant, a1, FILE_A)
    assert "already been imported" in refusal(db, tenant, a1, FILE_A)
    assert "already been imported" in refusal(db, tenant, a2, FILE_A)
    assert count(db, BankStatement) == 1 and count(db, BankStatementLine) == 2


def test_overlapping_different_file_is_refused_whole(env):
    db, tenant, a1, *_ = env
    run(db, tenant, a1, FILE_A)
    message = refusal(db, tenant, a1, FILE_OVERLAP)
    assert "already exist on this account" in message
    assert count(db, BankStatement) == 1 and count(db, BankStatementLine) == 2


def test_overlap_check_is_per_account(env):
    db, tenant, a1, a2, *_ = env
    run(db, tenant, a1, FILE_A)
    run(db, tenant, a2, FILE_OVERLAP)  # TRF-2 exists on a1 only
    assert count(db, BankStatement) == 2


def test_unreferenced_rows_overlapping_across_exports_are_caught(env):
    db, tenant, a1, *_ = env
    first = b"date,debit,credit,narration\n2026-01-05,5.00,,Fee\n2026-01-06,,9.00,In\n"
    second = b"date,debit,credit,narration\n2026-01-05,5.00,,Fee\n2026-01-07,,1.00,Other\n"
    run(db, tenant, a1, first)
    assert "already exist" in refusal(db, tenant, a1, second)


def test_hash_mismatch_refuses(env):
    db, tenant, a1, *_ = env
    assert "differs from the previewed" in refusal(db, tenant, a1, FILE_A, expected="0" * 64)
    assert "previewed file hash" in refusal(db, tenant, a1, FILE_A, expected="zz")
    assert count(db, BankStatement) == 0


def test_foreign_tenant_and_inactive_accounts_are_refused(env):
    db, tenant, _a1, _a2, closed, foreign = env
    assert "not found" in refusal(db, tenant, foreign, FILE_A)
    assert "not active" in refusal(db, tenant, closed, FILE_A)
    assert count(db, BankStatement) == 0


@pytest.mark.parametrize(
    "data,fragment",
    [
        (b"date,type,amount\n2026-01-15,credit,1.005\n", "decimal places"),
        (b"date,type,amount\n2026-02-10,credit,1.00\n", "Dates must be between"),
        (b"date,type,amount\n1999-12-31,credit,1.00\n", "Dates must be between"),
        (b"date,type,amount\n2025-01-01,credit,1.00\n2026-01-31,credit,1.00\n", "spans more than"),
        (b"date,type,amount\n2026-01-15,credit,0\n", "greater than zero"),
        (b"date,type,amount\n2026-01-15,credit,abc\n", "plain positive"),
        (b"", "empty"),
    ],
)
def test_out_of_range_and_malformed_input_is_refused(env, data, fragment):
    db, tenant, a1, *_ = env
    assert fragment in refusal(db, tenant, a1, data)
    assert count(db, BankStatement) == 0


def test_balances_must_agree_and_come_in_pairs(env):
    db, tenant, a1, *_ = env
    assert "does not equal" in refusal(
        db, tenant, a1, FILE_A, opening_balance=Decimal("100"), closing_balance=Decimal("999")
    )
    assert "both opening and closing" in refusal(db, tenant, a1, FILE_A, opening_balance=Decimal("100"))
    ok = run(db, tenant, a1, FILE_A, opening_balance=Decimal("100.00"), closing_balance=Decimal("390.50"))
    assert db.get(BankStatement, ok.statement_id).closing_balance == Decimal("390.50")


def test_statement_reference_is_validated_and_unique(env):
    db, tenant, a1, *_ = env
    assert "Statement reference" in refusal(db, tenant, a1, FILE_A, statement_ref="bad;ref")
    run(db, tenant, a1, FILE_A, statement_ref="JAN-2026")
    assert "reference already exists" in refusal(db, tenant, a1, FILE_OVERLAP, statement_ref="JAN-2026")


def test_audit_failure_rolls_back_the_statement(env):
    db, tenant, a1, *_ = env
    with pytest.raises(RuntimeError):
        run(db, tenant, a1, FILE_A, audit=Mock(side_effect=RuntimeError("audit down")))
    assert count(db, BankStatement) == 0 and count(db, BankStatementLine) == 0


def _legacy_statement(db, tenant, account, start, end):
    """Shaped like the importer's statements: lines carry a provenance source_key, no bank reference."""
    statement = BankStatement(
        id=uuid4(),
        tenant_id=tenant.id,
        account_id=account.id,
        statement_ref="legacy-2026-01",
        period_start=start,
        period_end=end,
        total_credits=Decimal(0),
        total_debits=Decimal(0),
        currency_code="NGN",
        minor_units=2,
        status="imported",
        total_lines=1,
        matched_lines=0,
        source_ref="legacy:erp:1",
        source_version="1",
        evidence_ref="legacy",
        imported_by_id=uuid4(),
        imported_at=NOW,
    )
    db.add(statement)
    db.flush()
    db.add(
        BankStatementLine(
            tenant_id=tenant.id,
            statement_id=statement.id,
            line_number=1,
            transaction_date=start,
            direction="credit",
            amount=Decimal("1.00"),
            external_ref="legacy:source_key:abc",
            description="legacy",
            is_matched=False,
        )
    )
    db.commit()


def test_upload_overlapping_a_legacy_imported_period_is_refused(env):
    db, tenant, a1, a2, *_ = env
    _legacy_statement(db, tenant, a1, date(2026, 1, 1), date(2026, 1, 31))
    assert "overlaps existing statement 'legacy-2026-01'" in refusal(db, tenant, a1, FILE_A)
    assert count(db, BankStatement) == 1
    run(db, tenant, a2, FILE_A)  # other account unaffected


def test_adjacent_period_after_legacy_statement_imports(env):
    db, tenant, a1, *_ = env
    _legacy_statement(db, tenant, a1, date(2025, 12, 1), date(2026, 1, 14))
    run(db, tenant, a1, FILE_A)
    assert count(db, BankStatement) == 2
