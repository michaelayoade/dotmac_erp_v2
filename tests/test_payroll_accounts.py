"""Account setup contracts; DB cases use the existing hosted-only PG fixture.

Creation qualification deliberately waits for the released Accounting a2 owner;
the current a1 dependency must refuse creation before importing its unsafe seam.
"""

import hashlib
import json
from importlib.metadata import version
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory
from dotmac_kernel.models import Tenant
from sqlalchemy.orm import Session
from test_posting_bridge_pg import engine as accounting_engine  # noqa: F401

from erp_v2 import payroll_accounts as setup


def spec(tenant, category):
    return {
        "version": 1,
        "tenant_id": str(tenant),
        "destination_database": "reviewed_destination",
        "accounts": [
            dict(
                purpose="NHIS",
                action="create",
                code="2133",
                name="NHIS Payable",
                category_id=str(setup.NHIS_CATEGORY_ID),
                account_id=None,
                account_class="LIABILITY",
                normal_balance="CREDIT",
                currency_code=None,
            ),
            dict(
                purpose="LOAN_REPAYMENT",
                action="reuse",
                code="1410",
                name="Staff Loan",
                category_id=str(category),
                account_id=str(setup.LOAN_ACCOUNT_ID),
                account_class="ASSET",
                normal_balance="DEBIT",
                currency_code=None,
            ),
        ],
    }


def test_spec_refuses_duplicate_keys_and_loan_liability():
    with pytest.raises(setup.OperatorRefusal, match="duplicate_json_key"):
        setup.parse_account_spec(b'{"version":1,"version":1}')
    raw = spec(uuid4(), uuid4())
    raw["accounts"][1]["account_class"] = "LIABILITY"
    with pytest.raises(setup.OperatorRefusal, match="staff_loan_receivable_required"):
        setup.parse_account_spec(json.dumps(raw).encode())


def test_operator_refuses_serving_dsn_and_unreviewed_digest(tmp_path, monkeypatch):
    path = tmp_path / "spec.json"
    raw = json.dumps(spec(uuid4(), uuid4())).encode()
    path.write_bytes(raw)
    args = SimpleNamespace(spec=path, apply=True, approved_spec_sha256="wrong")
    monkeypatch.setenv("ERP_PAYROLL_ACCOUNT_SETUP_APPROVED", "1")
    with pytest.raises(setup.OperatorRefusal, match="reviewed_account_apply_required"):
        setup._run(args)
    args.approved_spec_sha256 = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("DATABASE_URL", "forbidden-serving-material")
    with pytest.raises(setup.OperatorRefusal, match="serving_or_source_database_materials_forbidden"):
        setup._run(args)


def test_plan_transaction_sets_read_only_and_verifies_tenant(tmp_path, monkeypatch):
    path = tmp_path / "spec.json"
    tenant = uuid4()
    path.write_text(json.dumps(spec(tenant, uuid4())))
    statements = []
    engine = SimpleNamespace(dispose=lambda: None)
    monkeypatch.setattr(setup, "_safe_engine", lambda url: engine)
    monkeypatch.setattr(setup, "plan_accounts", lambda db, parsed: [])
    for key in ("DATABASE_URL", "PLATFORM_DATABASE_URL", "LEGACY_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "opaque-test-only")

    class SessionSpy:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def begin(self):
            return self

        def execute(self, statement, params=None):
            statements.append((str(statement), params))

        def scalar(self, statement):
            statements.append((str(statement), None))
            return "reviewed_destination" if "current_database" in str(statement) else tenant

    monkeypatch.setattr(setup, "Session", SessionSpy)
    result = setup._run(SimpleNamespace(spec=path, apply=False))
    assert statements[0][0] == "SET TRANSACTION READ ONLY"
    assert statements[2] == ("SELECT set_config('app.current_tenant', :tenant, true)", {"tenant": str(tenant)})
    assert statements[3][0] == "SELECT public.app_current_tenant_id()"
    assert result["payroll_runtime_wired"] is False


def test_receipt_private_exclusive_and_sanitized(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    receipt = tmp_path / "receipt.json"

    def secret_failure(args):
        raise RuntimeError("private-dsn-must-not-be-output")

    monkeypatch.setattr(setup, "_run", secret_failure)
    args = ["--spec", str(tmp_path / "unused.json"), "--receipt", str(receipt)]
    assert setup.main(args) == 2
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text()) == {"ready": False, "refusal": "payroll_account_setup_refused"}
    before = receipt.read_bytes()
    assert setup.main(args) == 2
    assert receipt.read_bytes() == before
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    assert setup.main(["--spec", "unused", "--receipt", str(linked / "new.json")]) == 2
    assert not (tmp_path / "new.json").exists()


@pytest.fixture
def seeded(accounting_engine):  # noqa: F811
    # Constants are imported source identities, so rollback each fixture's rows.
    with Session(accounting_engine) as db, db.begin():
        tenant, asset_id = uuid4(), uuid4()
        db.add(Tenant(id=tenant, slug=f"pa-{tenant.hex[:24]}", name="Payroll accounts"))
        db.flush()
        db.add_all(
            [
                AccountCategory(
                    id=asset_id, tenant_id=tenant, code="AST", name="Assets", account_class=AccountClass.ASSET
                ),
                AccountCategory(
                    id=setup.NHIS_CATEGORY_ID,
                    tenant_id=tenant,
                    code="LIA",
                    name="Liabilities",
                    account_class=AccountClass.LIABILITY,
                ),
            ]
        )
        db.flush()
        db.add(
            Account(
                id=setup.LOAN_ACCOUNT_ID,
                tenant_id=tenant,
                category_id=asset_id,
                code="1410",
                name="Staff Loan",
                kind=AccountKind.POSTING,
                normal_balance=NormalBalance.DEBIT,
                posting_allowed=True,
            )
        )
        db.add(
            Account(
                tenant_id=tenant,
                category_id=setup.NHIS_CATEGORY_ID,
                code="2132",
                name="NHF Payable",
                kind=AccountKind.POSTING,
                normal_balance=NormalBalance.CREDIT,
                posting_allowed=True,
            )
        )
        db.flush()
        parsed = setup.parse_account_spec(json.dumps(spec(tenant, asset_id)).encode())
        yield db, parsed
        db.rollback()


def test_plan_executes_only_selects_and_does_not_call_owner(seeded, monkeypatch):
    db, parsed = seeded
    statements = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    def forbidden():
        raise AssertionError("planner called creation owner")

    monkeypatch.setattr(setup, "_creation_owner", forbidden)
    connection = db.connection()
    sa.event.listen(connection, "before_cursor_execute", observe)
    try:
        plan = setup.plan_accounts(db, parsed)
    finally:
        sa.event.remove(connection, "before_cursor_execute", observe)
    assert statements and set(statements) == {"SELECT"}
    assert {row["purpose"]: row["action"] for row in plan} == {"NHIS": "create", "LOAN_REPAYMENT": "reuse"}
    assert db.scalar(sa.select(sa.func.count()).select_from(Account).where(Account.tenant_id == parsed.tenant_id)) == 2


def test_a1_creation_is_refused_before_owner_import(seeded, monkeypatch):
    db, parsed = seeded
    monkeypatch.setattr(setup, "version", lambda package: "0.1.0a1")
    with pytest.raises(setup.OperatorRefusal, match="accounting_a2_creation_release_required"):
        setup.apply_accounts(db, parsed)
    assert db.scalar(sa.select(sa.func.count()).select_from(Account).where(Account.tenant_id == parsed.tenant_id)) == 2


def test_real_owner_creation_replay_and_conflict(seeded):
    if version("dotmac-accounting") == "0.1.0a1":
        pytest.skip("real owner creation awaits released Accounting a2 adoption")
    db, parsed = seeded
    first = setup.apply_accounts(db, parsed)
    replay = setup.apply_accounts(db, parsed)
    assert {r["purpose"]: r["account_id"] for r in first} == {r["purpose"]: r["account_id"] for r in replay}
    assert all(r["action"] == "reuse" for r in replay)
    nhis = db.scalar(sa.select(Account).where(Account.tenant_id == parsed.tenant_id, Account.code == "2133"))
    nhis.name = "Conflicting name"
    db.flush()
    with pytest.raises(setup.OperatorRefusal, match="existing_account_conflict"):
        setup.apply_accounts(db, parsed)


def test_resolver_rejects_loan_class_drift_and_inactive_accounts(seeded):
    db, parsed = seeded
    nhis = Account(
        tenant_id=parsed.tenant_id,
        category_id=setup.NHIS_CATEGORY_ID,
        code="2133",
        name="NHIS Payable",
        kind=AccountKind.POSTING,
        normal_balance=NormalBalance.CREDIT,
        posting_allowed=True,
    )
    db.add(nhis)
    db.flush()
    refs = setup.resolve_future_deduction_accounts(db, parsed.tenant_id)
    assert refs == {"NHIS": nhis.id, "LOAN_REPAYMENT": setup.LOAN_ACCOUNT_ID}
    assert all(row["action"] == "reuse" for row in setup.apply_accounts(db, parsed))
    loan = db.get(Account, setup.LOAN_ACCOUNT_ID)
    asset = db.get(AccountCategory, loan.category_id)
    for wrong_class in (AccountClass.REVENUE, AccountClass.LIABILITY):
        asset.account_class = wrong_class
        db.flush()
        with pytest.raises(setup.OperatorRefusal, match="account_category_conflict"):
            setup.resolve_future_deduction_accounts(db, parsed.tenant_id)
    asset.account_class = AccountClass.ASSET
    loan.is_active = False
    db.flush()
    with pytest.raises(setup.OperatorRefusal, match="future_payroll_account_invalid"):
        setup.resolve_future_deduction_accounts(db, parsed.tenant_id)
    with pytest.raises(setup.OperatorRefusal, match="future_payroll_account_invalid"):
        setup.resolve_future_deduction_accounts(db, uuid4())
