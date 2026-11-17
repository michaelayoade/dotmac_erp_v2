"""Tax account of an imported supplier-invoice line (``tax_account_ref``): one pure rule.

The payables owner requires a tax account on every taxed line. Legacy never stored one on the
line, so it is derived here from the legacy tax code and, for invoices legacy already posted,
from the legacy GL as posted. One pure function is shared by the importer (enrolment map) and by
the three verifiers (unpaid admission, settled admission, B1b), so the mapped value and the
proof can never diverge.

Two regimes, per invoice (design RUN-G-TAX-ACCOUNT-DESIGN, revision 3):

* Legacy-posted (the invoice's legacy journal has ledger lines; ledger lines are GL truth). The
  GL is history, so the lines follow what legacy actually posted:

  - A1 split: every taxed line splits on the snapshot account (legacy's tax-paid account, or its
    deferral pair), the ledger debit on each such account equals the tax mapped to it, no other
    tax-code account is debited, and the debits total the invoice. ``tax_account_ref`` is that
    split account. Where the expense debits sit is not proved (the g6 population has invoices whose
    expense was posted on another account).
  - A2 folded: no debit on any tax-code paid or deferral account, and per expense account the
    debit equals the sum of (line_amount + tax_amount). ``tax_account_ref`` is the line's own
    expense account (D1).
  - A3 other: anything else. The importer still lands the line's expense account (so the row is
    owner-valid) and the verifiers refuse the invoice (``legacy_gl_tax_treatment_unrecognised``).

* Not legacy-posted: legacy's CURRENT posting rule (``ap/posting/invoice.py``,
  ``posting/tax.py:resolve_tax_posting_account_id``), because v2 posts these invoices now.

  - B1 split (tax != 0, a tax code of the invoice's organisation, VAT or GST, a tax-paid
    account): the deferral pair of the tax-paid account when set (D4), else the tax-paid account.
  - B2 folded (everything else taxed): the line's expense account (D1).

Untaxed lines have no tax account. Accounts are canonical UUID strings, the form
``posting_account_ref`` uses. Nothing here guesses: a missing referenced row is the caller's
named refusal, never a silent fold.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

SPLIT_TAX_TYPES: Final = frozenset({"VAT", "GST"})
CLASSES: Final = ("A1", "A2", "A3", "B1", "B2")
LEGACY_POSTED_CLASSES: Final = frozenset({"A1", "A2", "A3"})


class TaxAccountMappingError(ValueError):
    """A fact the rule needs is absent or malformed; the line is refused, never guessed."""


def canonical(value: object) -> str | None:
    """Canonical account/entity id string (dashed UUID when it parses)."""

    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


@dataclass(frozen=True)
class TaxCodeFacts:
    organization_id: object
    tax_type: object
    paid_account_id: object  # tax.tax_code.tax_paid_account_id
    deferral_account_id: object  # gl.account.deferral_pair_account_id of the paid account


@dataclass(frozen=True)
class LineFacts:
    line_id: object
    line_amount: Decimal
    tax_amount: Decimal
    expense_account_id: object
    tax_code: TaxCodeFacts | None  # None only when the line has no tax code


@dataclass(frozen=True)
class LineTax:
    klass: str | None  # A1 A2 A3 B1 B2, None for an untaxed line
    ref: str | None  # canonical account id, None for an untaxed line


@dataclass(frozen=True)
class InvoiceTax:
    legacy_posted: bool
    lines: Mapping[str, LineTax]  # keyed by canonical line id
    klass: str | None  # invoice class: A1 A2 A3, "B" (unposted taxed), None (no taxed line)

    @property
    def other(self) -> bool:
        """``tax_treatment_other``: legacy GL shows a treatment the rule does not recognise."""
        return self.klass == "A3"


def _money(value: object, what: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise TaxAccountMappingError(f"{what}_not_numeric") from exc
    if not amount.is_finite():
        raise TaxAccountMappingError(f"{what}_not_numeric")
    return amount


def snapshot_split_account(line: LineFacts, organization_id: object) -> str | None:
    """Legacy's current split rule for one line: the account, or None when the line folds."""

    code = line.tax_code
    if (
        _money(line.tax_amount, "tax_amount") == 0
        or code is None
        or canonical(code.organization_id) != canonical(organization_id)
        or str(code.tax_type).strip().upper() not in SPLIT_TAX_TYPES
        or code.paid_account_id is None
    ):
        return None
    return canonical(code.deferral_account_id if code.deferral_account_id is not None else code.paid_account_id)


def _nonzero(amounts: Mapping[str, Decimal]) -> dict[str, Decimal]:
    return {account: value for account, value in amounts.items() if value != 0}


def _add(target: dict[str, Decimal], account: object, amount: Decimal) -> None:
    key = canonical(account)
    if key is None:
        raise TaxAccountMappingError("line_expense_account_missing")
    target[key] = target.get(key, Decimal(0)) + amount


def classify_invoice(
    organization_id: object,
    lines: Sequence[LineFacts],
    ledger_debits: Mapping[object, Decimal] | None,
) -> InvoiceTax:
    """Classify one invoice's taxed lines.

    ``ledger_debits`` is the per-account sum of the legacy posted ledger debits of the invoice's
    journal, or None when the invoice is not legacy-posted (no ledger lines).
    """

    taxed = [line for line in lines if _money(line.tax_amount, "tax_amount") != 0]
    if any(_money(line.tax_amount, "tax_amount") < 0 or _money(line.line_amount, "line_amount") < 0 for line in lines):
        raise TaxAccountMappingError("negative_line_amount")
    result: dict[str, LineTax] = {}
    for line in lines:
        key = canonical(line.line_id)
        if key is None or key in result:
            raise TaxAccountMappingError("line_id_missing_or_duplicate")
        result[key] = LineTax(None, None)
    if not taxed:
        return InvoiceTax(ledger_debits is not None, result, None)
    splits = {canonical(line.line_id): snapshot_split_account(line, organization_id) for line in taxed}

    if ledger_debits is None:
        for line in taxed:
            account = splits[canonical(line.line_id)]
            if account is not None:
                result[canonical(line.line_id)] = LineTax("B1", account)
            else:
                result[canonical(line.line_id)] = LineTax("B2", canonical(line.expense_account_id))
        return InvoiceTax(False, result, "B")

    actual: dict[str, Decimal] = {}
    for account, amount in ledger_debits.items():
        _add(actual, account, _money(amount, "ledger_debit"))
    actual = _nonzero(actual)

    tax_accounts = {account for account in splits.values() if account is not None}
    for line in taxed:
        if line.tax_code is not None:
            tax_accounts.update(
                canonical(a) for a in (line.tax_code.paid_account_id, line.tax_code.deferral_account_id) if a
            )
    total = sum((line.line_amount + line.tax_amount for line in lines), Decimal(0))

    # A1: every taxed line splits on the snapshot account; the ledger debit on each split account is
    # exactly the tax mapped to it, no other tax-code account is debited, and the debits total the
    # invoice. Where the expense debits sit is not part of the proof: the ref is the tax account.
    if all(account is not None for account in splits.values()):
        split_tax: dict[str, Decimal] = {}
        for line in taxed:
            _add(split_tax, splits[canonical(line.line_id)], line.tax_amount)
        if (
            all(actual.get(account, Decimal(0)) == tax for account, tax in split_tax.items())
            and not ((tax_accounts - split_tax.keys()) & actual.keys())
            and sum(actual.values(), Decimal(0)) == total
        ):
            for line in taxed:
                result[canonical(line.line_id)] = LineTax("A1", splits[canonical(line.line_id)])
            return InvoiceTax(True, result, "A1")

    # A2: no debit on any tax-code paid or deferral account; per expense account the debit equals
    # line amount plus tax.
    folded: dict[str, Decimal] = {}
    for line in lines:
        _add(folded, line.expense_account_id, line.line_amount + line.tax_amount)
    if not (tax_accounts & actual.keys()) and _nonzero(folded) == actual:
        for line in taxed:
            result[canonical(line.line_id)] = LineTax("A2", canonical(line.expense_account_id))
        return InvoiceTax(True, result, "A2")

    # A3: land the folded form (owner-valid); the verifiers refuse the invoice.
    for line in taxed:
        result[canonical(line.line_id)] = LineTax("A3", canonical(line.expense_account_id))
    return InvoiceTax(True, result, "A3")


def class_counts(outcomes: Iterable[InvoiceTax]) -> dict[str, dict[str, int]]:
    """Receipt counts only: per class, taxed lines and invoices holding such a line."""

    counts = {name: {"lines": 0, "invoices": 0} for name in CLASSES}
    for outcome in outcomes:
        seen: set[str] = set()
        for line in outcome.lines.values():
            if line.klass is not None:
                counts[line.klass]["lines"] += 1
                seen.add(line.klass)
        for name in seen:
            counts[name]["invoices"] += 1
    return counts


# ---------------------------------------------------------------------------------------------
# The one read-only SQL of the importer (legacy names, resolved through the snapshot search_path).

INVOICE_LINE_SQL: Final = """
SELECT i.invoice_id, i.organization_id, i.journal_entry_id,
       l.line_id, l.line_amount, l.tax_amount, l.expense_account_id, l.tax_code_id,
       tc.tax_code_id AS code_row, tc.organization_id AS code_organization_id, tc.tax_type,
       tc.tax_paid_account_id,
       pa.account_id AS paid_row, pa.deferral_pair_account_id,
       da.account_id AS deferral_row
FROM supplier_invoice_line l
JOIN supplier_invoice i ON i.invoice_id = l.invoice_id
LEFT JOIN tax_code tc ON tc.tax_code_id = l.tax_code_id
LEFT JOIN account pa ON pa.account_id = tc.tax_paid_account_id
LEFT JOIN account da ON da.account_id = pa.deferral_pair_account_id
"""

LEDGER_DEBIT_SQL: Final = """
SELECT journal_entry_id, account_id, debit_amount
FROM posted_ledger_line
WHERE journal_entry_id IN (SELECT journal_entry_id FROM supplier_invoice WHERE journal_entry_id IS NOT NULL)
"""


def code_facts(
    tax_code_id: object,
    code_row: object,
    organization_id: object,
    tax_type: object,
    paid_account_id: object,
    paid_row: object,
    deferral_account_id: object,
    deferral_row: object,
) -> TaxCodeFacts | None:
    """Tax-code facts of one line, or a named refusal when a referenced row is missing."""

    if tax_code_id is None:
        return None
    if code_row is None:
        raise TaxAccountMappingError("tax_code_row_missing")
    if paid_account_id is not None and paid_row is None:
        raise TaxAccountMappingError("tax_paid_account_row_missing")
    if deferral_account_id is not None and deferral_row is None:
        raise TaxAccountMappingError("tax_deferral_account_row_missing")
    return TaxCodeFacts(organization_id, tax_type, paid_account_id, deferral_account_id)


def classify_snapshot(
    rows: Iterable[Mapping[str, object]], ledger: Iterable[Mapping[str, object]]
) -> dict[str, InvoiceTax]:
    """Classify every invoice of ``INVOICE_LINE_SQL`` rows against ``LEDGER_DEBIT_SQL`` rows.

    Keyed by canonical invoice id. Pure over already-read rows.
    """

    debits: dict[str, dict[str, Decimal]] = {}
    for row in ledger:
        journal = canonical(row["journal_entry_id"])
        account = debits.setdefault(journal, {})
        key = canonical(row["account_id"])
        account[key] = account.get(key, Decimal(0)) + _money(row["debit_amount"] or 0, "ledger_debit")
    grouped: dict[str, tuple[object, str | None, list[LineFacts]]] = {}
    for row in rows:
        invoice = canonical(row["invoice_id"])
        entry = grouped.setdefault(invoice, (row["organization_id"], canonical(row["journal_entry_id"]), []))
        entry[2].append(
            LineFacts(
                line_id=row["line_id"],
                line_amount=_money(row["line_amount"], "line_amount"),
                tax_amount=_money(row["tax_amount"], "tax_amount"),
                expense_account_id=row["expense_account_id"],
                tax_code=code_facts(
                    row["tax_code_id"] if _money(row["tax_amount"], "tax_amount") != 0 else None,
                    row["code_row"],
                    row["code_organization_id"],
                    row["tax_type"],
                    row["tax_paid_account_id"],
                    row["paid_row"],
                    row["deferral_pair_account_id"],
                    row["deferral_row"],
                ),
            )
        )
    outcomes: dict[str, InvoiceTax] = {}
    for invoice, (organization, journal, lines) in grouped.items():
        outcomes[invoice] = classify_invoice(organization, lines, debits.get(journal) if journal else None)
    return outcomes


__all__ = [
    "CLASSES",
    "INVOICE_LINE_SQL",
    "LEDGER_DEBIT_SQL",
    "InvoiceTax",
    "LineFacts",
    "LineTax",
    "TaxAccountMappingError",
    "TaxCodeFacts",
    "canonical",
    "class_counts",
    "classify_invoice",
    "classify_snapshot",
    "code_facts",
    "snapshot_split_account",
]
