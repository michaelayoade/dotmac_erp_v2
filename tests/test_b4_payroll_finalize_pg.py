"""B4 payroll batch -> approve -> finalize -> post against real PostgreSQL (hosted CI).

Real payroll a4, accounting a2 and kernel migrations; real owner commands and the real posting
bridge. Reuses the disposable-database fixture and tenant/account seed of the bridge test.
Account codes, mappings and amounts here are test inputs only (Finance owns the real mappings).
"""

import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory, JournalEntry, JournalLine, PostedLedgerLine
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, PartyType, Tenant
from dotmac_kernel.money import Currency, Money
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dotmac_payroll import service as payroll
from dotmac_payroll.contracts import PayComponentInput, PayRuleInput
from dotmac_payroll.models import (
    PayrollCalculation,
    PayrollCalculationLine,
    PayrollLiability,
    PayrollRun,
)
from sqlalchemy.orm import Session
from test_posting_bridge_pg import NOW, seed, soft_close

from erp_v2.assembly import assembly
from erp_v2.journals import policy
from erp_v2.journals.routes import journals_web_feature
from erp_v2.web import payroll_run_services as svc

engine = bridge_pg.engine  # the disposable-database fixture, re-exposed under its own name

MODELS = (PayrollCalculation, PayrollLiability, JournalEntry, JournalLine)
GOOD_FILE = (
    "employee_ref,component_code,amount,evidence_ref\n"
    "E1,BASIC,100000.00,hr:contract-E1\n"
    "E2,BASIC,50000.00,hr:contract-E2\n"
).encode()


@pytest.fixture(scope="module", autouse=True)
def _registries():
    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))
    manifests = [journals_web_feature, FeatureManifest(name="kernel_stub")]
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


def counts(engine, tenant_id) -> dict[str, int]:
    with Session(engine) as db:
        return {
            m.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(m).where(m.tenant_id == tenant_id))
            for m in MODELS
        }


def audits(engine, tenant_id, action):
    with Session(engine) as db:
        return list(
            db.scalars(sa.select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == action))
        )


def component(db, env, code, kind, *, expense=None, liability=None):
    return payroll.create_pay_component(
        db,
        tenant_id=env.tenant_id,
        command=PayComponentInput(
            component_code=code,
            name=code,
            kind=kind,
            expense_account_ref=None if expense is None else str(env.accounts[expense]),
            liability_account_ref=None if liability is None else str(env.accounts[liability]),
            liability_destination_ref=None if liability is None else f"dest:{code}",
        ),
    )


def rule(component_id, sequence, method, *, fixed=None, rate=None, bases=(), mode="external"):
    return PayRuleInput(
        component_id=component_id,
        sequence=sequence,
        calculation_method=method,
        fixed_amount=None if fixed is None else Money.of(Decimal(fixed), Currency("NGN", 2)),
        rate=None if rate is None else Decimal(rate),
        basis_component_ids=tuple(bases),
        prorates=False,
        settlement_mode=mode,
    )


def world(engine, *, paye_liability="2110", net_ref="2040", with_third=True, currency="NGN", paye_mode="external"):
    env = seed(engine)
    with Session(engine) as db:
        categories = {
            c.code: c for c in db.scalars(sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id))
        }
        asset = AccountCategory(tenant_id=env.tenant_id, code="AST", name="AST", account_class=AccountClass.ASSET)
        db.add(asset)
        db.flush()
        for code, category, balance in (
            ("5100", categories["EXP"], NormalBalance.DEBIT),
            ("2110", categories["LIA"], NormalBalance.CREDIT),
            ("2120", categories["LIA"], NormalBalance.CREDIT),
            ("1410", asset, NormalBalance.DEBIT),
            ("1300", asset, NormalBalance.DEBIT),  # an asset account that is NOT a declared recovery account
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
        parties = [
            Party(tenant_id=env.tenant_id, party_type=PartyType.person, display_name=n)
            for n in ("creator", "approver", "finalizer", "other")
        ]
        db.add_all(parties)
        db.flush()
        env.party_ids = [p.id for p in parties]
        if currency:
            policy.set_functional_currency(db, tenant_id=env.tenant_id, code=currency, reason="test seed")
        basic = component(db, env, "BASIC", "earning", expense="5000")
        housing = component(db, env, "HOUSING", "earning", expense="5000")
        paye = component(db, env, "PAYE", "deduction", liability=paye_liability)
        pension = component(db, env, "PENSION_ER", "employer_liability", expense="5100", liability="2120")
        structure = payroll.create_pay_structure(
            db, tenant_id=env.tenant_id, code="INPUT", name="Input", currency=Currency("NGN", 2)
        )
        net = {"2040": "2040", "uuid": str(env.accounts["2040"])}.get(net_ref) or str(env.accounts[net_ref])
        revision = payroll.publish_pay_structure_revision(
            db,
            tenant_id=env.tenant_id,
            structure_id=structure.id,
            version=1,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            net_payable_account_ref=net,
            rules=(
                rule(basic.id, 1, "input"),
                rule(housing.id, 2, "fixed", fixed="20000.00"),
                rule(paye.id, 3, "percentage", rate="0.10", bases=(basic.id,), mode=paye_mode),
                rule(pension.id, 4, "percentage", rate="0.05", bases=(basic.id,)),
            ),
            published_by_id=env.party_ids[0],
            published_at=NOW,
        )
        fixed_structure = payroll.create_pay_structure(
            db, tenant_id=env.tenant_id, code="FIXED", name="Fixed", currency=Currency("NGN", 2)
        )
        fixed_revision = payroll.publish_pay_structure_revision(
            db,
            tenant_id=env.tenant_id,
            structure_id=fixed_structure.id,
            version=1,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            net_payable_account_ref=net,
            rules=(rule(housing.id, 1, "fixed", fixed="20000.00"),),
            published_by_id=env.party_ids[0],
            published_at=NOW,
        )
        env.revision_id, env.fixed_revision_id = revision.id, fixed_revision.id
        for employee, rev in (("E1", revision), ("E2", revision), *((("E3", fixed_revision),) if with_third else ())):
            payroll.assign_employee_pay_structure(
                db,
                tenant_id=env.tenant_id,
                employee_ref=employee,
                revision_id=rev.id,
                effective_from=date(2026, 1, 1),
                effective_to=None,
                source_ref="test",
                source_version="1",
            )
        env.tenant = db.get(Tenant, env.tenant_id)
        db.commit()
    return env


def new_run(engine, env, *, ref="RUN-2026-01", start=date(2026, 1, 1), end=date(2026, 1, 31), currency="NGN"):
    with Session(engine) as db:
        run = payroll.create_payroll_run(
            db,
            tenant_id=env.tenant_id,
            run_ref=ref,
            period_start=start,
            period_end=end,
            currency=Currency(currency, 2),
            created_by_id=env.party_ids[0],
            created_at=NOW,
        )
        db.commit()
        return run.id


def run_command(engine, env, command, run_id, party_index, *, commit=True, caller_write=False, **kwargs):
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[party_index])
        if caller_write:
            db.add(
                AccountCategory(
                    tenant_id=env.tenant_id,
                    code=f"CALLER-{uuid4().hex[:10]}",
                    name="c",
                    account_class=AccountClass.EXPENSE,
                )
            )
            db.flush()
        try:
            return command(db, tenant=tenant, party=party, run_id=run_id, now=NOW, **kwargs)
        finally:
            if commit:
                db.commit()  # the request boundary commits after a caught refusal too


def batch(engine, env, run_id, data=GOOD_FILE, party_index=0, **kw):
    return run_command(engine, env, svc.batch_calculate, run_id, party_index, input_bytes=data, **kw)


def approve(engine, env, run_id, party_index=1, **kw):
    return run_command(engine, env, svc.approve, run_id, party_index, **kw)


def finalize(engine, env, run_id, party_index=2, **kw):
    return run_command(engine, env, svc.finalize, run_id, party_index, **kw)


def refusal(engine, env, command, run_id, code, party_index, **kw):
    before = counts(engine, env.tenant_id)
    with pytest.raises(svc.RunRefused) as exc:
        run_command(engine, env, command, run_id, party_index, caller_write=True, **kw)
    assert exc.value.code == code, str(exc.value)
    assert counts(engine, env.tenant_id) == before  # refusal leaves nothing
    with Session(engine) as db:  # the caller's earlier write survives
        assert db.scalar(
            sa.select(sa.func.count())
            .select_from(AccountCategory)
            .where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code.like("CALLER-%"))
        )
    assert any(
        a.details["code"] == code and a.is_success is False for a in audits(engine, env.tenant_id, svc.AUDIT_REFUSED)
    )
    return exc.value


def status(engine, run_id):
    with Session(engine) as db:
        return db.get(PayrollRun, run_id).status


def ready_to_finalize(engine, env, **kw):
    run_id = new_run(engine, env, **kw)
    batch(engine, env, run_id)
    approve(engine, env, run_id)
    return run_id


def test_batch_calculates_every_active_assignment_and_only_input_employees_need_the_file(engine):
    env = world(engine)
    run_id = new_run(engine, env)
    out = batch(engine, env, run_id)
    assert out.calculated == 3 and out.skipped_existing == 0 and out.replayed is False
    assert len(out.input_sha256) == 64 and out.input_rows == 2
    with Session(engine) as db:
        got = {
            c.employee_ref: (c.gross_amount, c.deduction_amount, c.net_amount, c.employer_liability_amount)
            for c in db.scalars(sa.select(PayrollCalculation).where(PayrollCalculation.tenant_id == env.tenant_id))
        }
        assert got["E1"] == (Decimal("120000.00"), Decimal("10000.00"), Decimal("110000.00"), Decimal("5000.00"))
        assert got["E2"] == (Decimal("70000.00"), Decimal("5000.00"), Decimal("65000.00"), Decimal("2500.00"))
        assert got["E3"] == (Decimal("20000.00"), Decimal("0.00"), Decimal("20000.00"), Decimal("0.00"))
        evidence = {
            line.evidence_ref
            for line in db.scalars(
                sa.select(PayrollCalculationLine).where(PayrollCalculationLine.component_code == "BASIC")
            )
        }
        assert evidence >= {"hr:contract-E1", "hr:contract-E2"}
    (event,) = audits(engine, env.tenant_id, svc.AUDIT_BATCH_CALCULATED)
    assert event.details["calculated"] == 3 and "100000" not in str(event.details)  # hash and counts only


def test_batch_replay_is_a_no_op(engine):
    env = world(engine)
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    before = counts(engine, env.tenant_id)
    again = batch(engine, env, run_id, data=None)
    assert again.calculated == 0 and again.skipped_existing == 3 and again.replayed is True
    assert counts(engine, env.tenant_id) == before
    assert len(audits(engine, env.tenant_id, svc.AUDIT_BATCH_CALCULATED)) == 1


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (None, "input_file_missing_rows"),
        (b"employee_ref,component_code,amount,evidence_ref\nE1,BASIC,1.00,x\n", "input_file_missing_rows"),
        (b"employee_ref,component_code,amount,evidence_ref\nE1,BASIC,1.00,\nE2,BASIC,1.00,y\n", "input_file_refused"),
        (GOOD_FILE + b"E1,HOUSING,5.00,hr:x\n", "input_file_surplus_rows"),
        (GOOD_FILE + b"E9,BASIC,5.00,hr:x\n", "input_for_unknown_or_calculated_employee"),
        (GOOD_FILE + b"E3,HOUSING,5.00,hr:x\n", "input_file_surplus_rows"),
        (b"employee_ref,component_code,amount,evidence_ref\nE1,BASIC,1.005,x\n", "input_file_refused"),
    ],
)
def test_input_employees_need_complete_evidenced_rows_and_nothing_is_half_calculated(engine, data, code):
    env = world(engine)
    run_id = new_run(engine, env)
    refusal(engine, env, svc.batch_calculate, run_id, code, 0, input_bytes=data)
    assert status(engine, run_id) == "draft"


def test_one_failing_employee_rolls_the_whole_batch_back(engine, monkeypatch):
    env = world(engine)
    run_id = new_run(engine, env)
    real = payroll.calculate_employee_payroll
    seen = []

    def flaky(db, **kwargs):
        seen.append(kwargs["employee_ref"])
        if len(seen) == 2:
            raise payroll.PayrollRuleViolation("boom")
        return real(db, **kwargs)

    monkeypatch.setattr(payroll, "calculate_employee_payroll", flaky)
    refusal(engine, env, svc.batch_calculate, run_id, "calculation_refused", 0, input_bytes=GOOD_FILE)
    assert len(seen) == 2


def test_overlap_with_an_existing_run_refuses_and_cancelled_runs_do_not_count(engine):
    env = world(engine)
    legacy = new_run(engine, env, ref="LEGACY-01", start=date(2026, 1, 1), end=date(2026, 1, 31))
    with Session(engine) as db:  # imported legacy runs arrive finalized
        db.execute(sa.update(PayrollRun).where(PayrollRun.id == legacy).values(status="finalized"))
        db.commit()
    overlapping = new_run(engine, env, ref="RUN-OVERLAP", start=date(2026, 1, 15), end=date(2026, 2, 14))
    err = refusal(
        engine, env, svc.batch_calculate, overlapping, "period_overlaps_existing_run", 0, input_bytes=GOOD_FILE
    )
    assert "LEGACY-01" in str(err)
    with Session(engine) as db:
        db.execute(
            sa.update(PayrollRun)
            .where(PayrollRun.id == legacy)
            .values(status="cancelled", cancelled_by_id=env.party_ids[0], cancelled_at=NOW, cancel_reason="test")
        )
        db.commit()
    assert batch(engine, env, overlapping).calculated == 3


def test_run_currency_must_equal_the_functional_currency(engine):
    env = world(engine, currency="USD")  # the run (NGN) differs from the tenant functional currency
    run_id = new_run(engine, env)
    err = refusal(engine, env, svc.batch_calculate, run_id, "non_functional_currency", 0, input_bytes=GOOD_FILE)
    assert "USD" in str(err)


def test_unset_functional_currency_fails_closed_never_defaulting(engine):
    env = world(engine, currency=None)
    run_id = new_run(engine, env)
    refusal(engine, env, svc.batch_calculate, run_id, "functional_currency_unset", 0, input_bytes=GOOD_FILE)


def test_finalize_posts_balanced_with_2040_equal_to_total_net_pay(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    assert status(engine, run_id) == "approved"
    out = finalize(engine, env, run_id)
    assert out.replayed is False and out.net_total == Decimal("195000.00")
    with Session(engine) as db:
        run = db.get(PayrollRun, run_id)
        assert run.status == "finalized" and run.finalized_by_id == env.party_ids[2]
        assert run.approved_by_id == env.party_ids[1] and run.created_by_id == env.party_ids[0]
        journal = db.get(JournalEntry, out.journal_id)
        assert journal.source_owner == "payroll" and journal.source_document_kind == "payroll_run"
        assert journal.source_document_id == str(run_id) and journal.status.value == "POSTED"
        lines = {
            db.get(Account, r.account_id).code: (r.debit, r.credit)
            for r in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == journal.id))
        }
        net = sum(db.scalars(sa.select(PayrollCalculation.net_amount).where(PayrollCalculation.run_id == run_id)))
        assert lines["2040"] == (0, net) and net == Decimal("195000.00")  # 2040 credit total = net pay
        assert lines["5000"][0] == Decimal("210000.00")  # gross earnings
        assert lines["5100"][0] == Decimal("7500.00")  # employer expense
        assert lines["2110"][1] == Decimal("15000.00") and lines["2120"][1] == Decimal("7500.00")
        assert sum(d for d, _ in lines.values()) == sum(c for _, c in lines.values()) == Decimal("217500.00")
        liabilities = db.scalars(sa.select(PayrollLiability).where(PayrollLiability.run_id == run_id)).all()
        assert len(liabilities) == 7 and all(row.status == "outstanding" for row in liabilities)
    (event,) = audits(engine, env.tenant_id, svc.AUDIT_FINALIZED_POSTED)
    assert event.details["journal_id"] == str(out.journal_id) and event.actor_id == str(env.party_ids[2])
    assert event.details["approved_by"] == str(env.party_ids[1])


def test_net_payable_reference_may_be_the_2040_account_uuid(engine):
    env = world(engine, net_ref="uuid")
    run_id = ready_to_finalize(engine, env)
    assert finalize(engine, env, run_id).net_total == Decimal("195000.00")


def test_finalize_is_idempotent(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    first = finalize(engine, env, run_id)
    after_first = counts(engine, env.tenant_id)
    again = finalize(engine, env, run_id)
    other = finalize(engine, env, run_id, party_index=3)
    assert again.replayed is other.replayed is True
    assert again.journal_id == other.journal_id == first.journal_id
    assert counts(engine, env.tenant_id) == after_first
    assert after_first["journal_entries"] == 1 and after_first["payroll_liabilities"] == 7
    assert len(audits(engine, env.tenant_id, svc.AUDIT_FINALIZED_POSTED)) == 1
    again_approve = approve(engine, env, run_id, party_index=3)  # approving a finalized run is a no-op replay
    assert again_approve.replayed is True


def test_concurrent_finalizers_yield_one_journal_and_one_set_of_liabilities(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    results, errors = [], []
    first_in = threading.Event()

    def holder():
        with Session(engine) as db:
            tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[2])
            results.append(svc.finalize(db, tenant=tenant, party=party, run_id=run_id, now=NOW))
            first_in.set()
            time.sleep(1.0)  # keep the transaction (and the run row lock) open
            db.commit()

    def waiter():
        first_in.wait(10)
        try:
            with Session(engine) as db:
                tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[3])
                results.append(svc.finalize(db, tenant=tenant, party=party, run_id=run_id, now=NOW))
                db.commit()
        except Exception as exc:  # pragma: no cover - failure detail for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert sorted(r.replayed for r in results) == [False, True]
    assert len({r.journal_id for r in results}) == 1
    c = counts(engine, env.tenant_id)
    assert c["journal_entries"] == 1 and c["payroll_liabilities"] == 7
    assert len(audits(engine, env.tenant_id, svc.AUDIT_FINALIZED_POSTED)) == 1


def test_separation_of_duties_creator_approver_finalizer(engine):
    env = world(engine)
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    refusal(engine, env, svc.approve, run_id, "sod_creator_is_approver", 0)
    refusal(engine, env, svc.finalize, run_id, "not_approved", 2)
    approve(engine, env, run_id)  # a different person approves
    refusal(engine, env, svc.finalize, run_id, "sod_approver_is_finalizer", 1)
    refusal(engine, env, svc.finalize, run_id, "sod_creator_is_finalizer", 0)
    assert status(engine, run_id) == "approved"
    finalize(engine, env, run_id)  # a third person finalizes
    assert status(engine, run_id) == "finalized"


def test_a_party_of_another_tenant_is_refused(engine):
    env, other = world(engine), world(engine)
    run_id = ready_to_finalize(engine, env)
    with Session(engine) as db:
        with pytest.raises(svc.RunRefused) as exc:
            svc.finalize(
                db, tenant=db.get(Tenant, env.tenant_id), party=db.get(Party, other.party_ids[2]), run_id=run_id
            )
        assert exc.value.code == "actor_tenant"
        with pytest.raises(svc.RunRefused) as exc:
            svc.finalize(
                db, tenant=db.get(Tenant, other.tenant_id), party=db.get(Party, other.party_ids[2]), run_id=run_id
            )
        assert exc.value.code == "not_found"


def edit_lines(engine, where, values):
    """Simulate imported data: payroll evidence is append-only, so lift its trigger for this edit only."""
    table = "mod_payroll.payroll_calculation_lines"
    with Session(engine) as db:
        db.execute(sa.text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
        db.execute(sa.update(PayrollCalculationLine).where(*where).values(values))
        db.execute(sa.text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
        db.commit()


def blank_mapping(engine, column, component_code="PAYE", value=None):
    edit_lines(engine, [PayrollCalculationLine.component_code == component_code], {column: value})


@pytest.mark.parametrize(
    ("component_code", "column"),
    [("PAYE", "liability_account_ref"), ("BASIC", "expense_account_ref"), ("PENSION_ER", "expense_account_ref")],
)
def test_finalize_refuses_when_any_component_has_no_mapping(engine, component_code, column):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    blank_mapping(engine, column, component_code)  # e.g. imported data with the mapping stripped
    err = refusal(engine, env, svc.finalize, run_id, "missing_account_mapping", 2)
    assert component_code in str(err)
    assert status(engine, run_id) == "approved"


def test_approve_refuses_an_unmapped_component_too(engine):
    env = world(engine)
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    blank_mapping(engine, "liability_account_ref")
    refusal(engine, env, svc.approve, run_id, "missing_account_mapping", 1)


def test_mapping_to_an_unknown_or_unusable_account_refuses(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    blank_mapping(engine, "liability_account_ref", value=str(uuid4()))
    refusal(engine, env, svc.finalize, run_id, "mapping_account_unknown", 2)
    blank_mapping(engine, "liability_account_ref", value=env.accounts["2110"].hex)
    refusal(engine, env, svc.finalize, run_id, "mapping_ref_form_refused", 2)
    blank_mapping(engine, "liability_account_ref", value=str(env.accounts["2110"]))
    with Session(engine) as db:
        db.execute(sa.update(Account).where(Account.id == env.accounts["2110"]).values(is_active=False))
        db.commit()
    refusal(engine, env, svc.finalize, run_id, "mapping_account_unusable", 2)


def test_external_deduction_mapped_to_an_asset_account_is_refused_and_creates_no_payable(engine):
    env = world(engine, paye_liability="1410")  # asset account but the rule is still external
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    err = refusal(engine, env, svc.approve, run_id, "mapping_account_class_refused", 1)
    assert "internal_recovery" in str(err)
    assert counts(engine, env.tenant_id)["payroll_liabilities"] == 0


def test_internal_recovery_maps_to_asset_books_as_recovered_and_is_never_a_payable(engine):
    env = world(engine, paye_liability="1410", paye_mode="internal_recovery")
    run_id = ready_to_finalize(engine, env)
    out = finalize(engine, env, run_id)
    with Session(engine) as db:
        loan = db.scalars(
            sa.select(PayrollLiability).where(
                PayrollLiability.run_id == run_id, PayrollLiability.settlement_mode == "internal_recovery"
            )
        ).all()
        assert len(loan) == 2  # E1 and E2 PAYE recoveries
        assert all(
            row.status == "settled" and row.amount_settled == row.amount and row.beneficiary_type == "employee"
            for row in loan
        )
        outstanding = db.scalars(
            sa.select(PayrollLiability).where(
                PayrollLiability.run_id == run_id, PayrollLiability.status == "outstanding"
            )
        ).all()
        assert outstanding and all(row.settlement_mode == "external" for row in outstanding)
        lines = {
            db.get(Account, r.account_id).code: (r.debit, r.credit)
            for r in db.scalars(sa.select(PostedLedgerLine).where(PostedLedgerLine.journal_id == out.journal_id))
        }
        assert lines["1410"] == (0, Decimal("15000.00"))  # Cr staff loan asset, not a liability
        assert "2110" not in lines  # no PAYE payable
        assert lines["2040"] == (0, Decimal("195000.00"))
        assert sum(d for d, _ in lines.values()) == sum(c for _, c in lines.values())


@pytest.mark.parametrize("account", ["2110", "1300"])  # a liability account; an undeclared asset account
def test_internal_recovery_only_credits_a_declared_recovery_account(engine, account):
    env = world(engine, paye_liability=account, paye_mode="internal_recovery")
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    err = refusal(engine, env, svc.approve, run_id, "internal_recovery_account_not_allowed", 1)
    assert "1410" in str(err)
    assert counts(engine, env.tenant_id)["payroll_liabilities"] == 0


def test_a_deduction_mapped_to_2040_is_refused_so_2040_holds_net_pay_only(engine):
    env = world(engine, paye_liability="2040")
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    refusal(engine, env, svc.approve, run_id, "deduction_mapped_to_net_payable", 1)


def test_net_payable_account_other_than_2040_is_refused(engine):
    env = world(engine, net_ref="2120")
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    refusal(engine, env, svc.approve, run_id, "net_payable_not_2040", 1)


def test_closed_period_refuses_finalize_and_rolls_back_liabilities_and_ledger(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    soft_close(engine, env)
    refusal(engine, env, svc.finalize, run_id, "posting_period_closed", 2)
    assert status(engine, run_id) == "approved"
    assert counts(engine, env.tenant_id)["payroll_liabilities"] == 0


def test_roster_and_evidence_are_rechecked_at_approval(engine):
    env = world(engine)
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    with Session(engine) as db:  # a new active employee appears after the batch
        payroll.assign_employee_pay_structure(
            db,
            tenant_id=env.tenant_id,
            employee_ref="E4",
            revision_id=env.fixed_revision_id,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            source_ref="test",
            source_version="1",
        )
        db.commit()
    err = refusal(engine, env, svc.approve, run_id, "roster_incomplete", 1)
    assert "E4" in str(err)
    assert batch(engine, env, run_id, data=None).calculated == 1  # the batch picks up exactly the missing one
    edit_lines(
        engine,
        [PayrollCalculationLine.component_code == "BASIC", PayrollCalculationLine.amount == 100000],
        {"evidence_ref": " "},
    )
    refusal(engine, env, svc.approve, run_id, "evidence_incomplete", 1)


def test_finalized_run_without_a_journal_is_referred_not_repaired(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    with Session(engine) as db:
        db.execute(sa.update(PayrollRun).where(PayrollRun.id == run_id).values(status="finalized"))
        db.commit()
    refusal(engine, env, svc.finalize, run_id, "finalized_without_ledger_entry", 2)
    assert counts(engine, env.tenant_id)["journal_entries"] == 0


def test_posted_journal_identity_is_the_payroll_consequence_source(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    out = finalize(engine, env, run_id)
    with Session(engine) as db:
        journal = db.get(JournalEntry, out.journal_id)
        assert (journal.source_owner, journal.source_document_kind, journal.source_version) == (
            "payroll",
            "payroll_run",
            "1",
        )
        assert journal.posting_date == date(2026, 1, 31) and journal.currency_code == "NGN"
        assert isinstance(journal.id, UUID) and datetime.now(UTC) > journal.created_at


def test_only_the_run_creator_may_batch_calculate_so_the_uploader_cannot_approve(engine):
    env = world(engine)
    run_id = new_run(engine, env)
    refusal(engine, env, svc.batch_calculate, run_id, "sod_not_run_creator", 3, input_bytes=GOOD_FILE)  # not creator
    refusal(engine, env, svc.batch_calculate, run_id, "sod_not_run_creator", 1, input_bytes=GOOD_FILE)  # the approver
    assert batch(engine, env, run_id).calculated == 3  # the creator can


def test_mapping_classes_are_an_allowlist_liability_for_payables_expense_for_expenses(engine):
    # a deduction mapped to an EXPENSE account, and (separately) an earning mapped to a LIABILITY account
    env = world(engine, paye_liability="5100")
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    err = refusal(engine, env, svc.approve, run_id, "mapping_account_class_refused", 1)
    assert "LIABILITY-class" in str(err)
    env = world(engine)
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    blank_mapping(engine, "expense_account_ref", "BASIC", str(env.accounts["2110"]))
    err = refusal(engine, env, svc.approve, run_id, "mapping_account_class_refused", 1)
    assert "EXPENSE-class" in str(err)


def test_mid_period_joiner_needs_the_single_employee_form_for_proration(engine):
    env = world(engine)
    run_id = new_run(engine, env)
    with Session(engine) as db:
        payroll.assign_employee_pay_structure(
            db,
            tenant_id=env.tenant_id,
            employee_ref="E5",
            revision_id=env.fixed_revision_id,
            effective_from=date(2026, 1, 15),
            effective_to=None,
            source_ref="test",
            source_version="1",
        )
        db.commit()
    err = refusal(engine, env, svc.batch_calculate, run_id, "proration_required", 0, input_bytes=GOOD_FILE)
    assert "E5" in str(err)
    with Session(engine) as db:  # once calculated by the single-employee path, the batch skips them
        payroll.calculate_employee_payroll(
            db,
            tenant_id=env.tenant_id,
            run_id=run_id,
            employee_ref="E5",
            proration_factor=Decimal("0.5"),
            inputs=(),
            calculated_at=NOW,
        )
        db.commit()
    out = batch(engine, env, run_id)
    assert out.calculated == 3 and out.skipped_existing == 1


def _leaver(
    engine, env, *, employee="E6", effective_to=date(2026, 1, 20), effective_from=date(2026, 1, 1), revision_id=None
):
    with Session(engine) as db:
        payroll.assign_employee_pay_structure(
            db,
            tenant_id=env.tenant_id,
            employee_ref=employee,
            revision_id=revision_id or env.fixed_revision_id,
            effective_from=effective_from,
            effective_to=effective_to,
            source_ref="test",
            source_version="1",
        )
        db.commit()


def test_mid_period_leaver_refuses_in_batch_and_is_part_of_the_roster(engine):
    env = world(engine)
    _leaver(engine, env)
    run_id = new_run(engine, env)
    err = refusal(engine, env, svc.batch_calculate, run_id, "proration_required", 0, input_bytes=GOOD_FILE)
    assert "E6" in str(err) and "left before the period end" in str(err)
    with Session(engine) as db:  # the single-employee form calculates the leaver at its end date
        payroll.calculate_employee_payroll(
            db,
            tenant_id=env.tenant_id,
            run_id=run_id,
            employee_ref="E6",
            proration_factor=Decimal("0.6"),
            inputs=(),
            calculated_at=NOW,
        )
        db.commit()
    out = batch(engine, env, run_id)
    assert out.calculated == 3 and out.skipped_existing == 1
    approve(engine, env, run_id)  # the leaver's calculation is not "without an active assignment"
    assert status(engine, run_id) == "approved"


def test_uncalculated_leaver_blocks_approval_and_earlier_leavers_are_ignored(engine):
    env = world(engine)
    _leaver(
        engine, env, employee="E7", effective_from=date(2025, 6, 1), effective_to=date(2025, 12, 31)
    )  # left before the period
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    approve(engine, env, run_id)  # E7 ended before the period: not in the roster
    env2 = world(engine)
    run2 = new_run(engine, env2)
    batch(engine, env2, run2)
    _leaver(engine, env2)  # a leaver recorded after the batch must still be calculated before approval
    err = refusal(engine, env2, svc.approve, run2, "roster_incomplete", 1)
    assert "E6" in str(err)


def cancel(engine, env, run_id, party_index=0, reason="entered against the wrong period", **kw):
    return run_command(engine, env, svc.cancel_run, run_id, party_index, reason=reason, **kw)


def test_cancel_a_draft_run_records_actor_reason_and_audit_and_frees_the_period(engine):
    env = world(engine)
    run_id = new_run(engine, env)
    batch(engine, env, run_id)
    out = cancel(engine, env, run_id, party_index=3)
    assert out.replayed is False and status(engine, run_id) == "cancelled"
    with Session(engine) as db:
        run = db.get(PayrollRun, run_id)
        assert run.cancelled_by_id == env.party_ids[3] and run.cancel_reason == "entered against the wrong period"
    (event,) = audits(engine, env.tenant_id, svc.AUDIT_CANCELLED)
    assert event.details["previous_status"] == "draft" and event.actor_id == str(env.party_ids[3])
    assert cancel(engine, env, run_id, party_index=3).replayed is True  # same actor and reason
    assert len(audits(engine, env.tenant_id, svc.AUDIT_CANCELLED)) == 1
    new_run(engine, env, ref="RUN-2026-01B")  # the cancelled run no longer blocks the period


def test_cancel_an_approved_run_then_nothing_can_finalize_it(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    cancel(engine, env, run_id, party_index=1)
    assert status(engine, run_id) == "cancelled"
    refusal(engine, env, svc.finalize, run_id, "not_approved", 2)
    assert counts(engine, env.tenant_id)["journal_entries"] == 0


@pytest.mark.parametrize("reason", ["", "   "])
def test_cancel_requires_a_reason(engine, reason):
    env = world(engine)
    run_id = new_run(engine, env)
    before = status(engine, run_id)
    with pytest.raises(svc.RunRefused) as exc:
        cancel(engine, env, run_id, reason=reason)
    assert exc.value.code == "reason_required" and status(engine, run_id) == before == "draft"


def test_a_finalized_run_cannot_be_cancelled_and_cancel_by_another_actor_conflicts(engine):
    env = world(engine)
    run_id = ready_to_finalize(engine, env)
    finalize(engine, env, run_id)
    with pytest.raises(svc.RunRefused) as exc:
        cancel(engine, env, run_id)
    assert exc.value.code == "not_cancellable" and status(engine, run_id) == "finalized"
    other = new_run(engine, env, ref="RUN-2026-02", start=date(2026, 2, 1), end=date(2026, 2, 28))
    cancel(engine, env, other, party_index=3)
    with pytest.raises(svc.RunRefused) as exc:
        cancel(engine, env, other, party_index=1)
    assert exc.value.code == "not_cancellable"


def test_leaver_on_an_input_structure_goes_through_the_erp_form_batch_approve_and_finalize(engine):
    from erp_v2.web import payroll_actions as actions

    env = world(engine)
    _leaver(engine, env, revision_id=env.revision_id)  # E6 has an input rule (BASIC) and left on 20 Jan
    run_id = new_run(engine, env)
    refusal(engine, env, svc.batch_calculate, run_id, "proration_required", 0, input_bytes=GOOD_FILE)
    with Session(engine) as db:  # the ERP single-employee form resolves input components at the owner's date
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.party_ids[0])
        calculation = actions.calculate_employee(
            db,
            tenant=tenant,
            party=party,
            run_id=run_id,
            employee_ref="E6",
            proration_factor="0.60000000",
            component_inputs="BASIC | 60000.00 | hr:leaver-E6",
        )
        assert calculation.employee_ref == "E6"
        db.commit()
    out = batch(engine, env, run_id)
    assert out.calculated == 3 and out.skipped_existing == 1
    approve(engine, env, run_id)
    finalize(engine, env, run_id)
    assert status(engine, run_id) == "finalized"
