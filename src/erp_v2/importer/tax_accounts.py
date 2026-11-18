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

  - A1D split through the legacy VAT-deferral journal (conversion spec section 9). Legacy posted a separate
    ``SUPPLIER_INVOICE_VAT_DEFERRAL`` journal per invoice (Dr deferral account, Cr 1440 / an expense account). The
    deferral journal is the ``journal_entry`` with that ``source_document_type``, ``source_document_id`` = the invoice,
    ``source_module`` AP and status POSTED; amounts come from its posted ledger lines. Proof order: A1 on the invoice
    journal alone first (an A1 invoice that also has a deferral journal is A3 ``deferral_on_split_invoice``); else with
    exactly one deferral journal the A1D test on the combined NET map (debit - credit per account over both journals,
    excluding the AP control account, zeros dropped): every taxed line splits, each split account's net equals its
    mapped tax, no other tax-code account has a non-zero net, the nets sum to the invoice total, none is negative.
    ``tax_account_ref`` is the split account, as A1. Expense placement only sets ``a1d_expense_elsewhere``. Two or more
    deferral journals are never summed: A3 ``deferral_journal_multiple``. An invoice with a deferral journal is never
    A2. Otherwise A3 ``deferral_amount_mismatch`` (a split-account net differs from its tax) or
    ``deferral_combined_unbalanced``. A3 without a deferral journal is reason ``other``.

* Not legacy-posted: legacy's CURRENT posting rule (``ap/posting/invoice.py``,
  ``posting/tax.py:resolve_tax_posting_account_id``), because v2 posts these invoices now.

  - B1 split (tax != 0, a tax code of the invoice's organisation, VAT or GST, a tax-paid
    account): the deferral pair of the tax-paid account when set (D4), else the tax-paid account.
  - B2 folded (everything else taxed): the line's expense account (D1).

* X (``tax_sign_unsupported``): a taxed invoice with a negative line or tax amount. Every line ref is
  NULL and the invoice is counted, never raised on: payables CHECKs reject negative amounts so such
  a row cannot land, and the verifiers refuse negatives themselves.

A NULL line expense account is keyed by a sentinel that never equals a ledger account (the operator
oracle's semantics): A1 counts it as expense_elsewhere, A2 cannot match (the invoice is A3), and a taxed
null-expense line gets a NULL ref. A NULL ledger account is the named refusal ``ledger_account_missing``.

Untaxed lines have no tax account, and an invoice with no taxed line is never sign-checked (a legacy
credit note with an untaxed negative line is irrelevant to the tax account). Accounts are canonical UUID strings, the
form ``posting_account_ref`` uses. Nothing here guesses: a missing referenced row is the caller's
named refusal, never a silent fold.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

SPLIT_TAX_TYPES: Final = frozenset({"VAT", "GST"})
CLASSES: Final = ("A1", "A1D", "A2", "A3", "B1", "B2", "X")
EXPENSE_ELSEWHERE: Final = "a1_expense_elsewhere"
EXPENSE_ELSEWHERE_A1D: Final = "a1d_expense_elsewhere"
DEFERRAL_NOT_POSTED: Final = "deferral_journal_not_posted"
UNTAXED_WITH_DEFERRAL: Final = "untaxed_with_deferral"
A3_REASONS_KEY: Final = "a3_reasons"
A3_REASONS: Final = (
    "deferral_amount_mismatch",
    "deferral_combined_unbalanced",
    "deferral_journal_multiple",
    "deferral_on_split_invoice",
    "other",
)
DEFERRAL_DOCUMENT_TYPE: Final = "SUPPLIER_INVOICE_VAT_DEFERRAL"
LEGACY_POSTED_CLASSES: Final = frozenset({"A1", "A1D", "A2", "A3"})
ACCEPTED_CLASSES: Final = frozenset({"A1", "A1D", "A2"})


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
    klass: str | None  # A1 A2 A3 B1 B2 X, None for an untaxed line
    ref: str | None  # canonical account id, None for an untaxed line


@dataclass(frozen=True)
class InvoiceTax:
    legacy_posted: bool
    lines: Mapping[str, LineTax]  # keyed by canonical line id
    klass: (
        str | None
    )  # invoice class: A1 A2 A3, "B" (unposted taxed), "X" (taxed with a negative amount), None (no taxed line)
    expense_elsewhere: bool = False  # A1 / A1D: the expense debits are not on the lines' expense accounts
    reason: str | None = None  # A3 only: one of A3_REASONS
    deferral_journals: int = 0  # POSTED legacy VAT-deferral journals linked to the invoice
    deferral_not_posted: int = 0  # linked deferral journals in another status (ignored)

    @property
    def other(self) -> bool:
        """``tax_treatment_other``: legacy GL shows a treatment the rule does not recognise."""
        return self.klass == "A3"


@dataclass(frozen=True)
class DeferralFacts:
    """The invoice's linked legacy VAT-deferral journals.

    ``journals`` holds one net map per POSTED deferral journal (Σ(debit - credit) per account over its posted ledger
    lines). ``not_posted`` counts linked journals in another status; they are ignored.
    """

    journals: tuple[Mapping[object, Decimal], ...] = ()
    not_posted: int = 0


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
    """Add to an expense/tax account map. A NULL account is keyed by ``None`` (the operator oracle's
    semantics): it never equals a ledger account, so A1 counts expense_elsewhere and A2 cannot match."""

    key = canonical(account)
    target[key] = target.get(key, Decimal(0)) + amount


def _add_ledger(target: dict[str, Decimal], account: object, amount: Decimal) -> None:
    key = canonical(account)
    if key is None:
        raise TaxAccountMappingError("ledger_account_missing")
    target[key] = target.get(key, Decimal(0)) + amount


def classify_invoice(
    organization_id: object,
    lines: Sequence[LineFacts],
    ledger_debits: Mapping[object, Decimal] | None,
    global_tax_accounts: Iterable[object] = (),
    *,
    invoice_net: Mapping[object, Decimal] | None = None,
    deferral: DeferralFacts | None = None,
    control_account: object = None,
) -> InvoiceTax:
    """Classify one invoice's taxed lines (see the module docstring for the A1D deferral-journal rule).

    ``invoice_net`` is the invoice journal's Σ(debit - credit) per account (defaults to ``ledger_debits``);
    ``deferral`` the linked deferral journals; ``control_account`` the invoice's AP control account, excluded
    from the combined net.
    """

    outcome = _classify(
        organization_id, lines, ledger_debits, global_tax_accounts, invoice_net, deferral, control_account
    )
    if deferral is not None and (deferral.journals or deferral.not_posted):
        outcome = replace(outcome, deferral_journals=len(deferral.journals), deferral_not_posted=deferral.not_posted)
    return outcome


def _classify(
    organization_id: object,
    lines: Sequence[LineFacts],
    ledger_debits: Mapping[object, Decimal] | None,
    global_tax_accounts: Iterable[object],
    invoice_net: Mapping[object, Decimal] | None,
    deferral: DeferralFacts | None,
    control_account: object,
) -> InvoiceTax:
    """Classify one invoice's taxed lines.

    ``ledger_debits`` is the per-account sum of the legacy posted ledger debits of the invoice's
    journal, or None when the invoice is not legacy-posted (no ledger lines). ``global_tax_accounts`` is
    every tax-paid account and deferral pair of EVERY tax code in the snapshot: a debit on another
    code's VAT account is never an A1 or A2 invoice.
    """

    taxed = [line for line in lines if _money(line.tax_amount, "tax_amount") != 0]
    result: dict[str, LineTax] = {}
    for line in lines:
        key = canonical(line.line_id)
        if key is None or key in result:
            raise TaxAccountMappingError("line_id_missing_or_duplicate")
        result[key] = LineTax(None, None)
    if not taxed:
        return InvoiceTax(ledger_debits is not None, result, None)
    if any(_money(line.tax_amount, "tax_amount") < 0 or _money(line.line_amount, "line_amount") < 0 for line in lines):
        # Taxed invoice with a negative amount: per-invoice class X, every ref NULL, never a raise.
        for line in taxed:
            result[canonical(line.line_id)] = LineTax("X", None)
        return InvoiceTax(ledger_debits is not None, result, "X")
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
        _add_ledger(actual, account, _money(amount, "ledger_debit"))
    actual = _nonzero(actual)

    tax_accounts = {account for account in splits.values() if account is not None}
    tax_accounts.update(canonical(a) for a in global_tax_accounts if a is not None)
    for line in taxed:
        if line.tax_code is not None:
            tax_accounts.update(
                canonical(a) for a in (line.tax_code.paid_account_id, line.tax_code.deferral_account_id) if a
            )
    total = sum((line.line_amount + line.tax_amount for line in lines), Decimal(0))

    n_deferral = len(deferral.journals) if deferral is not None else 0

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
            if n_deferral:
                return _a3(result, taxed, "deferral_on_split_invoice")
            for line in taxed:
                result[canonical(line.line_id)] = LineTax("A1", splits[canonical(line.line_id)])
            expected: dict[str, Decimal] = {}
            for line in lines:
                _add(expected, line.expense_account_id, line.line_amount)
            for account, tax in split_tax.items():
                _add(expected, account, tax)
            return InvoiceTax(True, result, "A1", _nonzero(expected) != actual)

    if n_deferral >= 2:
        return _a3(result, taxed, "deferral_journal_multiple")  # never summed
    if n_deferral == 1:
        split_accounts = {account for account in splits.values() if account is not None}
        if any(actual.get(account, Decimal(0)) != 0 for account in split_accounts):
            return _a3(result, taxed, "deferral_on_split_invoice")  # the invoice journal already split: no double count
        net: dict[str, Decimal] = {}
        for source in (ledger_debits if invoice_net is None else invoice_net, deferral.journals[0]):
            for account, amount in source.items():
                _add_ledger(net, account, _money(amount, "ledger_net"))
        control = canonical(control_account)
        net = _nonzero({account: value for account, value in net.items() if control is None or account != control})
        split_tax = {}
        for line in taxed:
            if splits[canonical(line.line_id)] is not None:
                _add(split_tax, splits[canonical(line.line_id)], line.tax_amount)
        mismatch = any(net.get(account, Decimal(0)) != tax for account, tax in split_tax.items())
        if (
            not mismatch
            and all(account is not None for account in splits.values())
            and not ((tax_accounts - split_tax.keys()) & net.keys())
            and sum(net.values(), Decimal(0)) == total
            and all(value >= 0 for value in net.values())
        ):
            for line in taxed:
                result[canonical(line.line_id)] = LineTax("A1D", splits[canonical(line.line_id)])
            expected = {}
            for line in lines:
                _add(expected, line.expense_account_id, line.line_amount)
            for account, tax in split_tax.items():
                _add(expected, account, tax)
            return InvoiceTax(True, result, "A1D", _nonzero(expected) != net)
        return _a3(result, taxed, "deferral_amount_mismatch" if mismatch else "deferral_combined_unbalanced")

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
    return _a3(result, taxed, "other")


def _a3(result: dict[str, LineTax], taxed: Sequence[LineFacts], reason: str) -> InvoiceTax:
    """Class A3 with a reason: the folded form lands (owner-valid) and the verifiers refuse the invoice."""

    for line in taxed:
        result[canonical(line.line_id)] = LineTax("A3", canonical(line.expense_account_id))
    return InvoiceTax(True, result, "A3", reason=reason)


def class_counts(outcomes: Iterable[InvoiceTax]) -> dict[str, Any]:
    """Receipt counts only: per class, taxed lines and invoices holding such a line.

    Beyond the classes: ``a1_expense_elsewhere`` (A1 only), ``a1d_expense_elsewhere`` (A1D only),
    ``deferral_journal_not_posted`` and ``untaxed_with_deferral`` (invoices; lines are 0), and ``a3_reasons``,
    a map reason -> {lines, invoices} over the A3 invoices.
    """

    counts: dict[str, Any] = {
        name: {"lines": 0, "invoices": 0}
        for name in (*CLASSES, EXPENSE_ELSEWHERE, EXPENSE_ELSEWHERE_A1D, DEFERRAL_NOT_POSTED, UNTAXED_WITH_DEFERRAL)
    }
    counts[A3_REASONS_KEY] = {reason: {"lines": 0, "invoices": 0} for reason in A3_REASONS}
    for outcome in outcomes:
        if outcome.expense_elsewhere and outcome.klass in ("A1", "A1D"):
            key = EXPENSE_ELSEWHERE if outcome.klass == "A1" else EXPENSE_ELSEWHERE_A1D
            counts[key]["invoices"] += 1
            counts[key]["lines"] += sum(1 for item in outcome.lines.values() if item.klass == outcome.klass)
        if outcome.deferral_not_posted:
            counts[DEFERRAL_NOT_POSTED]["invoices"] += 1
        if outcome.klass is None and outcome.deferral_journals:
            counts[UNTAXED_WITH_DEFERRAL]["invoices"] += 1
        if outcome.klass == "A3":
            bucket = counts[A3_REASONS_KEY][outcome.reason or "other"]
            bucket["invoices"] += 1
            bucket["lines"] += sum(1 for item in outcome.lines.values() if item.klass == "A3")
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
SELECT i.invoice_id, i.organization_id, i.journal_entry_id, i.ap_control_account_id,
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

GLOBAL_TAX_ACCOUNT_SQL: Final = """
SELECT tc.tax_paid_account_id, pa.deferral_pair_account_id
FROM tax_code tc
LEFT JOIN account pa ON pa.account_id = tc.tax_paid_account_id
"""

DEFERRAL_JOURNAL_SQL: Final = """
SELECT journal_entry_id, source_document_id, status
FROM journal_entry
WHERE source_document_type = 'SUPPLIER_INVOICE_VAT_DEFERRAL'
  AND source_module = 'AP'
  AND source_document_id IN (SELECT invoice_id FROM supplier_invoice)
"""

# Ledger lines (GL truth) of every invoice journal and of every POSTED deferral journal.
LEDGER_DEBIT_SQL: Final = """
SELECT journal_entry_id, account_id, debit_amount, credit_amount
FROM posted_ledger_line
WHERE journal_entry_id IN (SELECT journal_entry_id FROM supplier_invoice WHERE journal_entry_id IS NOT NULL)
   OR journal_entry_id IN (
       SELECT journal_entry_id FROM journal_entry
       WHERE source_document_type = 'SUPPLIER_INVOICE_VAT_DEFERRAL' AND source_module = 'AP'
         AND status = 'POSTED'
         AND source_document_id IN (SELECT invoice_id FROM supplier_invoice))
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
    rows: Iterable[Mapping[str, object]],
    ledger: Iterable[Mapping[str, object]],
    global_accounts: Iterable[Mapping[str, object]] = (),
    deferral_journals: Iterable[Mapping[str, object]] = (),
) -> dict[str, InvoiceTax]:
    """Classify every invoice of ``INVOICE_LINE_SQL`` rows against ``LEDGER_DEBIT_SQL`` rows.

    ``deferral_journals`` are ``DEFERRAL_JOURNAL_SQL`` rows. Keyed by canonical invoice id. Pure over already-read rows.
    """

    debits: dict[str, dict[str, Decimal]] = {}
    nets: dict[str, dict[str, Decimal]] = {}
    for row in ledger:
        journal = canonical(row["journal_entry_id"])
        account = debits.setdefault(journal, {})
        key = canonical(row["account_id"])
        if key is None:
            raise TaxAccountMappingError("ledger_account_missing")
        debit = _money(row["debit_amount"] or 0, "ledger_debit")
        account[key] = account.get(key, Decimal(0)) + debit
        net = nets.setdefault(journal, {})
        net[key] = net.get(key, Decimal(0)) + debit - _money(row.get("credit_amount") or 0, "ledger_credit")
    posted_deferral: dict[str, list[str]] = {}
    not_posted: dict[str, int] = {}
    for row in deferral_journals:
        invoice = canonical(row["source_document_id"])
        if str(row["status"]).strip().upper() == "POSTED":
            posted_deferral.setdefault(invoice, []).append(canonical(row["journal_entry_id"]))
        else:
            not_posted[invoice] = not_posted.get(invoice, 0) + 1
    controls: dict[str, object] = {}
    grouped: dict[str, tuple[object, str | None, list[LineFacts]]] = {}
    for row in rows:
        invoice = canonical(row["invoice_id"])
        entry = grouped.setdefault(invoice, (row["organization_id"], canonical(row["journal_entry_id"]), []))
        controls[invoice] = row.get("ap_control_account_id")
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
    every_tax_account = {
        canonical(value)
        for row in global_accounts
        for value in (row["tax_paid_account_id"], row["deferral_pair_account_id"])
        if value is not None
    }
    outcomes: dict[str, InvoiceTax] = {}
    for invoice, (organization, journal, lines) in grouped.items():
        facts = DeferralFacts(
            tuple(nets.get(j, {}) for j in posted_deferral.get(invoice, [])), not_posted.get(invoice, 0)
        )
        outcomes[invoice] = classify_invoice(
            organization,
            lines,
            debits.get(journal) if journal else None,
            every_tax_account,
            invoice_net=nets.get(journal) if journal else None,
            deferral=facts,
            control_account=controls.get(invoice),
        )
    return outcomes


__all__ = [
    "A3_REASONS",
    "ACCEPTED_CLASSES",
    "CLASSES",
    "DEFERRAL_JOURNAL_SQL",
    "EXPENSE_ELSEWHERE",
    "GLOBAL_TAX_ACCOUNT_SQL",
    "INVOICE_LINE_SQL",
    "LEDGER_DEBIT_SQL",
    "DeferralFacts",
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
