"""Cash GL split on real PostgreSQL (hosted CI): the per-bank account creator and the governed re-point.

Real migrations, real accounting a2 create_account, real B7 reconciliation, real B3/B5 tables, the
runtime NOBYPASSRLS role. Nothing here posts a journal or moves a balance.
"""

import hashlib
import json
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting.contracts import AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory
from dotmac_banking.models import BankAccount
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dotmac_payables.contracts import SupplierPaymentStatus
from dotmac_payables.models import SupplierPayment
from dual_control_support import set_dual_control_engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_bank_recon_pg import (  # noqa: F401  (hosted-Postgres fixtures and seed helpers)
    _rt,
    _scoped,
    runtime_engine,
    seed,
)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.banking import recon_service as recon_svc
from erp_v2.banking.recon_routes import banking_recon_feature
from erp_v2.banking.recon_service import ReconRefused
from erp_v2.cash_gl import service as svc
from erp_v2.cash_gl import setup_cli as setup
from erp_v2.cash_gl.models import CashAccountRepoint
from erp_v2.cash_gl.routes import cash_gl_feature
from erp_v2.journals.routes import journals_web_feature
from erp_v2.payroll_payouts import models as payout
from erp_v2.supplier_payments.service import payer_references

NOW = datetime(2026, 2, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


@pytest.fixture(autouse=True, scope="module")
def _registries():
    manifests = [cash_gl_feature, banking_recon_feature, journals_web_feature, FeatureManifest(name="kernel_stub")]
    install_audit_actions(AuditActionRegistry.from_manifests(manifests))
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


def run(engine, fn, **kwargs):
    with Session(engine, expire_on_commit=False) as db:
        result = fn(db, now=NOW, **kwargs)
        db.commit()
        return result


def make_account(
    engine, env, code, *, klass="AST", kind=AccountKind.POSTING, postable=True, active=True, currency=None
):
    with Session(engine) as db:
        category = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == klass)
        )
        row = Account(
            tenant_id=env.tenant_id,
            category_id=category.id,
            code=code,
            name=f"Account {code}",
            kind=kind,
            normal_balance=NormalBalance.DEBIT if klass == "AST" else NormalBalance.CREDIT,
            posting_allowed=postable,
            is_active=active,
            currency_code=currency,
        )
        db.add(row)
        db.flush()
        new_id = row.id
        db.commit()
        return new_id


def add_bank(engine, env, code, ref) -> BankAccount:
    with Session(engine, expire_on_commit=False) as db:
        row = BankAccount(
            id=uuid4(),
            tenant_id=env.tenant_id,
            institution_id=env.institution_id,
            account_code=code,
            account_name=code,
            account_identifier=f"id-{code}",
            account_type_code="current",
            currency_code="NGN",
            minor_units=2,
            cash_account_ref=ref,
            status="active",
        )
        db.add(row)
        db.commit()
        return row


def bank_ref(engine, env, bank_id) -> str:
    with Session(engine) as db:
        return db.scalar(sa.select(BankAccount.cash_account_ref).where(BankAccount.id == bank_id))


def shared_env(engine):
    """Two bank accounts on one cash GL (by id and by code), no baseline: the problem being fixed."""
    env = seed(engine, baseline=False)
    env.twin = add_bank(engine, env, "OP2", "1000")
    env.new_a = make_account(engine, env, "1000-A")
    env.new_b = make_account(engine, env, "1000-B")
    return env


def propose(engine, env, bank_id, target, who="opener", evidence="Finance minute 2026-11-16"):
    return run(
        engine,
        svc.propose,
        tenant_id=env.tenant_id,
        proposer_party_id=env.parties[who],
        bank_account_id=bank_id,
        target_account_id=target,
        evidence_ref=evidence,
    )


def approve(engine, env, request_id, who="approver"):
    return run(engine, svc.approve, tenant_id=env.tenant_id, request_id=request_id, approver_party_id=env.parties[who])


def refused(code, fn, *args, **kwargs):
    with pytest.raises(svc.RepointRefused) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code, exc.value.code


# --- (1) per-bank cash GL account creator ---------------------------------------------------------


def spec_for(env, category, *accounts):
    return setup.SetupSpec(
        env.tenant_id,
        "db",
        tuple(setup.CashAccountSpec(bank_id, code, f"Cash {code}", category, None, None) for bank_id, code in accounts),
    )


def category_id(engine, env, code="AST"):
    with Session(engine) as db:
        return db.scalar(
            sa.select(AccountCategory.id).where(
                AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == code
            )
        )


def count_accounts(engine, env):
    with Session(engine) as db:
        return db.scalar(sa.select(sa.func.count()).select_from(Account).where(Account.tenant_id == env.tenant_id))


def test_setup_plan_writes_nothing_apply_creates_and_is_idempotent(engine):
    env = shared_env(engine)
    cat = category_id(engine, env)
    spec = spec_for(env, cat, (env.bank_id, "1210-01"), (env.twin.id, "1210-02"))
    before = count_accounts(engine, env)
    with Session(engine) as db:
        plan = setup.plan_accounts(db, spec)
        db.rollback()
    assert [p["action"] for p in plan] == ["create", "create"] and count_accounts(engine, env) == before
    with Session(engine) as db:
        first = setup.apply_accounts(db, spec)
        db.commit()
    assert count_accounts(engine, env) == before + 2
    with Session(engine) as db:
        again = setup.apply_accounts(db, spec)
        db.commit()
    assert [p["action"] for p in again] == ["reuse", "reuse"] and count_accounts(engine, env) == before + 2
    assert [p["account_id"] for p in again] == [p["account_id"] for p in first]
    with Session(engine) as db:
        row = db.scalar(sa.select(Account).where(Account.tenant_id == env.tenant_id, Account.code == "1210-01"))
        assert row.kind == AccountKind.POSTING and row.posting_allowed and row.normal_balance == NormalBalance.DEBIT
    # creating accounts never re-points a bank account
    assert bank_ref(engine, env, env.bank_id) == str(env.accounts["1000"])


def test_setup_refuses_conflicts(engine):
    env = shared_env(engine)
    cat = category_id(engine, env)
    liability = category_id(engine, env, "REV")
    existing = make_account(engine, env, "1210-X")

    def plan(spec):
        with Session(engine) as db:
            try:
                return setup.plan_accounts(db, spec)
            finally:
                db.rollback()

    # existing code with a different name is a conflict, not a silent reuse
    with pytest.raises(setup.OperatorRefusal, match="existing_account_conflict"):
        plan(
            setup.SetupSpec(
                env.tenant_id, "db", (setup.CashAccountSpec(env.bank_id, "1210-X", "Other", cat, None, None),)
            )
        )
    with pytest.raises(setup.OperatorRefusal, match="category_conflict"):
        plan(
            setup.SetupSpec(
                env.tenant_id, "db", (setup.CashAccountSpec(env.bank_id, "1210-N", "n", liability, None, None),)
            )
        )
    with pytest.raises(setup.OperatorRefusal, match="bank_account_unknown_or_closed"):
        plan(spec_for(env, cat, (uuid4(), "1210-N")))
    with pytest.raises(setup.OperatorRefusal, match="currency_conflict"):
        plan(
            setup.SetupSpec(env.tenant_id, "db", (setup.CashAccountSpec(env.bank_id, "1210-N", "n", cat, None, "USD"),))
        )
    with pytest.raises(setup.OperatorRefusal, match="parent_conflict"):  # a posting account cannot be a parent
        plan(
            setup.SetupSpec(
                env.tenant_id,
                "db",
                (setup.CashAccountSpec(env.bank_id, "1210-N", "n", cat, env.accounts["1010"], None),),
            )
        )
    # an existing account (matching spec) that already serves ANOTHER bank account is a shared GL
    with Session(engine) as db:
        db.execute(sa.update(BankAccount).where(BankAccount.id == env.twin.id).values(cash_account_ref=str(existing)))
        db.commit()
    with pytest.raises(setup.OperatorRefusal, match="account_used_by_another_bank_account"):
        plan(
            setup.SetupSpec(
                env.tenant_id, "db", (setup.CashAccountSpec(env.bank_id, "1210-X", "Account 1210-X", cat, None, None),)
            )
        )


def test_setup_spec_parsing_and_operator_gates(tmp_path, monkeypatch):
    tenant, bank, cat = uuid4(), uuid4(), uuid4()
    good = {
        "version": 1,
        "tenant_id": str(tenant),
        "destination_database": "reviewed",
        "accounts": [
            dict(
                bank_account_id=str(bank),
                code="1210-01",
                name="Cash",
                category_id=str(cat),
                parent_id=None,
                currency_code=None,
            )
        ],
    }
    assert setup.parse_spec(json.dumps(good).encode()).accounts[0].code == "1210-01"
    with pytest.raises(setup.OperatorRefusal, match="duplicate_json_key"):
        setup.parse_spec(b'{"version":1,"version":1}')
    bad = json.loads(json.dumps(good))
    bad["accounts"] = bad["accounts"] * 2
    with pytest.raises(setup.OperatorRefusal, match="duplicate_account_code"):
        setup.parse_spec(json.dumps(bad).encode())
    bad["accounts"] = []
    with pytest.raises(setup.OperatorRefusal, match="account_count_out_of_range"):
        setup.parse_spec(json.dumps(bad).encode())
    path = tmp_path / "spec.json"
    raw = json.dumps(good).encode()
    path.write_bytes(raw)
    args = SimpleNamespace(spec=path, apply=True, approved_spec_sha256="wrong")
    monkeypatch.setenv("ERP_CASH_GL_SETUP_APPROVED", "1")
    with pytest.raises(setup.OperatorRefusal, match="reviewed_account_apply_required"):
        setup._run(args)
    args.approved_spec_sha256 = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("DATABASE_URL", "forbidden")
    with pytest.raises(setup.OperatorRefusal, match="serving_or_source_database_materials_forbidden"):
        setup._run(args)


# --- (2) governed re-point ------------------------------------------------------------------------


def test_sod_proposer_cannot_approve_and_db_enforces_it(engine):
    env = shared_env(engine)
    req = propose(engine, env, env.twin.id, env.new_b)
    refused(
        "same_person",
        run,
        engine,
        svc.approve,
        tenant_id=env.tenant_id,
        request_id=req.id,
        approver_party_id=env.parties["opener"],
    ) if False else None
    with Session(engine) as db:
        with pytest.raises(svc.RepointRefused) as exc:
            svc.approve(db, tenant_id=env.tenant_id, request_id=req.id, approver_party_id=env.parties["opener"])
        assert exc.value.code == "same_person"
        db.rollback()
    assert bank_ref(engine, env, env.twin.id) == "1000"  # nothing changed
    # the CHECK constraint refuses a one-person approval even by direct write
    with Session(engine) as db:
        with pytest.raises(IntegrityError):
            db.execute(
                sa.update(CashAccountRepoint)
                .where(CashAccountRepoint.id == req.id)
                .values(
                    status="approved",
                    decided_by_party_id=env.parties["opener"],
                    decided_at=NOW,
                    applied_ref=str(env.new_b),
                )
            )
        db.rollback()
    approve(engine, env, req.id)
    assert bank_ref(engine, env, env.twin.id) == str(env.new_b)


def test_inactive_actor_refused(engine):
    env = shared_env(engine)
    refused(
        "actor_invalid",
        run,
        engine,
        svc.propose,
        tenant_id=env.tenant_id,
        proposer_party_id=uuid4(),
        bank_account_id=env.twin.id,
        target_account_id=env.new_b,
        evidence_ref="x",
    )


def test_target_account_checks(engine):
    env = shared_env(engine)
    cases = {
        "target_unknown": uuid4(),
        "target_not_asset": make_account(engine, env, "4100", klass="REV"),
        "target_not_postable": make_account(engine, env, "1100", kind=AccountKind.CONTROL, postable=False),
        "target_inactive": make_account(engine, env, "1101", active=False),
        "target_currency": make_account(engine, env, "1102", currency="USD"),
        "already_a_cash_gl": env.new_a,  # becomes another bank account's cash GL below
    }
    approve(engine, env, propose(engine, env, env.bank_id, env.new_a).id)
    for code, target in cases.items():
        refused(code, propose, engine, env, env.twin.id, target)
    # a bank account may not be re-pointed to the account it already uses
    refused("unchanged", propose, engine, env, env.bank_id, env.new_a)


def test_target_with_any_journal_line_is_refused_draft_or_posted(engine):
    from decimal import Decimal as D

    from dotmac_accounting import service as accounting
    from dotmac_accounting.contracts import CreateJournal, JournalKind, JournalLineInput, SourceIdentity
    from dotmac_kernel.cache import TenantScope
    from test_bank_recon_pg import post

    env = shared_env(engine)
    env.accounts["NEWA"], env.accounts["NEWB"] = env.new_a, env.new_b
    post(engine, env, debit_code="NEWA", credit_code="4000", amount="5", day=date(2026, 1, 10))
    refused("target_has_activity", propose, engine, env, env.twin.id, env.new_a)
    with Session(engine) as db:  # a DRAFT journal line also counts
        accounting.create_journal(
            db,
            scope=TenantScope(env.tenant_id),
            command=CreateJournal(
                number=f"D-{uuid4().hex[:8]}",
                kind=JournalKind.STANDARD,
                entry_date=date(2026, 1, 11),
                posting_date=date(2026, 1, 11),
                currency_code="NGN",
                exchange_rate=D(1),
                description="draft",
                reference=None,
                source=SourceIdentity("test", "journal", f"d-{uuid4().hex}", "1", "c" * 64),
                lines=(
                    JournalLineInput(env.new_b, debit=D("5")),
                    JournalLineInput(env.accounts["4000"], credit=D("5")),
                ),
            ),
            idempotency_key=f"test:{uuid4()}",
            idempotency_expires_at=None,
            recorded_at=NOW,
        )
        db.commit()
    refused("target_has_activity", propose, engine, env, env.twin.id, env.new_b)
    # activity arriving between proposal and approval is refused at approval too
    other = shared_env(engine)
    other.accounts["NEWB"] = other.new_b
    req = propose(engine, other, other.twin.id, other.new_b)
    post(engine, other, debit_code="NEWB", credit_code="4000", amount="5", day=date(2026, 1, 10))
    refused("target_has_activity", approve, engine, other, req.id)
    assert bank_ref(engine, other, other.twin.id) == "1000"


def test_target_must_be_debit_asset_only_when_class_asset(engine):
    env = shared_env(engine)
    # a LIABILITY account with a debit side is still not an ASSET
    refused("target_not_asset", propose, engine, env, env.twin.id, make_account(engine, env, "2100", klass="REV"))


def test_refused_after_baseline_and_with_reconciliation_in_flight(engine):
    env = seed(engine, baseline=False)  # one bank, its own GL
    spare = make_account(engine, env, "1000-A")
    anchor = None
    from test_bank_recon_pg import JAN, statement

    anchor = statement(engine, env, closing="0.00", start=date(2025, 12, 1), end=date(2025, 12, 31), ref="BASELINE")
    recon = run(
        engine,
        recon_svc.open_reconciliation,
        tenant_id=env.tenant_id,
        actor=env.parties["opener"],
        account_id=env.bank_id,
        statement_id=anchor,
        baseline_date=date(2025, 12, 31),
    )
    refused("reconciliation_in_flight", propose, engine, env, env.bank_id, spare)
    run(engine, recon_svc.prepare, tenant_id=env.tenant_id, actor=env.parties["preparer"], reconciliation_id=recon.id)
    run(engine, recon_svc.approve, tenant_id=env.tenant_id, actor=env.parties["approver"], reconciliation_id=recon.id)
    refused("baseline_approved", propose, engine, env, env.bank_id, spare)
    assert JAN and bank_ref(engine, env, env.bank_id) == str(env.accounts["1000"])


def test_baseline_approved_after_proposal_is_refused_at_approval(engine):
    env = shared_env(engine)
    req = propose(engine, env, env.twin.id, env.new_b)
    # an approved reconciliation appears for the twin between proposal and approval
    from test_bank_recon_pg import statement

    anchor = statement(
        engine,
        env,
        closing="0.00",
        start=date(2025, 12, 1),
        end=date(2025, 12, 31),
        ref="BASELINE",
        account_id=env.twin.id,
    )
    with Session(engine) as db:
        db.execute(
            sa.text(
                "INSERT INTO erp_bank_reconciliations (id, tenant_id, account_id, statement_id, gl_account_id, "
                "period_start, period_end, currency_code, status, kind, opened_by_party_id, opened_at, "
                "created_at, updated_at, statement_closing_balance, gl_balance, items_total, difference, "
                "snapshot_sha256, prepared_by_party_id, prepared_at, approved_by_party_id, approved_at) VALUES "
                "(:id, :t, :a, :s, :g, '2025-12-01', '2025-12-31', 'NGN', 'approved', 'baseline', :o, now(), now(), "
                "now(), 0, 0, 0, 0, :h, :p, now(), :ap, now())"
            ),
            {
                "id": uuid4(),
                "t": env.tenant_id,
                "a": env.twin.id,
                "s": anchor,
                "g": env.accounts["1000"],
                "o": env.parties["opener"],
                "h": "a" * 64,
                "p": env.parties["preparer"],
                "ap": env.parties["approver"],
            },
        )
        db.commit()
    refused("baseline_approved", approve, engine, env, req.id)
    assert bank_ref(engine, env, env.twin.id) == "1000"


def in_flight_payment(engine, env, bank_id, status):
    with Session(engine) as db:
        db.add(
            SupplierPayment(
                tenant_id=env.tenant_id,
                number=f"SP-{uuid4().hex[:8]}",
                supplier_ref="S1",
                supplier_name_snapshot="Supplier",
                currency_code="NGN",
                exchange_rate=Decimal(1),
                value_date=date(2026, 1, 15),
                cash_account_ref=str(env.accounts["1000"]),
                payer_instrument_ref=str(bank_id),
                gross_amount=Decimal("10.00"),
                wht_amount=Decimal(0),
                net_amount=Decimal("10.00"),
                request_fingerprint="f" * 64,
                status=status,
                prepared_by="preparer",
                prepared_at=NOW,
            )
        )
        db.commit()


def test_unposted_supplier_payment_blocks_and_recorded_does_not(engine):
    env = shared_env(engine)
    in_flight_payment(engine, env, env.twin.id, SupplierPaymentStatus.DRAFT)
    refused("payments_in_flight", propose, engine, env, env.twin.id, env.new_b)
    # another bank account's draft does not block this one
    other = shared_env(engine)
    in_flight_payment(engine, other, other.bank_id, SupplierPaymentStatus.DRAFT)
    assert propose(engine, other, other.twin.id, other.new_b).status == "proposed"


def test_payment_appearing_between_proposal_and_approval_is_refused(engine):
    env = shared_env(engine)
    req = propose(engine, env, env.twin.id, env.new_b)
    in_flight_payment(engine, env, env.twin.id, SupplierPaymentStatus.DRAFT)
    refused("payments_in_flight", approve, engine, env, req.id)
    assert bank_ref(engine, env, env.twin.id) == "1000"


def test_unposted_payout_batch_blocks(engine):
    env = shared_env(engine)
    with Session(engine) as db:
        db.add(
            payout.PayrollPayoutBatch(
                tenant_id=env.tenant_id,
                request_id=uuid4(),
                number=f"PB-{uuid4().hex[:8]}",
                run_id=uuid4(),
                kind=payout.KIND_NET_PAY,
                status=payout.APPROVED,
                bank_account_id=env.twin.id,
                currency_code="NGN",
                value_date=date(2026, 1, 31),
                total_amount=Decimal("10.00"),
                line_count=1,
                prepared_by_party_id=env.parties["opener"],
                prepared_at=NOW,
                approved_by_party_id=env.parties["approver"],
                approved_at=NOW,
            )
        )
        db.commit()
    refused("payments_in_flight", propose, engine, env, env.twin.id, env.new_b)


def test_approve_repoints_audits_is_idempotent_and_payers_pick_up_the_new_ref(engine):
    env = shared_env(engine)
    req = propose(engine, env, env.twin.id, env.new_b)
    refused("open_request_exists", propose, engine, env, env.twin.id, env.new_a)
    with Session(engine) as db:  # the old code-form ref was never a canonical id: B3 refused it
        from erp_v2.supplier_payments.service import PaymentRefused

        with pytest.raises(PaymentRefused):
            payer_references(db, tenant_id=env.tenant_id, bank_account_id=env.twin.id, currency="NGN")
    done = approve(engine, env, req.id)
    assert done.status == "approved" and done.applied_ref == str(env.new_b)
    assert bank_ref(engine, env, env.twin.id) == str(env.new_b)
    with Session(engine) as db:
        cash, payer = payer_references(db, tenant_id=env.tenant_id, bank_account_id=env.twin.id, currency="NGN")
        assert (cash, payer) == (str(env.new_b), str(env.twin.id))
        events = {
            e.action
            for e in db.scalars(
                sa.select(AuditEvent).where(AuditEvent.tenant_id == env.tenant_id, AuditEvent.entity_id == str(req.id))
            )
        }
    assert events == {svc.AUDIT_PROPOSED, svc.AUDIT_APPROVED}
    # replays and repeats are refused, never doubled
    refused("wrong_state", approve, engine, env, req.id, "approver2")
    refused("unchanged", propose, engine, env, env.twin.id, env.new_b)
    refused("already_a_cash_gl", propose, engine, env, env.bank_id, env.new_b)
    # the sibling bank account is untouched and its own re-point is a separate request
    assert bank_ref(engine, env, env.bank_id) == str(env.accounts["1000"])


def test_decided_requests_are_immutable_by_trigger(engine):
    env = shared_env(engine)
    req = propose(engine, env, env.twin.id, env.new_b)
    approve(engine, env, req.id)
    rejected = propose(engine, env, env.bank_id, env.new_a)
    run(
        engine,
        svc.reject,
        tenant_id=env.tenant_id,
        request_id=rejected.id,
        decider_party_id=env.parties["approver"],
        reason="no",
    )
    for row in (req, rejected):
        for column in ("decision_note", "status"):
            with Session(engine) as db:
                with pytest.raises(DBAPIError) as err:
                    db.execute(
                        sa.text(f"UPDATE erp_cash_account_repoints SET {column} = 'proposed' WHERE id = :i"),  # noqa: S608
                        {"i": row.id},
                    )
                assert "immutable" in str(err.value).lower()
                db.rollback()


def test_reject_is_audited_and_changes_nothing(engine):
    env = shared_env(engine)
    req = propose(engine, env, env.twin.id, env.new_b)
    row = run(
        engine,
        svc.reject,
        tenant_id=env.tenant_id,
        request_id=req.id,
        decider_party_id=env.parties["approver"],
        reason="wrong account",
    )
    assert row.status == "rejected" and bank_ref(engine, env, env.twin.id) == "1000"
    refused("wrong_state", approve, engine, env, req.id)
    propose(engine, env, env.twin.id, env.new_b)  # a new proposal is possible


def test_drift_of_the_bank_reference_is_refused(engine):
    env = shared_env(engine)
    req = propose(engine, env, env.twin.id, env.new_b)
    with Session(engine) as db:
        db.execute(sa.update(BankAccount).where(BankAccount.id == env.twin.id).values(cash_account_ref=str(env.new_a)))
        db.commit()
    refused("drift", approve, engine, env, req.id)


def test_b7_shared_cash_gl_refusal_clears_after_repoint_and_half_split_stays_refused(engine):
    env = shared_env(engine)
    from test_bank_recon_pg import statement

    anchor = statement(engine, env, closing="0.00", start=date(2025, 12, 1), end=date(2025, 12, 31), ref="BASELINE")
    open_kwargs = dict(
        tenant_id=env.tenant_id,
        actor=env.parties["opener"],
        account_id=env.bank_id,
        statement_id=anchor,
        baseline_date=date(2025, 12, 31),
    )
    with pytest.raises(ReconRefused) as exc:
        run(engine, recon_svc.open_reconciliation, **open_kwargs)
    assert exc.value.code == "shared_cash_gl"
    approve(engine, env, propose(engine, env, env.twin.id, env.new_b).id)
    # half split: the partner left on the retired shared GL is refused until it is re-pointed too
    with pytest.raises(ReconRefused) as exc:
        run(engine, recon_svc.open_reconciliation, **open_kwargs)
    assert exc.value.code == "retired_cash_gl"
    # the retired account cannot be chosen as a target again
    refused("target_retired", propose, engine, env, env.bank_id, env.accounts["1000"])
    approve(engine, env, propose(engine, env, env.bank_id, env.new_a).id)
    recon = run(engine, recon_svc.open_reconciliation, **open_kwargs)
    assert recon.status == "open"
    with Session(engine) as db:
        twin = db.get(BankAccount, env.twin.id)
        assert recon_svc.resolve_gl_account(db, env.tenant_id, twin) == env.new_b


def test_payer_lookup_and_approval_serialize_on_the_bank_account_row(engine):
    env = shared_env(engine)
    first = propose(engine, env, env.twin.id, env.new_b)
    holder_ready, release, errors = threading.Event(), threading.Event(), []

    def holder():
        with Session(engine) as db:
            from erp_v2.supplier_payments.service import PaymentRefused

            try:  # code-form ref raises AFTER taking the FOR SHARE lock on the bank account row
                payer_references(db, tenant_id=env.tenant_id, bank_account_id=env.twin.id, currency="NGN")
            except PaymentRefused:
                pass
            holder_ready.set()
            release.wait(10)
            db.rollback()

    thread = threading.Thread(target=holder)
    thread.start()
    assert holder_ready.wait(10)
    try:
        with Session(engine) as db:
            db.execute(sa.text("SET LOCAL lock_timeout = '500ms'"))
            with pytest.raises(DBAPIError) as err:
                svc.approve(db, tenant_id=env.tenant_id, request_id=first.id, approver_party_id=env.parties["approver"])
            assert "lock timeout" in str(err.value).lower()
            db.rollback()
    finally:
        release.set()
        thread.join()
    assert not errors
    assert approve(engine, env, first.id).status == "approved"


def test_runtime_role_flow_and_immutability(engine, runtime_engine):  # noqa: F811
    env = shared_env(engine)
    req = _rt(
        runtime_engine,
        env,
        svc.propose,
        proposer_party_id=env.parties["opener"],
        bank_account_id=env.twin.id,
        target_account_id=env.new_b,
        evidence_ref="minute",
    )
    done = _rt(runtime_engine, env, svc.approve, request_id=req.id, approver_party_id=env.parties["approver"])
    assert done.status == "approved" and bank_ref(engine, env, env.twin.id) == str(env.new_b)
    with _scoped(runtime_engine, env.tenant_id) as db:
        for sql in (
            "UPDATE erp_cash_account_repoints SET to_account_id = from_ref::uuid",
            "UPDATE erp_cash_account_repoints SET proposed_by_party_id = decided_by_party_id",
            "UPDATE erp_cash_account_repoints SET evidence_ref = 'x'",
            "DELETE FROM erp_cash_account_repoints",
            "TRUNCATE erp_cash_account_repoints",
        ):
            with pytest.raises(sa.exc.ProgrammingError) as err:
                db.execute(sa.text(sql))
            assert "permission denied" in str(err.value.orig).lower(), sql
            db.rollback()
    other = shared_env(engine)  # tenant isolation: another tenant sees none of these requests
    with _scoped(runtime_engine, other.tenant_id) as db:
        assert db.scalar(sa.text("SELECT count(*) FROM erp_cash_account_repoints")) == 0


# --- dual-control policy (D-9): default OFF ----------------------------------------------------------------------


def test_policy_off_the_proposer_can_approve_the_repoint(engine):
    env = shared_env(engine)
    set_dual_control_engine(engine, env.tenant_id, False)
    req = propose(engine, env, env.twin.id, env.new_b)
    assert req.dual_control is False  # stamped from the tenant policy
    done = approve(engine, env, req.id, who="opener")  # the proposer approves their own request
    assert done.status == "approved" and done.decided_by_party_id == done.proposed_by_party_id
    assert bank_ref(engine, env, env.twin.id) == str(env.new_b)


def test_policy_on_stamps_true_and_the_proposer_is_refused(engine):
    env = shared_env(engine)  # seed has the policy ON
    req = propose(engine, env, env.twin.id, env.new_b)
    assert req.dual_control is True
    with Session(engine) as db:
        with pytest.raises(svc.RepointRefused) as exc:
            svc.approve(db, tenant_id=env.tenant_id, request_id=req.id, approver_party_id=env.parties["opener"])
        assert exc.value.code == "same_person"
        db.rollback()


def _repoint_row(env, *, dual_control, applied_ref):
    return CashAccountRepoint(
        tenant_id=env.tenant_id,
        bank_account_id=env.twin.id,
        from_ref="1000",
        to_account_id=env.new_b,
        to_account_code="1000-B",
        status="approved",
        evidence_ref="minute",
        dual_control=dual_control,
        proposed_by_party_id=env.parties["opener"],
        proposed_at=NOW,
        decided_by_party_id=env.parties["opener"],  # the same person
        decided_at=NOW,
        applied_ref=applied_ref,
    )


def test_check_two_person_is_rewritten_and_its_integrity_term_stays_unconditional(engine):
    env = shared_env(engine)
    with Session(engine) as db:  # on: refused by the rewritten CHECK
        db.add(_repoint_row(env, dual_control=True, applied_ref="x"))
        with pytest.raises(IntegrityError) as err:
            db.flush()
        assert "erp_cash_account_repoints_two_person" in str(err.value)
        db.rollback()
    with Session(engine) as db:  # off: same person accepted
        db.add(_repoint_row(env, dual_control=False, applied_ref="x"))
        db.flush()
        db.rollback()
    with Session(engine) as db:  # off: an approved row still records what it applied
        db.add(_repoint_row(env, dual_control=False, applied_ref=None))
        with pytest.raises(IntegrityError) as err:
            db.flush()
        assert "erp_cash_account_repoints_two_person_integrity" in str(err.value)
        db.rollback()
