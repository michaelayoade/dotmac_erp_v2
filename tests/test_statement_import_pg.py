"""Statement import against real PostgreSQL: real banking/accounting migrations, commits,
locks, audit rows and concurrent writers. Runs only on the disposable hosted CI service
(ERP_TEST_POSTGRES_URL). The owner role bypasses RLS; tenant scoping is asserted through
explicit tenant filters and per-test tenants.
"""

import hashlib
import os
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting.models import JournalEntry
from dotmac_banking.models import BankAccount, BankInstitution, BankStatement, BankStatementLine
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Tenant
from sqlalchemy.orm import Session

from erp_v2.banking import statement_import as si

NOW = datetime(2026, 2, 1, 12, tzinfo=UTC)
FILE_A = (
    b"date,type,amount,description,reference\n"
    b"2026-01-15,credit,300.50,Deposit,TRF-1\n"
    b"2026-01-16,debit,10.00,Fee,TRF-2\n"
)
FILE_B = (
    b"date,type,amount,description,reference\n"
    b"2026-01-16,debit,10.00,Fee again,TRF-2\n"
    b"2026-01-20,credit,5.00,New,TRF-3\n"
)


@pytest.fixture(scope="module")
def engine():
    raw_url = os.environ.get("ERP_TEST_POSTGRES_URL")
    if not raw_url:
        pytest.skip("ERP_TEST_POSTGRES_URL is required for hosted PostgreSQL qualification")
    admin_url = sa.make_url(raw_url)
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert admin_url.get_backend_name() == "postgresql"
    assert admin_url.host in {"localhost", "127.0.0.1", "::1"} and not admin_url.query
    assert admin_url.username == "postgres" and admin_url.database == "postgres"
    from erp_v2.migrate import upgrade_heads

    suffix = uuid4().hex
    database, owner = f"stmt_import_{suffix}", f"stmt_owner_{suffix}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    owner_engine = None
    created_database = created_owner = False
    try:
        with admin.connect() as connection:
            for role, options in (("app_admin", "LOGIN BYPASSRLS"), ("app_user", "LOGIN"), ("platform_api", "LOGIN")):
                if not connection.scalar(
                    sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}
                ):
                    connection.exec_driver_sql(f'CREATE ROLE "{role}" {options}')
            connection.exec_driver_sql(f'CREATE ROLE "{owner}" LOGIN BYPASSRLS CREATEROLE')
            created_owner = True
            connection.exec_driver_sql(f'GRANT app_admin, app_user, platform_api TO "{owner}" WITH ADMIN OPTION')
            connection.exec_driver_sql(f'CREATE DATABASE "{database}" OWNER "{owner}"')
            created_database = True
        schema_admin = sa.create_engine(admin_url.set(database=database), hide_parameters=True)
        try:
            with schema_admin.begin() as connection:
                connection.exec_driver_sql("ALTER SCHEMA public OWNER TO app_admin")
                connection.exec_driver_sql(f'GRANT USAGE, CREATE ON SCHEMA public TO "{owner}"')
        finally:
            schema_admin.dispose()
        owner_url = admin_url.set(username=owner, database=database)
        upgrade_heads(owner_url.render_as_string(hide_password=False))
        owner_engine = sa.create_engine(owner_url, hide_parameters=True, pool_size=8)
        yield owner_engine
    finally:
        if owner_engine is not None:
            owner_engine.dispose()
        with admin.connect() as connection:
            if created_database:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            if created_owner:
                connection.exec_driver_sql(f'DROP ROLE "{owner}"')
        admin.dispose()


def seed(engine, accounts=1):
    tenant_id = uuid4()
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, slug=f"t{tenant_id.hex[:24]}", name="Stmt"))
        db.flush()
        institution = BankInstitution(
            id=uuid4(), tenant_id=tenant_id, code="BK", name="Bank", country_code="NG", status="active"
        )
        db.add(institution)
        db.flush()
        ids = []
        for n in range(accounts):
            account = BankAccount(
                id=uuid4(),
                tenant_id=tenant_id,
                institution_id=institution.id,
                account_code=f"OP{n}",
                account_name="Operating",
                account_identifier=f"id-{n}",
                account_type_code="current",
                currency_code="NGN",
                minor_units=2,
                cash_account_ref="GL:1",
                status="active",
            )
            db.add(account)
            ids.append(account.id)
        db.commit()
    return SimpleNamespace(tenant_id=tenant_id, accounts=ids)


def run(engine, env, data, account_index=0, *, commit=True):
    with Session(engine) as db:
        result = si.import_statement(
            db,
            tenant_id=env.tenant_id,
            actor_party_id=uuid4(),
            data=data,
            file_format="csv",
            expected_sha256=hashlib.sha256(data).hexdigest(),
            options=si.ImportOptions(account_id=env.accounts[account_index]),
            now=NOW,
        )
        if commit:
            db.commit()
        return result


def n(engine, model, tenant_id):
    with Session(engine) as db:
        return db.scalar(sa.select(sa.func.count()).select_from(model).where(model.tenant_id == tenant_id))


@pytest.fixture(autouse=True, scope="module")
def audit_registry(engine):
    from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions

    from erp_v2.assembly import assembly

    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))


def test_import_commits_statement_lines_provenance_and_audit_without_any_journal(engine):
    env = seed(engine)
    result = run(engine, env, FILE_A)
    with Session(engine) as db:
        statement = db.get(BankStatement, result.statement_id)
        assert statement.source_ref.endswith(result.sha256) and statement.evidence_ref.endswith(result.sha256)
        assert statement.total_lines == 2
        events = db.scalars(
            sa.select(AuditEvent).where(
                AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == si.AUDIT_STATEMENT_IMPORTED
            )
        ).all()
        assert len(events) == 1 and events[0].entity_id == str(result.statement_id)
    assert n(engine, BankStatementLine, env.tenant_id) == 2
    assert n(engine, JournalEntry, env.tenant_id) == 0  # no GL posting in B6


def test_replay_and_overlap_are_refused_after_commit(engine):
    env = seed(engine)
    run(engine, env, FILE_A)
    for data in (FILE_A, FILE_B):
        with pytest.raises(si.StatementImportRefused):
            run(engine, env, data)
    assert n(engine, BankStatement, env.tenant_id) == 1
    assert n(engine, BankStatementLine, env.tenant_id) == 2


def test_uncommitted_import_leaves_nothing(engine):
    env = seed(engine)
    run(engine, env, FILE_A, commit=False)
    assert n(engine, BankStatement, env.tenant_id) == 0
    run(engine, env, FILE_A)  # and is therefore still importable
    assert n(engine, BankStatement, env.tenant_id) == 1


def test_tenants_are_isolated_the_same_file_imports_into_each(engine):
    one, two = seed(engine), seed(engine)
    run(engine, one, FILE_A)
    run(engine, two, FILE_A)
    assert n(engine, BankStatement, one.tenant_id) == n(engine, BankStatement, two.tenant_id) == 1
    with pytest.raises(si.StatementImportRefused, match="not found"):
        run(engine, SimpleNamespace(tenant_id=two.tenant_id, accounts=one.accounts), FILE_B)


def test_concurrent_identical_and_overlapping_imports_land_exactly_once(engine):
    env = seed(engine)
    outcomes: list[str] = []
    barrier = threading.Barrier(4)

    def worker(data):
        barrier.wait()
        try:
            run(engine, env, data)
            outcomes.append("ok")
        except si.StatementImportRefused:
            outcomes.append("refused")

    threads = [threading.Thread(target=worker, args=(d,)) for d in (FILE_A, FILE_A, FILE_B, FILE_B)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    # FILE_A and FILE_B overlap on TRF-2, so exactly one statement can ever land.
    assert outcomes.count("ok") == 1 and outcomes.count("refused") == 3
    assert n(engine, BankStatement, env.tenant_id) == 1
    with Session(engine) as db:
        refs = db.scalars(
            sa.select(BankStatementLine.external_ref).where(BankStatementLine.tenant_id == env.tenant_id)
        ).all()
        assert len(refs) == len(set(refs)) == 2


def test_period_overlap_with_an_existing_statement_is_refused_even_with_different_references(engine):
    env = seed(engine)
    run(engine, env, FILE_A)  # covers 2026-01-15..2026-01-16
    other = b"date,type,amount,description,reference\n2026-01-15,credit,999.00,Other export,ZZ-9\n"
    with pytest.raises(si.StatementImportRefused, match="overlaps existing statement"):
        run(engine, env, other)
    assert n(engine, BankStatement, env.tenant_id) == 1
