# ruff: noqa: S608  (test-only SQL built from generated uuids and constant table names)
"""B-16 AR producer against real PostgreSQL (hosted CI; ERP_TEST_POSTGRES_URL).

Real accounting, numbering, tax, banking, kernel and (when a13 is on the path) billing migrations; real billing
commands. The e2_0032 object tests (RLS, grants, triggers, downgrade guard) run everywhere; the billing flows need
the unreleased dotmac-billing a13 command surface and skip when an older release is installed (the composition PR
bumps the pin; locally a13 source is put on PYTHONPATH).
"""

import threading
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_posting_bridge_pg as bridge_pg
from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory
from dotmac_banking.models import BankAccount, BankInstitution
from dotmac_customers.models import CustomerAccount
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.models import Party, PartyType, Tenant
from dotmac_kernel.money import Currency
from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
from dotmac_kernel.setting_scopes import ScopeKindRegistry, install_scope_kinds
from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types
from dotmac_kernel.settings_resolver import SettingChangeContext, upsert_by_key
from dotmac_numbering.service import SeriesConfiguration, configure_series
from dotmac_tax.contracts import TaxAuthorityInput, TaxJurisdictionInput, TaxRuleInput
from dotmac_tax.service import (
    ensure_tax_authority_in_transaction,
    ensure_tax_code_in_transaction,
    ensure_tax_jurisdiction_in_transaction,
    ensure_tax_rule_in_transaction,
)
from dual_control_support import set_dual_control_engine
from sqlalchemy.orm import Session
from test_posting_bridge_pg import NOW

from erp_v2.ar import checks, queries, service, settings, terms
from erp_v2.ar.models import AR_TABLES, ArDraft, ArReceipt
from erp_v2.ar.routes import receivables_producer_feature
from erp_v2.ar.service import AllocationInput, ArRefused, LineInput
from erp_v2.assembly import assembly
from erp_v2.journals import policy as journal_policy
from erp_v2.journals.routes import journals_web_feature

engine = bridge_pg.engine  # disposable-database fixture under its own name

DAY = date(2026, 1, 12)  # in the open January period, on or before NOW (2026-01-15)


def _a13() -> bool:
    import dotmac_billing

    return tuple(int(p) for p in dotmac_billing.__version__.replace("a", ".").split(".")[:4]) >= (0, 1, 0, 13)


needs_a13 = pytest.mark.skipif(not _a13(), reason="dotmac-billing a13 (unreleased) is required for the billing flows")


@pytest.fixture(scope="module", autouse=True)
def registries():
    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))
    manifests = [receivables_producer_feature, journals_web_feature, FeatureManifest(name="kernel_stub")]
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    install_scope_kinds(ScopeKindRegistry.from_manifests(manifests))


def _account(db, env, category, code, name, balance=NormalBalance.CREDIT):
    row = Account(
        tenant_id=env.tenant_id,
        category_id=category.id,
        code=code,
        name=name,
        kind=AccountKind.POSTING,
        normal_balance=balance,
    )
    db.add(row)
    db.flush()
    env.accounts[code] = row.id
    return row


def world(engine, *, configured=True, enabled=True, dual=False):
    env = bridge_pg.seed(engine)
    set_dual_control_engine(engine, env.tenant_id, dual)
    with Session(engine) as db:
        revenue = AccountCategory(tenant_id=env.tenant_id, code="REV", name="REV", account_class=AccountClass.REVENUE)
        asset = AccountCategory(tenant_id=env.tenant_id, code="AST", name="AST", account_class=AccountClass.ASSET)
        liability = db.scalar(
            sa.select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "LIA")
        )
        db.add_all([revenue, asset])
        db.flush()
        _account(db, env, revenue, "4000", "Services revenue")
        _account(db, env, revenue, "4100", "Other revenue")
        _account(db, env, asset, "1400", "Trade receivables", NormalBalance.DEBIT)
        cash = _account(db, env, asset, "1000", "Operating cash", NormalBalance.DEBIT)
        _account(db, env, liability, "1440", "Output VAT", NormalBalance.CREDIT)
        _account(db, env, liability, "1455", "Deferred output VAT", NormalBalance.CREDIT)
        env.people = {}
        for name in ("preparer", "approver", "solo"):
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
        journal_policy.set_functional_currency(db, tenant_id=env.tenant_id, code="NGN", reason="test seed")
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
            account_identifier="recv-acct-1",
            account_type_code="current",
            currency_code="NGN",
            minor_units=2,
            cash_account_ref=str(cash.id),
            status="active",
        )
        db.add(bank)
        env.bank_id = bank.id
        env.customers = {}
        for number, name in (("C-1", "Customer One"), ("C-2", "Customer Two")):
            customer = CustomerAccount(tenant_id=env.tenant_id, account_number=number, display_name=name)
            db.add(customer)
            db.flush()
            env.customers[number] = customer.id
        authority = ensure_tax_authority_in_transaction(
            db, tenant_id=env.tenant_id, command=TaxAuthorityInput(code="FIRS", name="FIRS")
        )
        jurisdiction = ensure_tax_jurisdiction_in_transaction(
            db,
            tenant_id=env.tenant_id,
            command=TaxJurisdictionInput(
                authority_id=authority.authority_id,
                code="NG",
                name="Nigeria",
                country_code="NG",
                currency=Currency("NGN", 2),
            ),
        )
        code = ensure_tax_code_in_transaction(
            db,
            tenant_id=env.tenant_id,
            jurisdiction_id=jurisdiction.jurisdiction_id,
            code="VAT",
            name="VAT 7.5%",
            tax_kind_code="vat",
        )
        ensure_tax_rule_in_transaction(
            db,
            tenant_id=env.tenant_id,
            command=TaxRuleInput(
                tax_code_id=code.tax_code_id,
                version=1,
                effective_from=date(2025, 1, 1),
                effective_to=None,
                priority=1,
                fact_kind="service",
                recognition_basis_code="issued",
                transaction_side="output",
                calculation_method="percentage",
                rate=Decimal("0.075"),
                fixed_amount=None,
                inclusive=False,
                recoverable_rate=Decimal(0),
            ),
        )
        env.vat = code.tax_code_id
        db.commit()
    env.tenant = SimpleNamespace(id=env.tenant_id)
    if configured:
        configure(engine, env, enabled=enabled)
    return env


def configure(engine, env, *, enabled=True, **over):
    values = {
        settings.KEY_ENABLED: enabled,
        settings.KEY_SERIES_INVOICE: "AR-INV",
        settings.KEY_SERIES_CREDIT_NOTE: "AR-CN",
        settings.KEY_SERIES_RECEIPT: "AR-RCT",
        settings.KEY_TERMS: {"net_30": 30},
        settings.KEY_DEFAULT_TERMS: "net_30",
        settings.KEY_SELLER: {"legal_name": "Dotmac Technologies Ltd", "address_line": "1 Example Road"},
        settings.KEY_PAYMENT_INSTRUCTIONS: {"method_code": "bank_transfer", "bank_name": "Example Bank"},
        settings.KEY_AUTO_APPLY: True,
        **over,
    }
    with Session(engine) as db:
        for key, value in values.items():
            upsert_by_key(
                db,
                settings.DOMAIN,
                key,
                value,
                tenant_id=env.tenant_id,
                changed_by=SettingChangeContext(actor_party_id=None, reason="test"),
            )
        for code, prefix in (("AR-INV", "INV"), ("AR-CN", "CN"), ("AR-RCT", "RCT")):
            configure_series(
                db,
                scope=env.scope,
                configuration=SeriesConfiguration(series_code=code, prefix=prefix, min_digits=4),
            )
        db.commit()


def act(engine, env, who, fn, **kw):
    """One request-like unit: call, then commit (also after a caught refusal)."""
    with Session(engine) as db:
        tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.people[who])
        kw.setdefault("now", NOW)
        try:
            return fn(db, tenant=tenant, party=party, **kw)
        finally:
            db.commit()


def refused(engine, env, who, fn, **kw):
    with pytest.raises(ArRefused) as info:
        act(engine, env, who, fn, **kw)
    return info.value.code


def line(description="Install", quantity="1", unit="100", account="4000", vat=False, env=None):
    return LineInput(description, Decimal(quantity), Decimal(unit), env.accounts[account], env.vat if vat else None)


def draft_invoice(engine, env, lines, *, customer="C-1", who="preparer", day=DAY, request_id=None):
    return act(
        engine,
        env,
        who,
        service.create_draft,
        request_id=request_id or uuid4(),
        kind="invoice",
        customer_account_id=env.customers[customer],
        document_date=day,
        lines=lines,
    )


def issue_invoice(engine, env, lines, *, customer="C-1", who="approver", day=DAY):
    draft = draft_invoice(engine, env, lines, customer=customer, day=day)
    return act(engine, env, who, service.issue_draft, draft_id=draft.id)


def draft_receipt(engine, env, amount, allocations=(), *, customer="C-1", reference="BANK-REF-001", day=DAY):
    return act(
        engine,
        env,
        "preparer",
        service.create_draft,
        request_id=uuid4(),
        kind="receipt",
        customer_account_id=env.customers[customer],
        document_date=day,
        bank_account_id=env.bank_id,
        bank_reference=reference,
        amount=Decimal(amount),
        allocations=[AllocationInput(doc, Decimal(a)) for doc, a in allocations],
    )


def post_receipt(engine, env, amount, allocations=(), *, who="approver", **kw):
    draft = draft_receipt(engine, env, amount, allocations, **kw)
    return draft, act(engine, env, who, service.post_receipt, draft_id=draft.id)


def outstanding(engine, env, document_id):
    with Session(engine) as db:
        return queries.exposure(db, env.tenant_id, document_id).outstanding


def billing_counts(engine, env):
    with engine.connect() as c:
        out = {}
        for table in ("billing_accounts", "rated_obligations", "documents", "confirmed_settlements"):
            out[table] = c.scalar(
                sa.text(f"SELECT count(*) FROM mod_billing.{table} WHERE tenant_id = :t"),
                {"t": env.tenant_id},
            )
        for table in AR_TABLES:
            out[table] = c.scalar(
                sa.text(f"SELECT count(*) FROM public.{table} WHERE tenant_id = :t"),
                {"t": env.tenant_id},
            )
        return out


# --- e2_0032 objects (no billing needed) ------------------------------------------------------------------------


def test_tables_force_rls_and_exact_runtime_grants(engine):
    with engine.connect() as c:
        for table in AR_TABLES:
            assert c.scalar(sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table})
        grants = {
            (table, priv): c.scalar(sa.text(f"SELECT has_table_privilege('app_user', '{table}', '{priv}')"))
            for table in AR_TABLES
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
    assert all(grants[(t, "SELECT")] for t in AR_TABLES)
    assert all(grants[(t, "INSERT")] for t in AR_TABLES)
    assert not any(grants[(t, "TRUNCATE")] for t in AR_TABLES)
    # drafts: column-limited UPDATE only (no table-wide UPDATE), no DELETE; lines: full DML; the others append-only
    assert not grants[("erp_ar_drafts", "UPDATE")] and not grants[("erp_ar_drafts", "DELETE")]
    assert grants[("erp_ar_draft_lines", "UPDATE")] and grants[("erp_ar_draft_lines", "DELETE")]
    for table in ("erp_ar_line_accounts", "erp_ar_receipts"):
        assert not grants[(table, "UPDATE")] and not grants[(table, "DELETE")]
    with engine.connect() as c:
        for column, expected in (
            ("status", True),
            ("issued_by", True),
            ("kind", False),
            ("dual_control", False),
            ("prepared_by", False),
            ("request_id", False),
        ):
            assert (
                c.scalar(sa.text(f"SELECT has_column_privilege('app_user', 'erp_ar_drafts', '{column}', 'UPDATE')"))
                is expected
            ), column


def _raw_draft(c, tenant_id, *, status="draft", dual=False, prepared=None, issued=None, kind="invoice"):
    prepared = prepared or uuid4()
    draft_id = uuid4()
    c.execute(
        sa.text(
            "INSERT INTO erp_ar_drafts (id, tenant_id, kind, customer_account_id, currency, document_date, status, "
            "prepared_by, issued_by, dual_control, request_id) VALUES (:i, :t, :k, :c, 'NGN', '2026-01-12', :s, :p, "
            ":iss, :d, :r)"
        ),
        {
            "i": draft_id,
            "t": tenant_id,
            "k": kind,
            "c": uuid4(),
            "s": status,
            "p": prepared,
            "iss": issued,
            "d": dual,
            "r": uuid4(),
        },
    )
    return draft_id


def test_triggers_hold_the_invariants_against_the_owner(engine):
    env = bridge_pg.seed(engine)
    t = env.tenant_id
    with engine.begin() as c:
        draft = _raw_draft(c, t)
        c.execute(
            sa.text(
                "INSERT INTO erp_ar_draft_lines (id, tenant_id, draft_id, n, description) VALUES (:i, :t, :d, 1, 'x')"
            ),
            {"i": uuid4(), "t": t, "d": draft},
        )
    with engine.connect() as c:
        for sql in (
            f"DELETE FROM erp_ar_drafts WHERE id = '{draft}'",
            f"UPDATE erp_ar_drafts SET kind = 'receipt' WHERE id = '{draft}'",
            f"UPDATE erp_ar_drafts SET dual_control = true WHERE id = '{draft}'",
            f"UPDATE erp_ar_drafts SET request_id = gen_random_uuid() WHERE id = '{draft}'",
            f"UPDATE erp_ar_drafts SET billing_document_id = gen_random_uuid() WHERE id = '{draft}'",  # without issued
        ):
            with pytest.raises(sa.exc.DBAPIError):
                c.execute(sa.text(sql))
            c.rollback()
    # draft -> issued is fine; then every further change, and any line change, is refused
    with engine.begin() as c:
        c.execute(
            sa.text(
                f"UPDATE erp_ar_drafts SET status = 'issued', issued_by = gen_random_uuid(), "
                f"billing_document_id = gen_random_uuid() WHERE id = '{draft}'"
            )
        )
    with engine.connect() as c:
        for sql in (
            f"UPDATE erp_ar_drafts SET status = 'draft', issued_by = NULL WHERE id = '{draft}'",
            f"UPDATE erp_ar_drafts SET status = 'deleted', issued_by = NULL WHERE id = '{draft}'",
            f"UPDATE erp_ar_draft_lines SET description = 'y' WHERE draft_id = '{draft}'",
            f"DELETE FROM erp_ar_draft_lines WHERE draft_id = '{draft}'",
            "INSERT INTO erp_ar_draft_lines (id, tenant_id, draft_id, n) "
            f"VALUES (gen_random_uuid(), '{t}', '{draft}', 2)",
        ):
            with pytest.raises(sa.exc.DBAPIError):
                c.execute(sa.text(sql))
            c.rollback()


def test_dual_control_stamp_check_refuses_a_self_issue(engine):
    env = bridge_pg.seed(engine)
    person = uuid4()
    with engine.connect() as c:
        with pytest.raises(sa.exc.IntegrityError):
            _raw_draft(c, env.tenant_id, status="issued", dual=True, prepared=person, issued=person)
        c.rollback()
    with engine.begin() as c:  # stamped OFF: the same person may issue (single-operator practice)
        _raw_draft(c, env.tenant_id, status="issued", dual=False, prepared=person, issued=person)


def test_append_only_tables_refuse_update_and_delete_even_for_the_owner(engine):
    env = bridge_pg.seed(engine)
    t = env.tenant_id
    with engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO erp_ar_line_accounts (id, tenant_id, billing_document_id, line_number, "
                "revenue_account_id, "
                "pre_tax, tax) VALUES (gen_random_uuid(), :t, gen_random_uuid(), 1, gen_random_uuid(), 10, 0)"
            ),
            {"t": t},
        )
        c.execute(
            sa.text(
                "INSERT INTO erp_ar_receipts (id, tenant_id, billing_settlement_id, draft_id, bank_account_id, "
                "cash_account_id, bank_reference, receipt_number, received_on, recorded_by) VALUES (gen_random_uuid(), "
                ":t, gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'R', 'N-1', "
                "'2026-01-12', gen_random_uuid())"
            ),
            {"t": t},
        )
    with engine.connect() as c:
        for sql in (
            "UPDATE erp_ar_line_accounts SET tax = 1",
            "DELETE FROM erp_ar_line_accounts",
            "UPDATE erp_ar_receipts SET bank_reference = 'X'",
            "DELETE FROM erp_ar_receipts",
        ):
            with pytest.raises(sa.exc.DBAPIError):
                c.execute(sa.text(sql))
            c.rollback()


def test_unique_bank_reference_per_bank_account_and_rls_isolation(engine):
    a, b = bridge_pg.seed(engine), bridge_pg.seed(engine)
    bank, ref = uuid4(), "SAME-REF"
    insert = (
        "INSERT INTO erp_ar_receipts (id, tenant_id, billing_settlement_id, draft_id, bank_account_id, "
        "cash_account_id, "
        "bank_reference, receipt_number, received_on, recorded_by) VALUES (gen_random_uuid(), :t, gen_random_uuid(), "
        "gen_random_uuid(), :b, gen_random_uuid(), :r, :n, '2026-01-12', gen_random_uuid())"
    )
    with engine.begin() as c:
        c.execute(sa.text(insert), {"t": a.tenant_id, "b": bank, "r": ref, "n": "A-1"})
        c.execute(sa.text(insert), {"t": b.tenant_id, "b": bank, "r": ref, "n": "B-1"})  # another tenant: fine
    with engine.connect() as c:
        with pytest.raises(sa.exc.IntegrityError):
            c.execute(sa.text(insert), {"t": a.tenant_id, "b": bank, "r": ref, "n": "A-2"})
        c.rollback()
    role = f"ar_rt_{uuid4().hex[:12]}"
    admin = sa.create_engine(
        sa.make_url(__import__("os").environ["ERP_TEST_POSTGRES_URL"]),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOBYPASSRLS')
        c.exec_driver_sql(f'GRANT app_user TO "{role}"')
    rt = sa.create_engine(engine.url.set(username=role), hide_parameters=True)
    try:
        with Session(rt) as db:
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a.tenant_id)})
            rows = db.execute(sa.text("SELECT tenant_id FROM erp_ar_receipts")).all()
            assert {r[0] for r in rows} == {a.tenant_id}
            with pytest.raises(sa.exc.DBAPIError):  # cannot write another tenant's rows
                db.execute(sa.text(insert), {"t": b.tenant_id, "b": uuid4(), "r": "Z", "n": "Z-1"})
    finally:
        rt.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


def test_downgrade_refuses_while_rows_exist(engine):
    from alembic import command

    env = bridge_pg.seed(engine)
    with engine.begin() as c:
        _raw_draft(c, env.tenant_id)
    from erp_v2.migrate import build_config

    cfg = build_config(engine.url.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match="refusing to drop"):
        command.downgrade(cfg, "e2_0031_billing_fact_postings")
    with engine.connect() as c:
        assert "e2_0032_ar_producer" in {r[0] for r in c.execute(sa.text("SELECT version_num FROM alembic_version"))}


# --- the producer (needs the a13 command surface) --------------------------------------------------------------


@needs_a13
def test_flag_off_refuses_every_action_and_writes_nothing(engine):
    env = world(engine, enabled=False)
    before = billing_counts(engine, env)
    assert (
        refused(
            engine,
            env,
            "preparer",
            service.create_draft,
            request_id=uuid4(),
            kind="invoice",
            customer_account_id=env.customers["C-1"],
            document_date=DAY,
            lines=[line(env=env)],
        )
        == "ar_producer_disabled"
    )
    assert refused(engine, env, "approver", service.issue_draft, draft_id=uuid4()) == "ar_producer_disabled"
    assert (
        refused(engine, env, "approver", service.void_document, document_id=uuid4(), occurred_on=DAY)
        == "ar_producer_disabled"
    )
    assert billing_counts(engine, env) == before
    with Session(engine) as db:  # not even a refusal audit row
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == service.AUDIT_REFUSED)
            )
            == 0
        )


@needs_a13
def test_issue_one_line_invoice_takes_number_from_series_and_records_line_accounts(engine):
    env = world(engine)
    outcome = issue_invoice(engine, env, [line(env=env, vat=True)])
    assert outcome.document_id is not None and not outcome.replayed
    with Session(engine) as db:
        document = queries.billing_document(db, env.tenant_id, outcome.document_id)
        assert document.document_number.startswith("INV")
        assert (document.subtotal, document.tax_total, document.grand_total) == (
            Decimal("100"),
            Decimal("7.5"),
            Decimal("107.5"),
        )
        accounts = queries.line_accounts(db, env.tenant_id, document.id)
        lines = queries.billing_lines(db, env.tenant_id, document.id)
        assert [(a.line_number, a.pre_tax, a.tax, a.revenue_account_id, a.treatment_code) for a in accounts] == [
            (1, Decimal("100"), Decimal("7.5"), env.accounts["4000"], "standard_rated")
        ]
        assert [(la.line_number, la.pre_tax_amount, la.tax_amount) for la in lines] == [
            (a.line_number, a.pre_tax, a.tax) for a in accounts
        ]
        assert document.due_at.astimezone(terms.LAGOS).date() == date(2026, 2, 11)  # 30 days from the invoice date
        draft = db.get(ArDraft, queries.draft_for_document(db, env.tenant_id, document.id).id)
        assert draft.status == "issued" and draft.issued_by == env.people["approver"]
        audit = db.scalar(
            sa.select(sa.func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == service.AUDIT_INVOICE_ISSUED)
        )
        assert audit == 1
    assert outstanding(engine, env, document.id) == Decimal("107.5")


@needs_a13
def test_issue_three_line_invoice_rows_equal_the_fact_lines(engine):
    env = world(engine)
    lines = [
        line("A", "2", "50", vat=True, env=env),
        line("B", "1", "30", account="4100", env=env),
        line("C", "3", "10.50", vat=True, env=env),
    ]
    outcome = issue_invoice(engine, env, lines)
    with Session(engine) as db:
        document = queries.billing_document(db, env.tenant_id, outcome.document_id)
        accounts = queries.line_accounts(db, env.tenant_id, document.id)
        fact_lines = queries.billing_lines(db, env.tenant_id, document.id)
        assert [(a.line_number, a.pre_tax, a.tax) for a in accounts] == [
            (la.line_number, la.pre_tax_amount, la.tax_amount) for la in fact_lines
        ]
        assert [a.revenue_account_id for a in accounts] == [env.accounts[c] for c in ("4000", "4100", "4000")]
        assert sum(a.pre_tax for a in accounts) == document.subtotal == Decimal("161.50")
        assert sum(a.tax for a in accounts) == document.tax_total == Decimal("9.86")  # 7.5 + 2.36 (2.3625 -> 2.36)
        assert document.authoring_mode == "explicit_multiline"


@needs_a13
def test_double_post_and_two_sessions_give_one_issue(engine):
    env = world(engine)
    draft = draft_invoice(engine, env, [line(env=env)])
    results, errors = [], []

    def worker():
        try:
            results.append(act(engine, env, "approver", service.issue_draft, draft_id=draft.id))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert {r.document_id for r in results} == {results[0].document_id}
    assert sorted(r.replayed for r in results) == [False, True]
    third = act(engine, env, "approver", service.issue_draft, draft_id=draft.id)
    assert third.replayed and third.document_id == results[0].document_id
    counts = billing_counts(engine, env)
    assert counts["documents"] == 1 and counts["erp_ar_line_accounts"] == 1


@needs_a13
def test_draft_create_is_idempotent_per_request_and_a_changed_payload_conflicts(engine):
    env = world(engine)
    rid = uuid4()
    first = draft_invoice(engine, env, [line(env=env)], request_id=rid)
    again = draft_invoice(engine, env, [line(env=env)], request_id=rid)
    assert again.id == first.id and again.replayed
    assert (
        refused(
            engine,
            env,
            "preparer",
            service.create_draft,
            request_id=rid,
            kind="invoice",
            customer_account_id=env.customers["C-1"],
            document_date=DAY,
            lines=[line(unit="999", env=env)],
        )
        == "idempotency_conflict"
    )


@needs_a13
def test_receipt_with_two_allocations_and_a_remainder_replay_and_reference_reuse(engine):
    env = world(engine)
    one = issue_invoice(engine, env, [line(unit="100", env=env)]).document_id
    two = issue_invoice(engine, env, [line(unit="60", env=env)]).document_id
    draft, posted = post_receipt(engine, env, "200", [(one, "100"), (two, "50")])
    assert posted.settlement_id is not None and not posted.replayed
    assert outstanding(engine, env, one) == 0 and outstanding(engine, env, two) == Decimal("10")
    with Session(engine) as db:
        receipt = queries.receipt_for_settlement(db, env.tenant_id, posted.settlement_id)
        assert receipt.receipt_number.startswith("RCT") and receipt.cash_account_id == env.accounts["1000"]
        assert receipt.bank_reference == "BANK-REF-001" and receipt.recorded_by == env.people["approver"]
        position = service.billing.rebuild_receivable_position(
            db,
            scope=env.scope,
            billing_account_id=queries.billing_account_for_customer(db, env.tenant_id, env.customers["C-1"], "NGN").id,
            currency="NGN",
            minor_units=2,
        )
        assert position.available_credit == Decimal("50")  # the remainder is customer credit
    again = act(engine, env, "approver", service.post_receipt, draft_id=draft.id)  # a replay is a no-op
    assert again.replayed and again.settlement_id == posted.settlement_id
    other = draft_receipt(engine, env, "10", reference="  bank-ref-001 ")  # same reference, normalised
    assert refused(engine, env, "approver", service.post_receipt, draft_id=other.id) == "bank_reference_reused"
    with Session(engine) as db:
        assert (
            db.scalar(sa.select(sa.func.count()).select_from(ArReceipt).where(ArReceipt.tenant_id == env.tenant_id))
            == 1
        )


@needs_a13
def test_auto_apply_on_issue_uses_unallocated_credit(engine):
    env = world(engine)
    post_receipt(engine, env, "40")  # no allocations: pure customer credit
    document = issue_invoice(engine, env, [line(unit="100", env=env)]).document_id
    assert outstanding(engine, env, document) == Decimal("60")
    configure(engine, env, **{settings.KEY_AUTO_APPLY: False})
    other = issue_invoice(engine, env, [line(unit="100", env=env)], customer="C-2").document_id
    post_receipt(engine, env, "40", customer="C-2", reference="BANK-REF-002")
    later = issue_invoice(engine, env, [line(unit="70", env=env)], customer="C-2").document_id
    assert outstanding(engine, env, other) == Decimal("100") and outstanding(engine, env, later) == Decimal("70")


@needs_a13
def test_apply_credit_deallocate_and_reverse_a_receipt_as_bounced(engine):
    env = world(engine)
    document = issue_invoice(engine, env, [line(unit="100", env=env)]).document_id
    draft, posted = post_receipt(engine, env, "100")  # credit, auto-apply already ran at issue (before the receipt)
    with Session(engine) as db:
        account = queries.billing_document(db, env.tenant_id, document).billing_account_id
    request = uuid4()
    applied = act(
        engine,
        env,
        "approver",
        service.apply_credit,
        billing_account_id=account,
        document_id=document,
        amount=Decimal("60"),
        occurred_on=DAY,
        request_id=request,
    )
    assert outstanding(engine, env, document) == Decimal("40")
    again = act(
        engine,
        env,
        "approver",
        service.apply_credit,
        billing_account_id=account,
        document_id=document,
        amount=Decimal("60"),
        occurred_on=DAY,
        request_id=request,
    )  # same request id: the original
    assert outstanding(engine, env, document) == Decimal("40") and again.document_id == applied.document_id
    assert again.replayed
    with Session(engine) as db:  # the replay wrote no second success audit row
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.tenant_id == env.tenant_id, AuditEvent.action == service.AUDIT_CREDIT_APPLIED)
            )
            == 1
        )
    with Session(engine) as db:
        rows = queries.allocation_rows(db, env.tenant_id, document_id=document)
        assert len(rows) == 1
    act(engine, env, "approver", service.deallocate, allocation_id=rows[0].id, occurred_on=DAY, request_id=uuid4())
    assert outstanding(engine, env, document) == Decimal("100")
    act(
        engine,
        env,
        "approver",
        service.apply_credit,
        billing_account_id=account,
        document_id=document,
        amount=Decimal("30"),
        occurred_on=DAY,
        request_id=uuid4(),
    )
    reversed_ = act(
        engine, env, "approver", service.reverse_receipt, receipt_draft_id=draft.id, reason="bounced", occurred_on=DAY
    )
    assert reversed_.settlement_id == posted.settlement_id
    assert outstanding(engine, env, document) == Decimal("100")  # the 30 drawn from the bounced receipt came back
    with Session(engine) as db:
        position = service.billing.rebuild_receivable_position(
            db, scope=env.scope, billing_account_id=account, currency="NGN", minor_units=2
        )
        assert position.available_credit == 0
    replay = act(
        engine, env, "approver", service.reverse_receipt, receipt_draft_id=draft.id, reason="bounced", occurred_on=DAY
    )
    assert replay.replayed
    assert (
        refused(
            engine, env, "approver", service.reverse_receipt, receipt_draft_id=draft.id, reason="oops", occurred_on=DAY
        )
        == "reason_invalid"
    )


@needs_a13
def test_credit_notes_within_outstanding_over_it_and_without_an_invoice(engine):
    env = world(engine)
    document = issue_invoice(engine, env, [line(unit="100", env=env, vat=True)]).document_id  # 107.50
    kwargs = dict(
        request_id=None,
        kind="credit_note",
        customer_account_id=env.customers["C-1"],
        document_date=DAY,
        original_document_id=document,
    )

    def credit(unit):
        draft = act(
            engine,
            env,
            "preparer",
            service.create_draft,
            **{
                **kwargs,
                "request_id": uuid4(),
                "lines": [line("Credit", unit=unit, env=env, vat=True)],
                "reason": "service correction",
            },
        )
        return act(engine, env, "approver", service.issue_draft, draft_id=draft.id)

    ok = credit("40")  # 40 + 3 tax
    assert outstanding(engine, env, document) == Decimal("64.5")
    with Session(engine) as db:
        note = queries.billing_document(db, env.tenant_id, ok.document_id)
        assert note.document_kind == "credit_note" and note.document_number.startswith("CN")
        assert [(a.line_number, a.pre_tax, a.tax) for a in queries.line_accounts(db, env.tenant_id, note.id)] == [
            (1, Decimal("40"), Decimal("3"))
        ]
    over = act(
        engine,
        env,
        "preparer",
        service.create_draft,
        **{**kwargs, "request_id": uuid4(), "lines": [line("Credit", unit="100", env=env, vat=True)], "reason": "x"},
    )
    assert refused(engine, env, "approver", service.issue_draft, draft_id=over.id) == "credit_exceeds_receivable"
    assert (
        refused(
            engine,
            env,
            "preparer",
            service.create_draft,
            **{**kwargs, "request_id": uuid4(), "original_document_id": None, "lines": [line(env=env)], "reason": "x"},
        )
        == "credit_note_requires_invoice"
    )
    assert outstanding(engine, env, document) == Decimal("64.5")


@needs_a13
def test_void_rules_and_a_credit_note_can_be_voided(engine):
    env = world(engine)
    document = issue_invoice(engine, env, [line(unit="100", env=env)]).document_id
    post_receipt(engine, env, "30", [(document, "30")])
    assert (
        refused(engine, env, "approver", service.void_document, document_id=document, occurred_on=DAY)
        == "void_has_allocations"
    )
    clean = issue_invoice(engine, env, [line(unit="55", env=env)], customer="C-2").document_id
    assert act(engine, env, "approver", service.void_document, document_id=clean, occurred_on=DAY).document_id == clean
    assert act(engine, env, "approver", service.void_document, document_id=clean, occurred_on=DAY).replayed
    other = issue_invoice(engine, env, [line(unit="80", env=env)], customer="C-2").document_id
    draft = act(
        engine,
        env,
        "preparer",
        service.create_draft,
        request_id=uuid4(),
        kind="credit_note",
        customer_account_id=env.customers["C-2"],
        document_date=DAY,
        original_document_id=other,
        lines=[line("Credit", unit="20", env=env)],
        reason="r",
    )
    note = act(engine, env, "approver", service.issue_draft, draft_id=draft.id).document_id
    assert (
        refused(engine, env, "approver", service.void_document, document_id=other, occurred_on=DAY)
        == "void_has_credits"
    )
    act(engine, env, "approver", service.void_document, document_id=note, occurred_on=DAY)
    assert outstanding(engine, env, other) == Decimal("80")
    act(engine, env, "approver", service.void_document, document_id=other, occurred_on=DAY)


@needs_a13
def test_a_migrated_invoice_cannot_be_voided(engine):
    env = world(engine)
    document = issue_invoice(engine, env, [line(unit="100", env=env)]).document_id
    with engine.begin() as c:  # stand in for an imported open item (billing owns the refusal)
        c.execute(sa.text("ALTER TABLE mod_billing.documents DISABLE TRIGGER USER"))
        c.execute(
            sa.text("UPDATE mod_billing.documents SET authoring_mode = 'imported_open_item' WHERE id = :i"),
            {"i": document},
        )
        c.execute(sa.text("ALTER TABLE mod_billing.documents ENABLE TRIGGER USER"))
    assert (
        refused(engine, env, "approver", service.void_document, document_id=document, occurred_on=DAY)
        == "opening_account_owner_required"
    )


@needs_a13
def test_a_mid_chain_billing_refusal_leaves_zero_billing_and_erp_rows(engine):
    env = world(engine)
    draft = draft_invoice(engine, env, [line("A", env=env), line("B", unit="20", env=env)], customer="C-2")
    configure(engine, env, **{settings.KEY_SERIES_INVOICE: "AR-UNKNOWN"})  # numbering fails after the draft document
    before = billing_counts(engine, env)
    code = refused(engine, env, "approver", service.issue_draft, draft_id=draft.id)
    assert code
    after = billing_counts(engine, env)
    for key in ("billing_accounts", "rated_obligations", "documents", "erp_ar_line_accounts"):
        assert after[key] == before[key], key
    with Session(engine) as db:
        assert db.get(ArDraft, draft.id).status == "draft"
    configure(engine, env)  # healed: the same draft now issues
    assert act(engine, env, "approver", service.issue_draft, draft_id=draft.id).document_id


@needs_a13
def test_validation_refusals_at_issue(engine):
    env = world(engine)
    for account, code in (
        ("1400", "revenue_account_is_control"),
        ("2000", "revenue_account_is_control"),
        ("1455", "revenue_account_is_tax"),
        ("1440", "revenue_account_is_tax"),
    ):
        assert (
            refused(
                engine,
                env,
                "preparer",
                service.create_draft,
                request_id=uuid4(),
                kind="invoice",
                customer_account_id=env.customers["C-1"],
                document_date=DAY,
                lines=[line(account=account, env=env)],
            )
            == code
        )
    assert (
        refused(
            engine,
            env,
            "preparer",
            service.create_draft,
            request_id=uuid4(),
            kind="invoice",
            customer_account_id=env.customers["C-1"],
            document_date=DAY,
            lines=[line(unit="-5", env=env)],
        )
        == "negative_line_unsupported"
    )
    assert (
        refused(
            engine,
            env,
            "preparer",
            service.create_draft,
            request_id=uuid4(),
            kind="invoice",
            customer_account_id=env.customers["C-1"],
            document_date=date(2026, 3, 1),
            lines=[line(env=env)],
        )
        == "date_future"
    )
    closed = draft_invoice(engine, env, [line(env=env)], day=date(2025, 12, 20))  # no period covers December
    assert refused(engine, env, "approver", service.issue_draft, draft_id=closed.id) == "period_missing"


@needs_a13
def test_dual_control_on_refuses_same_person_off_allows_it(engine):
    env = world(engine, dual=True)
    draft = draft_invoice(engine, env, [line(env=env)], who="solo")
    assert refused(engine, env, "solo", service.issue_draft, draft_id=draft.id) == "same_person"
    doc = act(engine, env, "approver", service.issue_draft, draft_id=draft.id).document_id
    assert refused(engine, env, "approver", service.void_document, document_id=doc, occurred_on=DAY) == "same_person"
    act(engine, env, "solo", service.void_document, document_id=doc, occurred_on=DAY)
    off = world(engine, dual=False)
    solo = issue_invoice(engine, off, [line(env=off)], who="preparer")  # one person holding both roles
    assert solo.document_id
    reciept_draft = draft_receipt(engine, off, "10")
    assert act(engine, off, "preparer", service.post_receipt, draft_id=reciept_draft.id).settlement_id


@needs_a13
def test_runtime_role_issues_and_posts_end_to_end(engine):
    """The NOBYPASSRLS runtime role does a full issue + receipt + void: every grant the producer needs is there."""
    import os

    env = world(engine)
    role = f"ar_flow_{uuid4().hex[:12]}"
    admin = sa.create_engine(
        sa.make_url(os.environ["ERP_TEST_POSTGRES_URL"]), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOBYPASSRLS')
        c.exec_driver_sql(f'GRANT app_user TO "{role}"')
    rt = sa.create_engine(engine.url.set(username=role), hide_parameters=True)

    def run(who, fn, **kw):
        with Session(rt, expire_on_commit=False) as db:
            db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(env.tenant_id)})
            tenant, party = db.get(Tenant, env.tenant_id), db.get(Party, env.people[who])
            try:
                return fn(db, tenant=tenant, party=party, now=NOW, **kw)
            finally:
                db.commit()

    try:
        draft = run(
            "preparer",
            service.create_draft,
            request_id=uuid4(),
            kind="invoice",
            customer_account_id=env.customers["C-1"],
            document_date=DAY,
            lines=[line(env=env, vat=True)],
        )
        invoice = run("approver", service.issue_draft, draft_id=draft.id)
        receipt = run(
            "preparer",
            service.create_draft,
            request_id=uuid4(),
            kind="receipt",
            customer_account_id=env.customers["C-1"],
            document_date=DAY,
            bank_account_id=env.bank_id,
            bank_reference="RT-REF-1",
            amount=Decimal("107.5"),
            allocations=[AllocationInput(invoice.document_id, Decimal("107.5"))],
        )
        run("approver", service.post_receipt, draft_id=receipt.id)
        assert outstanding(engine, env, invoice.document_id) == 0
        run("approver", service.reverse_receipt, receipt_draft_id=receipt.id, reason="void", occurred_on=DAY)
        assert outstanding(engine, env, invoice.document_id) == Decimal("107.5")
        run("approver", service.void_document, document_id=invoice.document_id, occurred_on=DAY)
    finally:
        rt.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


@needs_a13
def test_gate_check_reports_setup_and_pre_flip_state(engine):
    env = world(engine, configured=False)
    with Session(engine) as db:
        report = checks.ready(db, env.tenant_id)
    assert not report.ok and "ar_setup_incomplete:ar.series.invoice" in report.reasons
    configure(engine, env, enabled=False)
    with Session(engine) as db:
        report = checks.ready(db, env.tenant_id)
    assert "ar_producer_enabled_before_flip" not in report.reasons
    assert not any(r.startswith("ar_rows_before_flip") for r in report.reasons)
    assert not any(r.startswith("ar_setup_incomplete") and "not_enrolled" not in r for r in report.reasons)
    configure(engine, env, enabled=True)
    issue_invoice(engine, env, [line(env=env)])
    with Session(engine) as db:
        report = checks.ready(db, env.tenant_id)
    assert not report.ok
    assert "ar_producer_enabled_before_flip" in report.reasons
    assert "ar_rows_before_flip:erp_ar_drafts" in report.reasons
    assert "ar_rows_before_flip:erp_ar_line_accounts" in report.reasons


@needs_a13
def test_review_fixes_credit_note_shape_treatment_terms_periods_locks_and_replays(engine):
    from erp_v2.migration_ledger.models import MigrationException

    env = world(engine)
    configure(engine, env, **{settings.KEY_TERMS: {"net_30": 30, "net_60": 60}})
    plain = issue_invoice(engine, env, [line(unit="100", env=env)]).document_id  # no VAT on it
    base = dict(
        kind="credit_note",
        customer_account_id=env.customers["C-1"],
        document_date=DAY,
        original_document_id=plain,
        reason="r",
    )
    # 1. more than one credit line is refused for now
    assert (
        refused(
            engine,
            env,
            "preparer",
            service.create_draft,
            request_id=uuid4(),
            lines=[line("a", env=env), line("b", env=env)],
            **base,
        )
        == "credit_note_single_line_only"
    )
    # 2. a taxed credit line whose treatment is not on the original is refused at issue
    taxed = act(
        engine, env, "preparer", service.create_draft, request_id=uuid4(), lines=[line("a", env=env, vat=True)], **base
    )
    assert refused(engine, env, "approver", service.issue_draft, draft_id=taxed.id) == "credit_tax_treatment_mismatch"
    assert outstanding(engine, env, plain) == Decimal("100")
    # 3. per-draft payment terms drive the due date; an unknown code is refused
    sixty = act(
        engine,
        env,
        "preparer",
        service.create_draft,
        request_id=uuid4(),
        kind="invoice",
        customer_account_id=env.customers["C-1"],
        document_date=DAY,
        lines=[line(env=env)],
        payment_terms_code="net_60",
    )
    doc = act(engine, env, "approver", service.issue_draft, draft_id=sixty.id).document_id
    with Session(engine) as db:
        due = queries.billing_document(db, env.tenant_id, doc).due_at
        assert due.astimezone(terms.LAGOS).date() == date(2026, 3, 13)
        assert queries.draft(db, env.tenant_id, sixty.id).payment_terms_code == "net_60"
    assert (
        refused(
            engine,
            env,
            "preparer",
            service.create_draft,
            request_id=uuid4(),
            kind="invoice",
            customer_account_id=env.customers["C-1"],
            document_date=DAY,
            lines=[line(env=env)],
            payment_terms_code="net_999",
        )
        == "payment_terms_unknown"
    )
    # 5. every dated action needs an open period
    with Session(engine) as db:
        account = queries.billing_document(db, env.tenant_id, doc).billing_account_id
    assert (
        refused(
            engine,
            env,
            "approver",
            service.apply_credit,
            billing_account_id=account,
            document_id=doc,
            amount=Decimal(1),
            occurred_on=date(2025, 12, 20),
            request_id=uuid4(),
        )
        == "period_missing"
    )
    assert (
        refused(engine, env, "approver", service.void_document, document_id=doc, occurred_on=date(2025, 12, 20))
        == "period_missing"
    )
    # 8. deallocations of different rows may share a request id; a replay is a no-op
    one = issue_invoice(engine, env, [line(unit="50", env=env)], customer="C-2").document_id
    two = issue_invoice(engine, env, [line(unit="50", env=env)], customer="C-2").document_id
    _, posted = post_receipt(engine, env, "100", [(one, "50"), (two, "50")], customer="C-2", reference="BANK-REF-77")
    with Session(engine) as db:
        rows = queries.allocation_rows(db, env.tenant_id, settlement_id=posted.settlement_id)
    shared = uuid4()
    for row in rows:
        act(engine, env, "approver", service.deallocate, allocation_id=row.id, occurred_on=DAY, request_id=shared)
    assert outstanding(engine, env, one) == outstanding(engine, env, two) == Decimal("50")
    replay = act(
        engine, env, "approver", service.deallocate, allocation_id=rows[0].id, occurred_on=DAY, request_id=shared
    )
    assert replay.replayed
    # 7. the F-D notice is a structural filter over every open E35 item (no truncation)
    with Session(engine) as db:
        for i in range(2105):
            db.add(
                MigrationException(
                    tenant_id=env.tenant_id,
                    class_code="E35",
                    legacy_key=f"row:{i}",
                    source_kind="t",
                    state="open",
                    fingerprint="b" * 64,
                    source_receipt_sha256="a" * 64,
                    detail={"party": ["other", "NGN"]},
                )
            )
        db.add(
            MigrationException(
                tenant_id=env.tenant_id,
                class_code="E35",
                legacy_key="ar_unapplied_credit_note:CN-9",
                source_kind="t",
                state="open",
                fingerprint="c" * 64,
                source_receipt_sha256="a" * 64,
                detail={"party": [str(env.customers["C-1"]), "NGN"]},
            )
        )
        db.commit()
    with Session(engine) as db:
        assert queries.credit_note_notice(db, env.tenant_id, env.customers["C-1"]) == ["ar_unapplied_credit_note:CN-9"]
        assert queries.credit_note_notice(db, env.tenant_id, env.customers["C-2"]) == []


@needs_a13
def test_d9_on_only_the_preparer_may_edit_or_discard_a_draft(engine):
    env = world(engine, dual=True)
    draft = draft_invoice(engine, env, [line(env=env)], who="preparer")
    edit = dict(
        draft_id=draft.id, customer_account_id=env.customers["C-1"], document_date=DAY, lines=[line(unit="5", env=env)]
    )
    assert refused(engine, env, "solo", service.update_draft, **edit) == "not_preparer"
    assert refused(engine, env, "solo", service.discard_draft, draft_id=draft.id) == "not_preparer"
    act(engine, env, "preparer", service.update_draft, **edit)
    off = world(engine, dual=False)
    other = draft_invoice(engine, off, [line(env=off)], who="preparer")
    act(
        engine,
        off,
        "solo",
        service.update_draft,
        draft_id=other.id,
        customer_account_id=off.customers["C-1"],
        document_date=DAY,
        lines=[line(unit="5", env=off)],
    )


@needs_a13
def test_a_manual_due_date_overrides_the_terms_and_is_recorded_as_manual(engine):
    env = world(engine)
    customer = env.customers["C-1"]
    base = dict(kind="invoice", customer_account_id=customer, document_date=DAY, lines=[line(env=env)])
    assert (
        refused(engine, env, "preparer", service.create_draft, request_id=uuid4(), due_date=date(2026, 1, 11), **base)
        == "due_date_before_invoice"
    )
    draft = act(engine, env, "preparer", service.create_draft, request_id=uuid4(), due_date=date(2026, 3, 1), **base)
    doc = act(engine, env, "approver", service.issue_draft, draft_id=draft.id).document_id
    with Session(engine) as db:
        document = queries.billing_document(db, env.tenant_id, doc)
        assert document.due_at.astimezone(terms.LAGOS).date() == date(2026, 3, 1)  # not +30 days
        basis = document.due_date_basis
        assert basis["derivation_policy"] == terms.MANUAL_POLICY and basis["override_actor"] == str(
            env.people["approver"]
        )
        assert queries.draft(db, env.tenant_id, draft.id).due_date == date(2026, 3, 1)
    # the same day is allowed; an edit that moves the invoice date past the due date is refused
    same = act(engine, env, "preparer", service.create_draft, request_id=uuid4(), due_date=DAY, **base)
    assert (
        refused(
            engine,
            env,
            "preparer",
            service.update_draft,
            draft_id=same.id,
            customer_account_id=customer,
            document_date=date(2026, 1, 13),
            lines=[line(env=env)],
            due_date=DAY,
        )
        == "due_date_before_invoice"
    )
    # no manual date: the terms-derived basis is unchanged
    plain = issue_invoice(engine, env, [line(env=env)]).document_id
    with Session(engine) as db:
        assert queries.billing_document(db, env.tenant_id, plain).due_date_basis["derivation_policy"] == (
            "erp_v2.ar.net_days"
        )
