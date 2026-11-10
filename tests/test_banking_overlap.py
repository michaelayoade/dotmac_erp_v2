"""Candidate evidence only; SQLite does not prove PostgreSQL RLS."""

from datetime import date
from decimal import Decimal
from unittest.mock import ANY, Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_banking.models import BankStatement, BankStatementLine
from fastapi import HTTPException
from test_banking_observations import accept, fixture_document
from test_banking_observations import banking as banking
from test_workflow_route_security import browser as browser

from erp_v2.banking import overlap
from erp_v2.banking.models import BankingObservationReceipt


def seed(banking):
    db, principal, binding, account = banking
    accept(db, principal, fixture_document(binding.installation_id))
    statement = db.scalar(sa.select(BankStatement))
    line = db.scalar(sa.select(BankStatementLine))
    return db, principal, account, statement, line


def manual(db, statement, line, *, statement_changes=None, line_changes=None):
    def copy(model, original):
        return {c.name: getattr(original, c.name) for c in model.__table__.columns if c.name != "created_at"}

    values = copy(BankStatement, statement)
    values.update(id=uuid4(), statement_ref=str(uuid4()), source_ref=str(uuid4()))
    values.update(statement_changes or {})
    imported = BankStatement(**values)
    values = copy(BankStatementLine, line)
    values.update(id=uuid4(), statement_id=imported.id)
    values.update(line_changes or {})
    other = BankStatementLine(**values)
    db.add_all([imported, other])
    db.flush()
    return imported, other


def test_reference_candidate_keeps_changed_dates_and_evidence(banking):
    db, principal, account, statement, line = seed(banking)
    imported, other = manual(db, statement, line, line_changes={"transaction_date": date(2026, 1, 1)})
    result = overlap.review(db, tenant_id=principal.tenant_id, account_id=account.id)
    (row,) = result["candidates"]
    assert row[0].statement_id == statement.id
    assert row[4].id == imported.id and row[5].id == other.id
    assert row[3].transaction_date != row[5].transaction_date
    assert row[6] is True


@pytest.mark.parametrize("reference", ["", "   ", "\t\r\n", "different", "TXN-1", " txn-1 "])
def test_unshared_and_blank_references_are_only_weak_candidates(banking, reference):
    db, principal, _, statement, line = seed(banking)
    manual(db, statement, line, line_changes={"external_ref": reference})
    (row,) = overlap.review(db, tenant_id=principal.tenant_id)["candidates"]
    assert row[6] is False


@pytest.mark.parametrize(
    "statement_changes,line_changes",
    [
        ({"tenant_id": uuid4()}, {"tenant_id": uuid4()}),
        ({"account_id": uuid4()}, {}),
        ({"currency_code": "USD"}, {}),
        ({"minor_units": 0}, {}),
        ({}, {"direction": "debit"}),
        ({}, {"amount": Decimal("123.46")}),
        ({}, {"external_ref": "different", "transaction_date": date(2026, 1, 1)}),
    ],
)
def test_candidates_require_same_tenant_account_money_and_evidence(banking, statement_changes, line_changes):
    db, principal, _, statement, line = seed(banking)
    manual(db, statement, line, statement_changes=statement_changes, line_changes=line_changes)
    assert overlap.review(db, tenant_id=principal.tenant_id)["candidates"] == []


@pytest.mark.parametrize("reference", ["", "   ", "\t\r\n"])
def test_blank_references_on_both_sides_do_not_prove_identity(banking, reference):
    db, principal, _, statement, line = seed(banking)
    line.external_ref = reference
    db.flush()
    manual(db, statement, line, line_changes={"transaction_date": date(2026, 1, 1)})
    assert overlap.review(db, tenant_id=principal.tenant_id)["candidates"] == []


def test_other_accepted_observations_are_excluded(banking):
    db, principal, _, statement, line = seed(banking)
    imported, _ = manual(db, statement, line)
    original = db.scalar(sa.select(BankingObservationReceipt))
    values = {c.name: getattr(original, c.name) for c in original.__table__.columns if c.name != "created_at"}
    values.update(id=uuid4(), statement_id=imported.id, idempotency_key="other", provider_transaction_id="other")
    db.add(BankingObservationReceipt(**values))
    db.flush()
    assert overlap.review(db, tenant_id=principal.tenant_id)["candidates"] == []


def test_account_selection_fails_closed_and_paging_is_bounded(banking, monkeypatch):
    db, principal, account, statement, line = seed(banking)
    for _ in range(overlap.PAGE_SIZE + 1):
        manual(db, statement, line)
    db.commit()
    # Read queries must not flush even pre-existing pending changes.
    account.account_name = "Pending change"
    monkeypatch.setattr(db, "flush", Mock(side_effect=AssertionError("unexpected flush")))
    monkeypatch.setattr(db, "commit", Mock(side_effect=AssertionError("unexpected commit")))
    first = overlap.review(db, tenant_id=principal.tenant_id)
    second = overlap.review(db, tenant_id=principal.tenant_id, page=2)
    assert len(first["candidates"]) == overlap.PAGE_SIZE and first["has_more"]
    assert len(second["candidates"]) == 1 and not second["has_more"]
    assert first["candidates"][0][5].id != second["candidates"][0][5].id
    assert account in db.dirty and not db.new and not db.deleted
    with pytest.raises(HTTPException) as exc:
        overlap.review(db, tenant_id=uuid4(), account_id=account.id)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        overlap.review(db, tenant_id=principal.tenant_id, page=overlap.MAX_PAGE + 1)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("role,expected", [(None, 302), ("staff", 403), ("admin", 200)])
def test_overlap_route_requires_financial_permission(browser, monkeypatch, role, expected):
    read = Mock(
        return_value={
            "accounts": [],
            "candidates": [],
            "account_id": None,
            "page": 1,
            "has_more": False,
            "accounts_truncated": False,
            "max_page": overlap.MAX_PAGE,
        }
    )
    monkeypatch.setattr(overlap, "review", read)
    browser.sign_in(role)
    response = browser.client.get("/finance/banking/overlap-review")
    assert response.status_code == expected
    if expected == 200:
        read.assert_called_once_with(ANY, tenant_id=browser.tenant.id, account_id=None, page=1)
        assert "Weak candidate" not in response.text
        assert "Review is required before feed activation" in response.text
    else:
        read.assert_not_called()


def test_populated_candidate_renders_without_mutation(banking, browser, monkeypatch):
    db, principal, _, statement, line = seed(banking)
    manual(db, statement, line, line_changes={"transaction_date": date(2026, 1, 1)})
    packet = overlap.review(db, tenant_id=principal.tenant_id)
    read = Mock(return_value=packet)
    monkeypatch.setattr(overlap, "review", read)
    browser.sign_in("admin")
    response = browser.client.get("/finance/banking/overlap-review")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Reference candidate" in response.text and "Transaction dates differ" in response.text
    assert "Human review required" in response.text
    for action in browser.actions:
        action.assert_not_called()
