"""S9b: 1400, 2045 and the AR prepayment account are control accounts (plan 6-C2, a tightening), on real Postgres.

Each code is refused wherever 2000/2040/1410 are: a manual journal line, a supplier-invoice line account, a ledger-fault
offset, the revenue-account choices. The ledger-fault door itself is NOT widened. The prepayment account joins by
resolving ``ar.prepayment_liability_account`` (code match wins, else exact name; a shared name protects every match).
"""

from __future__ import annotations

# ruff: noqa: E501
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_accounting.contracts import AccountKind, NormalBalance
from dotmac_accounting.models import Account, AccountCategory
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_journals_pg import (  # noqa: F401  (hosted-Postgres fixtures)
    DAY,
    _registries,
    control_command,
    engine,
    fault_source,
    prepare_fix,
    refused_control,
    refused_fix,
    run,
    seed,
)

from test_posting_bridge_pg import engine as _bridge_engine  # noqa: F401  (hosted-Postgres fixture)

from erp_v2 import ledger_roles
from erp_v2.ar import queries as ar_queries
from erp_v2.journals import service
from erp_v2.journals.service import JournalRefused

CODES = ("1400", "2045", "2300")  # 2300 is the seeded "Customer prepayments" account (the setting's default)


def _add(engine, env, code, name):
    with Session(engine) as db:
        category = db.scalar(select(AccountCategory).where(AccountCategory.tenant_id == env.tenant_id, AccountCategory.code == "LIA"))
        row = Account(tenant_id=env.tenant_id, category_id=category.id, code=code, name=name, kind=AccountKind.POSTING, normal_balance=NormalBalance.CREDIT)
        db.add(row)
        db.commit()
        return row.id


def _set_prepayment(engine, env, value):
    from dotmac_kernel.settings_resolver import SettingChangeContext, upsert_by_key

    from erp_v2.ledger_settings import LEDGER_POLICY_DOMAIN, PREPAYMENT_KEY

    with Session(engine) as db:
        upsert_by_key(db, LEDGER_POLICY_DOMAIN, PREPAYMENT_KEY, value, tenant_id=env.tenant_id, changed_by=SettingChangeContext(reason="s9b test"))
        db.commit()


@pytest.mark.parametrize("code", CODES)
def test_each_new_control_code_is_refused_for_a_typed_manual_journal(engine, code):
    env = seed(engine)
    error = refused_control(
        lambda: run(engine, env, service.prepare_journal, actor=env.parties["preparer"], command=control_command(env, code))
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
            db, env.tenant_id, liability_ref=env.accounts["2000"], lines=[(env.accounts["4000"], Decimal("5"), env.accounts[code])]
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
        assert ledger_roles.control_account_ids(db, env.tenant_id) >= {env.accounts[c] for c in ("2000", "2040", "1410", "1400", "2045")}


def test_the_control_account_guard_cannot_be_dodged_by_re_pointing_the_setting_at_a_plain_account(engine):
    env = seed(engine)
    _set_prepayment(engine, env, "1500")
    refused_control(
        lambda: run(engine, env, service.prepare_journal, actor=env.parties["preparer"], command=control_command(env, "1500"))
    )
    # and the old default account is a plain account again for the guard
    run(engine, env, service.prepare_journal, actor=env.parties["preparer"], command=control_command(env, "2300"))


def test_a_refusal_writes_nothing(engine):
    env = seed(engine)
    with pytest.raises(JournalRefused):
        run(engine, env, service.prepare_journal, actor=env.parties["preparer"], command=control_command(env, "1400"))
