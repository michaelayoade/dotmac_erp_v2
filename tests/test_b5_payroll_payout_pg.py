"""B5 payroll payouts against real PostgreSQL (hosted CI; ERP_TEST_POSTGRES_URL).

Real payroll a4, accounting a2 and kernel migrations; real owner commands, the real posting bridge and
real payee instruction rows. Runs are calculated/approved/finalized through the B4 services (reusing
its seed). Account codes, mappings and amounts are test inputs only.

Net pay (E1 110000.00, E2 65000.00, E3 20000.00 = 195000.00); statutory component liabilities:
PAYE dest:PAYE (E1 10000.00, E2 5000.00) and PENSION_ER dest:PENSION_ER (E1 5000.00, E2 2500.00).
"""

import os
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import test_b4_payroll_finalize_pg as b4
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, PostedLedgerLine
from dotmac_banking.models import BankAccount, BankInstitution
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, PartyPerson, PartyType, Tenant
from dotmac_kernel.money import Currency, Money
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dotmac_kernel.settings_resolver import SettingChangeContext, upsert_by_key
from dotmac_payables.models import SupplierPayment, SupplierPaymentStatus
from dotmac_payroll import service as payroll
from dotmac_payroll.models import PayrollLiability, PayrollLiabilitySettlement
from dotmac_people.models import Employee
from dual_control_support import set_dual_control_engine
from sqlalchemy.orm import Session
from test_posting_bridge_pg import NOW, soft_close

from erp_v2.assembly import assembly
from erp_v2.disbursement import bank_reference as bank_ref
from erp_v2.disbursement import policy as disbursement_policy
from erp_v2.disbursement.models import DisbursementFileReceipt
from erp_v2.journals.routes import journals_web_feature
from erp_v2.payees.models import PayeeBankInstruction
from erp_v2.payroll_payouts import service as svc
from erp_v2.payroll_payouts.models import PayrollPayoutBatch, PayrollPayoutLine
from erp_v2.payroll_payouts.service import PayoutRefused
from erp_v2.supplier_payments.routes import supplier_payments_feature

engine = bridge_pg.engine

VALUE_DATE = date(2026, 1, 12)  # on or before NOW (2026-01-15) and inside the open period
ROLES = ("preparer", "approver", "issuer", "recorder", "proposer", "decider", "canceller")


@pytest.fixture(scope="module", autouse=True)
def _registries():
    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))
    manifests = [journals_web_feature, supplier_payments_feature, FeatureManifest(name="kernel_stub")]
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


GOOD_FILE = (
    "employee_ref,component_code,amount,evidence_ref\n{E1},BASIC,100000.00,hr:contract-E1\n"
    "{E2},BASIC,50000.00,hr:contract-E2\n"
)


def world(engine, **kw):
    env = b4.world(engine, **kw)
    with Session(engine) as db:
        # Canonical employee UUIDs (as the B8 importer's _canon_ref spells them), with real people rows,
        # so payee resolution works; the b4 seed assigned the placeholders E1..E3.
        env.emp = {}
        for key in ("E1", "E2", "E3"):
            party = uuid4()
            db.add(Party(id=party, tenant_id=env.tenant_id, party_type=PartyType.person, display_name=key))
            db.flush()
            db.add(PartyPerson(party_id=party, first_name="Ada", last_name=key))
            db.flush()
            employee = Employee(
                tenant_id=env.tenant_id, party_id=party, employee_code=key, date_of_joining=date(2025, 1, 1)
            )
            db.add(employee)
            db.flush()
            env.emp[key] = str(employee.id)
            db.execute(
                sa.text(
                    "UPDATE mod_payroll.employee_pay_assignments SET employee_ref = :new "
                    "WHERE tenant_id = :t AND employee_ref = :old"
                ),
                {"new": env.emp[key], "old": key, "t": env.tenant_id},
            )
        category = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "AST")
        )
        cash = Account(
            tenant_id=env.tenant_id,
            category_id=category.id,
            code="1000",
            name="Operating cash",
            kind=AccountKind.POSTING,
            normal_balance=NormalBalance.DEBIT,
        )
        db.add(cash)
        db.flush()
        env.accounts["1000"] = cash.id
        env.people = {}
        for name in ROLES:
            party = Party(
                id=uuid4(),
                tenant_id=env.tenant_id,
                party_type=PartyType.person,
                display_name=name,
                email=f"{name}-{env.tenant_id.hex[:6]}@acme.test",
            )
            db.add(party)
            db.flush()
            env.people[name] = party.id
        institution = BankInstitution(
            id=uuid4(), tenant_id=env.tenant_id, code="BK", name="Bank", country_code="NG", status="active"
        )
        db.add(institution)
        db.flush()
        bank = BankAccount(
            id=uuid4(),
            tenant_id=env.tenant_id,
            institution_id=institution.id,
            account_code="OP1",
            account_name="Operating",
            account_identifier="payer-acct-1",
            account_type_code="current",
            currency_code="NGN",
            minor_units=2,
            cash_account_ref=str(cash.id),
            status="active",
        )
        db.add(bank)
        env.bank_id = bank.id
        db.commit()
    return env


def finalized(engine, env) -> UUID:
    run_id = b4.new_run(engine, env)
    b4.batch(engine, env, run_id, data=GOOD_FILE.format(**env.emp).encode())
    b4.approve(engine, env, run_id)
    b4.finalize(engine, env, run_id)
    return run_id


def seed_instructions(
    engine, env, *, proposer="proposer", decider="decider", origin="manual", name="EMP", refs=None, dual_control=True
):
    with Session(engine) as db:
        for i, key in enumerate(refs or ("E1", "E2", "E3")):
            ref = env.emp[key]
            manual = origin == "manual"
            db.add(
                PayeeBankInstruction(
                    tenant_id=env.tenant_id,
                    payee_kind="employee",
                    payee_ref=ref,
                    payee_label=f"Employee {ref}",
                    bank_code="058",
                    bank_name="Example Bank",
                    account_number=f"01234567{i}9",
                    account_last4=f"{i}9"[-2:].rjust(4, "0"),
                    account_name=f"{name} {ref}",
                    status="approved",
                    origin=origin,
                    dual_control=dual_control,
                    proposed_by_party_id=env.people[proposer] if manual else None,
                    proposed_at=NOW,
                    decided_by_party_id=env.people[decider],
                    decided_at=NOW,
                    approved_at=NOW,
                )
            )
        db.commit()


def act(engine, env, who, fn, **kw):
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.people[who])
        kw.setdefault("now", NOW)
        try:
            return fn(db, tenant=tenant, party=party, **kw)
        finally:
            db.commit()


def prepare(engine, env, run_id, *, kind="net_pay", who="preparer", **kw):
    kw.setdefault("request_id", uuid4())
    kw.setdefault("bank_account_id", env.bank_id)
    kw.setdefault("value_date", VALUE_DATE)
    return act(engine, env, who, svc.prepare_batch, run_id=run_id, kind=kind, **kw)


def approve(engine, env, batch_id, who="approver"):
    return act(engine, env, who, svc.approve_batch, batch_id=batch_id)


def record(engine, env, batch_id, ref="BANK-REF-1", who="recorder"):
    return act(engine, env, who, svc.record_batch, batch_id=batch_id, bank_reference=ref)


def generate(engine, env, batch_id, who="issuer"):
    return act(engine, env, who, svc.generate_bank_file, batch_id=batch_id)


def cancel(engine, env, batch_id, who="canceller", reason="wrong", recall=None):
    return act(engine, env, who, svc.cancel_batch, batch_id=batch_id, reason=reason, recall_reference=recall)


def approved_batch(engine, env, run_id, **kw) -> UUID:
    batch_id = prepare(engine, env, run_id, **kw).batch_id
    approve(engine, env, batch_id)
    return batch_id


def enable_file(engine, env, value=True):
    with Session(engine) as db:
        upsert_by_key(
            db,
            disbursement_policy.TREASURY_POLICY_DOMAIN,
            disbursement_policy.BANK_FILE_KEY,
            value,
            tenant_id=env.tenant_id,
            changed_by=SettingChangeContext(actor_party_id=None, reason="test"),
        )
        db.commit()


def refused(call, code):
    with pytest.raises(PayoutRefused) as exc:
        call()
    assert exc.value.code == code, exc.value.code
    return exc.value


def batch_row(engine, batch_id) -> PayrollPayoutBatch:
    with Session(engine) as db:
        return db.get(PayrollPayoutBatch, batch_id)


def lines(engine, batch_id) -> list[PayrollPayoutLine]:
    with Session(engine) as db:
        return list(db.scalars(sa.select(PayrollPayoutLine).where(PayrollPayoutLine.batch_id == batch_id)))


def liabilities(engine, env, run_id) -> dict[tuple[str, str], PayrollLiability]:
    with Session(engine) as db:
        return {
            (row.liability_key.split(":")[0], row.beneficiary_ref): row
            for row in db.scalars(
                sa.select(PayrollLiability).where(
                    PayrollLiability.tenant_id == env.tenant_id, PayrollLiability.run_id == run_id
                )
            )
        }


def counts(engine, env) -> dict[str, int]:
    with Session(engine) as db:
        out = {
            m.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == env.tenant_id))
            for m in (PayrollLiabilitySettlement, JournalEntry, PostedLedgerLine, DisbursementFileReceipt)
        }
        out["batches"] = db.scalar(
            sa.select(sa.func.count())
            .select_from(PayrollPayoutBatch)
            .where(PayrollPayoutBatch.tenant_id == env.tenant_id)
        )
        return out


def audits(engine, env, action):
    with Session(engine) as db:
        return list(
            db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == action))
        )


def settlement_posting(engine, env) -> tuple[dict[str, Decimal], dict[str, Decimal], int]:
    """(debits by account code, credits by account code, journal count) of payroll settlement journals."""
    with Session(engine) as db:
        journals = list(
            db.scalars(
                sa.select(JournalEntry.id).where(
                    JournalEntry.tenant_id == env.tenant_id,
                    JournalEntry.source_owner == "payroll",
                    JournalEntry.source_document_kind == "payroll_liability_settlement",
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


# --- prepare: only employee net pay -----------------------------------------


def test_net_pay_batch_holds_exactly_the_employee_net_pay_liabilities(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    out = prepare(engine, env, run_id)
    batch = batch_row(engine, out.batch_id)
    assert batch.status == "draft" and batch.kind == "net_pay" and batch.line_count == 3
    assert batch.total_amount == Decimal("195000") and batch.currency_code == "NGN"
    assert batch.prepared_by_party_id == env.people["preparer"] and batch.bank_account_id == env.bank_id
    by_id = {v.id: k for k, v in liabilities(engine, env, run_id).items()}
    assert sorted(by_id[line.liability_id][0] for line in lines(engine, out.batch_id)) == ["employee-net-pay"] * 3
    assert {line.beneficiary_ref for line in lines(engine, out.batch_id)} == set(env.emp.values())
    assert len(audits(engine, env, svc.AUDIT_PREPARED)) == 1


def test_a_deduction_or_employer_liability_can_never_be_selected_into_net_pay(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    paye = liabilities(engine, env, run_id)[("component", "dest:PAYE")]
    refused(lambda: prepare(engine, env, run_id, liability_ids=[paye.id]), "liability_not_eligible")
    assert counts(engine, env)["batches"] == 0
    # the direct eligibility check refuses the same thing, independent of selection
    with Session(engine) as db:
        run = db.get(__import__("dotmac_payroll.models", fromlist=["PayrollRun"]).PayrollRun, run_id)
        with pytest.raises(PayoutRefused) as exc:
            svc.check_liability(
                db, kind="net_pay", run=run, liability=db.get(PayrollLiability, paye.id), destination_ref=None
            )
        assert exc.value.code == "liability_not_net_pay"


def test_prepare_replays_on_the_same_request_id(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    request_id = uuid4()
    first = prepare(engine, env, run_id, request_id=request_id)
    again = prepare(engine, env, run_id, request_id=request_id)
    assert again.batch_id == first.batch_id and again.replayed is True
    assert counts(engine, env)["batches"] == 1 and len(audits(engine, env, svc.AUDIT_PREPARED)) == 1
    refused(
        lambda: prepare(engine, env, run_id, request_id=request_id, kind="statutory", destination_ref="dest:PAYE"),
        "request_reused",
    )


def test_a_liability_sits_in_one_live_batch_until_it_is_cancelled(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    first = prepare(engine, env, run_id)
    refused(lambda: prepare(engine, env, run_id), "nothing_to_pay")
    ids = [line.liability_id for line in lines(engine, first.batch_id)]
    refused(lambda: prepare(engine, env, run_id, liability_ids=ids[:1]), "liability_in_batch")
    cancel(engine, env, first.batch_id)
    assert prepare(engine, env, run_id).replayed is False
    assert all(not line.active for line in lines(engine, first.batch_id))


def test_unfinalized_run_and_foreign_bank_account_are_refused(engine):
    env = world(engine)
    draft = b4.new_run(engine, env, ref="RUN-DRAFT", start=date(2026, 3, 1), end=date(2026, 3, 31))
    refused(lambda: prepare(engine, env, draft), "run_not_finalized")
    run_id = finalized(engine, env)
    other = world(engine)
    refused(lambda: prepare(engine, env, run_id, bank_account_id=other.bank_id), "bank_account_unknown")
    with Session(engine) as db:
        db.execute(sa.update(BankAccount).where(BankAccount.id == env.bank_id).values(status="suspended"))
        db.commit()
    refused(lambda: prepare(engine, env, run_id), "bank_account_inactive")
    assert counts(engine, env)["batches"] == 0


def test_a_subset_selection_is_allowed_and_totals_follow_it(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    e3 = liabilities(engine, env, run_id)[("employee-net-pay", env.emp["E3"])]
    out = prepare(engine, env, run_id, liability_ids=[e3.id])
    batch = batch_row(engine, out.batch_id)
    assert batch.line_count == 1 and batch.total_amount == Decimal("20000")


# --- segregation of duties --------------------------------------------------------


def test_preparer_cannot_approve_and_approval_replays_only_for_the_approver(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = prepare(engine, env, run_id).batch_id
    refused(lambda: approve(engine, env, batch_id, who="preparer"), "sod_approver_is_preparer")
    assert approve(engine, env, batch_id).replayed is False
    assert approve(engine, env, batch_id).replayed is True
    refused(lambda: approve(engine, env, batch_id, who="canceller"), "not_draft")
    assert len(audits(engine, env, svc.AUDIT_APPROVED)) == 1


def test_four_distinct_people_preparer_approver_file_issuer_recorder(engine):
    env = world(engine)
    seed_instructions(engine, env)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    refused(lambda: generate(engine, env, batch_id, who="preparer"), "sod_file_issuer_is_batch_party")
    refused(lambda: generate(engine, env, batch_id, who="approver"), "sod_file_issuer_is_batch_party")
    generate(engine, env, batch_id)
    refused(lambda: record(engine, env, batch_id, who="preparer"), "sod_recorder_is_preparer")
    refused(lambda: record(engine, env, batch_id, who="approver"), "sod_recorder_is_approver")
    refused(lambda: record(engine, env, batch_id, who="issuer"), "sod_recorder_is_file_issuer")
    assert counts(engine, env)["payroll_liability_settlements"] == 0
    assert record(engine, env, batch_id).replayed is False


def test_database_also_refuses_a_non_distinct_people_update(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = prepare(engine, env, run_id).batch_id
    with Session(engine) as db:
        with pytest.raises(sa.exc.IntegrityError):
            db.execute(
                sa.update(PayrollPayoutBatch)
                .where(PayrollPayoutBatch.id == batch_id)
                .values(approved_by_party_id=env.people["preparer"], status="approved")
            )
            db.flush()
        db.rollback()


@pytest.mark.parametrize("who_is_decider", ["approver", "proposer"])
def test_approver_may_not_be_the_current_instruction_proposer_or_decider(engine, who_is_decider):
    env = world(engine)
    if who_is_decider == "approver":
        seed_instructions(engine, env, decider="approver")
    else:
        seed_instructions(engine, env, proposer="approver")
    run_id = finalized(engine, env)
    batch_id = prepare(engine, env, run_id).batch_id
    refused(lambda: approve(engine, env, batch_id), "sod_approver_is_instruction_party")
    assert batch_row(engine, batch_id).status == "draft"


def test_file_issuer_may_not_be_an_instruction_proposer_or_decider(engine):
    env = world(engine)
    seed_instructions(engine, env, proposer="issuer")
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    refused(lambda: generate(engine, env, batch_id), "sod_file_issuer_is_instruction_party")
    assert (
        counts(engine, env)["erp_disbursement_file_receipts"] == 0 and batch_row(engine, batch_id).status == "approved"
    )


def test_approver_independence_is_rechecked_at_file_and_record(engine):
    env = world(engine)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)  # approved while no instruction existed
    seed_instructions(engine, env, decider="approver")  # then the approver approves the destinations
    refused(lambda: generate(engine, env, batch_id), "sod_approver_is_instruction_party")
    refused(lambda: record(engine, env, batch_id), "bank_file_required")  # files are on: record needs the file
    enable_file(engine, env, False)
    refused(lambda: record(engine, env, batch_id), "sod_approver_is_instruction_party")
    assert counts(engine, env)["payroll_liability_settlements"] == 0


def test_legacy_migrated_instructions_do_not_restrict_anyone(engine):
    env = world(engine)
    seed_instructions(engine, env, origin="legacy_import", decider="approver")
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    generate(engine, env, batch_id)
    assert record(engine, env, batch_id).replayed is False


# --- recording: posting, idempotency, partial refusal ---------------------------------


def test_record_settles_every_line_and_posts_2040_debit_equal_to_paid_and_bank_credit(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    out = record(engine, env, batch_id, "  bank ref  1 ")
    assert out.replayed is False and out.journals == 3
    debits, credits, journals = settlement_posting(engine, env)
    assert journals == 3
    assert debits["2040"] == Decimal("195000") and credits["1000"] == Decimal("195000")
    assert credits["2040"] == 0 and debits["1000"] == 0
    batch = batch_row(engine, batch_id)
    assert batch.status == "recorded" and batch.bank_reference == "BANK REF 1"  # normalised and stored
    assert batch.recorded_by_party_id == env.people["recorder"]
    rows = liabilities(engine, env, run_id)
    for emp in env.emp.values():
        liability = rows[("employee-net-pay", emp)]
        assert liability.status == "settled" and liability.amount_settled == liability.amount
    for key, liability in rows.items():  # components untouched
        if key[0] == "component":
            assert liability.status == "outstanding" and liability.amount_settled == 0
    assert all(
        line.journal_id and line.settlement_id and len(line.consequence_fingerprint) == 64
        for line in lines(engine, batch_id)
    )
    assert len(audits(engine, env, svc.AUDIT_RECORDED)) == 1


def test_double_record_replays_and_never_double_posts(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    record(engine, env, batch_id, "REF-A")
    after = counts(engine, env)
    again = record(engine, env, batch_id, "ref-a")  # normalises to the same reference
    assert again.replayed is True and counts(engine, env) == after
    assert len(audits(engine, env, svc.AUDIT_RECORDED)) == 1
    refused(lambda: record(engine, env, batch_id, "REF-B"), "already_recorded")
    assert counts(engine, env) == after


def test_concurrent_record_yields_one_set_of_settlements_and_journals(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    holder = Session(engine)
    winner = svc.record_batch(
        holder,
        tenant=holder.get(Tenant, env.tenant_id),
        party=holder.get(Party, env.people["recorder"]),
        batch_id=batch_id,
        bank_reference="REF-C",
        now=NOW,
    )
    outcome: dict[str, object] = {}

    def second():
        try:
            outcome["result"] = record(engine, env, batch_id, "REF-C")
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(1.0)  # blocked on the batch row lock
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and "error" not in outcome, outcome
    assert winner.replayed is False and outcome["result"].replayed is True
    assert counts(engine, env)["payroll_liability_settlements"] == 3
    assert settlement_posting(engine, env)[2] == 3
    assert len(audits(engine, env, svc.AUDIT_RECORDED)) == 1


def test_partial_batch_is_refused_whole_when_one_liability_was_settled_elsewhere(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    e3 = liabilities(engine, env, run_id)[("employee-net-pay", env.emp["E3"])]
    with Session(engine) as db:  # an out-of-band settlement of one employee after approval
        payroll.record_liability_settlement(
            db,
            tenant_id=env.tenant_id,
            liability_id=e3.id,
            amount=Money.of(Decimal("20000.00"), Currency("NGN", 2)),
            settlement_ref="MANUAL-1",
            evidence_ref="manual",
            recorded_by_id=env.people["canceller"],
            recorded_at=NOW,
        )
        db.commit()
    before = counts(engine, env)
    refused(lambda: record(engine, env, batch_id), "liability_not_outstanding")
    assert counts(engine, env) == before and batch_row(engine, batch_id).status == "approved"
    assert liabilities(engine, env, run_id)[("employee-net-pay", env.emp["E1"])].status == "outstanding"
    assert any(a.details["code"] == "liability_not_outstanding" for a in audits(engine, env, svc.AUDIT_REFUSED))


def test_closed_period_refuses_and_rolls_back_every_settlement_and_journal(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    soft_close(engine, env)
    before = counts(engine, env)
    refused(lambda: record(engine, env, batch_id), "posting_period_closed")
    assert counts(engine, env) == before
    assert batch_row(engine, batch_id).status == "approved"
    assert liabilities(engine, env, run_id)[("employee-net-pay", env.emp["E1"])].amount_settled == 0


def test_future_value_date_is_refused_and_the_day_itself_is_accepted(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id, value_date=date(2026, 1, 20))
    refused(lambda: record(engine, env, batch_id), "value_date_future")
    assert (
        act(
            engine,
            env,
            "recorder",
            svc.record_batch,
            batch_id=batch_id,
            bank_reference="REF-D",
            now=datetime(2026, 1, 20, 9, tzinfo=UTC),
        ).replayed
        is False
    )


def test_bank_reference_is_required_printable_normalised_and_unique_per_bank_account(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    e1 = liabilities(engine, env, run_id)[("employee-net-pay", env.emp["E1"])]
    first = approved_batch(engine, env, run_id, liability_ids=[e1.id])
    refused(lambda: record(engine, env, first, "x"), "bank_reference_invalid")
    refused(lambda: record(engine, env, first, "AB\ncd"), "bank_reference_invalid")
    record(engine, env, first, "Ref  One")
    second = approved_batch(engine, env, run_id)  # the remaining employees
    refused(lambda: record(engine, env, second, "REF ONE"), "bank_reference_reused")
    assert record(engine, env, second, "REF TWO").replayed is False


def supplier_payment_row(engine, env, ref, *, bank_id=None):
    """A recorded B3 supplier payment holding `ref` on a bank account (the row B3 itself leaves behind)."""
    with Session(engine) as db:
        row = SupplierPayment(
            tenant_id=env.tenant_id,
            number=f"SP-{uuid4().hex[:8]}",
            supplier_ref="supplier-x",
            supplier_name_snapshot="Supplier X",
            currency_code="NGN",
            exchange_rate=Decimal("1"),
            value_date=VALUE_DATE,
            cash_account_ref=str(env.accounts["1000"]),
            payer_instrument_ref=str(bank_id or env.bank_id),
            gross_amount=Decimal("100"),
            wht_amount=Decimal("0"),
            net_amount=Decimal("100"),
            request_fingerprint="0" * 64,
            status=SupplierPaymentStatus.DRAFT,
            prepared_by="p1",
            prepared_at=NOW,
        )
        db.add(row)
        db.commit()  # the owner's trigger only allows creation as a draft; then walk it forward
        row.status = SupplierPaymentStatus.APPROVED
        row.approval_reference = "appr-1"
        row.approved_by = "p2"
        row.approved_at = NOW
        db.commit()
        row.status = SupplierPaymentStatus.RECORDED
        row.bank_reference = ref
        row.recorded_by = "p3"
        row.recorded_at = NOW
        db.commit()


def test_b5_record_after_b3_with_the_same_reference_on_the_same_account_is_refused(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    supplier_payment_row(engine, env, "SUPPLIER-1")
    before = counts(engine, env)
    refused(lambda: record(engine, env, batch_id, "supplier-1"), "bank_reference_reused")
    assert counts(engine, env) == before
    assert batch_row(engine, batch_id).status == "approved"


def test_a_supplier_reference_on_a_different_bank_account_does_not_block_b5(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    with Session(engine) as db:
        other = uuid4()
        db.add(
            BankAccount(
                id=other,
                tenant_id=env.tenant_id,
                institution_id=db.scalar(
                    sa.select(BankInstitution.id).where(BankInstitution.tenant_id == env.tenant_id)
                ),
                account_code="OP2",
                account_name="Other",
                account_identifier="payer-acct-2",
                account_type_code="current",
                currency_code="NGN",
                minor_units=2,
                cash_account_ref=str(env.accounts["1000"]),
                status="active",
            )
        )
        db.commit()
    supplier_payment_row(engine, env, "SUPPLIER-2", bank_id=other)
    assert record(engine, env, batch_id, "SUPPLIER-2").replayed is False


def test_concurrent_b5_records_of_one_reference_second_one_refuses(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    e1 = liabilities(engine, env, run_id)[("employee-net-pay", env.emp["E1"])]
    first = approved_batch(engine, env, run_id, liability_ids=[e1.id])
    second = approved_batch(engine, env, run_id)
    holder = Session(engine)
    svc.record_batch(
        holder,
        tenant=holder.get(Tenant, env.tenant_id),
        party=holder.get(Party, env.people["recorder"]),
        batch_id=first,
        bank_reference="RACE-1",
        now=NOW,
    )
    outcome: dict[str, object] = {}

    def other():
        try:
            outcome["result"] = record(engine, env, second, "RACE-1")
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=other)
    thread.start()
    time.sleep(1.0)
    assert thread.is_alive()  # waiting on the advisory lock, not on any row lock of its own
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and "result" not in outcome, outcome
    assert isinstance(outcome["error"], PayoutRefused) and outcome["error"].code == "bank_reference_reused"
    assert batch_row(engine, second).status == "approved"


def test_a_concurrent_b3_style_claim_waits_for_the_b5_recorder_then_refuses(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    holder = Session(engine)
    svc.record_batch(
        holder,
        tenant=holder.get(Tenant, env.tenant_id),
        party=holder.get(Party, env.people["recorder"]),
        batch_id=batch_id,
        bank_reference="RACE-2",
        now=NOW,
    )
    outcome: dict[str, object] = {}

    def b3_side():
        with Session(engine) as db:
            try:
                bank_ref.claim(db, tenant_id=env.tenant_id, bank_account_id=env.bank_id, reference="RACE-2")
                outcome["result"] = "claimed"
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                db.rollback()

    thread = threading.Thread(target=b3_side)
    thread.start()
    time.sleep(1.0)
    assert thread.is_alive()
    holder.commit()
    holder.close()
    thread.join(timeout=30)
    assert not thread.is_alive() and "result" not in outcome, outcome
    assert isinstance(outcome["error"], bank_ref.BankReferenceReused)


@pytest.mark.parametrize("bad", ["AB\u0085CD", "AB\u0080CD", "AB\u009fCD"])
def test_c1_control_characters_in_a_bank_reference_are_refused(engine, bad):
    env = world(engine)
    batch_id = approved_batch(engine, env, finalized(engine, env))
    refused(lambda: record(engine, env, batch_id, bad), "bank_reference_invalid")


def test_bank_account_changed_after_approval_refuses_record(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    with Session(engine) as db:
        db.execute(sa.update(BankAccount).where(BankAccount.id == env.bank_id).values(currency_code="USD"))
        db.commit()
    refused(lambda: record(engine, env, batch_id), "bank_account_currency")
    assert counts(engine, env)["payroll_liability_settlements"] == 0


def test_another_tenants_party_cannot_act_and_another_tenants_batch_is_not_found(engine):
    env, other = world(engine), world(engine)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    with Session(engine) as db:
        stranger = db.get(Party, other.people["recorder"])
        with pytest.raises(PayoutRefused) as exc:
            svc.record_batch(
                db, tenant=db.get(Tenant, env.tenant_id), party=stranger, batch_id=batch_id, bank_reference="REF-X"
            )
        assert exc.value.code == "actor_tenant"
    refused(lambda: record(engine, other, batch_id, "REF-X"), "not_found")


# --- bank file --------------------------------------------------------------


def test_bank_file_is_dormant_by_default_and_refused_without_writing(engine):
    env = world(engine)
    seed_instructions(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    with Session(engine) as db:
        assert disbursement_policy.bank_file_generation_enabled(db, env.tenant_id) is False
    refused(lambda: generate(engine, env, batch_id), "bank_file_disabled")
    assert counts(engine, env)["erp_disbursement_file_receipts"] == 0
    assert record(engine, env, batch_id).replayed is False  # recording never needs a file


def test_enabled_file_is_built_once_with_receipts_and_no_bytes_retained(engine):
    env = world(engine)
    seed_instructions(engine, env)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    built = generate(engine, env, batch_id)
    text = built.file.content.decode()
    assert built.line_count == 3 and built.file.row_count == 3 and len(built.file.sha256) == 64
    assert (
        "110000.00" in text
        and "65000.00" in text
        and "20000.00" in text
        and "5000.00" not in text.replace("65000.00", "")
    )
    assert "PAYE" not in text and "dest:" not in text  # no deduction or remittance leaks into the file
    batch = batch_row(engine, batch_id)
    assert batch.status == "file_issued" and batch.file_sha256 == built.file.sha256 and batch.file_row_count == 3
    assert batch.file_issued_by_party_id == env.people["issuer"]
    with Session(engine) as db:
        receipts = list(
            db.scalars(sa.select(DisbursementFileReceipt).where(DisbursementFileReceipt.tenant_id == env.tenant_id))
        )
        assert len(receipts) == 3 and {r.file_sha256 for r in receipts} == {built.file.sha256}
        assert {r.document_id for r in receipts} == {line.id for line in lines(engine, batch_id)}
        assert not any(isinstance(getattr(r, c.name), bytes) for r in receipts for c in r.__table__.columns)
    (event,) = audits(engine, env, svc.AUDIT_FILE_GENERATED)
    assert event.details["sha256"] == built.file.sha256 and "0123456" not in str(event.details)
    assert "0123456" not in repr(built) and "0123456" not in repr(built.file)
    assert counts(engine, env)["payroll_liability_settlements"] == 0  # a file records nothing


def test_one_file_per_batch_in_service_and_schema(engine):
    env = world(engine)
    seed_instructions(engine, env)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    generate(engine, env, batch_id)
    refused(lambda: generate(engine, env, batch_id, who="canceller"), "file_already_issued")
    assert counts(engine, env)["erp_disbursement_file_receipts"] == 3
    with Session(engine) as db:  # the schema refuses a second receipt for any line
        line = lines(engine, batch_id)[0]
        db.add(
            DisbursementFileReceipt(
                tenant_id=env.tenant_id,
                document_kind="payroll_payout",
                document_id=line.id,
                payee_instruction_id=uuid4(),
                file_format="generic-csv-v1",
                file_sha256="b" * 64,
                row_count=3,
                generated_by_party_id=env.people["issuer"],
                generated_at=NOW,
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()


def test_cancel_after_a_file_needs_an_audited_recall_reference(engine):
    env = world(engine)
    seed_instructions(engine, env)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    generate(engine, env, batch_id)
    refused(lambda: cancel(engine, env, batch_id), "file_issued")
    refused(lambda: cancel(engine, env, batch_id, recall="bad\nref"), "recall_reference_invalid")
    cancel(engine, env, batch_id, recall="BANK-RECALL-9")
    batch = batch_row(engine, batch_id)
    assert batch.status == "cancelled" and batch.recall_reference == "BANK-RECALL-9"
    (event,) = audits(engine, env, svc.AUDIT_CANCELLED)
    assert event.details["recall_reference"] == "BANK-RECALL-9"
    refused(lambda: record(engine, env, batch_id), "not_approved")
    # liabilities are released for a fresh batch, which may get its own (single) file
    again = approved_batch(engine, env, run_id)
    assert generate(engine, env, again).line_count == 3


def test_cancel_without_a_file_needs_a_reason_but_no_recall_reference(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = prepare(engine, env, run_id).batch_id
    refused(lambda: cancel(engine, env, batch_id, reason="  "), "reason_required")
    assert cancel(engine, env, batch_id).replayed is False
    assert cancel(engine, env, batch_id).replayed is True


def test_record_refuses_if_any_instruction_changed_since_the_file(engine):
    env = world(engine)
    seed_instructions(engine, env)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    generate(engine, env, batch_id)
    with Session(engine) as db:  # supersede E2's destination with a new approved instruction
        old = db.scalar(
            sa.select(PayeeBankInstruction).where(
                PayeeBankInstruction.tenant_id == env.tenant_id, PayeeBankInstruction.payee_ref == env.emp["E2"]
            )
        )
        old.status = "superseded"
        db.flush()
        db.add(
            PayeeBankInstruction(
                tenant_id=env.tenant_id,
                payee_kind="employee",
                payee_ref=env.emp["E2"],
                payee_label="Employee E2",
                bank_code="044",
                bank_name="Other Bank",
                account_number="9999999999",
                account_last4="9999",
                account_name="EMP E2",
                status="approved",
                origin="manual",
                proposed_by_party_id=env.people["proposer"],
                proposed_at=NOW,
                decided_by_party_id=env.people["decider"],
                decided_at=NOW,
                approved_at=NOW,
            )
        )
        db.commit()
    refused(lambda: record(engine, env, batch_id), "payee_instruction_changed")
    assert counts(engine, env)["payroll_liability_settlements"] == 0
    assert batch_row(engine, batch_id).status == "file_issued"


def test_an_unchanged_instruction_allows_recording_after_the_file(engine):
    env = world(engine)
    seed_instructions(engine, env)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    generate(engine, env, batch_id)
    assert record(engine, env, batch_id).journals == 3


@pytest.mark.parametrize(
    "bad", ["EMP,ONE", 'EMP "ONE"', "EMP;ONE", "EMP\nONE", "EMP" + chr(0x2028) + "ONE", "EMP\tONE"]
)
def test_unsafe_cell_refuses_the_whole_file_and_writes_no_receipt(engine, bad):
    env = world(engine)
    seed_instructions(engine, env)
    with Session(engine) as db:
        row = db.scalar(
            sa.select(PayeeBankInstruction).where(
                PayeeBankInstruction.tenant_id == env.tenant_id, PayeeBankInstruction.payee_ref == env.emp["E2"]
            )
        )
        db.execute(
            sa.text("UPDATE payee_bank_instructions SET account_name = :n WHERE id = :i"), {"n": bad, "i": row.id}
        )
        db.commit()
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    refused(lambda: generate(engine, env, batch_id), "file_invalid_cell")
    assert (
        counts(engine, env)["erp_disbursement_file_receipts"] == 0 and batch_row(engine, batch_id).status == "approved"
    )


def test_missing_instruction_refuses_the_file(engine):
    env = world(engine)
    seed_instructions(engine, env, refs=("E1", "E2"))
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    refused(lambda: generate(engine, env, batch_id), "no_approved_payee_instruction")
    assert counts(engine, env)["erp_disbursement_file_receipts"] == 0


# --- statutory remittances: recorded the same way, never a file ------------------


def test_statutory_remittance_is_recorded_without_a_file_dr_liability_cr_bank(engine):
    env = world(engine)
    enable_file(engine, env)  # even with files enabled a remittance has none
    run_id = finalized(engine, env)
    out = prepare(engine, env, run_id, kind="statutory", destination_ref="dest:PAYE")
    batch = batch_row(engine, out.batch_id)
    assert batch.kind == "statutory" and batch.line_count == 2 and batch.total_amount == Decimal("15000")
    approve(engine, env, out.batch_id)
    refused(lambda: generate(engine, env, out.batch_id), "statutory_no_file")
    assert counts(engine, env)["erp_disbursement_file_receipts"] == 0
    refused(lambda: record(engine, env, out.batch_id, who="approver"), "sod_recorder_is_approver")
    assert record(engine, env, out.batch_id, "FIRS-REMIT-1").journals == 2
    debits, credits, journals = settlement_posting(engine, env)
    assert journals == 2 and debits["2110"] == Decimal("15000") and credits["1000"] == Decimal("15000")
    assert "2040" not in debits
    rows = liabilities(engine, env, run_id)
    assert rows[("component", "dest:PAYE")].status == "settled"
    assert rows[("employee-net-pay", env.emp["E1"])].status == "outstanding"  # net pay untouched


def test_statutory_batch_needs_one_destination_and_never_takes_net_pay(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    refused(lambda: prepare(engine, env, run_id, kind="statutory"), "destination_required")
    refused(lambda: prepare(engine, env, run_id, kind="statutory", destination_ref="dest:NOPE"), "nothing_to_pay")
    net = liabilities(engine, env, run_id)[("employee-net-pay", env.emp["E1"])]
    refused(
        lambda: prepare(engine, env, run_id, kind="statutory", destination_ref="dest:PAYE", liability_ids=[net.id]),
        "liability_not_eligible",
    )
    pension = liabilities(engine, env, run_id)[("component", "dest:PENSION_ER")]
    refused(
        lambda: prepare(engine, env, run_id, kind="statutory", destination_ref="dest:PAYE", liability_ids=[pension.id]),
        "liability_not_eligible",
    )


def test_statutory_sod_has_no_instruction_rule_but_keeps_three_distinct_people(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = prepare(engine, env, run_id, kind="statutory", destination_ref="dest:PENSION_ER").batch_id
    refused(lambda: approve(engine, env, batch_id, who="preparer"), "sod_approver_is_preparer")
    approve(engine, env, batch_id)
    refused(lambda: record(engine, env, batch_id, who="preparer"), "sod_recorder_is_preparer")
    assert record(engine, env, batch_id).journals == 2


# --- internal recovery exclusion (a3 explicit kind; a4 settlement_mode) ---------------


def test_asset_class_liability_account_is_never_paid_out(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    with Session(engine) as db:
        run = db.get(__import__("dotmac_payroll.models", fromlist=["PayrollRun"]).PayrollRun, run_id)
        row = liabilities(engine, env, run_id)[("component", "dest:PAYE")]
        fake = SimpleNamespace(
            run_id=run.id,
            tenant_id=run.tenant_id,
            liability_key=row.liability_key,
            beneficiary_type="external",
            component_id=row.component_id,
            beneficiary_ref=row.beneficiary_ref,
            liability_account_ref=str(env.accounts["1410"]),  # Staff Loan: asset class
            status="outstanding",
            amount_settled=Decimal(0),
            amount=Decimal(10),
        )
        with pytest.raises(PayoutRefused) as exc:
            svc.check_liability(db, kind="statutory", run=run, liability=fake, destination_ref=None)
        assert exc.value.code == "internal_recovery_excluded"


@pytest.mark.parametrize(
    ("attrs", "allowed"),
    [
        ({}, True),  # payroll a3: no settlement_mode, the explicit liability-kind checks are the guard
        ({"settlement_mode": "external"}, True),  # a4 external
        ({"settlement_mode": "internal_recovery"}, False),  # a4 internal recovery
        ({"settlement_mode": None}, False),  # a4 liability without a mode is not payable
    ],
)
def test_a4_settlement_mode_gate_turns_on_when_the_attribute_exists(attrs, allowed):
    assert svc.settlement_mode_allows_payout(SimpleNamespace(**attrs)) is allowed


def test_a4_internal_recovery_net_pay_or_remittance_is_refused_by_the_selection_check(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    with Session(engine) as db:
        run = db.get(__import__("dotmac_payroll.models", fromlist=["PayrollRun"]).PayrollRun, run_id)
        rows = liabilities(engine, env, run_id)
        for key, kind in ((("employee-net-pay", env.emp["E1"]), "net_pay"), (("component", "dest:PAYE"), "statutory")):
            live = db.get(PayrollLiability, rows[key].id)
            live.settlement_mode = "internal_recovery"  # a4 column simulated on the loaded instance
            with pytest.raises(PayoutRefused) as exc:
                svc.check_liability(db, kind=kind, run=run, liability=live, destination_ref=None)
            assert exc.value.code == "internal_recovery_excluded"
            live.settlement_mode = "external"
            svc.check_liability(db, kind=kind, run=run, liability=live, destination_ref=None)
        db.rollback()


# --- real a4: internal recovery is never payable; remittances need an external beneficiary ----


def internal_world(engine):
    """PAYE is a staff-loan style deduction: internal_recovery, mapped to the ASSET account 1410."""
    return world(engine, paye_liability="1410", paye_mode="internal_recovery")


def test_a4_internal_recovery_liability_is_settled_employee_side_and_in_no_payout(engine):
    env = internal_world(engine)
    run_id = finalized(engine, env)
    with Session(engine) as db:
        recoveries = list(
            db.scalars(
                sa.select(PayrollLiability).where(
                    PayrollLiability.run_id == run_id, PayrollLiability.settlement_mode == "internal_recovery"
                )
            )
        )
    assert len(recoveries) == 2
    assert all(
        r.status == "settled" and r.amount_settled == r.amount and r.beneficiary_type == "employee" for r in recoveries
    )
    ids = [r.id for r in recoveries]
    # never in a net-pay batch (all three: the default selection, and an explicit pick)
    out = prepare(engine, env, run_id)
    assert not {line.liability_id for line in lines(engine, out.batch_id)} & set(ids)
    refused(lambda: prepare(engine, env, run_id, liability_ids=ids), "liability_not_eligible")
    # never in a remittance: neither by the PAYE destination nor by naming the employee as destination
    refused(lambda: prepare(engine, env, run_id, kind="statutory", destination_ref="dest:PAYE"), "nothing_to_pay")
    for recovery in recoveries:
        refused(
            lambda r=recovery: prepare(
                engine,
                env,
                run_id,
                kind="statutory",
                destination_ref=r.beneficiary_ref,
                liability_ids=[r.id],
            ),
            "liability_not_statutory",
        )
    # a settled internal recovery is also refused by the owner for settlement
    with Session(engine) as db:
        with pytest.raises(payroll.PayrollRuleViolation):
            payroll.record_liability_settlement(
                db,
                tenant_id=env.tenant_id,
                liability_id=recoveries[0].id,
                amount=Money.of(Decimal(1), Currency("NGN", 2)),
                settlement_ref="x",
                evidence_ref="y",
                recorded_by_id=env.people["recorder"],
                recorded_at=NOW,
            )
        db.rollback()
    assert counts(engine, env)["batches"] == 1  # only the net-pay batch above


def test_a4_external_remittance_still_works_beside_an_internal_recovery(engine):
    env = internal_world(engine)
    run_id = finalized(engine, env)
    out = prepare(engine, env, run_id, kind="statutory", destination_ref="dest:PENSION_ER")
    assert batch_row(engine, out.batch_id).total_amount == Decimal("7500.00")


def test_a4_statutory_remittance_requires_beneficiary_type_external(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    from dotmac_payroll.models import PayrollRun

    with Session(engine) as db:
        run = db.get(PayrollRun, run_id)
        rows = liabilities(engine, env, run_id)
        live = db.get(PayrollLiability, rows[("component", "dest:PAYE")].id)
        assert live.settlement_mode == "external" and live.beneficiary_type == "external"
        svc.check_liability(db, kind="statutory", run=run, liability=live, destination_ref=None)
        for beneficiary in ("employee", "supplier", ""):
            live.beneficiary_type = beneficiary  # simulated on the loaded instance, never flushed
            with pytest.raises(PayoutRefused) as exc:
                svc.check_liability(db, kind="statutory", run=run, liability=live, destination_ref=None)
            assert exc.value.code == "liability_not_statutory"
        db.rollback()


# --- privileges and migration ---------------------------------------------------


@pytest.fixture()
def runtime_engine(engine):
    role = f"b5_rt_{uuid4().hex[:12]}"
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


def test_runtime_role_cannot_change_amounts_or_membership_or_delete_and_is_tenant_isolated(engine, runtime_engine):
    env, other = world(engine), world(engine)
    run_id = finalized(engine, env)
    batch_id = prepare(engine, env, run_id).batch_id

    def scoped(tenant):
        db = Session(runtime_engine)
        db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        return db

    with scoped(env.tenant_id) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(PayrollPayoutLine)) == 3
        db.execute(sa.text("UPDATE erp_payroll_payout_batches SET updated_at = now()"))  # lifecycle column: allowed
        db.commit()
    for statement in (
        "UPDATE erp_payroll_payout_lines SET amount = amount + 1",
        "UPDATE erp_payroll_payout_lines SET liability_id = gen_random_uuid()",
        "UPDATE erp_payroll_payout_batches SET total_amount = 1",
        "UPDATE erp_payroll_payout_batches SET prepared_by_party_id = gen_random_uuid()",
        "DELETE FROM erp_payroll_payout_lines",
        "DELETE FROM erp_payroll_payout_batches",
        "TRUNCATE erp_payroll_payout_lines",
    ):
        with scoped(env.tenant_id) as db:
            with pytest.raises(sa.exc.ProgrammingError) as err:
                db.execute(sa.text(statement))
            assert "permission denied" in str(err.value.orig).lower(), statement
    with scoped(other.tenant_id) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(PayrollPayoutBatch)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(PayrollPayoutLine)) == 0
    assert batch_row(engine, batch_id).line_count == 3


def _run_downgrade(engine, *, with_batch: bool, env=None):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from erp_v2.migrations.versions import e2_0019_payroll_payouts as migration

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if not with_batch:
                connection.execute(sa.text("DELETE FROM erp_payroll_payout_lines"))
                connection.execute(sa.text("DELETE FROM erp_payroll_payout_batches"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            return connection.execute(sa.text("SELECT to_regclass('public.erp_payroll_payout_batches')")).scalar()
        finally:
            transaction.rollback()


def test_downgrade_refuses_when_batches_exist_and_drops_when_empty(engine):
    env = world(engine)
    prepare(engine, env, finalized(engine, env))
    with pytest.raises(RuntimeError, match="payout batches exist"):
        _run_downgrade(engine, with_batch=True)
    assert _run_downgrade(engine, with_batch=False) is None


# --- review round 1 -----------------------------------------------------------------


def test_with_files_enabled_recording_requires_the_issued_file(engine):
    env = world(engine)
    seed_instructions(engine, env)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    before = counts(engine, env)
    refused(lambda: record(engine, env, batch_id), "bank_file_required")
    assert counts(engine, env) == before and batch_row(engine, batch_id).status == "approved"
    generate(engine, env, batch_id)
    assert record(engine, env, batch_id).journals == 3


def test_statutory_recording_never_needs_a_file_even_when_files_are_enabled(engine):
    env = world(engine)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id, kind="statutory", destination_ref="dest:PAYE")
    assert record(engine, env, batch_id).journals == 2


@pytest.mark.parametrize("role", ["proposer", "decider"])
def test_without_files_the_recorder_may_not_be_an_instruction_proposer_or_decider(engine, role):
    env = world(engine)
    if role == "proposer":
        seed_instructions(engine, env, proposer="recorder")
    else:
        seed_instructions(engine, env, decider="recorder")
    run_id = finalized(engine, env)
    batch_id = approved_batch(engine, env, run_id)
    refused(lambda: record(engine, env, batch_id), "sod_recorder_is_instruction_party")
    assert counts(engine, env)["payroll_liability_settlements"] == 0
    assert record(engine, env, batch_id, who="issuer").replayed is False  # someone independent can


def test_unknown_employee_beneficiary_is_refused_at_prepare_never_skipped(engine):
    env = world(engine)
    seed_instructions(engine, env)
    run_id = finalized(engine, env)
    ghost = str(uuid4())
    with Session(engine) as db:  # a liability whose employee is not in people (placeholder-style ref)
        db.execute(
            sa.text(
                "UPDATE mod_payroll.payroll_liabilities SET beneficiary_ref = :g "
                "WHERE tenant_id = :t AND run_id = :r AND beneficiary_ref = :e"
            ),
            {"g": ghost, "t": env.tenant_id, "r": run_id, "e": env.emp["E3"]},
        )
        db.commit()
    before = counts(engine, env)
    refused(lambda: prepare(engine, env, run_id), "payee_unresolved")
    assert counts(engine, env) == before
    with Session(engine) as db:  # a non-UUID placeholder is unresolved too
        db.execute(
            sa.text("UPDATE mod_payroll.payroll_liabilities SET beneficiary_ref = 'E3' WHERE beneficiary_ref = :g"),
            {"g": ghost},
        )
        db.commit()
    refused(lambda: prepare(engine, env, run_id), "payee_unresolved")


def _recorded_supplier_payment(engine, env, reference):
    """A real B3 payment (prepare, approve, record) on the same bank account, through the owner commands."""
    import test_b1_supplier_invoice_post_pg as b1
    from dotmac_payables.models import PaymentObligation

    from erp_v2.supplier_payments import service as sp
    from erp_v2.supplier_payments.service import AllocationRequest

    with Session(engine) as db:
        liability = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "LIA")
        )
        tax = Account(
            tenant_id=env.tenant_id,
            category_id=liability.id,
            code="2100",
            name="Input VAT",
            kind=AccountKind.POSTING,
            normal_balance=NormalBalance.DEBIT,
        )
        db.add(tax)
        db.flush()
        env.tax_account = tax.id
        db.commit()
    invoice_id = b1.invoice(engine, env, number=f"AP-{uuid4().hex[:6]}")
    b1.approve(engine, env, invoice_id)
    with Session(engine) as db:
        obligation = db.scalar(sa.select(PaymentObligation.id).where(PaymentObligation.invoice_id == invoice_id))
    created = act(
        engine,
        env,
        "preparer",
        sp.prepare_payment,
        request_id=uuid4(),
        bank_account_id=env.bank_id,
        value_date=VALUE_DATE,
        allocations=[AllocationRequest(obligation, Decimal("215"))],
    )
    act(engine, env, "approver", sp.approve_payment, payment_id=created.payment_id)
    act(engine, env, "recorder", sp.record_payment, payment_id=created.payment_id, bank_reference=reference)


def test_bank_reference_used_by_a_supplier_payment_on_the_same_account_is_refused(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    _recorded_supplier_payment(engine, env, "SHARED REF 7")
    batch_id = approved_batch(engine, env, run_id)
    before = counts(engine, env)
    refused(lambda: record(engine, env, batch_id, "  shared  ref 7 "), "bank_reference_reused")  # normalised match
    assert counts(engine, env) == before
    assert record(engine, env, batch_id, "OTHER REF 8").replayed is False


def test_a_supplier_reference_on_another_bank_account_does_not_block(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    _recorded_supplier_payment(engine, env, "ELSEWHERE 1")
    with Session(engine) as db:  # a second bank account: the same reference there is a different bank's reference
        institution = db.scalar(sa.select(BankInstitution).where(BankInstitution.tenant_id == env.tenant_id))
        other = BankAccount(
            id=uuid4(),
            tenant_id=env.tenant_id,
            institution_id=institution.id,
            account_code="OP2",
            account_name="Second",
            account_identifier="payer-acct-2",
            account_type_code="current",
            currency_code="NGN",
            minor_units=2,
            cash_account_ref=str(env.accounts["1000"]),
            status="active",
        )
        db.add(other)
        other_id = other.id
        db.commit()
    batch_id = approved_batch(engine, env, run_id, bank_account_id=other_id)
    assert record(engine, env, batch_id, "ELSEWHERE 1").replayed is False


# --- dual-control policy (D-9): default OFF ----------------------------------------------------------------------
# Payroll a5 still owns its own settlement separation until payroll a6; the ERP-side checks below are the ones
# this policy controls, so the steps that stop at the ERP layer (approve, issue the file) use the same person.


def test_policy_off_the_preparer_approves_the_instruction_party_acts_and_the_batch_is_stamped_off(engine):
    env = world(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    seed_instructions(engine, env, proposer="approver", decider="approver", dual_control=False)
    enable_file(engine, env)
    run_id = finalized(engine, env)
    out = prepare(engine, env, run_id)
    assert batch_row(engine, out.batch_id).dual_control is False
    assert all(line.dual_control is False for line in lines(engine, out.batch_id))
    assert approve(engine, env, out.batch_id, who="preparer").replayed is False  # preparer approves
    # the file is issued by the batch preparer (also an instruction party): none of the ERP refusals applies
    assert generate(engine, env, out.batch_id, who="preparer") is not None
    assert batch_row(engine, out.batch_id).status == "file_issued"


def test_policy_on_stamps_true_and_the_refusals_still_fire(engine):
    env = world(engine)  # the seed turns the policy ON
    run_id = finalized(engine, env)
    out = prepare(engine, env, run_id)
    assert batch_row(engine, out.batch_id).dual_control is True
    refused(lambda: approve(engine, env, out.batch_id, who="preparer"), "sod_approver_is_preparer")


def test_check_distinct_people_is_rewritten_not_dual_control_or_predicate(engine):
    env = world(engine)
    run_id = finalized(engine, env)
    batch_id = prepare(engine, env, run_id).batch_id
    sql = (
        "UPDATE erp_payroll_payout_batches SET dual_control=:on, approved_by_party_id=:who, status='approved' "
        "WHERE id=:id"
    )
    with Session(engine) as db, pytest.raises(sa.exc.IntegrityError) as err:  # on: the preparer is refused
        db.execute(sa.text(sql), {"on": True, "who": env.people["preparer"], "id": batch_id})
    assert "distinct_people" in str(err.value)
    with Session(engine) as db:  # off: the database accepts the same person
        db.execute(sa.text(sql), {"on": False, "who": env.people["preparer"], "id": batch_id})
        db.rollback()
