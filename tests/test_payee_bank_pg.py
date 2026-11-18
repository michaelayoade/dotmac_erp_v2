"""Payee bank instructions on real PostgreSQL: migration, privileges, constraints, real audit rows.

Runs only on the disposable hosted CI service (ERP_TEST_POSTGRES_URL). The owner role bypasses
RLS, so tenant scoping is asserted with explicit filters; privileges of the runtime role are
asserted from the catalog.
"""

import os
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel import audit_actions
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry
from dotmac_kernel.models import Party, PartyPerson, PartyType, Tenant
from dotmac_payables.models import SupplierInvoice
from dotmac_people.models import Employee
from dual_control_support import set_dual_control, set_dual_control_engine
from sqlalchemy.orm import Session

from erp_v2.importer.minting import MintContext, run_mint_step
from erp_v2.payees import service
from erp_v2.payees.models import PayeeBankInstruction
from erp_v2.payees.routes import payee_bank_feature
from erp_v2.payees.service import PayeeBankError

NUMBER = "0123456789"
OTHER = "9876543210"
PREPARER, APPROVER, APPROVER2 = uuid4(), uuid4(), uuid4()


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
    database, owner = f"payee_bank_{suffix}", f"payee_owner_{suffix}"
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


@pytest.fixture(autouse=True)
def audit_registry(monkeypatch):
    monkeypatch.setattr(
        audit_actions, "_active_registry", AuditActionRegistry.from_manifests([payee_bank_feature]), raising=False
    )


def new_tenant(engine, suppliers=("sup-1", "sup-2", "sup-3")):
    """Tenant with payees in the REAL systems of record (payables supplier set)."""
    tenant_id = uuid4()
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, slug=f"t{tenant_id.hex[:24]}", name="Payee"))
        db.flush()
        set_dual_control(db, tenant_id, True)  # existing separation-of-duties assertions run with the policy ON
        for n, ref in enumerate(suppliers):
            add_supplier(db, tenant_id, ref, n)
        db.commit()
    return tenant_id


def add_supplier(db, tenant_id, ref, n=0):
    db.add(
        SupplierInvoice(
            tenant_id=tenant_id,
            number=f"N-{ref}-{n}",
            supplier_ref=ref,
            supplier_name_snapshot=f"System {ref}",
            invoice_date=date(2026, 1, 1),
            received_date=date(2026, 1, 1),
            currency_code="NGN",
            exchange_rate=Decimal(1),
            liability_account_ref="x",
            subtotal=Decimal(10),
            tax_amount=Decimal(0),
            total_amount=Decimal(10),
            payment_schedule=[],
            request_fingerprint="f" * 64,
        )
    )
    db.flush()


def add_employee(db, tenant_id, code="E001"):
    party = uuid4()
    db.add(Party(id=party, tenant_id=tenant_id, party_type=PartyType.person, display_name=code, email=f"{code}@x.io"))
    db.flush()
    db.add(PartyPerson(party_id=party, first_name="Ada", last_name="Test"))
    db.flush()
    emp = Employee(tenant_id=tenant_id, party_id=party, employee_code=code, date_of_joining=date(2025, 1, 1))
    db.add(emp)
    db.flush()
    return emp


def propose(db, tenant, *, ref="sup-1", number=NUMBER, by=PREPARER, kind="supplier", **over):
    args = dict(
        tenant_id=tenant,
        proposer_party_id=by,
        payee_kind=kind,
        payee_ref=ref,
        bank_code="058",
        bank_name="Example Bank",
        account_number=number,
        account_name="ACME SUPPLIES LTD",
        evidence_ref="letter-1",
    )
    args.update(over)
    return service.propose(db, **args)


def test_migration_creates_the_table_with_rls_and_lifecycle_only_runtime_privileges(engine):
    with engine.connect() as c:
        rls = c.execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'payee_bank_instructions'"
            )
        ).one()
        assert tuple(rls) == (True, True)

        def can(column, priv):
            return c.scalar(
                sa.text("SELECT has_column_privilege('app_user', 'public.payee_bank_instructions', :c, :p)"),
                {"c": column, "p": priv},
            )

        for column in (
            "account_number",
            "account_last4",
            "bank_code",
            "account_name",
            "payee_ref",
            "proposed_by_party_id",
        ):
            assert can(column, "UPDATE") is False, column
        for column in ("status", "decided_by_party_id", "approved_at", "revoked_at", "superseded_by_id"):
            assert can(column, "UPDATE") is True, column
        table = "public.payee_bank_instructions"
        assert c.scalar(sa.text(f"SELECT has_table_privilege('app_user', '{table}', 'INSERT')")) is True
        assert c.scalar(sa.text(f"SELECT has_table_privilege('app_user', '{table}', 'DELETE')")) is False
        assert c.scalar(sa.text(f"SELECT has_table_privilege('app_user', '{table}', 'TRUNCATE')")) is False


def test_full_lifecycle_supersession_and_real_audit_rows_without_the_number(engine):
    tenant = new_tenant(engine)
    with Session(engine) as db:
        first = propose(db, tenant)
        service.approve(db, tenant_id=tenant, instruction_id=first.id, approver_party_id=APPROVER)
        second = propose(db, tenant, number=OTHER)
        service.approve(db, tenant_id=tenant, instruction_id=second.id, approver_party_id=APPROVER2)
        service.reveal_account_number(
            db, tenant_id=tenant, instruction_id=second.id, actor_party_id=APPROVER2, reason="verify"
        )
        service.revoke(db, tenant_id=tenant, instruction_id=second.id, revoker_party_id=APPROVER, reason="closed")
        db.commit()
    with Session(engine) as db:
        statuses = dict(
            db.execute(
                sa.select(PayeeBankInstruction.account_last4, PayeeBankInstruction.status).where(
                    PayeeBankInstruction.tenant_id == tenant
                )
            ).all()
        )
        assert statuses == {NUMBER[-4:]: "superseded", OTHER[-4:]: "revoked"}
        events = db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == tenant)).all()
        # created_at ties inside one transaction, so compare as a multiset.
        assert sorted(e.action for e in events) == sorted(
            [
                service.AUDIT_PROPOSED,
                service.AUDIT_APPROVED,
                service.AUDIT_PROPOSED,
                service.AUDIT_APPROVED,
                service.AUDIT_UNMASKED_READ,
                service.AUDIT_REVOKED,
            ]
        )
        assert not any(NUMBER in repr(e.details) or OTHER in repr(e.details) for e in events)
        with pytest.raises(PayeeBankError) as err:
            service.destination_for_payment(db, tenant_id=tenant, payee_kind="supplier", payee_ref="sup-1")
        assert err.value.code == "no_approved_instruction"


def test_database_refuses_self_approval_second_approved_row_and_anonymous_manual_rows(engine):
    tenant = new_tenant(engine)
    with Session(engine) as db:
        row = propose(db, tenant)
        db.commit()
        row_id = row.id
    with Session(engine) as db:
        with pytest.raises(sa.exc.IntegrityError):
            db.execute(
                sa.text(
                    "UPDATE payee_bank_instructions SET status='approved', approved_at=now(), "
                    "decided_by_party_id=:p WHERE id=:i"
                ),
                {"p": PREPARER, "i": row_id},
            )
        db.rollback()
    with Session(engine) as db:
        approved = propose(db, tenant, ref="sup-2")
        service.approve(db, tenant_id=tenant, instruction_id=approved.id, approver_party_id=APPROVER)
        db.flush()
        duplicate = PayeeBankInstruction(
            tenant_id=tenant,
            payee_kind="supplier",
            payee_ref="sup-2",
            payee_label="x",
            bank_code="058",
            account_number=OTHER,
            account_last4=OTHER[-4:],
            status="approved",
            origin="manual",
            proposed_by_party_id=PREPARER,
            proposed_at=datetime.now(UTC),
            decided_by_party_id=APPROVER,
            approved_at=datetime.now(UTC),
        )
        db.add(duplicate)
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()
    with Session(engine) as db:
        db.add(
            PayeeBankInstruction(
                tenant_id=tenant,
                payee_kind="supplier",
                payee_ref="sup-3",
                payee_label="x",
                account_number=NUMBER,
                account_last4=NUMBER[-4:],
                status="proposed",
                origin="manual",
                proposed_by_party_id=None,
                proposed_at=datetime.now(UTC),
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()


def test_concurrent_approvals_of_one_proposal_have_exactly_one_winner(engine):
    tenant = new_tenant(engine)
    with Session(engine) as db:
        row = propose(db, tenant)
        db.commit()
        row_id = row.id
    holder = Session(engine)
    service.approve(holder, tenant_id=tenant, instruction_id=row_id, approver_party_id=APPROVER)
    outcome = {}

    def loser():
        with Session(engine) as db:
            try:
                service.approve(db, tenant_id=tenant, instruction_id=row_id, approver_party_id=APPROVER2)
                db.commit()
                outcome["won"] = True
            except PayeeBankError as exc:
                db.rollback()
                outcome["code"] = exc.code

    thread = threading.Thread(target=loser)
    thread.start()
    time.sleep(1.0)  # the second approver is now blocked on the first's row lock
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and outcome == {"code": "wrong_state"}
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(PayeeBankInstruction)
                .where(PayeeBankInstruction.tenant_id == tenant, PayeeBankInstruction.status == "approved")
            )
            == 1
        )


def test_legacy_import_mint_migrates_complete_rows_as_approved_on_the_real_table(engine):
    tenant = new_tenant(engine)
    src = sa.create_engine("sqlite://").connect()
    src.execute(sa.text("ATTACH DATABASE ':memory:' AS public"))
    src.execute(
        sa.text(
            "CREATE TABLE public.employee (employee_id TEXT, organization_id TEXT, employee_code TEXT, "
            "bank_name TEXT, bank_account_number TEXT, bank_account_name TEXT, bank_branch_code TEXT)"
        )
    )
    src.execute(
        sa.text(
            "CREATE TABLE public.supplier (supplier_id TEXT, organization_id TEXT, legal_name TEXT, "
            "trading_name TEXT, bank_details TEXT)"
        )
    )
    supplier = uuid4()
    with Session(engine) as db:
        add_supplier(db, tenant, str(supplier))
        db.commit()
    details = (
        '{"bank_code": "058", "bank_name": "Example Bank", "account_name": "SUP", "account_number": "%s"}' % NUMBER
    )
    src.execute(
        sa.text("INSERT INTO public.supplier VALUES (:s, :t, 'Acme', NULL, :d)"),
        {"s": str(supplier), "t": str(tenant), "d": details},
    )
    ctx = MintContext(tenant_id=str(tenant), actor_id="enrolment-actor", occurred_at=datetime(2026, 11, 14, tzinfo=UTC))
    with engine.connect() as dest:
        result = run_mint_step("payee_bank_instructions", src, dest, ctx)
        dest.commit()
    assert result.refused is None and result.created == 1
    with Session(engine) as db:
        row = db.scalar(sa.select(PayeeBankInstruction).where(PayeeBankInstruction.tenant_id == tenant))
        # complete legacy row: migrated straight to approved by the declared actor, with provenance
        assert (row.status, row.origin, row.proposed_by_party_id, row.payee_ref) == (
            "approved",
            "legacy_import",
            None,
            str(supplier),
        )
        assert row.decided_by_party_id is not None and row.approved_at is not None
        assert row.source_ref == f"legacy:supplier.bank_details:{supplier}" and len(row.source_fingerprint) == 64
        migrated = db.scalars(
            sa.select(AuditEvent).where(AuditEvent.tenant_id == tenant, AuditEvent.action == service.AUDIT_MIGRATED)
        ).all()
        assert len(migrated) == 1 and NUMBER not in repr(migrated[0].details)
        dest = service.destination_for_payment(db, tenant_id=tenant, payee_kind="supplier", payee_ref=str(supplier))
        assert dest.instruction_id == row.id
        # every later change is two-person: the migration actor is not a bypass for anyone else
        change = propose(db, tenant, ref=str(supplier), number=OTHER)
        with pytest.raises(PayeeBankError) as same:
            service.approve(db, tenant_id=tenant, instruction_id=change.id, approver_party_id=PREPARER)
        assert same.value.code == "same_person"
        service.approve(db, tenant_id=tenant, instruction_id=change.id, approver_party_id=APPROVER)
        db.commit()


def test_real_systems_of_record_resolve_payees_and_unknowns_are_refused(engine):
    tenant, other = new_tenant(engine), new_tenant(engine, suppliers=())
    with Session(engine) as db:
        emp = add_employee(db, tenant)
        row = propose(db, tenant, ref=str(emp.id), kind="employee")
        assert row.payee_label == "E001 Ada Test"
        sup = propose(db, tenant, ref="sup-1")
        assert sup.payee_label == "System sup-1"
        db.commit()
    with Session(engine) as db:
        for kwargs in ({"ref": "sup-1", "tenant": other}, {"ref": "nobody"}, {"ref": str(uuid4()), "kind": "employee"}):
            kwargs.setdefault("tenant", tenant)
            with pytest.raises(PayeeBankError) as err:
                propose(db, **kwargs)
            assert err.value.code == "unknown_payee"
            db.rollback()


def test_a_correction_replaces_an_open_legacy_proposal_on_real_postgres(engine):
    tenant = new_tenant(engine)
    with Session(engine) as db:
        emp = add_employee(db, tenant)
        legacy = PayeeBankInstruction(
            tenant_id=tenant,
            payee_kind="employee",
            payee_ref=str(emp.id),
            payee_label="legacy",
            bank_code=None,
            bank_name="Example Bank",
            account_number=NUMBER,
            account_last4=NUMBER[-4:],
            status="proposed",
            origin="legacy_import",
            proposed_at=datetime.now(UTC),
        )
        db.add(legacy)
        db.flush()
        new = propose(db, tenant, ref=str(emp.id), kind="employee", number=OTHER)
        db.commit()
        legacy_id, new_id = legacy.id, new.id
    with Session(engine) as db:
        old = db.get(PayeeBankInstruction, legacy_id)
        assert (old.status, old.decision_note) == ("rejected", f"replaced by {new_id}")
        service.approve(db, tenant_id=tenant, instruction_id=new_id, approver_party_id=APPROVER)
        db.commit()
        events = db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == tenant)).all()
        assert any(e.action == service.AUDIT_REJECTED and e.details["replaced_by"] == str(new_id) for e in events)


@pytest.fixture()
def runtime_engine(engine):
    """NOBYPASSRLS LOGIN member of app_user: the role the runtime actually uses."""
    role = f"payee_rt_{uuid4().hex[:12]}"
    admin = sa.create_engine(
        sa.make_url(os.environ["ERP_TEST_POSTGRES_URL"]), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOBYPASSRLS')
        c.exec_driver_sql(f'GRANT app_user TO "{role}"')
    rt = sa.create_engine(engine.url.set(username=role), hide_parameters=True)
    try:
        yield rt
    finally:
        rt.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


def scoped(rt, tenant):
    db = Session(rt)
    db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
    return db


def test_runtime_role_lifecycle_rls_and_privileges_without_bypass(engine, runtime_engine):
    tenant, other = new_tenant(engine), new_tenant(engine)
    with engine.connect() as c:
        assert (
            c.scalar(
                sa.text("SELECT rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": runtime_engine.url.username}
            )
            is False
        )
    with scoped(runtime_engine, tenant) as db:
        first = propose(db, tenant)
        service.approve(db, tenant_id=tenant, instruction_id=first.id, approver_party_id=APPROVER)
        assert (
            service.reveal_account_number(
                db, tenant_id=tenant, instruction_id=first.id, actor_party_id=APPROVER, reason="verify"
            )
            == NUMBER
        )
        service.revoke(db, tenant_id=tenant, instruction_id=first.id, revoker_party_id=APPROVER2, reason="closed")
        second = propose(db, tenant, number=OTHER)
        second_id = second.id
        db.commit()
    with scoped(runtime_engine, tenant) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(PayeeBankInstruction)) == 2
        for statement in (
            "UPDATE payee_bank_instructions SET account_number = '1111111111'",
            "UPDATE payee_bank_instructions SET payee_ref = 'x'",
            "DELETE FROM payee_bank_instructions",
            "TRUNCATE payee_bank_instructions",
        ):
            with pytest.raises(sa.exc.ProgrammingError) as err:
                db.execute(sa.text(statement))
            assert "permission denied" in str(err.value.orig).lower(), statement
            db.rollback()
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
    with scoped(runtime_engine, other) as db:  # another tenant sees nothing, and cannot touch it
        assert db.scalar(sa.select(sa.func.count()).select_from(PayeeBankInstruction)) == 0
        db.execute(sa.text("UPDATE payee_bank_instructions SET status = 'revoked' WHERE id = :i"), {"i": second_id})
        db.commit()
    with Session(engine) as db:
        assert db.get(PayeeBankInstruction, second_id).status == "proposed"
    with Session(runtime_engine) as db:  # no tenant scope at all: RLS yields no rows
        assert db.scalar(sa.select(sa.func.count()).select_from(PayeeBankInstruction)) == 0


# --- dual-control policy (D-9): default OFF ----------------------------------------------------------------------


def test_policy_off_the_proposer_can_approve_and_the_row_is_stamped_off(engine):
    tenant = new_tenant(engine)
    set_dual_control_engine(engine, tenant, False)
    with Session(engine) as db:
        row = propose(db, tenant)
        approved = service.approve(db, tenant_id=tenant, instruction_id=row.id, approver_party_id=PREPARER)
        db.commit()
        assert approved.status == "approved" and approved.dual_control is False
        assert approved.proposed_by_party_id == approved.decided_by_party_id == PREPARER


def test_policy_on_stamps_true_and_the_proposer_is_refused(engine):
    tenant = new_tenant(engine)
    with Session(engine) as db:
        row = propose(db, tenant)
        assert row.dual_control is True
        with pytest.raises(PayeeBankError) as err:
            service.approve(db, tenant_id=tenant, instruction_id=row.id, approver_party_id=PREPARER)
        assert err.value.code == "same_person"


def _approved_clone(tenant, *, dual_control, decider, approved_at=True):
    return PayeeBankInstruction(
        tenant_id=tenant,
        payee_kind="supplier",
        payee_ref="sup-3",
        payee_label="x",
        bank_code="058",
        account_number=NUMBER,
        account_last4=NUMBER[-4:],
        status="approved",
        origin="manual",
        dual_control=dual_control,
        proposed_by_party_id=PREPARER,
        proposed_at=datetime.now(UTC),
        decided_by_party_id=decider,
        approved_at=datetime.now(UTC) if approved_at else None,
    )


def test_check_two_person_is_rewritten_and_its_integrity_terms_stay_unconditional(engine):
    tenant = new_tenant(engine)
    with Session(engine) as db:  # on: self-approval refused by the rewritten CHECK
        db.add(_approved_clone(tenant, dual_control=True, decider=PREPARER))
        with pytest.raises(sa.exc.IntegrityError) as err:
            db.flush()
        assert "ck_payee_bank_instructions_two_person" in str(err.value)
        db.rollback()
    with Session(engine) as db:  # off: the same person is accepted
        db.add(_approved_clone(tenant, dual_control=False, decider=PREPARER))
        db.flush()
        db.rollback()
    with Session(engine) as db:  # off: an approved row still needs its approval time (integrity companion)
        db.add(_approved_clone(tenant, dual_control=False, decider=APPROVER, approved_at=False))
        with pytest.raises(sa.exc.IntegrityError) as err:
            db.flush()
        assert "ck_payee_bank_instructions_two_person_integrity" in str(err.value)
        db.rollback()


def test_runtime_has_no_update_on_the_dual_control_column(engine, runtime_engine):
    with runtime_engine.connect() as c:
        allowed = c.execute(
            sa.text("SELECT has_column_privilege('app_user', 'payee_bank_instructions', 'dual_control', 'UPDATE')")
        ).scalar()
    assert allowed is False
