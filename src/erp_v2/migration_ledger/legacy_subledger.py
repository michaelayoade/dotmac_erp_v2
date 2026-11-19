# ruff: noqa: S608  (the only interpolated fragment is the constant "posted by D" predicate; values are bound)
"""The legacy subledger at the opening date D, RECOMPUTED from the snapshot (B-9 section 12.3b-addendum).

Read only (the caller's connection is ``SET TRANSACTION READ ONLY``). All amounts are functional. Sign convention:
net = debit - credit (liabilities negative, AR and loans positive), the same as the TB and the bridge.

"Posted by D" for any document with a ``journal_entry_id``: its ``gl.journal_entry`` is POSTED or REVERSED with
``posting_date <= D`` and there is NO journal r with ``r.journal_entry_id = j.reversal_journal_id``, POSTED or
REVERSED, ``posting_date <= D``. The CURRENT document status is never used: a document voided or paid after D was open
at D.

* 2000: -sum over invoices posted by D of (``functional_currency_amount`` - allocations dated <= D whose payment is
  posted by D; allocation = (``allocated_amount`` + ``discount_taken``) x the invoice ``exchange_rate``). The credit
  note
  row is stored negative, so it is never negated.
* 1400: sum of item residuals (``ar.invoice``, allocations also include ``write_off_amount``) plus the unallocated
  cash of
  payments posted by D (``gross_amount`` x ``exchange_rate`` less allocations dated <= D to invoices posted by D,
  negated).
* 2040: payroll net pay is 0 at D (refused if any salary slip was posted by D: the recipe would be missing); expense
  claims posted by D: ``net_payable_amount`` less the reimbursement postings dated <= D on the claim family
  {2040, 2030, 2110} (``gl.posted_ledger_line`` with ``source_document_type = 'EXPENSE_REIMBURSEMENT'`` and
  ``source_document_id`` = the claim id), negated.
* 1410: 0 (refused if any ``payroll.employee_loan`` was disbursed by D: the recipe would be missing).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

import sqlalchemy as sa

from erp_v2.migration_ledger.common import ZERO, refuse

CLAIM_FAMILY: Final = ("2040", "2030", "2110")


def _posted_by(alias: str) -> str:
    return (
        f"({alias}.status IN ('POSTED', 'REVERSED') AND {alias}.posting_date <= :d AND NOT EXISTS ("
        "SELECT 1 FROM gl.journal_entry r WHERE r.journal_entry_id = "
        f"{alias}.reversal_journal_id AND r.status IN ('POSTED', 'REVERSED') AND r.posting_date <= :d))"
    )


def _scalar(conn: sa.Connection, sql: str, **params) -> Decimal:
    return Decimal(conn.execute(sa.text(sql), params).scalar() or 0)


def sub_2000(conn: sa.Connection, d: date) -> Decimal:
    invoices = _scalar(
        conn,
        "SELECT coalesce(sum(i.functional_currency_amount), 0) FROM ap.supplier_invoice i "
        f"JOIN gl.journal_entry j ON j.journal_entry_id = i.journal_entry_id WHERE {_posted_by('j')}",
        d=d,
    )
    allocations = _scalar(
        conn,
        "SELECT coalesce(sum((a.allocated_amount + a.discount_taken) * i.exchange_rate), 0) "
        "FROM ap.payment_allocation a "
        "JOIN ap.supplier_invoice i ON i.invoice_id = a.invoice_id "
        "JOIN gl.journal_entry ij ON ij.journal_entry_id = i.journal_entry_id "
        "JOIN ap.supplier_payment p ON p.payment_id = a.payment_id "
        "JOIN gl.journal_entry pj ON pj.journal_entry_id = p.journal_entry_id "
        f"WHERE a.allocation_date <= :d AND {_posted_by('ij')} AND {_posted_by('pj')}",
        d=d,
    )
    return -(invoices - allocations)


def sub_1400(conn: sa.Connection, d: date) -> Decimal:
    items = _scalar(
        conn,
        "SELECT coalesce(sum(i.functional_currency_amount), 0) FROM ar.invoice i "
        f"JOIN gl.journal_entry j ON j.journal_entry_id = i.journal_entry_id WHERE {_posted_by('j')}",
        d=d,
    )
    item_allocations = _scalar(
        conn,
        "SELECT coalesce(sum((a.allocated_amount + a.discount_taken + a.write_off_amount) * i.exchange_rate), 0) "
        "FROM ar.payment_allocation a "
        "JOIN ar.invoice i ON i.invoice_id = a.invoice_id "
        "JOIN gl.journal_entry ij ON ij.journal_entry_id = i.journal_entry_id "
        "JOIN ar.customer_payment p ON p.payment_id = a.payment_id "
        "JOIN gl.journal_entry pj ON pj.journal_entry_id = p.journal_entry_id "
        f"WHERE a.allocation_date <= :d AND {_posted_by('ij')} AND {_posted_by('pj')}",
        d=d,
    )
    cash = _scalar(
        conn,
        "SELECT coalesce(sum(p.gross_amount * p.exchange_rate), 0) FROM ar.customer_payment p "
        f"JOIN gl.journal_entry j ON j.journal_entry_id = p.journal_entry_id WHERE {_posted_by('j')}",
        d=d,
    )
    cash_allocated = _scalar(
        conn,
        "SELECT coalesce(sum(a.allocated_amount * i.exchange_rate), 0) FROM ar.payment_allocation a "
        "JOIN ar.invoice i ON i.invoice_id = a.invoice_id "
        "JOIN gl.journal_entry ij ON ij.journal_entry_id = i.journal_entry_id "
        "JOIN ar.customer_payment p ON p.payment_id = a.payment_id "
        "JOIN gl.journal_entry pj ON pj.journal_entry_id = p.journal_entry_id "
        f"WHERE a.allocation_date <= :d AND {_posted_by('ij')} AND {_posted_by('pj')}",
        d=d,
    )
    return (items - item_allocations) - (cash - cash_allocated)


def sub_2040(conn: sa.Connection, d: date, family_account_ids: list[str]) -> Decimal:
    slips = _scalar(
        conn,
        "SELECT count(*) FROM payroll.salary_slip s JOIN gl.journal_entry j ON j.journal_entry_id = s.journal_entry_id "
        f"WHERE {_posted_by('j')}",
        d=d,
    )
    if slips:
        refuse("legacy_subledger_recipe_missing", "2040: payroll net pay was posted by the opening date")
    claims = _scalar(
        conn,
        "SELECT coalesce(sum(c.net_payable_amount), 0) FROM expense.expense_claim c "
        f"JOIN gl.journal_entry j ON j.journal_entry_id = c.journal_entry_id WHERE {_posted_by('j')}",
        d=d,
    )
    reimbursed = Decimal(
        conn.execute(
            sa.text(
                "SELECT coalesce(sum(l.debit_amount - l.credit_amount), 0) FROM gl.posted_ledger_line l "
                "JOIN expense.expense_claim c ON c.claim_id::text = l.source_document_id::text "
                "JOIN gl.journal_entry j ON j.journal_entry_id = c.journal_entry_id "
                "WHERE l.source_document_type = 'EXPENSE_REIMBURSEMENT' AND l.posting_date <= :d "
                "AND l.account_id::text = ANY(:family) AND " + _posted_by("j")
            ),
            {"d": d, "family": family_account_ids},
        ).scalar()
        or 0
    )
    return -(claims - reimbursed)


def sub_1410(conn: sa.Connection, d: date) -> Decimal:
    loans = _scalar(conn, "SELECT count(*) FROM payroll.employee_loan WHERE disbursement_date <= :d", d=d)
    if loans:
        refuse("legacy_subledger_recipe_missing", "1410: a staff loan was disbursed by the opening date")
    return ZERO


def recompute(conn: sa.Connection, d: date, family_account_ids: list[str]) -> dict[str, Decimal]:
    """Sub(2000, 1400, 2040, 1410) at D from the snapshot."""
    return {
        "2000": sub_2000(conn, d),
        "1400": sub_1400(conn, d),
        "2040": sub_2040(conn, d, family_account_ids),
        "1410": sub_1410(conn, d),
    }
