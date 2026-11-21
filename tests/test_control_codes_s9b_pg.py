"""S9b: 1400, 2045 and the AR prepayment account are control accounts (plan 6-C2, a tightening), on real Postgres.

Each code is refused wherever 2000/2040/1410 are: a manual journal line, a supplier-invoice line account, a ledger-fault
offset, the revenue-account choices. The ledger-fault door itself is NOT widened. The prepayment account joins by
resolving ``ar.prepayment_liability_account`` (code match wins, else exact name; a shared name protects every match).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from dotmac_accounting.contracts import AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_journals_pg import (  # noqa: F401  (hosted-Postgres fixtures)
    DAY,
    NOW,
    _registries,
    control_command,
    fault_source,
    prepare_fix,
    refused_control,
    refused_fix,
    run,
)
from test_journals_pg import (
    seed as _seed,
)
from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2 import ledger_roles
from erp_v2.ar import queries as ar_queries
from erp_v2.journals import service
from erp_v2.journals.service import JournalRefused


@pytest.fixture(scope="module")
def engine(_bridge_engine):  # noqa: F811
    return _bridge_engine


CODES = ("1400", "2045", "2300")  # 2300 is the seeded "Customer prepayments" account (the setting's default)


def seed(engine):
    """The journals world plus the AR prepayment account under its default name (kept out of the shared seed so that
    other worlds, which add their own "Customer prepayments", are not made ambiguous)."""
    env = _seed(engine)
    env.accounts["2045"] = _add(engine, env, "2045", "Claims")
    env.accounts["2300"] = _add(engine, env, "2300", "Customer prepayments")
    return env


def _add(engine, env, code, name):
    with Session(engine) as db:
        category = db.scalar(
            select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "LIA")
        )
        row = Account(
            tenant_id=env.tenant_id,
            category_id=category.id,
            code=code,
            name=name,
            kind=AccountKind.POSTING,
            normal_balance=NormalBalance.CREDIT,
        )
        db.add(row)
        db.commit()
        return row.id


def _set_ar(engine, env, key, value):
    from dotmac_kernel.settings_resolver import SettingChangeContext, upsert_by_key

    from erp_v2.ledger_settings import LEDGER_POLICY_DOMAIN

    with Session(engine) as db:
        upsert_by_key(
            db,
            LEDGER_POLICY_DOMAIN,
            key,
            value,
            tenant_id=env.tenant_id,
            changed_by=SettingChangeContext(reason="s9b test"),
        )
        db.commit()


def _set_prepayment(engine, env, value):
    from dotmac_kernel.settings_resolver import SettingChangeContext, upsert_by_key

    from erp_v2.ledger_settings import LEDGER_POLICY_DOMAIN, PREPAYMENT_KEY

    with Session(engine) as db:
        upsert_by_key(
            db,
            LEDGER_POLICY_DOMAIN,
            PREPAYMENT_KEY,
            value,
            tenant_id=env.tenant_id,
            changed_by=SettingChangeContext(reason="s9b test"),
        )
        db.commit()


@pytest.mark.parametrize("code", CODES)
def test_each_new_control_code_is_refused_for_a_typed_manual_journal(engine, code):
    env = seed(engine)
    error = refused_control(
        lambda: run(
            engine, env, service.prepare_journal, actor=env.parties["preparer"], command=control_command(env, code)
        )
    )
    assert code in error.detail


@pytest.mark.parametrize("code", CODES)
def test_each_new_control_code_is_refused_as_a_supplier_invoice_line_or_tax_account(engine, code):
    env = seed(engine)
    with Session(engine) as db:
        as_line = ledger_roles.invoice_account_violation(
            db, env.tenant_id, liability_ref=env.accounts["2000"], lines=[(env.accounts[code], None, None)]
        )
        as_tax = ledger_roles.invoice_account_violation(
            db,
            env.tenant_id,
            liability_ref=env.accounts["2000"],
            lines=[(env.accounts["4000"], Decimal("5"), env.accounts[code])],
        )
    assert as_line is not None and as_line[0] == "line_account_is_control"
    assert as_tax is not None and as_tax[0] == "line_account_is_control"


@pytest.mark.parametrize("code", CODES)
def test_each_new_control_code_is_refused_as_a_ledger_fault_offset(engine, code):
    env = seed(engine)
    source = fault_source(engine, env)
    refused_fix(lambda: prepare_fix(engine, env, source, offset=code), "offset_is_control_account")


def test_the_ledger_fault_door_is_not_widened_to_the_new_control_codes(engine):
    env = seed(engine)
    source = fault_source(engine, env)
    for code in CODES:
        refused_fix(lambda code=code: prepare_fix(engine, env, source, code=code), "invalid_input")


def test_revenue_account_choices_exclude_every_control_account_including_the_prepayment_account(engine):
    env = seed(engine)
    with Session(engine) as db:
        offered = {a.code for a in ar_queries.revenue_account_choices(db, env.tenant_id)}
    assert "4000" in offered
    assert not offered & set(CODES) | (offered & {"2000", "2040", "1410"})


def test_the_prepayment_account_follows_the_setting_by_code_by_name_and_never_leaves_a_shared_name_open(engine):
    env = seed(engine)
    with Session(engine) as db:
        assert ledger_roles.prepayment_account_ids(db, env.tenant_id) == {env.accounts["2300"]}  # default name
    _set_prepayment(engine, env, "1500")  # a code
    with Session(engine) as db:
        assert ledger_roles.prepayment_account_ids(db, env.tenant_id) == {env.accounts["1500"]}
        assert env.accounts["2300"] not in ledger_roles.control_account_ids(db, env.tenant_id)
    first, second = _add(engine, env, "2301", "Twin"), _add(engine, env, "2302", "Twin")
    _set_prepayment(engine, env, "Twin")  # a shared name, no code match: protect all matches
    with Session(engine) as db:
        assert ledger_roles.prepayment_account_ids(db, env.tenant_id) == {first, second}
    _set_prepayment(engine, env, "No such account")  # nothing resolves: nothing to protect, no error
    with Session(engine) as db:
        assert ledger_roles.prepayment_account_ids(db, env.tenant_id) == frozenset()
        assert ledger_roles.control_account_ids(db, env.tenant_id) >= {
            env.accounts[c] for c in ("2000", "2040", "1410", "1400", "2045")
        }


def _post_t1(engine, env, credit_code):
    """A posted T1 journal (owner erp.migration, kind ar_opening_credit): Dr 1400 / Cr ``credit_code``."""
    from dotmac_kernel.cache import TenantScope

    from erp_v2.holdings import posting as hposting
    from erp_v2.posting.bridge import post_consequence

    lines = (
        hposting.Line(str(env.accounts["1400"]), Decimal("50"), Decimal(0), "t1"),
        hposting.Line(str(env.accounts[credit_code]), Decimal(0), Decimal("50"), "t1"),
    )
    source = hposting.Source("erp.migration", "ar_opening_credit", str(uuid4()), "1", "b" * 64)
    consequence = hposting.Consequence(source, DAY, "t1", "USD", Decimal(1), lines, "b" * 64)
    with Session(engine) as db:
        post_consequence(
            db, scope=TenantScope(env.tenant_id), consequence=consequence, approval_ref="t1", actor="t1", now=NOW
        )
        db.commit()


def test_re_pointing_the_setting_unprotects_an_unfunded_account_but_never_a_t1_funded_one(engine):
    env = seed(engine)
    _set_prepayment(engine, env, "1500")
    refused_control(
        lambda: run(
            engine, env, service.prepare_journal, actor=env.parties["preparer"], command=control_command(env, "1500")
        )
    )
    # 2300 was never funded by T1: a plain account again once the setting moves away from it
    run(engine, env, service.prepare_journal, actor=env.parties["preparer"], command=control_command(env, "2300"))
    # an account that carries a T1 credit stays protected after the setting is re-pointed (F2)
    env2 = seed(engine)
    _post_t1(engine, env2, "2300")
    _set_prepayment(engine, env2, "1500")
    with Session(engine) as db:
        assert env2.accounts["2300"] in ledger_roles.t1_credit_account_ids(db, env2.tenant_id)
        assert env2.accounts["2300"] in ledger_roles.control_account_ids(db, env2.tenant_id)
    refused_control(
        lambda: run(
            engine, env2, service.prepare_journal, actor=env2.parties["preparer"], command=control_command(env2, "2300")
        )
    )


@pytest.mark.parametrize("key", ["ar.prepayment_liability_account", "ar.prepaid_funding_account"])
def test_every_account_the_mapper_resolves_for_an_ar_setting_is_protected(engine, key):
    """F1: the mapper's resolution (id, code or exact name) must always land inside the guard's control set."""
    from erp_v2.billing_gl import settings as mapper_settings

    env = seed(engine)
    target = env.accounts["1500"]
    for value in (str(target), "1500", "Plain asset"):
        with Session(engine) as db:  # make the name form resolvable
            from dotmac_accounting.models import Account

            db.get(Account, target).name = "Plain asset"
            db.commit()
        _set_ar(engine, env, key, value)
        with Session(engine) as db:
            resolved = mapper_settings._account(db, env.tenant_id, value)
            assert resolved is not None and UUID(resolved) == target
            assert target in ledger_roles.control_account_ids(db, env.tenant_id), (key, value)
        refused_control(
            lambda: run(
                engine,
                env,
                service.prepare_journal,
                actor=env.parties["preparer"],
                command=control_command(env, "1500"),
            )
        )
        _set_ar(engine, env, key, "No such account")  # un-protect again for the next form


def test_a_refusal_writes_nothing(engine):
    env = seed(engine)
    with pytest.raises(JournalRefused):
        run(engine, env, service.prepare_journal, actor=env.parties["preparer"], command=control_command(env, "1400"))


def test_holdings_resolve_account_accepts_the_id_form_like_the_mapper_and_the_guard(engine):
    from erp_v2.holdings.config import resolve_account
    from erp_v2.holdings.errors import HoldingRefused

    env, other = seed(engine), seed(engine)
    target = env.accounts["1500"]
    with Session(engine) as db:
        for form in (str(target), "1500"):
            assert resolve_account(db, env.tenant_id, form, what="t").id == target
        with pytest.raises(HoldingRefused):  # a REAL account id, but of another tenant: never resolved
            resolve_account(db, env.tenant_id, str(other.accounts["1500"]), what="t")
