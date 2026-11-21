"""Staff loan entries (PR-3) against real PostgreSQL (hosted CI; ERP_TEST_POSTGRES_URL).

Real accounting/payables/payroll migrations, the real posting bridge, the real e2_0023 table. Covers the
advance (Dr 1410 / Cr cash GL), the opening bound against GL(1410), the adjustment, the control-difference
write-off, the employee balance with payroll recoveries, the B4 ``loan_recovery_exceeds_balance`` check and
the migration downgrade. No second person is required anywhere: one party prepares, approves and records.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import account_maintenance_support as accounts_gov
import pytest
import sqlalchemy as sa
import test_b3_supplier_payments_pg as b3
import test_b4_payroll_finalize_pg as b4
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, PostedLedgerLine
from dotmac_banking.models import BankAccount
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dual_control_support import set_dual_control_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from staff_loan_helpers import seed_recorded_opening
from test_posting_bridge_pg import NOW, entry

from erp_v2.assembly import assembly
from erp_v2.disbursement import bank_reference as bank_ref
from erp_v2.journals.routes import journals_web_feature
from erp_v2.payees.models import PayeeBankInstruction
from erp_v2.posting import bridge
from erp_v2.staff_loans import queries, service
from erp_v2.staff_loans.models import StaffLoanEntry
from erp_v2.staff_loans.service import LoanRefused
from erp_v2.supplier_payments.service import PaymentRefused
from erp_v2.web.payroll_run_services import RunRefused

engine = bridge_pg.engine
VALUE_DATE = date(2026, 1, 12)  # on or before NOW (2026-01-15)
OPENING_DATE = date(2025, 12, 31)


@pytest.fixture(scope="module", autouse=True)
def registries():
    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))
    manifests = [journals_web_feature, FeatureManifest(name="kernel_stub")]
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


def world(engine, *, instruction=True):
    env = b3._world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)  # the default policy; the policy tests set it explicitly
    with Session(engine) as db:
        asset = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "AST")
        )
        expense = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "EXP")
        )
        for code, category, balance in (
            ("1410", asset, NormalBalance.DEBIT),
            ("6100", expense, NormalBalance.DEBIT),
        ):
            row = Account(
                tenant_id=env.tenant_id,
                category_id=category.id,
                code=code,
                name=f"Account {code}",
                kind=AccountKind.POSTING,
                normal_balance=balance,
            )
            db.add(row)
            db.flush()
            env.accounts[code] = row.id
        db.commit()
    if instruction:
        seed_instruction(engine, env, "EMP-1")
    return env


def seed_instruction(engine, env, employee_ref):
    with Session(engine) as db:
        db.add(
            PayeeBankInstruction(
                tenant_id=env.tenant_id,
                payee_kind="employee",
                payee_ref=employee_ref,
                payee_label=employee_ref,
                bank_code="058",
                bank_name="Example Bank",
                account_number="0123456789",
                account_last4="6789",
                account_name="EMPLOYEE ONE",
                status="approved",
                origin="legacy_import",
                proposed_at=NOW,
                decided_by_party_id=env.people["approver"],
                decided_at=NOW,
                approved_at=NOW,
            )
        )
        db.commit()


def seed_gl_1410(engine, env, amount):
    """A GL debit on 1410 that comes from no loan entry (the legacy residue), via the posting bridge."""
    with Session(engine) as db:
        c = bridge_pg.consequence(env, amount=str(amount), document=f"gl-{uuid4()}")
        c.entries = (
            entry(env.accounts["1410"], debit=str(amount)),
            entry(env.accounts["2000"], credit=str(amount)),
        )
        bridge.post_consequence(db, scope=env.scope, consequence=c, approval_ref="seed", actor="seed", now=NOW)
        db.commit()


def act(env, who, fn, **kw):
    return b3.act(ENGINE[0], env, who, fn, **kw)


ENGINE = [None]


@pytest.fixture(autouse=True)
def _bind(engine):
    ENGINE[0] = engine


def advance(env, *, amount="5000", employee="EMP-1", who="preparer", request_id=None, value_date=VALUE_DATE):
    return act(
        env,
        who,
        service.prepare_advance,
        request_id=request_id or uuid4(),
        employee_ref=employee,
        amount=Decimal(amount),
        bank_account_id=env.bank_id,
        value_date=value_date,
        evidence_ref="LOAN-AGREEMENT-1",
    )


def opening(env, *, amount="1000", employee="EMP-1", value_date=OPENING_DATE, who="preparer"):
    return act(
        env,
        who,
        service.prepare_opening,
        request_id=uuid4(),
        employee_ref=employee,
        amount=Decimal(amount),
        value_date=value_date,
        evidence_ref="FINANCE-SCHEDULE-2025",
    )


def adjustment(env, *, amount, employee="EMP-1", offset="6100", who="preparer", value_date=VALUE_DATE):
    return act(
        env,
        who,
        service.prepare_adjustment,
        request_id=uuid4(),
        employee_ref=employee,
        amount=Decimal(amount),
        offset_account_id=env.accounts[offset],
        value_date=value_date,
        evidence_ref="FINANCE-NOTE-1",
    )


def control_difference(env, *, amount, offset="6100", who="preparer"):
    return act(
        env,
        who,
        service.prepare_control_difference,
        request_id=uuid4(),
        amount=Decimal(amount),
        offset_account_id=env.accounts[offset],
        value_date=VALUE_DATE,
        evidence_ref="FINANCE-WRITE-OFF-1",
    )


def approve(env, entry_id, who="preparer"):
    return act(env, who, service.approve_entry, entry_id=entry_id)


def record(env, entry_id, who="preparer", ref=None, now=NOW):
    return act(env, who, service.record_entry, entry_id=entry_id, bank_reference=ref, now=now)


def posted_advance(env, *, amount="5000", employee="EMP-1", ref="BANK-REF-1"):
    created = advance(env, amount=amount, employee=employee)
    approve(env, created.entry_id)
    return record(env, created.entry_id, ref=ref)


def row(engine, entry_id):
    with Session(engine) as db:
        return db.get(StaffLoanEntry, entry_id)


def balance(engine, env, employee="EMP-1"):
    with Session(engine) as db:
        return queries.employee_balance(db, tenant_id=env.tenant_id, employee_ref=employee)


def position(engine, env):
    with Session(engine) as db:
        return queries.book_position(db, tenant_id=env.tenant_id)


def refused(call, code):
    with pytest.raises(LoanRefused) as exc:
        call()
    assert exc.value.code == code, exc.value.code
    return exc.value


def counts(engine, env):
    with Session(engine) as db:

        def n(model):
            return db.scalar(sa.select(sa.func.count()).select_from(model).where(model.tenant_id == env.tenant_id))

        return {"entries": n(StaffLoanEntry), "journals": n(JournalEntry), "ledger": n(PostedLedgerLine)}


def audits(engine, env, action):
    with Session(engine) as db:
        return list(
            db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == action))
        )


def lines(engine, journal_id):
    with Session(engine) as db:
        return {
            db.get(Account, r.account_id).code: (r.debit, r.credit)
            for r in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal_id))
        }


# --- advance -------------------------------------------------------------------------------------


def test_advance_posts_dr_1410_cr_the_bank_cash_gl_with_one_person_doing_every_step(engine):
    env = world(engine)
    done = posted_advance(env, amount="5000.5", ref="  bank  ref-1 ")
    assert done.replayed is False and done.journal_id is not None
    stored = row(engine, done.entry_id)
    assert stored.state == "recorded" and stored.kind == "advance" and stored.amount == Decimal("5000.5")
    assert stored.bank_reference == "BANK REF-1"  # the shared normalisation
    # a single person prepared, approved and recorded: no second-person rule applies
    assert stored.prepared_by_party_id == stored.approved_by_party_id == stored.recorded_by_party_id
    assert lines(engine, done.journal_id) == {"1410": (Decimal("5000.5"), 0), "1000": (0, Decimal("5000.5"))}
    with Session(engine) as db:
        journal = db.get(JournalEntry, done.journal_id)
        assert (journal.source_owner, journal.source_document_kind) == ("erp", "staff_loan_entry")
        assert journal.source_document_id == str(done.entry_id)
    assert balance(engine, env).balance == Decimal("5000.5")
    for action in (service.AUDIT_PREPARED, service.AUDIT_APPROVED, service.AUDIT_RECORDED):
        assert len(audits(engine, env, action)) == 1


def test_a_second_person_may_approve_and_record_too(engine):
    env = world(engine)
    created = advance(env)
    approve(env, created.entry_id, who="approver")
    done = record(env, created.entry_id, who="recorder", ref="BANK-REF-2")
    assert row(engine, done.entry_id).recorded_by_party_id == env.people["recorder"]


def test_prepare_replays_on_the_same_request_and_refuses_a_changed_one(engine):
    env = world(engine)
    request_id = uuid4()
    first = advance(env, request_id=request_id)
    again = advance(env, request_id=request_id)
    assert again.entry_id == first.entry_id and again.replayed is True
    assert counts(engine, env)["entries"] == 1 and len(audits(engine, env, service.AUDIT_PREPARED)) == 1
    refused(lambda: advance(env, request_id=request_id, amount="6000"), "request_conflict")


def test_record_and_approve_replay_without_a_second_journal(engine):
    env = world(engine)
    done = posted_advance(env)
    before = counts(engine, env)
    again = record(env, done.entry_id, ref="bank-ref-1")
    assert again.replayed is True and again.journal_id == done.journal_id
    assert approve(env, done.entry_id).replayed is True
    assert counts(engine, env) == before
    refused(lambda: record(env, done.entry_id, ref="OTHER-REF"), "already_recorded")


def test_advance_needs_an_approved_employee_instruction_at_approve_and_again_at_record(engine):
    env = world(engine, instruction=False)
    created = advance(env)
    before = counts(engine, env)
    refused(lambda: approve(env, created.entry_id), "payee_instruction_required")
    assert counts(engine, env) == before and row(engine, created.entry_id).state == "draft"
    assert any(a.details["code"] == "payee_instruction_required" for a in audits(engine, env, service.AUDIT_REFUSED))
    seed_instruction(engine, env, "EMP-1")
    approve(env, created.entry_id)
    with Session(engine) as db:  # the instruction is withdrawn after approval
        db.execute(
            sa.update(PayeeBankInstruction)
            .where(PayeeBankInstruction.tenant_id == env.tenant_id)
            .values(status="superseded")
        )
        db.commit()
    refused(lambda: record(env, created.entry_id, ref="BANK-REF-1"), "payee_instruction_required")
    assert counts(engine, env)["journals"] == 0 and row(engine, created.entry_id).state == "approved"


def test_advance_value_date_in_the_future_is_refused_at_record(engine):
    env = world(engine)
    created = advance(env, value_date=date(2026, 1, 20))
    approve(env, created.entry_id)
    refused(lambda: record(env, created.entry_id, ref="BANK-REF-1"), "value_date_future")
    assert counts(engine, env)["journals"] == 0
    record(env, created.entry_id, ref="BANK-REF-1", now=datetime(2026, 1, 20, tzinfo=UTC))
    assert row(engine, created.entry_id).state == "recorded"


@pytest.mark.parametrize("bad", [None, "ab", "bad\x07ref"])
def test_advance_needs_a_valid_bank_reference(engine, bad):
    env = world(engine)
    created = advance(env)
    approve(env, created.entry_id)
    refused(lambda: record(env, created.entry_id, ref=bad), "bank_reference_invalid")
    assert counts(engine, env)["journals"] == 0


def test_bank_reference_is_unique_across_loans_and_supplier_payments(engine):
    env = world(engine)
    posted_advance(env, ref="SHARED-REF-1")
    second = advance(env, amount="100")
    approve(env, second.entry_id)
    refused(lambda: record(env, second.entry_id, ref="shared-ref-1"), "bank_reference_reused")
    # and the other way round: B3 may not reuse a reference a loan advance already carries
    env2 = world(engine)
    b3.seed_legacy_payee(engine, env2)
    posted_advance(env2, ref="SHARED-REF-2")
    oid = b3.open_obligation(engine, env2)
    payment_id = b3.approved_payment(engine, env2, oid)
    with pytest.raises(PaymentRefused) as exc:
        b3.record(engine, env2, payment_id, "shared-ref-2")
    assert exc.value.code == "bank_reference_reused"
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(StaffLoanEntry)
                .where(StaffLoanEntry.bank_reference == "SHARED-REF-2")
            )
            == 1
        )


def test_claim_sees_a_loan_reference(engine):
    env = world(engine)
    posted_advance(env, ref="CLAIM-REF-1")
    with Session(engine) as db:
        with pytest.raises(bank_ref.BankReferenceReused):
            bank_ref.claim(db, tenant_id=env.tenant_id, bank_account_id=env.bank_id, reference="CLAIM-REF-1")
        bank_ref.claim(db, tenant_id=env.tenant_id, bank_account_id=env.bank_id, reference="CLAIM-REF-OTHER")


def test_advance_bank_account_checks_come_from_the_reused_payer_references(engine):
    env = world(engine)
    other = world(engine)
    refused(
        lambda: act(
            env,
            "preparer",
            service.prepare_advance,
            request_id=uuid4(),
            employee_ref="EMP-1",
            amount=Decimal(100),
            bank_account_id=other.bank_id,
            value_date=VALUE_DATE,
            evidence_ref="X-1",
        ),
        "bank_account_unknown",
    )
    with Session(engine) as db:
        db.execute(sa.update(BankAccount).where(BankAccount.id == env.bank_id).values(status="suspended"))
        db.commit()
    refused(lambda: advance(env), "bank_account_inactive")
    assert counts(engine, env)["entries"] == 0


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"amount": "0"}, "invalid_amount"),
        ({"amount": "-5"}, "invalid_amount"),
        ({"amount": "1.0000001"}, "invalid_amount"),
        ({"employee": "  "}, "employee_required"),
    ],
)
def test_prepare_validates_input(engine, kwargs, code):
    env = world(engine)
    refused(lambda: advance(env, **kwargs), code)
    assert counts(engine, env)["entries"] == 0


def test_entries_need_evidence(engine):
    env = world(engine)
    refused(
        lambda: act(
            env,
            "preparer",
            service.prepare_opening,
            request_id=uuid4(),
            employee_ref="EMP-1",
            amount=Decimal(1),
            value_date=OPENING_DATE,
            evidence_ref=" ",
        ),
        "evidence_required",
    )


def test_a_refused_record_rolls_the_savepoint_back_entirely(engine):
    env = world(engine)
    created = advance(env)
    approve(env, created.entry_id)
    bridge_pg.soft_close(engine, env)
    refused(lambda: record(env, created.entry_id, ref="BANK-REF-1"), "posting_period_closed")
    stored = row(engine, created.entry_id)
    assert stored.state == "approved" and stored.bank_reference is None and stored.journal_id is None
    assert counts(engine, env)["journals"] == 0


# --- opening -------------------------------------------------------------------------------------


def test_opening_is_subledger_only_and_bounded_by_gl_1410(engine):
    env = world(engine)
    seed_gl_1410(engine, env, 805000)
    before = counts(engine, env)
    created = opening(env, amount="500000")
    approve(env, created.entry_id)
    done = record(env, created.entry_id)
    assert done.journal_id is None and row(engine, done.entry_id).state == "recorded"
    after = counts(engine, env)
    assert after["journals"] == before["journals"] and after["ledger"] == before["ledger"]  # no GL
    assert balance(engine, env).balance == Decimal(500000)
    pos = position(engine, env)
    assert (pos.gl_balance, pos.subledger_total, pos.difference) == (Decimal(805000), Decimal(500000), Decimal(305000))
    # 305,000 of headroom is left: one more opening that fits, then one that does not
    second = opening(env, amount="305000", employee="EMP-2")
    approve(env, second.entry_id)
    record(env, second.entry_id)
    assert position(engine, env).difference == 0
    refused(lambda: opening(env, amount="0.01", employee="EMP-3"), "opening_exceeds_gl")


def test_opening_above_the_gl_headroom_is_refused_at_prepare_and_record_rechecks(engine):
    env = world(engine)
    seed_gl_1410(engine, env, 1000)
    refused(lambda: opening(env, amount="1000.01"), "opening_exceeds_gl")
    first = opening(env, amount="800")
    second = opening(env, amount="800", employee="EMP-2")
    approve(env, first.entry_id)
    approve(env, second.entry_id)  # each fits alone
    record(env, first.entry_id)
    refused(lambda: record(env, second.entry_id), "opening_exceeds_gl")  # re-checked against the new total
    assert row(engine, second.entry_id).state == "approved"


def test_opening_with_no_gl_at_all_is_refused(engine):
    env = world(engine)
    refused(lambda: opening(env, amount="1"), "opening_exceeds_gl")


def test_an_employees_openings_share_one_date_and_precede_other_entries(engine):
    env = world(engine)
    seed_gl_1410(engine, env, 10000)
    first = opening(env, amount="100")
    approve(env, first.entry_id)
    record(env, first.entry_id)
    refused(lambda: opening(env, amount="50", value_date=date(2026, 1, 5)), "opening_date_mismatch")
    again = opening(env, amount="50")  # same date: a second schedule line for the same employee
    approve(env, again.entry_id)
    record(env, again.entry_id)
    assert balance(engine, env).balance == Decimal(150)
    # an entry dated before the employee's opening is already inside it
    refused(lambda: advance(env, value_date=date(2025, 12, 1)), "entry_before_opening")
    # and an opening cannot be recorded after an earlier-dated advance exists for that employee
    seed_instruction(engine, env, "EMP-9")
    posted_advance(env, employee="EMP-9", ref="BANK-REF-9")
    refused(lambda: opening(env, employee="EMP-9", value_date=date(2026, 1, 20)), "opening_after_entries")


# --- adjustment ----------------------------------------------------------------------------------


def test_adjustment_posts_both_directions_against_the_offset(engine):
    env = world(engine)
    posted_advance(env)
    up = adjustment(env, amount="250")
    approve(env, up.entry_id)
    done_up = record(env, up.entry_id)
    assert lines(engine, done_up.journal_id) == {"1410": (Decimal(250), 0), "6100": (0, Decimal(250))}
    down = adjustment(env, amount="-1000")
    approve(env, down.entry_id)
    done_down = record(env, down.entry_id)
    assert lines(engine, done_down.journal_id) == {"1410": (0, Decimal(1000)), "6100": (Decimal(1000), 0)}
    assert balance(engine, env).balance == Decimal(4250)


def test_adjustment_cannot_take_a_balance_below_zero(engine):
    env = world(engine)
    posted_advance(env, amount="100")
    refused(lambda: adjustment(env, amount="-100.01"), "adjustment_exceeds_balance")
    ok = adjustment(env, amount="-100")
    approve(env, ok.entry_id)
    record(env, ok.entry_id)
    assert balance(engine, env).balance == 0


@pytest.mark.parametrize(
    ("offset", "code"),
    [
        ("1410", "offset_is_control_account"),
        ("2000", "offset_is_control_account"),
        ("2040", "offset_is_control_account"),
        ("1000", "offset_is_cash_account"),
    ],
)
def test_adjustment_offset_may_not_be_a_control_or_cash_account(engine, offset, code):
    env = world(engine)
    posted_advance(env)
    refused(lambda: adjustment(env, amount="10", offset=offset), code)
    assert counts(engine, env)["entries"] == 1


def test_adjustment_offset_must_be_open_for_posting(engine):
    env = world(engine)
    with Session(engine) as db:
        accounts_gov.retire(db, env.accounts["6100"])
        db.commit()
    refused(lambda: adjustment(env, amount="10"), "offset_not_postable")
    refused(
        lambda: act(
            env,
            "preparer",
            service.prepare_adjustment,
            request_id=uuid4(),
            employee_ref="EMP-1",
            amount=Decimal(10),
            offset_account_id=uuid4(),
            value_date=VALUE_DATE,
            evidence_ref="X",
        ),
        "offset_unknown",
    )


def test_missing_1410_account_refuses(engine):
    env = world(engine)
    with Session(engine) as db:
        accounts_gov.recode(db, env.accounts["1410"], "1410-OLD")
        db.commit()
    refused(lambda: advance(env), "staff_loan_account_unavailable")


# --- control difference (the 805,000 clean-up) ----------------------------------------------------


def test_control_difference_is_capped_at_the_measured_difference(engine):
    env = world(engine)
    seed_gl_1410(engine, env, 805000)
    opened = opening(env, amount="5000")
    approve(env, opened.entry_id)
    record(env, opened.entry_id)
    assert position(engine, env).difference == Decimal(800000)
    refused(lambda: control_difference(env, amount="800000.01"), "control_difference_exceeds")
    partial = control_difference(env, amount="300000")
    approve(env, partial.entry_id)
    done = record(env, partial.entry_id)
    assert lines(engine, done.journal_id) == {"6100": (Decimal(300000), 0), "1410": (0, Decimal(300000))}
    stored = row(engine, partial.entry_id)
    assert stored.employee_ref is None and stored.amount == Decimal(-300000)
    pos = position(engine, env)
    assert (pos.gl_balance, pos.subledger_total, pos.difference) == (Decimal(505000), Decimal(5000), Decimal(500000))
    assert balance(engine, env).balance == Decimal(5000)  # a write-off never touches an employee
    refused(lambda: control_difference(env, amount="500000.01"), "control_difference_exceeds")
    rest = control_difference(env, amount="500000")
    approve(env, rest.entry_id)
    record(env, rest.entry_id)
    assert position(engine, env).difference == 0
    refused(lambda: control_difference(env, amount="0.01"), "control_difference_exceeds")  # nothing left to write off


def test_control_difference_rechecks_at_record_and_needs_a_positive_difference(engine):
    env = world(engine)
    refused(lambda: control_difference(env, amount="1"), "control_difference_exceeds")  # GL 0: nothing to write off
    seed_gl_1410(engine, env, 1000)
    first = control_difference(env, amount="800")
    second = control_difference(env, amount="800")
    approve(env, first.entry_id)
    approve(env, second.entry_id)
    record(env, first.entry_id)
    refused(lambda: record(env, second.entry_id), "control_difference_exceeds")
    assert row(engine, second.entry_id).state == "approved"


# --- the table ------------------------------------------------------------------------------------


def _raw(engine, statement, **params):
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text(statement), params)
        finally:
            transaction.rollback()


def test_the_database_refuses_changed_amounts_and_edits_to_recorded_rows(engine):
    env = world(engine)
    created = advance(env)
    with pytest.raises(DBAPIError, match="immutable"):
        _raw(engine, "UPDATE erp_staff_loan_entries SET amount = 1 WHERE id = :id", id=created.entry_id)
    with pytest.raises(DBAPIError, match="state transition"):
        _raw(
            engine,
            "UPDATE erp_staff_loan_entries SET state = 'recorded' WHERE id = :id",
            id=created.entry_id,
        )
    approve(env, created.entry_id)
    done = record(env, created.entry_id, ref="BANK-REF-1")
    with pytest.raises(DBAPIError, match="final"):
        _raw(engine, "UPDATE erp_staff_loan_entries SET bank_reference = 'X-ONE' WHERE id = :id", id=done.entry_id)


def test_the_database_refuses_malformed_shapes(engine):
    env = world(engine)
    base = dict(
        tenant_id=env.tenant_id,
        request_id=uuid4(),
        evidence_ref="e",
        value_date=VALUE_DATE,
        state="draft",
        prepared_by_party_id=uuid4(),
        prepared_at=NOW,
    )
    bad = [
        dict(kind="opening", employee_ref="E", amount=Decimal(-1)),
        dict(kind="opening", employee_ref="E", amount=Decimal(1), offset_account_id=uuid4()),
        dict(kind="advance", employee_ref="E", amount=Decimal(1)),  # no bank account
        dict(kind="adjustment", employee_ref="E", amount=Decimal(1)),  # no offset
        dict(kind="control_difference", employee_ref="E", amount=Decimal(-1), offset_account_id=uuid4()),
        dict(kind="control_difference", employee_ref=None, amount=Decimal(1), offset_account_id=uuid4()),
        dict(kind="opening", employee_ref=None, amount=Decimal(1)),
        dict(kind="opening", employee_ref="E", amount=Decimal(0)),
        dict(kind="opening", employee_ref="E", amount=Decimal(1), state="recorded"),  # not approved/recorded by anyone
    ]
    for values in bad:
        with Session(engine) as db:
            db.add(StaffLoanEntry(**{**base, "request_id": uuid4(), **values}))
            with pytest.raises(sa.exc.IntegrityError):
                db.flush()
            db.rollback()


def test_migration_downgrade_refuses_while_entries_exist(engine):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from erp_v2.migrations.versions import e2_0023_staff_loan_entries as migration

    env = world(engine)
    seed_recorded_opening(engine, env.tenant_id, "EMP-1", "1")
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="staff loan entries exist"):
                    migration.downgrade()
        finally:
            transaction.rollback()
    with engine.connect() as c:
        assert c.scalar(sa.text("SELECT to_regclass('public.erp_staff_loan_entries')")) is not None
    # with no rows it drops (inside a rolled-back transaction, so the shared schema is untouched)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("DELETE FROM erp_staff_loan_entries"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert connection.scalar(sa.text("SELECT to_regclass('public.erp_staff_loan_entries')")) is None
        finally:
            transaction.rollback()


def test_no_other_tenants_rows_are_visible_in_a_balance(engine):
    env, other = world(engine), world(engine)
    seed_recorded_opening(engine, other.tenant_id, "EMP-1", "777")
    assert balance(engine, env).balance == 0
    assert balance(engine, other).balance == 777


# --- the balance with payroll recoveries, and the B4 check -----------------------------------------


def b4_world(engine, *, openings=None, opening_date=OPENING_DATE):
    env = b4.world(engine, paye_liability="1410", paye_mode="internal_recovery")
    for employee, amount in (openings or {}).items():
        seed_recorded_opening(engine, env.tenant_id, employee, amount, value_date=opening_date)
    return env


def b4_refused(engine, env, fn, run_id, code):
    with pytest.raises(RunRefused) as exc:
        b4.act(engine, env, fn, run_id)
    assert exc.value.code == code, exc.value.code
    return exc.value


def run_status(engine, run_id):
    from dotmac_payroll.models import PayrollLiability, PayrollRun

    with Session(engine) as db:
        return (
            db.get(PayrollRun, run_id).status,
            db.scalar(
                sa.select(sa.func.count()).select_from(PayrollLiability).where(PayrollLiability.run_id == run_id)
            ),
        )


def test_recovery_above_the_loan_balance_is_refused_at_approve_with_nothing_written(engine):
    env = b4_world(engine, openings={"E1": "10000", "E2": "4999.99"})  # E1's PAYE recovery is 10,000, E2's is 5,000
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    err = None
    try:
        b4.approve(engine, env, run_id)
    except RunRefused as exc:
        err = exc
    assert err is not None and err.code == "loan_recovery_exceeds_balance"
    assert "E2" in str(err) and "E1" not in str(err)
    assert run_status(engine, run_id) == ("draft", 0)


def test_recovery_with_no_loan_at_all_is_refused(engine):
    env = b4_world(engine)
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    with pytest.raises(RunRefused) as exc:
        b4.approve(engine, env, run_id)
    assert exc.value.code == "loan_recovery_exceeds_balance"


def test_recovery_within_the_balance_finalizes_and_lowers_the_balance(engine):
    env = b4_world(engine, openings={"E1": "12000", "E2": "5000"})
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    b4.approve(engine, env, run_id)
    b4.finalize(engine, env, run_id)
    assert run_status(engine, run_id)[0] == "finalized"
    e1, e2 = balance(engine, env, "E1"), balance(engine, env, "E2")
    assert (e1.entries, e1.recoveries, e1.balance) == (Decimal(12000), Decimal(10000), Decimal(2000))
    assert (e2.recoveries, e2.balance) == (Decimal(5000), Decimal(0))


def test_finalize_rechecks_after_a_balance_fell_between_approve_and_finalize(engine):
    env = b4_world(engine, openings={"E1": "10000", "E2": "5000"})
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    b4.approve(engine, env, run_id)
    # Finance writes E2's loan down after the run was approved
    with Session(engine) as db:
        db.add(
            StaffLoanEntry(
                tenant_id=env.tenant_id,
                request_id=uuid4(),
                employee_ref="E2",
                kind="adjustment",
                amount=Decimal(-1000),
                evidence_ref="n",
                value_date=VALUE_DATE,
                offset_account_id=env.accounts["5000"],
                journal_id=uuid4(),
                state="recorded",
                dual_control=False,
                prepared_by_party_id=uuid4(),
                prepared_at=NOW,
                approved_by_party_id=uuid4(),
                approved_at=NOW,
                recorded_by_party_id=uuid4(),
                recorded_at=NOW,
            )
        )
        db.commit()
    with pytest.raises(RunRefused) as exc:
        b4.finalize(engine, env, run_id)
    assert exc.value.code == "loan_recovery_exceeds_balance"
    assert run_status(engine, run_id) == ("approved", 0)


def test_a_run_ending_on_or_before_the_opening_date_is_refused_not_silently_ignored(engine):
    # the opening (2026-03-31) is dated after the run's period (2026-01): its recovery is already inside the
    # opening balance, so the run is refused rather than recovering the same money twice
    env = b4_world(engine, openings={"E1": "10000", "E2": "5000"}, opening_date=date(2026, 3, 31))
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    with pytest.raises(RunRefused) as exc:
        b4.approve(engine, env, run_id)
    assert exc.value.code == "recovery_before_loan_opening"
    assert "E1" in str(exc.value) and run_status(engine, run_id) == ("draft", 0)


def test_finalize_refuses_when_an_opening_dated_after_the_period_is_recorded_after_approval(engine):
    env = b4_world(engine)
    for employee, amount in (("E1", "10000"), ("E2", "5000")):
        seed_recorded_adjustment(engine, env, employee, amount)  # a loan balance, no opening yet
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    b4.approve(engine, env, run_id)  # passes: no opening, balance covers the recovery
    for employee in ("E1", "E2"):  # Finance records an opening dated after the run's period (2026-01)
        seed_recorded_opening(engine, env.tenant_id, employee, "10000", value_date=date(2026, 3, 31))
    with pytest.raises(RunRefused) as exc:
        b4.finalize(engine, env, run_id)
    assert exc.value.code == "recovery_before_loan_opening"
    assert run_status(engine, run_id) == ("approved", 0)


def test_a_run_ending_after_the_opening_date_is_deducted_as_usual(engine):
    env = b4_world(engine, openings={"E1": "10000", "E2": "5000"}, opening_date=date(2026, 1, 30))  # run ends 01-31
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    b4.approve(engine, env, run_id)
    b4.finalize(engine, env, run_id)
    assert balance(engine, env, "E1").balance == 0


def test_a_run_without_internal_recovery_never_consults_the_loans(engine):
    env = b4.world(engine)  # external PAYE: no staff-loan subledger rows at all
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id)
    b4.approve(engine, env, run_id)
    b4.finalize(engine, env, run_id)
    assert run_status(engine, run_id)[0] == "finalized"


def test_all_balances_sum_the_subledger_for_the_book_position(engine):
    env = world(engine)
    seed_gl_1410(engine, env, 10000)
    for employee, amount in (("EMP-A", "100"), ("EMP-B", "250.5")):
        seed_recorded_opening(engine, env.tenant_id, employee, amount)
    with Session(engine) as db:
        balances = queries.all_balances(db, tenant_id=env.tenant_id)
    assert {k: v.balance for k, v in balances.items()} == {"EMP-A": Decimal(100), "EMP-B": Decimal("250.5")}
    pos = position(engine, env)
    assert (pos.subledger_total, pos.difference) == (Decimal("350.5"), Decimal("9649.5"))


# --- dual-control policy (PR-4): off = one person, on = separation ----------------------------------


def test_policy_off_is_stamped_on_the_row_and_one_person_may_do_every_step(engine):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    created = advance(env)
    approve(env, created.entry_id)
    record(env, created.entry_id, ref="BANK-REF-OFF")
    assert row(engine, created.entry_id).dual_control is False


def test_policy_on_requires_separate_preparer_approver_and_recorder(engine):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, True)
    created = advance(env)
    assert row(engine, created.entry_id).dual_control is True
    refused(lambda: approve(env, created.entry_id, who="preparer"), "sod_approver_is_preparer")
    approve(env, created.entry_id, who="approver")
    refused(lambda: record(env, created.entry_id, who="preparer", ref="BANK-REF-ON"), "sod_recorder_is_preparer")
    refused(lambda: record(env, created.entry_id, who="approver", ref="BANK-REF-ON"), "sod_recorder_is_approver")
    assert row(engine, created.entry_id).state == "approved" and counts(engine, env)["journals"] == 0
    record(env, created.entry_id, who="recorder", ref="BANK-REF-ON")
    assert row(engine, created.entry_id).state == "recorded"


def test_policy_on_refuses_an_approver_who_decided_the_employee_instruction(engine):
    env = world(engine, instruction=False)
    with Session(engine) as db:
        db.add(
            PayeeBankInstruction(
                tenant_id=env.tenant_id,
                payee_kind="employee",
                payee_ref="EMP-1",
                payee_label="E",
                bank_code="058",
                bank_name="Example Bank",
                account_number="0123456789",
                account_last4="6789",
                account_name="EMPLOYEE ONE",
                status="approved",
                origin="manual",
                proposed_at=NOW,
                proposed_by_party_id=env.people["recorder"],
                decided_by_party_id=env.people["approver"],
                decided_at=NOW,
                approved_at=NOW,
            )
        )
        db.commit()
    set_dual_control_engine(engine, env.tenant_id, True)
    created = advance(env)
    refused(lambda: approve(env, created.entry_id, who="approver"), "sod_approver_is_instruction_party")


def test_the_database_backstops_the_policy_stamped_on_the_row(engine):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, True)
    created = advance(env)
    with pytest.raises(sa.exc.IntegrityError):
        with Session(engine) as db:
            db.execute(
                sa.text(
                    "UPDATE erp_staff_loan_entries SET state='approved', approved_by_party_id=prepared_by_party_id,"
                    " approved_at=now() WHERE id=:i"
                ),
                {"i": created.entry_id},
            )


# --- cancel ---------------------------------------------------------------------------------------


def cancel(env, entry_id, reason="entered in error", who="preparer"):
    return act(env, who, service.cancel_entry, entry_id=entry_id, reason=reason)


@pytest.mark.parametrize("approved", [False, True])
def test_a_draft_or_approved_entry_can_be_cancelled_with_a_reason_and_never_posts(engine, approved):
    env = world(engine)
    created = advance(env)
    if approved:
        approve(env, created.entry_id)
    done = cancel(env, created.entry_id, reason="  duplicate request  ")
    stored = row(engine, created.entry_id)
    assert stored.state == "cancelled" and stored.cancel_reason == "duplicate request"
    assert stored.cancelled_by_party_id == env.people["preparer"] and stored.cancelled_at is not None
    assert done.replayed is False and cancel(env, created.entry_id).replayed is True
    assert len(audits(engine, env, service.AUDIT_CANCELLED)) == 1
    refused(lambda: approve(env, created.entry_id), "not_draft")
    refused(lambda: record(env, created.entry_id, ref="BANK-REF-1"), "not_approved")
    assert counts(engine, env)["journals"] == 0 and balance(engine, env).balance == 0


def test_cancel_needs_a_reason_and_a_recorded_entry_cannot_be_cancelled(engine):
    env = world(engine)
    created = advance(env)
    refused(lambda: cancel(env, created.entry_id, reason="  "), "reason_required")
    assert row(engine, created.entry_id).state == "draft"
    approve(env, created.entry_id)
    record(env, created.entry_id, ref="BANK-REF-1")
    refused(lambda: cancel(env, created.entry_id), "not_cancellable")
    assert row(engine, created.entry_id).state == "recorded"
    assert any(a.details["code"] == "not_cancellable" for a in audits(engine, env, service.AUDIT_REFUSED))


def test_a_cancelled_request_id_cannot_be_replayed_and_a_cancelled_entry_does_not_count(engine):
    env = world(engine)
    seed_gl_1410(engine, env, 1000)
    request_id = uuid4()
    created = act(
        env,
        "preparer",
        service.prepare_opening,
        request_id=request_id,
        employee_ref="EMP-1",
        amount=Decimal(900),
        value_date=OPENING_DATE,
        evidence_ref="S",
    )
    cancel(env, created.entry_id)
    refused(
        lambda: act(
            env,
            "preparer",
            service.prepare_opening,
            request_id=request_id,
            employee_ref="EMP-1",
            amount=Decimal(900),
            value_date=OPENING_DATE,
            evidence_ref="S",
        ),
        "request_cancelled",
    )
    assert position(engine, env).subledger_total == 0
    again = opening(env, amount="900")  # headroom is intact: the cancelled draft reserved nothing
    approve(env, again.entry_id)
    record(env, again.entry_id)


def test_the_database_makes_cancelled_final_and_requires_a_reason(engine):
    env = world(engine)
    created = advance(env)
    with pytest.raises(DBAPIError):  # cancelled without who/when/reason
        _raw(engine, "UPDATE erp_staff_loan_entries SET state = 'cancelled' WHERE id = :id", id=created.entry_id)
    cancel(env, created.entry_id)
    with pytest.raises(DBAPIError, match="final"):
        _raw(engine, "UPDATE erp_staff_loan_entries SET state = 'draft' WHERE id = :id", id=created.entry_id)
    done = posted_advance(env, ref="BANK-REF-9")
    with pytest.raises(DBAPIError):  # recorded -> cancelled is refused by the trigger
        _raw(
            engine,
            "UPDATE erp_staff_loan_entries SET state = 'cancelled', cancelled_by_party_id = prepared_by_party_id,"
            " cancelled_at = now(), cancel_reason = 'x', recorded_by_party_id = NULL WHERE id = :id",
            id=done.entry_id,
        )


# --- repayment ------------------------------------------------------------------------------------


def repayment(env, *, amount, employee="EMP-1", who="preparer", value_date=VALUE_DATE):
    return act(
        env,
        who,
        service.prepare_repayment,
        request_id=uuid4(),
        employee_ref=employee,
        amount=Decimal(amount),
        bank_account_id=env.bank_id,
        value_date=value_date,
        evidence_ref="RECEIPT-1",
    )


def test_repayment_posts_dr_bank_cash_gl_cr_1410_and_lowers_the_balance(engine):
    env = world(engine, instruction=False)  # no payee instruction is needed to RECEIVE money
    seed_gl_1410(engine, env, 5000)
    created = opening(env, amount="3000")
    approve(env, created.entry_id)
    record(env, created.entry_id)
    rep = repayment(env, amount="1200.5")
    assert row(engine, rep.entry_id).amount == Decimal("-1200.5")
    approve(env, rep.entry_id)
    done = record(env, rep.entry_id, ref="  bank  in-1 ")
    assert lines(engine, done.journal_id) == {"1000": (Decimal("1200.5"), 0), "1410": (0, Decimal("1200.5"))}
    assert row(engine, rep.entry_id).bank_reference == "BANK IN-1"
    assert balance(engine, env).balance == Decimal("1799.5")
    assert position(engine, env).difference == Decimal("2000")  # GL and subledger fell together


def test_repayment_may_not_exceed_the_balance_and_needs_a_valid_reference(engine):
    env = world(engine)
    posted_advance(env, amount="500")
    refused(lambda: repayment(env, amount="500.01"), "repayment_exceeds_balance")
    ok = repayment(env, amount="500")
    approve(env, ok.entry_id)
    refused(lambda: record(env, ok.entry_id, ref=None), "bank_reference_invalid")
    record(env, ok.entry_id, ref="BANK-IN-2")
    assert balance(engine, env).balance == 0
    refused(lambda: repayment(env, amount="0.01"), "repayment_exceeds_balance")
    refused(lambda: repayment(env, amount="-5"), "invalid_amount")


def test_repayment_shares_the_bank_reference_space_and_the_value_date_rule(engine):
    env = world(engine)
    posted_advance(env, amount="500", ref="SHARED-REF-7")
    rep = repayment(env, amount="100", value_date=date(2026, 1, 20))
    approve(env, rep.entry_id)
    refused(lambda: record(env, rep.entry_id, ref="BANK-IN-3"), "value_date_future")
    later = datetime(2026, 1, 20, tzinfo=UTC)
    refused(lambda: record(env, rep.entry_id, ref="shared-ref-7", now=later), "bank_reference_reused")
    record(env, rep.entry_id, ref="BANK-IN-3", now=later)
    assert row(engine, rep.entry_id).state == "recorded"


def test_repayment_follows_the_dual_control_policy(engine):
    env = world(engine)
    posted_advance(env, amount="500")
    set_dual_control_engine(engine, env.tenant_id, True)
    rep = repayment(env, amount="100")
    refused(lambda: approve(env, rep.entry_id, who="preparer"), "sod_approver_is_preparer")
    approve(env, rep.entry_id, who="approver")
    refused(lambda: record(env, rep.entry_id, who="approver", ref="BANK-IN-4"), "sod_recorder_is_approver")
    record(env, rep.entry_id, who="recorder", ref="BANK-IN-4")
    assert row(engine, rep.entry_id).dual_control is True


# --- control difference with a negative employee balance ------------------------------------------


def seed_recorded_adjustment(engine, env, employee, amount):
    with Session(engine) as db:
        db.add(
            StaffLoanEntry(
                tenant_id=env.tenant_id,
                request_id=uuid4(),
                employee_ref=employee,
                kind="adjustment",
                amount=Decimal(amount),
                evidence_ref="n",
                value_date=VALUE_DATE,
                offset_account_id=env.accounts.get("6100") or env.accounts["5000"],
                journal_id=uuid4(),
                state="recorded",
                dual_control=False,
                prepared_by_party_id=uuid4(),
                prepared_at=NOW,
                approved_by_party_id=uuid4(),
                approved_at=NOW,
                recorded_by_party_id=uuid4(),
                recorded_at=NOW,
            )
        )
        db.commit()


def test_control_difference_is_refused_while_any_employee_balance_is_below_zero(engine):
    env = world(engine)
    seed_gl_1410(engine, env, 10000)
    seed_recorded_adjustment(engine, env, "EMP-NEG", "-50")  # a fault: no loan behind it
    refused(lambda: control_difference(env, amount="1"), "employee_balance_negative")
    refused(lambda: opening(env, amount="1", employee="EMP-OTHER"), "employee_balance_negative")
    seed_recorded_adjustment(engine, env, "EMP-NEG", "50")  # repaired
    assert control_difference(env, amount="1").replayed is False
    assert opening(env, amount="1", employee="EMP-OTHER").replayed is False


# --- the 805,000 route, end to end ---------------------------------------------------------------


def test_the_805000_route_openings_within_the_gl_then_a_capped_write_off_ties_subledger_to_gl(engine):
    """GL(1410) holds a net debit of 805,000 (legacy FY2025); the loan records prove far less.

    Route: per-employee openings (never above the GL), then a control_difference write-off capped at
    GL - subledger. Afterwards GL(1410) equals the subledger exactly.
    """
    env = world(engine)
    seed_gl_1410(engine, env, 805000)
    for employee, amount in (("EMP-A", "100000"), ("EMP-B", "150000.50"), ("EMP-C", "49999.50")):
        created = opening(env, amount=amount, employee=employee)
        approve(env, created.entry_id)
        record(env, created.entry_id)
    pos = position(engine, env)
    assert (pos.gl_balance, pos.subledger_total, pos.difference) == (Decimal(805000), Decimal(300000), Decimal(505000))
    # an opening can never push the subledger above the GL
    refused(lambda: opening(env, amount="505000.01", employee="EMP-D"), "opening_exceeds_gl")
    # the write-off is capped at the measured difference
    refused(lambda: control_difference(env, amount="505000.01"), "control_difference_exceeds")
    write_off = control_difference(env, amount="505000")
    approve(env, write_off.entry_id)
    done = record(env, write_off.entry_id)
    assert lines(engine, done.journal_id) == {"6100": (Decimal(505000), 0), "1410": (0, Decimal(505000))}
    pos = position(engine, env)
    assert (pos.gl_balance, pos.subledger_total, pos.difference) == (Decimal(300000), Decimal(300000), Decimal(0))
    assert {b: balance(engine, env, b).balance for b in ("EMP-A", "EMP-B", "EMP-C")} == {
        "EMP-A": Decimal(100000),
        "EMP-B": Decimal("150000.5"),
        "EMP-C": Decimal("49999.5"),
    }
    refused(lambda: control_difference(env, amount="0.01"), "control_difference_exceeds")
