# ruff: noqa: F811
"""B5 returned salary (payroll a5 settlement reversal) against real PostgreSQL (hosted CI).

Reuses the B5 world: real payroll a5, accounting a2, the real posting bridge, real cash GL re-point rows.
Net pay: E1 110000.00, E2 65000.00, E3 20000.00. Returns are by "canceller", a person who is none of the
batch's preparer, approver, file issuer or recorder.
"""

import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_b5_payroll_payout_pg as b5
from dotmac_accounting.contracts import AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, PostedLedgerLine
from dotmac_banking.models import BankAccount
from dotmac_kernel.models import Party, Tenant
from dotmac_kernel.money import Currency, Money
from dotmac_payroll import service as payroll
from dotmac_payroll.models import PayrollLiability, PayrollLiabilitySettlement, PayrollSettlementReversal
from dual_control_support import set_dual_control_engine
from sqlalchemy.orm import Session
from test_b5_payroll_payout_pg import (  # noqa: F401  (hosted-Postgres fixtures and seed helpers)
    NOW,
    VALUE_DATE,
    _registries,
    approve,
    approved_batch,
    audits,
    batch_row,
    counts,
    enable_file,
    engine,
    finalized,
    generate,
    liabilities,
    lines,
    prepare,
    record,
    refused,
    runtime_engine,
    seed_instructions,
    settlement_posting,
    world,
)

from erp_v2.cash_gl.models import CashAccountRepoint
from erp_v2.payroll_payouts import service as svc
from erp_v2.payroll_payouts.models import PayrollPayoutLine

RET_NOW = NOW + timedelta(days=1)  # 2026-01-16
RET_DATE = date(2026, 1, 14)  # on or after the settlement posting date (2026-01-12), not in the future


def recorded(engine, env, ref="PAY-REF-1"):
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    record(engine, env, batch_id, ref)
    return run_id, batch_id


def line_of(engine, env, batch_id, key="E1") -> PayrollPayoutLine:
    return next(line for line in lines(engine, batch_id) if line.beneficiary_ref == env.emp[key])


def give_back(engine, env, batch_id, line, who="canceller", **kw):
    kw.setdefault("return_reference", "RET-1")
    kw.setdefault("reason", "account closed")
    kw.setdefault("value_date", RET_DATE)
    kw.setdefault("now", RET_NOW)
    return b5.act(engine, env, who, svc.return_line, batch_id=batch_id, line_id=line.id, **kw)


def reversal_posting(engine, env) -> tuple[dict[str, Decimal], dict[str, Decimal], int]:
    with Session(engine) as db:
        journals = list(
            db.scalars(
                sa.select(JournalEntry.id).where(
                    JournalEntry.tenant_id == env.tenant_id,
                    JournalEntry.source_owner == "payroll",
                    JournalEntry.source_document_kind == "payroll_settlement_reversal",
                )
            )
        )
        debits: dict[str, Decimal] = {}
        credits: dict[str, Decimal] = {}
        for row in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id.in_(journals))):
            code = db.get(Account, row.account_id).code
            debits[code] = debits.get(code, Decimal(0)) + row.debit
            credits[code] = credits.get(code, Decimal(0)) + row.credit
        return debits, credits, len(journals)


def reversal_count(engine, env) -> int:
    with Session(engine) as db:
        return db.scalar(
            sa.select(sa.func.count())
            .select_from(PayrollSettlementReversal)
            .where(PayrollSettlementReversal.tenant_id == env.tenant_id)
        )


def liability_of(engine, env, run_id, key="E1") -> PayrollLiability:
    return liabilities(engine, env, run_id)[("employee-net-pay", env.emp[key])]


def new_cash_account(engine, env, code="1001") -> Account:
    with Session(engine) as db:
        category = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "AST")
        )
        account = Account(
            tenant_id=env.tenant_id,
            category_id=category.id,
            code=code,
            name="Own cash GL",
            kind=AccountKind.POSTING,
            normal_balance=NormalBalance.DEBIT,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account


def repoint_bank(engine, env, *, governed: bool) -> Account:
    """Point the bank account at a new cash GL; ``governed`` also writes the approved re-point request."""
    target = new_cash_account(engine, env)
    with Session(engine) as db:
        old = str(env.accounts["1000"])
        db.execute(sa.update(BankAccount).where(BankAccount.id == env.bank_id).values(cash_account_ref=str(target.id)))
        if governed:
            db.add(
                CashAccountRepoint(
                    id=uuid4(),
                    tenant_id=env.tenant_id,
                    bank_account_id=env.bank_id,
                    from_ref=old,
                    to_account_id=target.id,
                    to_account_code=target.code,
                    status="approved",
                    evidence_ref="split-1",
                    proposed_by_party_id=env.people["proposer"],
                    proposed_at=NOW,
                    decided_by_party_id=env.people["decider"],
                    decided_at=NOW,
                    applied_ref=str(target.id),
                )
            )
        db.commit()
    return target


# --- the full flow -------------------------------------------------------------------------


def test_return_then_rebatch_then_repay_with_a_new_settlement_reference(engine):
    env = world(engine)
    run_id, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    out = give_back(engine, env, batch_id, line)
    assert out.replayed is False
    # Owner: whole settlement reversed, liability reopened, settlement row untouched.
    liability = liability_of(engine, env, run_id)
    assert liability.status == "outstanding" and liability.amount_settled == 0
    with Session(engine) as db:
        settlement = db.get(PayrollLiabilitySettlement, line.settlement_id)
        assert settlement is not None and settlement.amount == Decimal("110000")
        reversal = db.get(PayrollSettlementReversal, out.reversal_id)
        assert reversal.return_ref == "RET-1" and reversal.reversed_by_id == env.people["canceller"]
    # Ledger: Dr the bank cash GL, Cr 2040, for exactly the settled amount.
    debits, credits, journals = reversal_posting(engine, env)
    assert journals == 1
    assert debits["1000"] == Decimal("110000") and credits["2040"] == Decimal("110000")
    assert credits.get("1000", 0) == 0 and debits.get("2040", 0) == 0
    # ERP receipt: line RETURNED, inactive; batch stays recorded; the other lines are untouched.
    after = next(row for row in lines(engine, batch_id) if row.id == line.id)
    assert after.active is False and after.returned_by_party_id == env.people["canceller"]
    assert after.return_reference == "RET-1" and after.return_value_date == RET_DATE
    assert after.reversal_id == out.reversal_id and after.reversal_journal_id == out.journal_id
    assert len(after.reversal_fingerprint) == 64
    assert batch_row(engine, batch_id).status == "recorded"
    assert sum(1 for row in lines(engine, batch_id) if row.active) == 2
    details = audits(engine, env, svc.AUDIT_LINE_RETURNED)
    assert len(details) == 1 and details[0].details["return_reference"] == "RET-1"
    assert details[0].details["original_cash_gl"] == str(env.accounts["1000"])
    # Re-batch the reopened liability and pay it again.
    second = prepare(engine, env, run_id, liability_ids=[liability.id]).batch_id
    approve(engine, env, second)
    record(engine, env, second, "PAY-REF-2")
    again = liability_of(engine, env, run_id)
    assert again.status == "settled" and again.amount_settled == again.amount
    new_line = lines(engine, second)[0]
    assert new_line.settlement_id != line.settlement_id and new_line.active is True
    assert counts(engine, env)["payroll_liability_settlements"] == 4
    # The old settlement reference is refused by the owner (a5): re-payment needs a new reference.
    old_ref = f"{batch_row(engine, batch_id).number}:{line.id.hex[:12]}"
    with Session(engine) as db:
        with pytest.raises(payroll.PayrollConflict, match="settlement was reversed"):
            payroll.record_liability_settlement(
                db,
                tenant_id=env.tenant_id,
                liability_id=liability.id,
                amount=Money.of(Decimal("110000.00"), Currency("NGN", 2)),
                settlement_ref=old_ref,
                evidence_ref="PAY-REF-1",
                recorded_by_id=env.people["recorder"],
                recorded_at=RET_NOW,
            )
        db.rollback()


# --- separation of duties ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "code"),
    [
        ("preparer", "sod_returner_is_preparer"),
        ("approver", "sod_returner_is_approver"),
        ("recorder", "sod_returner_is_recorder"),
    ],
)
def test_the_returner_is_none_of_the_batch_people(engine, who, code):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    before = counts(engine, env)
    refused(lambda: give_back(engine, env, batch_id, line, who=who), code)
    assert counts(engine, env) == before and reversal_count(engine, env) == 0
    assert next(row for row in lines(engine, batch_id) if row.id == line.id).active is True


def test_the_file_issuer_cannot_return_either(engine):
    env = world(engine)
    seed_instructions(engine, env)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    generate(engine, env, batch_id)
    record(engine, env, batch_id)
    line = line_of(engine, env, batch_id)
    refused(lambda: give_back(engine, env, batch_id, line, who="issuer"), "sod_returner_is_file_issuer")
    assert reversal_count(engine, env) == 0
    assert give_back(engine, env, batch_id, line).replayed is False


def test_database_refuses_a_batch_person_as_the_returner_and_makes_a_return_write_once(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    out = give_back(engine, env, batch_id, line)
    other = line_of(engine, env, batch_id, "E2")
    values = {
        "active": False,
        "returned_at": RET_NOW,
        "returned_by_party_id": env.people["preparer"],
        "return_reference": "RET-DB",
        "return_reason": "x",
        "return_value_date": RET_DATE,
        "return_bank_account_id": env.bank_id,
        "reversal_id": uuid4(),
        "reversal_journal_id": uuid4(),
        "reversal_fingerprint": "f" * 64,
    }
    with Session(engine) as db:
        with pytest.raises(sa.exc.DBAPIError):
            db.execute(sa.update(PayrollPayoutLine).where(PayrollPayoutLine.id == other.id).values(**values))
            db.flush()
        db.rollback()
        with pytest.raises(sa.exc.DBAPIError):  # a different person but the wrong bank account
            wrong = {**values, "returned_by_party_id": env.people["proposer"], "return_bank_account_id": uuid4()}
            db.execute(sa.update(PayrollPayoutLine).where(PayrollPayoutLine.id == other.id).values(**wrong))
            db.flush()
        db.rollback()
        with pytest.raises(sa.exc.DBAPIError):  # a returned line is immutable
            db.execute(
                sa.update(PayrollPayoutLine).where(PayrollPayoutLine.id == line.id).values(return_reason="changed")
            )
            db.flush()
        db.rollback()
    assert out.replayed is False


# --- refusals ------------------------------------------------------------------------------


def test_statutory_lines_have_no_return_path(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = prepare(engine, env, run_id, kind="statutory", destination_ref="dest:PAYE").batch_id
    approve(engine, env, batch_id)
    record(engine, env, batch_id, "REMIT-1")
    line = lines(engine, batch_id)[0]
    refused(lambda: give_back(engine, env, batch_id, line), "statutory_no_return")
    assert reversal_count(engine, env) == 0


def test_only_a_recorded_batch_can_have_a_line_returned(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    refused(lambda: give_back(engine, env, batch_id, lines(engine, batch_id)[0]), "not_recorded")


def test_reason_and_reference_are_required(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    refused(lambda: give_back(engine, env, batch_id, line, reason="   "), "reason_required")
    refused(lambda: give_back(engine, env, batch_id, line, return_reference="ab"), "bank_reference_invalid")
    assert reversal_count(engine, env) == 0


def test_return_date_must_be_on_or_after_the_settlement_posting_date_and_not_in_the_future(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    refused(
        lambda: give_back(engine, env, batch_id, line, value_date=VALUE_DATE - timedelta(days=1)),
        "return_date_before_settlement",
    )
    refused(
        lambda: give_back(engine, env, batch_id, line, value_date=RET_NOW.date() + timedelta(days=1)),
        "return_date_future",
    )
    assert reversal_count(engine, env) == 0 and reversal_posting(engine, env)[2] == 0
    # The settlement's own posting date is allowed.
    assert give_back(engine, env, batch_id, line, value_date=VALUE_DATE).replayed is False


def test_a_return_reference_is_used_once_per_bank_account_and_never_equals_a_payout_reference(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env, ref="PAY-REF-1")
    first, second = line_of(engine, env, batch_id, "E1"), line_of(engine, env, batch_id, "E2")
    refused(lambda: give_back(engine, env, batch_id, first, return_reference="pay-ref-1"), "bank_reference_reused")
    give_back(engine, env, batch_id, first, return_reference="RET-A")
    refused(lambda: give_back(engine, env, batch_id, second, return_reference=" ret-a "), "bank_reference_reused")
    assert reversal_count(engine, env) == 1


def test_a_journal_that_differs_from_the_recorded_consequence_is_refused(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    with Session(engine) as db:
        db.execute(
            sa.update(PayrollPayoutLine).where(PayrollPayoutLine.id == line.id).values(consequence_fingerprint="0" * 64)
        )
        db.commit()
    refused(lambda: give_back(engine, env, batch_id, line), "original_journal_mismatch")
    assert reversal_count(engine, env) == 0


# --- the original cash GL, not the current mapping -------------------------------------------


def test_the_reversal_posts_to_the_original_journal_cash_gl_even_if_the_mapping_moved(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    target = repoint_bank(engine, env, governed=False)  # mapping drifted, original GL not retired by a request
    give_back(engine, env, batch_id, line)
    debits, credits, journals = reversal_posting(engine, env)
    assert journals == 1 and debits == {"1000": Decimal("110000"), "2040": Decimal(0)}
    assert target.code not in debits and credits["2040"] == Decimal("110000")
    event = audits(engine, env, svc.AUDIT_LINE_RETURNED)[0]
    assert event.details["original_cash_gl"] == str(env.accounts["1000"])
    assert event.details["current_cash_ref_differs"] is True
    assert event.details["original_cash_gl_retired"] is False


def test_a_retired_original_cash_gl_is_still_debited_and_the_liability_can_be_rebatched(engine):
    env = world(engine)
    run_id, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    target = repoint_bank(engine, env, governed=True)  # the #53 re-point retired the shared GL the salary left from
    give_back(engine, env, batch_id, line)
    debits, credits, journals = reversal_posting(engine, env)
    assert journals == 1 and debits["1000"] == Decimal("110000") and target.code not in debits
    assert credits["2040"] == Decimal("110000")
    event = audits(engine, env, svc.AUDIT_LINE_RETURNED)[0]
    assert event.details["original_cash_gl_retired"] is True and event.details["current_cash_ref_differs"] is True
    liability = liability_of(engine, env, run_id)
    assert liability.status == "outstanding" and liability.amount_settled == 0
    second = prepare(engine, env, run_id, liability_ids=[liability.id]).batch_id
    assert batch_row(engine, second).status == "draft"


# --- replay and concurrency -------------------------------------------------------------------


def test_replay_is_idempotent_and_audited_once(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    first = give_back(engine, env, batch_id, line)
    after = counts(engine, env)
    again = give_back(engine, env, batch_id, line, return_reference=" ret-1 ")  # normalises to the same reference
    assert again.replayed is True and again.reversal_id == first.reversal_id and again.journal_id == first.journal_id
    assert counts(engine, env) == after and reversal_count(engine, env) == 1 and reversal_posting(engine, env)[2] == 1
    assert len(audits(engine, env, svc.AUDIT_LINE_RETURNED)) == 1
    refused(lambda: give_back(engine, env, batch_id, line, return_reference="RET-2"), "already_returned")
    refused(lambda: give_back(engine, env, batch_id, line, who="proposer"), "already_returned")
    refused(lambda: give_back(engine, env, batch_id, line, reason="other"), "already_returned")
    assert counts(engine, env) == after


def _race(engine, env, batch_id, line, *, first, second):
    holder = Session(engine)
    winner = svc.return_line(
        holder,
        tenant=holder.get(Tenant, env.tenant_id),
        party=holder.get(Party, env.people[first["who"]]),
        batch_id=batch_id,
        line_id=line.id,
        return_reference=first["ref"],
        reason="account closed",
        value_date=RET_DATE,
        now=RET_NOW,
    )
    outcome: dict[str, object] = {}

    def other():
        try:
            outcome["result"] = give_back(
                engine, env, batch_id, line, who=second["who"], return_reference=second["ref"]
            )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=other)
    thread.start()
    time.sleep(1.0)  # blocked on the batch row lock
    assert thread.is_alive()
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive()
    return winner, outcome


def test_two_concurrent_returns_of_the_same_line_yield_one_reversal_and_one_journal(engine):
    env = world(engine)
    run_id, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    winner, outcome = _race(
        engine,
        env,
        batch_id,
        line,
        first={"who": "canceller", "ref": "RET-A"},
        second={"who": "proposer", "ref": "RET-B"},
    )
    assert winner.replayed is False and "result" not in outcome, outcome
    assert isinstance(outcome["error"], svc.PayoutRefused) and outcome["error"].code == "already_returned"
    assert reversal_count(engine, env) == 1 and reversal_posting(engine, env)[2] == 1
    assert liability_of(engine, env, run_id).status == "outstanding"
    assert len(audits(engine, env, svc.AUDIT_LINE_RETURNED)) == 1


def test_a_concurrent_identical_return_replays_without_a_second_posting(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    winner, outcome = _race(
        engine,
        env,
        batch_id,
        line,
        first={"who": "canceller", "ref": "RET-A"},
        second={"who": "canceller", "ref": "RET-A"},
    )
    assert winner.replayed is False and "error" not in outcome, outcome
    assert outcome["result"].replayed is True and outcome["result"].reversal_id == winner.reversal_id
    assert reversal_count(engine, env) == 1 and reversal_posting(engine, env)[2] == 1


def test_runtime_role_may_write_the_return_columns_once_and_nothing_else(engine, runtime_engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)

    def scoped():
        db = Session(runtime_engine)
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(env.tenant_id)})
        return db

    mark = sa.text(
        "UPDATE erp_payroll_payout_lines SET active = false, returned_at = now(), returned_by_party_id = :who, "
        "return_reference = 'RT-1', return_reason = 'closed', return_value_date = :d, return_bank_account_id = :b, "
        "reversal_id = gen_random_uuid(), reversal_journal_id = gen_random_uuid(), "
        "reversal_fingerprint = repeat('a', 64) WHERE id = :id"
    )
    args = {"who": env.people["canceller"], "d": RET_DATE, "b": env.bank_id, "id": line.id}
    with scoped() as db:
        db.execute(mark, args)
        db.commit()
    with scoped() as db:
        with pytest.raises(sa.exc.DBAPIError):  # write-once
            db.execute(sa.text("UPDATE erp_payroll_payout_lines SET return_reason = 'x' WHERE id = :id"), args)
    with scoped() as db:
        with pytest.raises(sa.exc.ProgrammingError) as err:  # amounts stay immutable
            db.execute(sa.text("UPDATE erp_payroll_payout_lines SET amount = amount + 1"))
        assert "permission denied" in str(err.value.orig).lower()


# --- the returner may not release the re-payment ----------------------------------------------


def _returned_and_rebatched(engine, env):
    run_id, batch_id = recorded(engine, env)
    give_back(engine, env, batch_id, line_of(engine, env, batch_id))
    liability = liability_of(engine, env, run_id)
    second = prepare(engine, env, run_id, liability_ids=[liability.id]).batch_id
    return run_id, second


def test_the_returner_may_not_approve_or_record_the_repayment(engine):
    env = world(engine)
    _run, second = _returned_and_rebatched(engine, env)
    refused(lambda: approve(engine, env, second, who="canceller"), "sod_approver_is_returner")
    assert batch_row(engine, second).status == "draft"
    approve(engine, env, second)
    refused(lambda: record(engine, env, second, "PAY-REF-2", who="canceller"), "sod_recorder_is_returner")
    assert batch_row(engine, second).status == "approved"
    assert record(engine, env, second, "PAY-REF-2").replayed is False


def test_the_returner_may_not_issue_the_file_of_the_repayment(engine):
    env = world(engine)
    seed_instructions(engine, env)
    _run, second = _returned_and_rebatched(engine, env)  # files are switched on only after the first payout
    enable_file(engine, env)
    approve(engine, env, second)
    refused(lambda: generate(engine, env, second, who="canceller"), "sod_file_issuer_is_returner")
    assert batch_row(engine, second).status == "approved"


def test_the_batch_detail_shows_the_repayment_of_a_return(engine):
    from erp_v2.payroll_payouts import queries

    env = world(engine)
    _run, second = _returned_and_rebatched(engine, env)
    with Session(engine) as db:
        data = queries.detail(db, tenant_id=env.tenant_id, batch_id=second)
    assert list(data["repayments"].values()) == ["RET-1"]


# --- dual-control policy (D-9): default OFF ----------------------------------------------------------------------


@pytest.mark.parametrize("who", ["preparer", "approver", "recorder"])
def test_policy_off_a_batch_person_may_record_the_return(engine, who):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    _run, batch_id = recorded(engine, env)
    line = line_of(engine, env, batch_id)
    assert line.dual_control is False
    try:
        out = give_back(engine, env, batch_id, line, who=who)
    except svc.PayoutRefused as exc:  # payroll a5's own reversal rules may still apply to the recorder
        assert not exc.code.startswith("sod_"), exc.code
    else:
        assert out.replayed is False


def test_policy_off_the_returner_guard_trigger_is_gated_on_the_line_stamp(engine):
    env = world(engine)
    _run, batch_id = recorded(engine, env)
    one, two = line_of(engine, env, batch_id, "E1"), line_of(engine, env, batch_id, "E2")
    values = {
        "active": False,
        "returned_at": RET_NOW,
        "returned_by_party_id": env.people["preparer"],
        "return_reference": "RET-DB",
        "return_reason": "x",
        "return_value_date": RET_DATE,
        "return_bank_account_id": env.bank_id,
        "reversal_id": uuid4(),
        "reversal_journal_id": uuid4(),
        "reversal_fingerprint": "f" * 64,
    }
    with Session(engine) as db:
        with pytest.raises(sa.exc.DBAPIError) as err:  # stamp true (the default of existing rows): refused
            db.execute(sa.update(PayrollPayoutLine).where(PayrollPayoutLine.id == one.id).values(**values))
            db.flush()
        assert "different person" in str(err.value)
        db.rollback()
        db.execute(sa.update(PayrollPayoutLine).where(PayrollPayoutLine.id == two.id).values(dual_control=False))
        db.execute(sa.update(PayrollPayoutLine).where(PayrollPayoutLine.id == two.id).values(**values))
        db.flush()  # stamp false: the same person is accepted by the trigger
        db.rollback()
