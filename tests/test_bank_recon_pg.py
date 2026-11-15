"""B7 bank reconciliation on real PostgreSQL: real migrations, real accounting commands, real triggers.

Hosted CI only (ERP_TEST_POSTGRES_URL), like the other *_pg tests; the owner role bypasses RLS, so
tenant isolation is asserted through explicit tenant filters and per-test tenants. Statements are
inserted as banking-owner rows exactly as B6 leaves them; ledger lines come from real accounting
create/post/reverse commands, so the reconciliation reads the genuine posted ledger.
"""

import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_accounting import service as accounting
from dotmac_accounting.contracts import (
    AccountClass,
    AccountKind,
    CreateJournal,
    JournalKind,
    JournalLineInput,
    NormalBalance,
    PeriodStatus,
    ReverseJournal,
    SourceIdentity,
)
from dotmac_accounting.models import (
    Account,
    AccountCategory,
    FiscalPeriod,
    FiscalYear,
    JournalEntry,
    PostedLedgerLine,
)
from dotmac_banking.models import BankAccount, BankInstitution, BankStatement, BankStatementLine
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, PartyType, Tenant
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2.banking import recon_models as m
from erp_v2.banking import recon_service as svc
from erp_v2.banking.recon_routes import banking_recon_feature
from erp_v2.banking.recon_service import ItemRequest, ReconRefused
from erp_v2.journals import policy
from erp_v2.journals.routes import journals_web_feature

NOW = datetime(2026, 2, 5, tzinfo=UTC)
JAN = date(2026, 1, 31)


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


@pytest.fixture(autouse=True, scope="module")
def _registries():
    manifests = [banking_recon_feature, journals_web_feature, FeatureManifest(name="kernel_stub")]
    install_audit_actions(AuditActionRegistry.from_manifests(manifests))
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


def seed(engine, *, ref_form="uuid", currency="NGN", closing="350.00") -> SimpleNamespace:
    tenant_id = uuid4()
    with Session(engine) as db:
        db.add(Tenant(id=tenant_id, slug=f"t{tenant_id.hex[:24]}", name="Recon"))
        db.flush()
        categories = {
            code: AccountCategory(tenant_id=tenant_id, code=code, name=code, account_class=klass)
            for code, klass in (
                ("AST", AccountClass.ASSET),
                ("REV", AccountClass.REVENUE),
                ("EXP", AccountClass.EXPENSE),
            )
        }
        db.add_all(categories.values())
        db.flush()
        accounts = {}
        for code, category, balance in (
            ("1000", "AST", NormalBalance.DEBIT),
            ("1010", "AST", NormalBalance.DEBIT),
            ("4000", "REV", NormalBalance.CREDIT),
            ("5000", "EXP", NormalBalance.DEBIT),
        ):
            row = Account(
                tenant_id=tenant_id,
                category_id=categories[category].id,
                code=code,
                name=f"Account {code}",
                kind=AccountKind.POSTING,
                normal_balance=balance,
            )
            db.add(row)
            db.flush()
            accounts[code] = row.id
        year = FiscalYear(
            tenant_id=tenant_id, code="FY26", name="FY 2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
        )
        db.add(year)
        db.flush()
        for number, (start, end) in enumerate(((date(2026, 1, 1), JAN), (date(2026, 2, 1), date(2026, 2, 28))), 1):
            period = FiscalPeriod(
                tenant_id=tenant_id,
                fiscal_year_id=year.id,
                period_number=number,
                name=f"P{number}",
                start_date=start,
                end_date=end,
                is_adjustment=False,
                status=PeriodStatus.FUTURE,
            )
            db.add(period)
            db.flush()
            accounting.open_period(
                db, scope=TenantScope(tenant_id), period_id=period.id, actor_ref="seed", occurred_at=NOW
            )
        parties = {}
        for name in ("opener", "preparer", "approver", "approver2", "matcher"):
            party = Party(
                id=uuid4(),
                tenant_id=tenant_id,
                party_type=PartyType.person,
                display_name=name,
                email=f"{name}-{tenant_id.hex[:6]}@acme.test",
            )
            db.add(party)
            db.flush()
            parties[name] = party.id
        policy.set_functional_currency(db, tenant_id=tenant_id, code="NGN", reason="test seed")
        institution = BankInstitution(
            id=uuid4(), tenant_id=tenant_id, code="BK", name="Bank", country_code="NG", status="active"
        )
        db.add(institution)
        db.flush()
        bank = BankAccount(
            id=uuid4(),
            tenant_id=tenant_id,
            institution_id=institution.id,
            account_code="OP1",
            account_name="Operating",
            account_identifier="id-1",
            account_type_code="current",
            currency_code=currency,
            minor_units=2,
            cash_account_ref=str(accounts["1000"]) if ref_form == "uuid" else "1000",
            status="active",
        )
        db.add(bank)
        bank_id, institution_id = bank.id, institution.id
        db.commit()
    env = SimpleNamespace(
        tenant_id=tenant_id, accounts=accounts, parties=parties, bank_id=bank_id, institution_id=institution_id
    )
    env.statement = statement(engine, env, closing=closing)
    return env


def statement(engine, env, *, closing, start=date(2026, 1, 1), end=JAN, ref="JAN", account_id=None) -> UUID:
    with Session(engine) as db:
        row = BankStatement(
            id=uuid4(),
            tenant_id=env.tenant_id,
            account_id=account_id or env.bank_id,
            statement_ref=ref,
            period_start=start,
            period_end=end,
            opening_balance=Decimal(0),
            closing_balance=Decimal(closing) if closing is not None else None,
            total_credits=Decimal(0),
            total_debits=Decimal(0),
            currency_code="NGN",
            minor_units=2,
            status="imported",
            total_lines=0,
            matched_lines=0,
            source_ref=f"src-{ref}-{uuid4().hex[:8]}",
            source_version="1",
            evidence_ref="sha",
            imported_by_id=env.parties["opener"],
            imported_at=NOW,
        )
        db.add(row)
        new_id = row.id
        db.commit()
        return new_id


def bank_line(engine, env, statement_id, number, day, direction, amount) -> UUID:
    with Session(engine) as db:
        row = BankStatementLine(
            id=uuid4(),
            tenant_id=env.tenant_id,
            statement_id=statement_id,
            line_number=number,
            transaction_date=day,
            direction=direction,
            amount=Decimal(amount),
            external_ref=f"ext-{uuid4().hex[:8]}",
            description="Bank narration",
            reference="TRF123456",
            is_matched=False,
        )
        db.add(row)
        new_id = row.id
        db.commit()
        return new_id


_counter = {"n": 0}


def post(engine, env, *, debit_code, credit_code, amount, day, description="Test", reference=None):
    """Post a real journal; return (journal id, ledger line id of the cash GL line, other ledger line)."""
    _counter["n"] += 1
    n = _counter["n"]
    with Session(engine) as db:
        scope = TenantScope(env.tenant_id)
        journal = accounting.create_journal(
            db,
            scope=scope,
            command=CreateJournal(
                number=f"J{n:06d}-{uuid4().hex[:6]}",
                kind=JournalKind.STANDARD,
                entry_date=day,
                posting_date=day,
                currency_code="NGN",
                exchange_rate=Decimal(1),
                description=description,
                reference=reference,
                source=SourceIdentity("test", "journal", f"d{n}-{uuid4().hex}", "1", "c" * 64),
                lines=(
                    JournalLineInput(env.accounts[debit_code], debit=Decimal(amount)),
                    JournalLineInput(env.accounts[credit_code], credit=Decimal(amount)),
                ),
            ),
            idempotency_key=f"test:{uuid4()}",
            idempotency_expires_at=None,
            recorded_at=NOW,
        )
        accounting.post_journal(
            db,
            scope=scope,
            journal_id=journal.id,
            approval_reference="appr",
            posted_by="test",
            idempotency_key=f"test-post:{uuid4()}",
            idempotency_expires_at=None,
            posted_at=NOW,
        )
        db.commit()
        cash = db.scalar(
            sa.select(PostedLedgerLine.id).where(
                PostedLedgerLine.tenant_id == env.tenant_id,
                PostedLedgerLine.journal_id == journal.id,
                PostedLedgerLine.account_id == env.accounts["1000"],
            )
        )
        return journal.id, cash


def reverse(engine, env, journal_id, day):
    with Session(engine) as db:
        reversal = accounting.reverse_journal(
            db,
            scope=TenantScope(env.tenant_id),
            command=ReverseJournal(
                journal_id=journal_id,
                number=f"R-{uuid4().hex[:10]}",
                posting_date=day,
                reason="reverse",
                approval_reference="appr",
                reversed_by="test",
            ),
            idempotency_key=f"rev:{uuid4()}",
            idempotency_expires_at=None,
            reversed_at=NOW,
        )
        db.commit()
        return reversal.id


def run(engine, fn, **kwargs):
    with Session(engine, expire_on_commit=False) as db:
        result = fn(db, now=NOW, **kwargs)
        db.commit()
        return result


def scenario(engine, **kw):
    """Jan: two bank receipts (100, 250) on the statement, a 40 payment posted but not at the bank."""
    env = seed(engine, **kw)
    env.bank = {
        "a": bank_line(engine, env, env.statement, 1, date(2026, 1, 10), "credit", "100.00"),
        "b": bank_line(engine, env, env.statement, 2, date(2026, 1, 20), "credit", "250.00"),
    }
    _, env.gl_a = post(engine, env, debit_code="1000", credit_code="4000", amount="100", day=date(2026, 1, 10))
    _, env.gl_b = post(engine, env, debit_code="1000", credit_code="4000", amount="250", day=date(2026, 1, 21))
    _, env.gl_pay = post(engine, env, debit_code="5000", credit_code="1000", amount="40", day=date(2026, 1, 28))
    return env


def open_recon(engine, env, who="opener"):
    return run(
        engine,
        svc.open_reconciliation,
        tenant_id=env.tenant_id,
        actor=env.parties[who],
        account_id=env.bank_id,
        statement_id=env.statement,
    )


def do_match(engine, env, bank, gl, who="matcher"):
    return run(
        engine,
        svc.match,
        tenant_id=env.tenant_id,
        actor=env.parties[who],
        account_id=env.bank_id,
        bank_line_ids=bank,
        gl_line_ids=gl,
    )


def prepared(engine, env):
    recon = open_recon(engine, env)
    do_match(engine, env, [env.bank["a"]], [env.gl_a])
    do_match(engine, env, [env.bank["b"]], [env.gl_b])
    run(
        engine,
        svc.add_items,
        tenant_id=env.tenant_id,
        actor=env.parties["opener"],
        reconciliation_id=recon.id,
        requests=[ItemRequest(m.SIDE_GL, env.gl_pay, m.ITEM_OUTSTANDING_PAYMENT, "cheque not yet presented")],
    )
    run(engine, svc.prepare, tenant_id=env.tenant_id, actor=env.parties["preparer"], reconciliation_id=recon.id)
    return recon.id


def row(engine, table, **where):
    with Session(engine) as db:
        conds = " AND ".join(f"{k} = :{k}" for k in where)
        return db.execute(sa.text(f"SELECT * FROM {table} WHERE {conds}"), where).mappings().first()  # noqa: S608


def ledger_state(engine, env):
    with Session(engine) as db:
        return (
            db.scalar(
                sa.select(sa.func.count()).select_from(JournalEntry).where(JournalEntry.tenant_id == env.tenant_id)
            ),
            db.scalar(
                sa.select(sa.func.count())
                .select_from(PostedLedgerLine)
                .where(PostedLedgerLine.tenant_id == env.tenant_id)
            ),
        )


# --- the happy path and what approval freezes -------------------------------------


def test_full_flow_prepare_approve_freezes_everything(engine):
    env = scenario(engine)
    before = ledger_state(engine, env)
    rid = prepared(engine, env)
    r = row(engine, "erp_bank_reconciliations", id=rid)
    assert r["status"] == "prepared"
    assert r["statement_closing_balance"] == Decimal("350") and r["gl_balance"] == Decimal("310")
    assert r["items_total"] == Decimal("40") and r["difference"] == 0
    approved = run(engine, svc.approve, tenant_id=env.tenant_id, actor=env.parties["approver"], reconciliation_id=rid)
    assert approved.status == m.STATUS_APPROVED and approved.approved_by_party_id == env.parties["approver"]
    # No GL writes from B7, before or after approval.
    assert ledger_state(engine, env) == before
    with Session(engine) as db:
        locked = db.scalars(sa.select(m.BankReconMatch).where(m.BankReconMatch.tenant_id == env.tenant_id)).all()
        assert len(locked) == 2 and all(x.locked_by_reconciliation_id == rid for x in locked)
        actions = {a for (a,) in db.execute(sa.select(AuditEvent.action).where(AuditEvent.tenant_id == env.tenant_id))}
        assert {
            svc.AUDIT_OPENED,
            svc.AUDIT_MATCHED,
            svc.AUDIT_ITEM_ADDED,
            svc.AUDIT_PREPARED,
            svc.AUDIT_APPROVED,
        } <= actions
        match_id = locked[0].id
    # Approved reconciliations are immutable: service and database refuse every change.
    with pytest.raises(ReconRefused) as exc:
        run(
            engine, svc.unmatch, tenant_id=env.tenant_id, actor=env.parties["matcher"], match_id=match_id, reason="oops"
        )
    assert exc.value.code == "match_locked"
    for stmt in (
        "UPDATE erp_bank_reconciliations SET status='open' WHERE id=:id",
        "UPDATE erp_bank_reconciliations SET difference=0, updated_at=now() WHERE id=:id",
        "DELETE FROM erp_bank_reconciliations WHERE id=:id",
    ):
        with Session(engine) as db, pytest.raises((IntegrityError, DBAPIError)):
            db.execute(sa.text(stmt), {"id": rid})
    with Session(engine) as db, pytest.raises((IntegrityError, DBAPIError)):
        db.execute(sa.text("DELETE FROM erp_bank_recon_items WHERE reconciliation_id=:id"), {"id": rid})
    with Session(engine) as db, pytest.raises((IntegrityError, DBAPIError)):
        db.execute(sa.text("UPDATE erp_bank_recon_matches SET status='unmatched' WHERE id=:id"), {"id": match_id})
    with Session(engine) as db, pytest.raises((IntegrityError, DBAPIError)):
        db.execute(sa.text("UPDATE erp_bank_recon_match_lines SET active=false WHERE match_id=:id"), {"id": match_id})
    for fn, kw in (
        (svc.add_items, {"requests": [ItemRequest(m.SIDE_GL, env.gl_pay, m.ITEM_OUTSTANDING_PAYMENT, "x")]}),
        (svc.cancel, {"reason": "x"}),
        (svc.return_to_open, {"reason": "x"}),
        (svc.prepare, {}),
    ):
        with pytest.raises(ReconRefused):
            run(engine, fn, tenant_id=env.tenant_id, actor=env.parties["preparer"], reconciliation_id=rid, **kw)


def test_cash_account_ref_may_be_a_declared_code(engine):
    env = scenario(engine, ref_form="code")
    rid = prepared(engine, env)
    assert row(engine, "erp_bank_reconciliations", id=rid)["gl_account_id"] == env.accounts["1000"]


# --- SoD -----------------------------------------------------------------------


def test_preparer_cannot_approve_and_opener_and_matchers_cannot_either(engine):
    env = scenario(engine)
    rid = prepared(engine, env)  # opener opened + itemized, matcher matched, preparer prepared
    for who, code in (("preparer", "sod_preparer"), ("opener", "sod_participant"), ("matcher", "sod_participant")):
        with pytest.raises(ReconRefused) as exc:
            run(engine, svc.approve, tenant_id=env.tenant_id, actor=env.parties[who], reconciliation_id=rid)
        assert exc.value.code == code
    assert row(engine, "erp_bank_reconciliations", id=rid)["status"] == "prepared"
    run(engine, svc.approve, tenant_id=env.tenant_id, actor=env.parties["approver2"], reconciliation_id=rid)


def test_database_refuses_self_approval_and_nonzero_difference_even_if_service_bypassed(engine):
    env = scenario(engine)
    rid = prepared(engine, env)
    r = row(engine, "erp_bank_reconciliations", id=rid)
    base = (
        "UPDATE erp_bank_reconciliations SET status='approved', approved_at=now(), approved_by_party_id=:who "
        "WHERE id=:id"
    )
    for who in (r["prepared_by_party_id"], r["opened_by_party_id"]):
        with Session(engine) as db, pytest.raises(IntegrityError):
            db.execute(sa.text(base), {"who": who, "id": rid})
    with Session(engine) as db, pytest.raises(IntegrityError):
        # tamper with the arithmetic in the same statement: the stored arithmetic CHECK refuses it
        db.execute(
            sa.text(
                "UPDATE erp_bank_reconciliations SET status='approved', approved_at=now(), approved_by_party_id=:who,"
                " difference=5 WHERE id=:id"
            ),
            {"who": env.parties["approver"], "id": rid},
        )
    with Session(engine) as db, pytest.raises(IntegrityError):
        # changing the frozen snapshot at approval is refused
        db.execute(
            sa.text(
                "UPDATE erp_bank_reconciliations SET status='approved', approved_at=now(), approved_by_party_id=:who,"
                " snapshot_sha256=:s WHERE id=:id"
            ),
            {"who": env.parties["approver"], "id": rid, "s": "f" * 64},
        )


# --- zero-difference gate ---------------------------------------------------------


def test_nonzero_difference_blocks_prepare(engine):
    env = scenario(engine, closing="360.00")
    recon = open_recon(engine, env)
    do_match(engine, env, [env.bank["a"]], [env.gl_a])
    do_match(engine, env, [env.bank["b"]], [env.gl_b])
    run(
        engine,
        svc.add_items,
        tenant_id=env.tenant_id,
        actor=env.parties["opener"],
        reconciliation_id=recon.id,
        requests=[ItemRequest(m.SIDE_GL, env.gl_pay, m.ITEM_OUTSTANDING_PAYMENT, "cheque")],
    )
    with pytest.raises(ReconRefused) as exc:
        run(engine, svc.prepare, tenant_id=env.tenant_id, actor=env.parties["preparer"], reconciliation_id=recon.id)
    assert exc.value.code == "not_reconcilable" and "difference_nonzero" in exc.value.detail
    assert row(engine, "erp_bank_reconciliations", id=recon.id)["status"] == "open"


def test_uncovered_lines_block_prepare(engine):
    env = scenario(engine)
    recon = open_recon(engine, env)
    with pytest.raises(ReconRefused) as exc:
        run(engine, svc.prepare, tenant_id=env.tenant_id, actor=env.parties["preparer"], reconciliation_id=recon.id)
    assert "uncovered_bank_lines" in exc.value.detail and "uncovered_gl_lines" in exc.value.detail


def test_ledger_drift_after_prepare_blocks_approval(engine):
    env = scenario(engine)
    rid = prepared(engine, env)
    post(engine, env, debit_code="1000", credit_code="4000", amount="7", day=date(2026, 1, 25))  # late journal
    with pytest.raises(ReconRefused) as exc:
        run(engine, svc.approve, tenant_id=env.tenant_id, actor=env.parties["approver"], reconciliation_id=rid)
    assert exc.value.code == "difference_nonzero"
    assert row(engine, "erp_bank_reconciliations", id=rid)["status"] == "prepared"
    back = run(
        engine,
        svc.return_to_open,
        tenant_id=env.tenant_id,
        actor=env.parties["approver"],
        reconciliation_id=rid,
        reason="late",
    )
    assert back.status == m.STATUS_OPEN and back.snapshot_sha256 is None and back.return_reason == "late"


def test_unmatch_after_prepare_blocks_approval(engine):
    env = scenario(engine)
    rid = prepared(engine, env)
    with Session(engine) as db:
        match_id = db.scalar(sa.select(m.BankReconMatch.id).where(m.BankReconMatch.tenant_id == env.tenant_id))
    run(engine, svc.unmatch, tenant_id=env.tenant_id, actor=env.parties["matcher"], match_id=match_id, reason="wrong")
    with pytest.raises(ReconRefused) as exc:
        run(engine, svc.approve, tenant_id=env.tenant_id, actor=env.parties["approver"], reconciliation_id=rid)
    assert exc.value.code in {"difference_nonzero", "not_reconcilable"}


def test_unrecorded_bank_charge_is_typed_blocks_prepare_and_is_never_posted(engine):
    env = scenario(engine, closing="340.00")  # the bank also took a 10 charge the books never recorded
    charge = bank_line(engine, env, env.statement, 3, date(2026, 1, 30), "debit", "10.00")
    recon = open_recon(engine, env)
    do_match(engine, env, [env.bank["a"]], [env.gl_a])
    do_match(engine, env, [env.bank["b"]], [env.gl_b])
    before = ledger_state(engine, env)
    run(
        engine,
        svc.add_items,
        tenant_id=env.tenant_id,
        actor=env.parties["opener"],
        reconciliation_id=recon.id,
        requests=[
            ItemRequest(m.SIDE_GL, env.gl_pay, m.ITEM_OUTSTANDING_PAYMENT, "cheque"),
            ItemRequest(m.SIDE_BANK, charge, m.ITEM_UNRECORDED_BANK_CHARGE, "needs a B2 journal"),
        ],
    )
    with pytest.raises(ReconRefused) as exc:
        run(engine, svc.prepare, tenant_id=env.tenant_id, actor=env.parties["preparer"], reconciliation_id=recon.id)
    assert exc.value.detail == "unrecorded_bank_items"  # arithmetic already ties; the gap needs a journal
    assert ledger_state(engine, env) == before  # B7 never posts the charge
    # Resolving it the supported way: a (B2-style) journal is posted, then its ledger line is matched.
    _, gl_charge = post(engine, env, debit_code="5000", credit_code="1000", amount="10", day=date(2026, 1, 30))
    run(
        engine,
        svc.remove_item,
        tenant_id=env.tenant_id,
        actor=env.parties["opener"],
        reconciliation_id=recon.id,
        item_id=_item_id(engine, env, charge),
    )
    do_match(engine, env, [charge], [gl_charge])
    run(engine, svc.prepare, tenant_id=env.tenant_id, actor=env.parties["preparer"], reconciliation_id=recon.id)
    run(engine, svc.approve, tenant_id=env.tenant_id, actor=env.parties["approver"], reconciliation_id=recon.id)


def _item_id(engine, env, ref_id):
    with Session(engine) as db:
        return db.scalar(sa.select(m.BankReconItem.id).where(m.BankReconItem.ref_id == ref_id))


def test_item_type_must_match_side_and_direction(engine):
    env = scenario(engine)
    recon = open_recon(engine, env)
    bad = (
        ItemRequest(m.SIDE_GL, env.gl_pay, m.ITEM_DEPOSIT_IN_TRANSIT, "wrong direction"),  # payment is not a deposit
        ItemRequest(m.SIDE_GL, env.gl_pay, m.ITEM_UNRECORDED_BANK_CHARGE, "wrong side"),
        ItemRequest(m.SIDE_BANK, env.bank["a"], m.ITEM_UNRECORDED_BANK_CHARGE, "credit is not a charge"),
        ItemRequest(m.SIDE_GL, uuid4(), m.ITEM_OUTSTANDING_PAYMENT, "unknown line"),
        ItemRequest(m.SIDE_GL, env.gl_pay, "free_text", "unknown type"),
        ItemRequest(m.SIDE_GL, env.gl_pay, m.ITEM_OUTSTANDING_PAYMENT, "  "),
    )
    for request in bad:
        with pytest.raises(ReconRefused):
            run(
                engine,
                svc.add_items,
                tenant_id=env.tenant_id,
                actor=env.parties["opener"],
                reconciliation_id=recon.id,
                requests=[request],
            )
    assert row(engine, "erp_bank_recon_items", tenant_id=env.tenant_id) is None


# --- matching: idempotent, exact, exclusive -------------------------------------


def test_matching_is_idempotent_exact_and_exclusive(engine):
    env = scenario(engine)
    first = do_match(engine, env, [env.bank["a"]], [env.gl_a])
    again = do_match(engine, env, [env.bank["a"]], [env.gl_a], who="opener")
    assert again.match_id == first.match_id and again.replayed and not first.replayed
    with pytest.raises(ReconRefused) as exc:  # amount differs: 100 vs 250
        do_match(engine, env, [env.bank["a"]], [env.gl_b])
    assert exc.value.code == "already_matched" or exc.value.code == "amount_mismatch"
    with pytest.raises(ReconRefused) as exc:
        do_match(engine, env, [env.bank["b"]], [env.gl_a])  # gl_a already matched
    assert exc.value.code in {"already_matched", "amount_mismatch"}
    with pytest.raises(ReconRefused) as exc:
        do_match(engine, env, [env.bank["b"]], [env.gl_pay])  # +250 vs -40
    assert exc.value.code == "amount_mismatch"
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(m.BankReconMatch)
                .where(m.BankReconMatch.tenant_id == env.tenant_id)
            )
            == 1
        )
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(m.BankReconMatchLine)
                .where(m.BankReconMatchLine.tenant_id == env.tenant_id)
            )
            == 2
        )


def test_refusal_leaves_no_rows_even_when_the_caller_commits(engine):
    env = scenario(engine)
    with Session(engine) as db:
        with pytest.raises(ReconRefused):
            svc.match(
                db,
                tenant_id=env.tenant_id,
                actor=env.parties["matcher"],
                account_id=env.bank_id,
                bank_line_ids=[env.bank["b"]],
                gl_line_ids=[env.gl_pay],
                now=NOW,
            )
        db.commit()
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(m.BankReconMatch)
                .where(m.BankReconMatch.tenant_id == env.tenant_id)
            )
            == 0
        )
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(m.BankReconMatchLine)
                .where(m.BankReconMatchLine.tenant_id == env.tenant_id)
            )
            == 0
        )


def test_many_to_one_match_and_unmatch_then_rematch(engine):
    env = scenario(engine)
    split_a = bank_line(engine, env, env.statement, 4, date(2026, 1, 11), "credit", "60.00")
    split_b = bank_line(engine, env, env.statement, 5, date(2026, 1, 11), "credit", "40.00")
    out = do_match(engine, env, [split_a, split_b], [env.gl_a])
    assert not out.replayed
    run(
        engine,
        svc.unmatch,
        tenant_id=env.tenant_id,
        actor=env.parties["matcher"],
        match_id=out.match_id,
        reason="split wrong",
    )
    again = run(
        engine, svc.unmatch, tenant_id=env.tenant_id, actor=env.parties["matcher"], match_id=out.match_id, reason="x"
    )
    assert again.replayed  # unmatch is idempotent
    # lines are free again
    assert not do_match(engine, env, [env.bank["a"]], [env.gl_a]).replayed
    with pytest.raises(ReconRefused):
        run(
            engine,
            svc.unmatch,
            tenant_id=env.tenant_id,
            actor=env.parties["matcher"],
            match_id=out.match_id,
            reason=" ",
        )


def test_reversal_pair_can_be_matched_ledger_only(engine):
    env = seed(engine)
    journal_id, gl_orig = post(engine, env, debit_code="1000", credit_code="4000", amount="80", day=date(2026, 1, 12))
    reverse(engine, env, journal_id, date(2026, 1, 13))
    with Session(engine) as db:
        lines = db.scalars(
            sa.select(PostedLedgerLine.id).where(
                PostedLedgerLine.tenant_id == env.tenant_id, PostedLedgerLine.account_id == env.accounts["1000"]
            )
        ).all()
    assert len(lines) == 2
    with pytest.raises(ReconRefused) as exc:  # a lone ledger line is not a reversal pair
        do_match(engine, env, [], [gl_orig])
    assert exc.value.code == "invalid_input"
    _, other = post(engine, env, debit_code="1000", credit_code="4000", amount="80", day=date(2026, 1, 14))
    with pytest.raises(ReconRefused) as exc:  # two lines of equal-and-opposite sign that are not one reversal
        do_match(engine, env, [], [lines[0], other])
    assert exc.value.code in {"amount_mismatch", "not_a_reversal_pair"}
    out = do_match(engine, env, [], list(lines))
    assert not out.replayed


def test_matching_across_the_period_end_leaves_the_line_uncleared_for_that_period(engine):
    env = scenario(engine)
    _, gl_feb = post(engine, env, debit_code="1000", credit_code="4000", amount="500", day=date(2026, 2, 2))
    straddling = bank_line(engine, env, env.statement, 6, date(2026, 1, 31), "credit", "500.00")
    do_match(engine, env, [straddling], [gl_feb])
    recon = open_recon(engine, env)
    with Session(engine) as db:
        recon = db.get(m.BankReconciliation, recon.id)
        open_bank = {r[0] for r in svc.uncleared_bank_lines(db, env.tenant_id, recon)}
        assert straddling in open_bank  # its partner is posted after the as-of date
        assert gl_feb not in {r[0] for r in svc.uncleared_gl_lines(db, env.tenant_id, recon)}


def test_suggestions_are_exact_amount_and_never_accepted(engine):
    env = scenario(engine)
    recon = open_recon(engine, env)
    with Session(engine) as db:
        recon = db.get(m.BankReconciliation, recon.id)
        found = svc.suggestions(db, env.tenant_id, recon)
    pairs = {(s.bank_line_id, s.gl_line_id) for s in found}
    assert (env.bank["a"], env.gl_a) in pairs and (env.bank["b"], env.gl_b) in pairs
    assert all(s.bank_line_id in env.bank.values() for s in found)
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(m.BankReconMatch)
                .where(m.BankReconMatch.tenant_id == env.tenant_id)
            )
            == 0
        )


# --- reconciliation lifecycle rules --------------------------------------------


def test_one_live_reconciliation_per_period_and_cancel_frees_it(engine):
    env = scenario(engine)
    first = open_recon(engine, env)
    with pytest.raises(ReconRefused) as exc:
        open_recon(engine, env, who="matcher")
    assert exc.value.code == "reconciliation_exists"
    run(
        engine,
        svc.cancel,
        tenant_id=env.tenant_id,
        actor=env.parties["opener"],
        reconciliation_id=first.id,
        reason="start over",
    )
    second = open_recon(engine, env)
    assert second.id != first.id
    with pytest.raises(ReconRefused):
        run(
            engine,
            svc.cancel,
            tenant_id=env.tenant_id,
            actor=env.parties["opener"],
            reconciliation_id=first.id,
            reason="again",
        )


def test_statement_without_closing_balance_and_foreign_currency_refuse(engine):
    env = seed(engine, closing=None)
    with pytest.raises(ReconRefused) as exc:
        open_recon(engine, env)
    assert exc.value.code == "statement_unusable"
    foreign = seed(engine, currency="USD")
    with pytest.raises(ReconRefused):
        open_recon(engine, foreign)


def test_unresolvable_cash_account_ref_refuses(engine):
    env = seed(engine)
    with Session(engine) as db:
        db.execute(
            sa.text("UPDATE mod_banking.bank_accounts SET cash_account_ref='GL:9' WHERE id=:id"), {"id": env.bank_id}
        )
        db.commit()
    with pytest.raises(ReconRefused) as exc:
        open_recon(engine, env)
    assert exc.value.code == "gl_account_unresolved"


def test_inactive_or_foreign_actor_refused(engine):
    env = scenario(engine)
    other = scenario(engine)
    with pytest.raises(ReconRefused) as exc:
        run(
            engine,
            svc.open_reconciliation,
            tenant_id=env.tenant_id,
            actor=other.parties["opener"],
            account_id=env.bank_id,
            statement_id=env.statement,
        )
    assert exc.value.code == "actor_invalid"


# --- tenant isolation ---------------------------------------------------------------


def test_tenant_isolation_everywhere(engine):
    a, b = scenario(engine), scenario(engine)
    # B cannot open on A's account, nor match A's lines (through its own account or A's).
    with pytest.raises(ReconRefused) as exc:
        run(
            engine,
            svc.open_reconciliation,
            tenant_id=b.tenant_id,
            actor=b.parties["opener"],
            account_id=a.bank_id,
            statement_id=a.statement,
        )
    assert exc.value.code == "account_unknown"
    with pytest.raises(ReconRefused) as exc:
        run(
            engine,
            svc.match,
            tenant_id=b.tenant_id,
            actor=b.parties["matcher"],
            account_id=b.bank_id,
            bank_line_ids=[a.bank["a"]],
            gl_line_ids=[a.gl_a],
        )
    assert exc.value.code == "line_unknown"
    rid_a = prepared(engine, a)
    with pytest.raises(ReconRefused) as exc:  # B cannot approve (or even see) A's reconciliation
        run(engine, svc.approve, tenant_id=b.tenant_id, actor=b.parties["approver"], reconciliation_id=rid_a)
    assert exc.value.code == "reconciliation_unknown"
    with Session(engine) as db:
        a_recon = db.get(m.BankReconciliation, rid_a)
        assert a_recon.status == m.STATUS_PREPARED
        b_open = open_recon_in(db, b)
        assert b_open.gl_account_id == b.accounts["1000"] and b_open.gl_account_id != a.accounts["1000"]
        # each tenant evaluates only its own ledger
        assert svc.evaluate(db, a.tenant_id, a_recon).gl_balance == Decimal("310")
        assert {r[0] for r in svc.uncleared_gl_lines(db, b.tenant_id, b_open)} == {b.gl_a, b.gl_b, b.gl_pay}
        db.rollback()


def open_recon_in(db, env):
    return svc.open_reconciliation(
        db,
        tenant_id=env.tenant_id,
        actor=env.parties["opener"],
        account_id=env.bank_id,
        statement_id=env.statement,
        now=NOW,
    )


# --- concurrency -------------------------------------------------------------------------


def _race(engine, jobs):
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def worker(index, job):
        with Session(engine) as db:
            try:
                barrier.wait(timeout=10)
                results[index] = ("ok", job(db))
                db.commit()
            except ReconRefused as exc:
                db.rollback()
                results[index] = ("refused", exc.code)

    threads = [threading.Thread(target=worker, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def test_concurrent_identical_matches_converge_on_one_match(engine):
    env = scenario(engine)

    def job(db):
        return svc.match(
            db,
            tenant_id=env.tenant_id,
            actor=env.parties["matcher"],
            account_id=env.bank_id,
            bank_line_ids=[env.bank["a"]],
            gl_line_ids=[env.gl_a],
            now=NOW,
        )

    results = _race(engine, [job] * 4)
    assert all(kind == "ok" for kind, _ in results), results
    assert len({value.match_id for _, value in results}) == 1
    assert sum(1 for _, value in results if not value.replayed) == 1


def test_concurrent_conflicting_matches_one_winner(engine):
    env = scenario(engine)
    twin = bank_line(engine, env, env.statement, 7, date(2026, 1, 10), "credit", "100.00")

    def make(bank_id):
        def job(db):
            return svc.match(
                db,
                tenant_id=env.tenant_id,
                actor=env.parties["matcher"],
                account_id=env.bank_id,
                bank_line_ids=[bank_id],
                gl_line_ids=[env.gl_a],
                now=NOW,
            )

        return job

    results = _race(engine, [make(env.bank["a"]), make(twin), make(env.bank["a"]), make(twin)])
    winners = {value.match_id for kind, value in results if kind == "ok" and not value.replayed}
    assert len(winners) == 1
    assert all(kind == "ok" or value == "already_matched" for kind, value in results), results
    with Session(engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(m.BankReconMatchLine)
                .where(
                    m.BankReconMatchLine.tenant_id == env.tenant_id,
                    m.BankReconMatchLine.side == "gl",
                    m.BankReconMatchLine.active.is_(True),
                )
            )
            == 1
        )


def test_concurrent_approvals_approve_once(engine):
    env = scenario(engine)
    rid = prepared(engine, env)

    def make(who):
        def job(db):
            return svc.approve(
                db, tenant_id=env.tenant_id, actor=env.parties[who], reconciliation_id=rid, now=NOW
            ).status

        return job

    results = _race(engine, [make("approver"), make("approver2"), make("approver")])
    assert sum(1 for kind, _ in results if kind == "ok") == 1, results
    assert all(kind == "ok" or value == "state_refused" for kind, value in results), results
    assert row(engine, "erp_bank_reconciliations", id=rid)["status"] == "approved"


def test_database_refuses_double_active_match_even_if_service_bypassed(engine):
    env = scenario(engine)
    out = do_match(engine, env, [env.bank["a"]], [env.gl_a])
    with Session(engine) as db, pytest.raises(IntegrityError):
        db.execute(
            sa.text(
                "INSERT INTO erp_bank_recon_match_lines (id, tenant_id, match_id, side, ref_id, amount, active)"
                " VALUES (:id, :t, :m, 'gl', :r, 100, true)"
            ),
            {"id": uuid4(), "t": env.tenant_id, "m": out.match_id, "r": env.gl_a},
        )
