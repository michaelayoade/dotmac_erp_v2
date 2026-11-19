"""Approved importer transforms for legacy supplier invoices (ERP v2 B-3), each itemised for the bridge.

Every rule here is a pure function over source facts. Each application is a ``BridgeLine`` (rule id, legacy and v2
amounts, delta) and every invoice lands in exactly ONE bucket, so the bridge can prove

    legacy = carried + transformed + queued

by count and amount (``TransformLedger.partition``). The transforms act on the v2 subledger documents only: the
legacy GL (posted ledger lines) is read as evidence and is never changed.

Rules (Michael's rulings, each its own bridge line):

* ``T-EXP-LEGACY``: an invoice legacy posted with its expense on another account (class A1/A1D ``expense_elsewhere``)
  lands its lines on the legacy GL account the ledger debited, where that mapping is unambiguous. Otherwise the lines
  keep their own accounts and the invoice is queued (``expense_elsewhere_ambiguous``, it still lands).
* ``T-ROUND-2DP``: amounts with more than 2 decimals round half-even to 2; the invoice residue goes on the largest
  line; a line moving by 0.01 or more refuses the invoice to the queue.
* ``T-ITEM-NONSTOCK``: an item-linked line lands as a plain, non-stock expense line (the item id is provenance in the
  bridge line; the verifiers' ``source_item_link`` change is owned by B-1).
* ``T-EXP-FALLBACK``: a line with no expense account lands on the Finance-named fallback account
  (``import.fallback_expense_account``, a REQUIRED declared input: the import refuses without it).
* ``T-UNPOSTED-CORRECT`` (D-M2): an invoice legacy never posted (DRAFT/SUBMITTED/APPROVED, no ledger lines) lands in
  its true state, coded for v2: VAT lines on the input-tax account (1455), a valid expense account (never a control or
  tax-role account), no folded form. A WHT-type tax on a line is removed from the line and the header, so the total is
  the gross (the WHT is a payment-time fact; the bridge line carries it). A taxed line without a valid code, an
  expense account that is a control or tax-role account, or a deferral journal already posted to the v2 ledger refuses
  the invoice to the queue.

Nothing guesses. A refused invoice is not landed (every line and the header carry a named refusal, surfaced by the
executor as a row exception); the one queue reason that still lands is ``expense_elsewhere_ambiguous``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa

from erp_v2.importer.tax_accounts import (
    DEFERRAL_JOURNAL_SQL,
    DEFERRAL_ON_UNPOSTED,
    GLOBAL_TAX_ACCOUNT_SQL,
    INVOICE_LINE_SQL,
    LEDGER_DEBIT_SQL,
    InvoiceTax,
    LineFacts,
    canonical,
    class_counts,
    classify_snapshot,
    group_invoice_rows,
    snapshot_split_account,
)

PARAM_FALLBACK_EXPENSE: Final = "import.fallback_expense_account"

T_EXP_LEGACY: Final = "T-EXP-LEGACY"
T_ROUND_2DP: Final = "T-ROUND-2DP"
T_ITEM_NONSTOCK: Final = "T-ITEM-NONSTOCK"
T_EXP_FALLBACK: Final = "T-EXP-FALLBACK"
T_UNPOSTED_CORRECT: Final = "T-UNPOSTED-CORRECT"
RULES: Final = (T_EXP_LEGACY, T_ROUND_2DP, T_ITEM_NONSTOCK, T_EXP_FALLBACK, T_UNPOSTED_CORRECT)

CARRY: Final = "CARRY"
TRANSFORM: Final = "TRANSFORM"
QUEUE: Final = "QUEUE"

UNPOSTED_STATUSES: Final = frozenset({"DRAFT", "SUBMITTED", "APPROVED"})
WHT_TAX_TYPES: Final = frozenset({"WITHHOLDING"})
# Finance-declared account roles (decision D-1). Declared here, not imported from ``erp_v2.ledger_roles``: the importer
# is sealed from the validated workflows (tests/test_preflip_import_path_exemption.py), and a test pins these
# constants equal to ledger_roles so they cannot drift.
CONTROL_ACCOUNT_CODES: Final = frozenset({"2000", "2040", "1410"})  # AP, payroll payable, staff loans
INVOICE_TAX_ACCOUNT_CODES: Final = frozenset({"1455"})  # input tax (V1)
INPUT_VAT_RELEASE_CODES: Final = frozenset({"1440"})  # release target
TAX_ROLE_CODES: Final = INVOICE_TAX_ACCOUNT_CODES | INPUT_VAT_RELEASE_CODES

Q_REASON_AMBIGUOUS: Final = "expense_elsewhere_ambiguous"
Q_WHT_PENDING: Final = "wht_payment_fact_pending"  # lands, but is queued until the a6 WHT table carries the fact
Q_ACCOUNT_CONTROL: Final = "expense_account_is_control"
Q_ACCOUNT_TAX_ROLE: Final = "expense_account_is_tax_role"
Q_TAX_NOT_INPUT: Final = "tax_account_not_input_tax"
Q_TAX_CODE: Final = "taxed_line_without_valid_tax_code"
Q_DEFERRAL_UNPOSTED: Final = DEFERRAL_ON_UNPOSTED
Q_ROUNDING: Final = "rounding_residue_too_large"
Q_TOTAL: Final = "total_mismatch_after_transform"
Q_HEADER_MISSING: Final = "header_amount_missing"
REFUSAL_KEY: Final = "_refusal"  # == executor.TRANSFORM_REFUSAL_KEY

CENT: Final = Decimal("0.01")
ZERO: Final = Decimal(0)

INVOICE_HEADER_SQL: Final = "SELECT invoice_id, status, subtotal, tax_amount, total_amount FROM supplier_invoice"
LINE_ITEM_SQL: Final = "SELECT line_id, item_id FROM supplier_invoice_line WHERE item_id IS NOT NULL"
DEST_ACCOUNT_SQL: Final = (
    "SELECT a.id, a.tenant_id, a.code, a.is_active, a.posting_allowed, c.account_class "
    "FROM mod_accounting.accounts a JOIN mod_accounting.account_categories c "
    "ON c.tenant_id = a.tenant_id AND c.id = a.category_id"
)
ACCOUNT_SQL: Final = "SELECT account_id, account_code, is_active, is_posting_allowed FROM account"


class TransformParamsError(ValueError):
    """A required declared input is missing or unusable; carries a stable ``code``. The run refuses."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AccountFacts:
    code: str | None
    active: bool = True
    postable: bool = True


def account_problem(account: object, accounts: Mapping[str, AccountFacts], *, require_usable: bool) -> str | None:
    """Why this account cannot be a v2 expense account, or None. Unknown accounts are not judged unless
    ``require_usable`` (an account the transform itself chooses must exist, be active, postable, and have no role)."""

    key = canonical(account)
    facts = accounts.get(key) if key is not None else None
    if facts is None:
        return "account_unknown" if require_usable else None
    code = (facts.code or "").strip()
    if code in CONTROL_ACCOUNT_CODES:
        return Q_ACCOUNT_CONTROL
    if code in TAX_ROLE_CODES:
        return Q_ACCOUNT_TAX_ROLE
    if require_usable and not (facts.active and facts.postable):
        return "account_inactive_or_not_postable"
    return None


@dataclass(frozen=True)
class DestAccount:
    """An account of the DESTINATION chart (mod_accounting), which is what the lines land on."""

    tenant_id: str | None
    code: str | None
    active: bool
    postable: bool
    account_class: str | None


def dest_account_problem(account: object, chart: Mapping[str, DestAccount], tenant_id: object) -> str | None:
    """Why this destination account cannot be the fallback expense account, or None."""

    facts = chart.get(canonical(account) or "")
    if facts is None:
        return "account_unknown"
    if canonical(facts.tenant_id) != canonical(tenant_id):
        return "account_other_tenant"
    code = (facts.code or "").strip()
    if code in CONTROL_ACCOUNT_CODES:
        return Q_ACCOUNT_CONTROL
    if code in TAX_ROLE_CODES:
        return Q_ACCOUNT_TAX_ROLE
    if not (facts.active and facts.postable):
        return "account_inactive_or_not_postable"
    if str(facts.account_class or "").upper() != "EXPENSE":
        return "account_not_expense_class"
    return None


@dataclass(frozen=True)
class TransformParams:
    """Declared run inputs. ``fallback_expense_account`` is Finance's F-1 answer and is required."""

    fallback_expense_account: str | None = None
    tenant_id: str | None = None  # the run's single tenant extent: the fallback must be in this tenant's chart

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None, tenant_id: object = None) -> TransformParams:
        tenant = None if tenant_id is None else canonical(tenant_id)
        raw = (values or {}).get(PARAM_FALLBACK_EXPENSE)
        if raw is None or str(raw).strip() == "":
            return cls(None, tenant)
        try:
            return cls(str(uuid.UUID(str(raw).strip())), tenant)
        except ValueError as exc:
            raise TransformParamsError("fallback_expense_account_not_a_uuid") from exc

    def validate(self, chart: Mapping[str, DestAccount]) -> str:
        """The fallback account (canonical id) or a named refusal. Validated against the DESTINATION chart: same
        tenant, active, posting-allowed, expense class, not a control account or tax-role code. Never a guess."""

        if self.fallback_expense_account is None:
            raise TransformParamsError("fallback_expense_account_missing")
        if self.tenant_id is None:
            raise TransformParamsError("fallback_expense_account_tenant_unknown")
        problem = dest_account_problem(self.fallback_expense_account, chart, self.tenant_id)
        if problem is not None:
            raise TransformParamsError(f"fallback_expense_account_invalid:{problem}")
        return canonical(self.fallback_expense_account) or ""


@dataclass(frozen=True)
class InvoiceFacts:
    invoice_id: str  # canonical
    organization_id: object
    status: str
    subtotal: Decimal | None
    tax_amount: Decimal | None
    total_amount: Decimal | None
    lines: tuple[LineFacts, ...]
    tax: InvoiceTax
    items: Mapping[str, str] = field(default_factory=dict)  # canonical line id -> legacy item id (item-linked lines)


@dataclass(frozen=True)
class BridgeLine:
    """One application of one rule: ``scope`` is ``line`` or ``header``; delta = v2 - legacy."""

    rule: str
    scope: str
    invoice_id: str
    line_id: str | None
    legacy_account: str | None
    v2_account: str | None
    legacy_amount: Decimal
    v2_amount: Decimal
    note: str = ""

    @property
    def delta(self) -> Decimal:
        return self.v2_amount - self.legacy_amount

    def as_dict(self) -> dict[str, Any]:
        body = {
            "rule": self.rule,
            "scope": self.scope,
            "invoice_id": self.invoice_id,
            "line_id": self.line_id,
            "legacy_account": self.legacy_account,
            "v2_account": self.v2_account,
            "legacy_amount": format(self.legacy_amount, "f"),
            "v2_amount": format(self.v2_amount, "f"),
            "delta": format(self.delta, "f"),
            "note": self.note,
        }
        body["fingerprint"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return body


@dataclass(frozen=True)
class LineOutcome:
    posting_account_ref: str | None
    tax_account_ref: str | None
    line_amount: Decimal
    tax_amount: Decimal
    legacy_amount: Decimal  # line_amount + tax_amount as legacy had it
    transformed: bool
    null_expense: bool  # the legacy line had no expense account


@dataclass(frozen=True)
class InvoiceOutcome:
    invoice_id: str
    bucket: str  # CARRY | TRANSFORM | QUEUE
    legacy_total: Decimal | None
    v2_total: Decimal | None  # None: not landed
    lands: bool
    queue_reason: str | None
    header: Mapping[str, Decimal | None]  # subtotal, tax_amount, total_amount as they land
    lines: Mapping[str, LineOutcome]
    bridge: tuple[BridgeLine, ...]
    queue_reasons: tuple[str, ...] = ()  # every queue reason (a landed invoice can carry several)
    header_changed: bool = False  # the landed header values differ from legacy

    @property
    def header_delta(self) -> Decimal:
        return sum((b.delta for b in self.bridge if b.scope == "header"), ZERO)


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except Exception:
        return None
    return out if out.is_finite() else None


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _r2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _legacy_expense_mapping(invoice: InvoiceFacts) -> dict[str, str] | None:
    """Line id -> legacy GL account, or None when the mapping is not unambiguous.

    Unambiguous means: the legacy expense side is one account (every line goes there), or the lines' own-account
    groups and the legacy debited accounts pair one-to-one by distinct non-zero sums that all match.
    """

    actual = {a: v for a, v in invoice.tax.expense_debits.items() if v != 0}
    if not actual:
        return None
    groups: dict[str | None, Decimal] = {}
    members: dict[str | None, list[str]] = {}
    for line in invoice.lines:
        key = canonical(line.expense_account_id)
        groups[key] = groups.get(key, ZERO) + line.line_amount
        members.setdefault(key, []).append(canonical(line.line_id) or "")
    if len(actual) == 1:
        ((account, amount),) = actual.items()
        if sum(groups.values(), ZERO) != amount:
            return None
        return {m: account for ids in members.values() for m in ids}
    if len(groups) != len(actual) or any(v == 0 for v in groups.values()):
        return None
    group_sums, actual_sums = Counter(groups.values()), Counter(actual.values())
    if group_sums != actual_sums or any(count != 1 for count in group_sums.values()):
        return None
    by_sum = {amount: account for account, amount in actual.items()}
    return {m: by_sum[groups[key]] for key, ids in members.items() for m in ids}


def transform_invoice(
    invoice: InvoiceFacts, fallback_account: str, accounts: Mapping[str, AccountFacts]
) -> InvoiceOutcome:
    """Apply every rule to one invoice; return its bucket, landing values and bridge lines. Pure."""

    iid = invoice.invoice_id
    status = invoice.status.strip().upper()
    unposted = (not invoice.tax.legacy_posted) and status in UNPOSTED_STATUSES
    legacy_total = invoice.total_amount
    bridge: list[BridgeLine] = []
    refusal: str | None = None
    landed_queue: list[str] = []

    def refuse(reason: str) -> None:
        nonlocal refusal
        refusal = refusal or reason

    key_of = {canonical(line.line_id) or "": line for line in invoice.lines}
    # Credit-note shaped invoices (a negative amount) cannot land today (payables CHECKs); no rule touches them.
    if any(line.line_amount < 0 or line.tax_amount < 0 for line in invoice.lines):
        return _carry(invoice)

    posting: dict[str, str | None] = {k: canonical(line.expense_account_id) for k, line in key_of.items()}
    legacy_posting = dict(posting)
    tax_ref: dict[str, str | None] = {k: invoice.tax.lines[k].ref for k in key_of}
    line_amount = {k: line.line_amount for k, line in key_of.items()}
    tax_amount = {k: line.tax_amount for k, line in key_of.items()}
    line_notes: dict[str, list[BridgeLine]] = {k: [] for k in key_of}
    changed: set[str] = set()

    # T-EXP-LEGACY: expense posted elsewhere follows the legacy GL account (posted A1/A1D only).
    if invoice.tax.expense_elsewhere and invoice.tax.klass in ("A1", "A1D"):
        mapping = _legacy_expense_mapping(invoice)
        if mapping is None or any(
            account_problem(account, accounts, require_usable=True) for account in set(mapping.values())
        ):
            landed_queue.append(Q_REASON_AMBIGUOUS)
        else:
            for k, account in mapping.items():
                if account != posting[k]:
                    posting[k] = account
                    changed.add(k)
                    line_notes[k].append(
                        BridgeLine(
                            T_EXP_LEGACY,
                            "line",
                            iid,
                            k,
                            legacy_posting[k],
                            account,
                            line_amount[k],
                            line_amount[k],
                            "legacy GL expense account",
                        )
                    )

    # T-EXP-FALLBACK: still no expense account.
    for k in key_of:
        if posting[k] is None:
            posting[k] = fallback_account
            changed.add(k)
            line_notes[k].append(
                BridgeLine(
                    T_EXP_FALLBACK,
                    "line",
                    iid,
                    k,
                    None,
                    fallback_account,
                    line_amount[k],
                    line_amount[k],
                    "no legacy expense account",
                )
            )

    # T-UNPOSTED-CORRECT (D-M2): never-approved / unposted invoices land coded for v2.
    wht_removed = ZERO
    if unposted:
        if invoice.tax.reason == DEFERRAL_ON_UNPOSTED:
            refuse(Q_DEFERRAL_UNPOSTED)
        for k, line in key_of.items():
            problem = account_problem(posting[k], accounts, require_usable=False)
            if problem is not None:
                refuse(problem)
            if line.tax_amount == 0:
                continue
            code = line.tax_code
            tax_type = str(code.tax_type).strip().upper() if code is not None else ""
            if code is not None and tax_type in WHT_TAX_TYPES:
                wht_removed += line.tax_amount
                tax_amount[k] = ZERO
                tax_ref[k] = None
                changed.add(k)
                line_notes[k].append(
                    BridgeLine(
                        T_UNPOSTED_CORRECT,
                        "line",
                        iid,
                        k,
                        None,
                        None,
                        line.line_amount + line.tax_amount,
                        line.line_amount,
                        f"wht_removed {format(line.tax_amount, 'f')}: payment-time fact",
                    )
                )
                continue
            split = snapshot_split_account(line, invoice.organization_id)
            if split is None:
                refuse(Q_TAX_CODE)
                continue
            facts = accounts.get(split)
            if facts is None or (facts.code or "").strip() not in INVOICE_TAX_ACCOUNT_CODES:
                refuse(Q_TAX_NOT_INPUT)
                continue
            tax_ref[k] = split
    # A legacy-posted or other invoice keeps the classifier's refs (GL history, verifiers prove them).

    # T-ROUND-2DP over lines and header (after WHT removal).
    sub, tax_h, total = invoice.subtotal, invoice.tax_amount, invoice.total_amount
    header: dict[str, Decimal | None] = {"subtotal": sub, "tax_amount": tax_h, "total_amount": total}
    needs_round = any(
        a != _r2(a)
        for a in [*line_amount.values(), *tax_amount.values(), *(x for x in (sub, tax_h, total) if x is not None)]
    )
    if wht_removed and (sub is None or tax_h is None):
        refuse(Q_HEADER_MISSING)
    if needs_round and (sub is None or tax_h is None or total is None):
        refuse(Q_HEADER_MISSING)
    if refusal is None and (wht_removed or needs_round) and sub is not None and tax_h is not None:
        new_sub, new_tax = sub, tax_h - wht_removed
        if total is not None and sub + tax_h != total:
            refuse(Q_TOTAL)  # the legacy header is not subtotal + tax: never reconcile it silently
        if needs_round:
            rl = {k: _r2(v) for k, v in line_amount.items()}
            rt = {k: _r2(v) for k, v in tax_amount.items()}
            # round the tax first and derive the subtotal from the 2-dp total, so a 2-dp header total is preserved
            new_tax = _r2(new_tax)
            new_sub = _r2((total if total is not None else sub + tax_h) - wht_removed) - new_tax
            res_sub = new_sub - sum(rl.values(), ZERO)
            if res_sub != 0 and rl:
                big = sorted(rl, key=lambda k: (-rl[k], k))[0]
                rl[big] += res_sub
            res_tax = new_tax - sum(rt.values(), ZERO)
            taxed = [k for k in rt if rt[k] != 0]
            if res_tax != 0:
                if taxed:
                    rt[sorted(taxed, key=lambda k: (-rt[k], k))[0]] += res_tax
                else:
                    refuse(Q_ROUNDING)
            for k in key_of:
                for before, after, what in (
                    (line_amount[k], rl[k], "line_amount"),
                    (tax_amount[k], rt[k], "tax_amount"),
                ):
                    if abs(after - before) >= CENT:
                        refuse(Q_ROUNDING)
                    elif after != before:
                        changed.add(k)
                        line_notes[k].append(
                            BridgeLine(T_ROUND_2DP, "line", iid, k, posting[k], posting[k], before, after, what)
                        )
            line_amount, tax_amount = rl, rt
        new_total = new_sub + new_tax
        header = {"subtotal": new_sub, "tax_amount": new_tax, "total_amount": new_total}
        if refusal is None and total is not None:
            after_round = _r2(total) if needs_round else total
            if after_round != total:
                bridge.append(
                    BridgeLine(T_ROUND_2DP, "header", iid, None, None, None, total, after_round, "header total rounded")
                )
            if wht_removed:
                bridge.append(
                    BridgeLine(
                        T_UNPOSTED_CORRECT,
                        "header",
                        iid,
                        None,
                        None,
                        None,
                        after_round,
                        new_total,
                        f"total change: wht {format(wht_removed, 'f')} removed, total is the gross",
                    )
                )

    # T-ITEM-NONSTOCK: bridge-only (no value changes); item id is provenance.
    for k, item in invoice.items.items():
        if k in key_of:
            line_notes[k].append(
                BridgeLine(
                    T_ITEM_NONSTOCK,
                    "line",
                    iid,
                    k,
                    posting[k],
                    posting[k],
                    line_amount[k],
                    line_amount[k],
                    f"item {item}",
                )
            )
            changed.add(k)

    if refusal is not None:
        return InvoiceOutcome(
            iid,
            QUEUE,
            legacy_total,
            None,
            False,
            refusal,
            {"subtotal": sub, "tax_amount": tax_h, "total_amount": total},
            _passthrough(key_of),
            (),
            (refusal,),
        )

    lines_out = {
        k: LineOutcome(
            posting[k],
            tax_ref[k],
            line_amount[k],
            tax_amount[k],
            key_of[k].line_amount + key_of[k].tax_amount,
            k in changed,
            legacy_posting[k] is None,
        )
        for k in key_of
    }
    all_bridge = tuple(b for k in key_of for b in line_notes[k]) + tuple(bridge)
    v2_total = header["total_amount"]
    header_changed = header != {"subtotal": sub, "tax_amount": tax_h, "total_amount": total}
    if wht_removed:
        # Landed at the gross, but a payment of it would skip the WHT: counted as queued until the a6 WHT table
        # carries the payment-time fact, so the invoice is never paid gross unnoticed.
        landed_queue.append(Q_WHT_PENDING)
    if landed_queue:
        return InvoiceOutcome(
            iid,
            QUEUE,
            legacy_total,
            v2_total,
            True,
            landed_queue[0],
            header,
            lines_out,
            all_bridge,
            tuple(landed_queue),
            header_changed,
        )
    return InvoiceOutcome(
        iid,
        TRANSFORM if all_bridge else CARRY,
        legacy_total,
        v2_total,
        True,
        None,
        header,
        lines_out,
        all_bridge,
        (),
        header_changed,
    )


def _passthrough(key_of: Mapping[str, LineFacts]) -> dict[str, LineOutcome]:
    return {
        k: LineOutcome(
            canonical(line.expense_account_id),
            None,
            line.line_amount,
            line.tax_amount,
            line.line_amount + line.tax_amount,
            False,
            line.expense_account_id is None,
        )
        for k, line in key_of.items()
    }


def _carry(invoice: InvoiceFacts) -> InvoiceOutcome:
    """No rule applies (a negative-amount invoice cannot land today and is neither transformed nor hidden). A NULL
    expense account keeps today's behaviour: a destination rejection in the receipt."""

    lines = {
        canonical(line.line_id) or "": LineOutcome(
            canonical(line.expense_account_id),
            invoice.tax.lines[canonical(line.line_id) or ""].ref,
            line.line_amount,
            line.tax_amount,
            line.line_amount + line.tax_amount,
            False,
            line.expense_account_id is None,
        )
        for line in invoice.lines
    }
    header = {"subtotal": invoice.subtotal, "tax_amount": invoice.tax_amount, "total_amount": invoice.total_amount}
    return InvoiceOutcome(
        invoice.invoice_id, CARRY, invoice.total_amount, invoice.total_amount, True, None, header, lines, ()
    )


@dataclass(frozen=True)
class TransformLedger:
    """Every invoice outcome of one run, with the bridge partition and the parity delta."""

    outcomes: tuple[InvoiceOutcome, ...]
    fallback_account: str
    # The independent universe: every source header id (line-less invoices included) and the source line row count.
    # None (unit tests of single rules) skips the universe checks and says so in the partition.
    source_invoice_ids: tuple[str, ...] | None = None
    source_line_count: int | None = None

    def partition(self) -> dict[str, Any]:
        """legacy = carried + transformed + queued, by invoice count, line count and legacy amount (header total /
        line amount + tax). ``balanced`` is true when each total equals the sum of its three buckets and v2 totals
        reconcile: v2 landed = carried + transformed v2 + landed queue."""

        buckets = {
            name: {"invoices": 0, "lines": 0, "legacy_total": ZERO, "legacy_line_amount": ZERO}
            for name in (CARRY, TRANSFORM, QUEUE)
        }
        v2_landed = ZERO
        delta_headers = ZERO
        landed_legacy = ZERO
        for o in self.outcomes:
            b = buckets[o.bucket]
            b["invoices"] += 1
            b["lines"] += len(o.lines)
            b["legacy_total"] += o.legacy_total or ZERO
            b["legacy_line_amount"] += sum((ln.legacy_amount for ln in o.lines.values()), ZERO)
            if o.lands:
                v2_landed += o.v2_total or ZERO
                landed_legacy += o.legacy_total or ZERO
                delta_headers += o.header_delta
        total_inv = len(self.outcomes)
        total_lines = sum(len(o.lines) for o in self.outcomes)
        total_legacy = sum((o.legacy_total or ZERO for o in self.outcomes), ZERO)
        ids = Counter(o.invoice_id for o in self.outcomes)
        universe = None
        if self.source_invoice_ids is not None and self.source_line_count is not None:
            source = Counter(self.source_invoice_ids)
            universe = {
                "source_invoices": sum(source.values()),
                "source_lines": self.source_line_count,
                "each_invoice_in_exactly_one_bucket": ids == source and all(n == 1 for n in ids.values()),
                "line_count_matches_source": total_lines == self.source_line_count,
            }
        balanced = (
            (
                universe is None
                or (universe["each_invoice_in_exactly_one_bucket"] and universe["line_count_matches_source"])
            )
            and sum(b["invoices"] for b in buckets.values()) == total_inv
            and sum(b["lines"] for b in buckets.values()) == total_lines
            and sum((b["legacy_total"] for b in buckets.values()), ZERO) == total_legacy
            and v2_landed == landed_legacy + delta_headers
        )
        return {
            "legacy": {"invoices": total_inv, "lines": total_lines, "total": format(total_legacy, "f")},
            "buckets": {
                name: {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in b.items()}
                for name, b in buckets.items()
            },
            "v2_landed_total": format(v2_landed, "f"),
            "header_delta_total": format(delta_headers, "f"),
            "universe": universe,
            "balanced": balanced,
        }

    def rules(self) -> dict[str, Any]:
        """Per rule and scope (``line`` / ``header``): applications, legacy and v2 amounts, delta. Header-scope deltas
        are the invoice total changes the bridge identity uses; line-scope rows itemise the per-line moves."""

        def blank() -> dict[str, Any]:
            return {"applications": 0, "legacy": ZERO, "v2": ZERO, "delta": ZERO}

        out: dict[str, Any] = {r: {"line": blank(), "header": blank()} for r in RULES}
        for o in self.outcomes:
            for b in o.bridge:
                row = out[b.rule][b.scope]
                row["applications"] += 1
                row["legacy"] += b.legacy_amount
                row["v2"] += b.v2_amount
                row["delta"] += b.delta
        return {
            r: {
                scope: {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in row.items()}
                for scope, row in scopes.items()
            }
            for r, scopes in out.items()
        }

    def queue(self) -> dict[str, Any]:
        queued = [o for o in self.outcomes if o.bucket == QUEUE]
        reasons = Counter(r for o in queued for r in (o.queue_reasons or (o.queue_reason,)))
        return {
            "invoices": len(queued),
            "landed": sum(1 for o in queued if o.lands),
            "by_reason": dict(sorted(reasons.items())),
            "total_mismatch_after_transform": reasons.get(Q_TOTAL, 0),
        }

    def landed(self) -> dict[str, Any]:
        """The transform manifest the verifiers read: per landed invoice a transform touched, the rules, the changed
        header values, the landed values of every transformed line, and its item-linked lines. Transforms are kept
        apart from Finance waivers: this is the importer's own record of what it changed and why."""

        out: dict[str, Any] = {}
        for o in self.outcomes:
            if not o.lands:
                continue
            lines = {
                k: {
                    "posting_account_ref": ln.posting_account_ref,
                    "tax_account_ref": ln.tax_account_ref,
                    "line_amount": _fmt(ln.line_amount),
                    "tax_amount": _fmt(ln.tax_amount),
                }
                for k, ln in o.lines.items()
                if ln.transformed
            }
            items = sorted(b.line_id for b in o.bridge if b.rule == T_ITEM_NONSTOCK and b.line_id)
            if not (o.header_changed or lines or items):
                continue
            out[o.invoice_id] = {
                "rules": sorted({b.rule for b in o.bridge}),
                "header": {f: _fmt(v) for f, v in o.header.items()} if o.header_changed else {},
                "lines": lines,
                "item_lines": items,
            }
        return out

    def changed_headers(self) -> list[dict[str, Any]]:
        """Landed invoices whose header values differ from legacy (WHT removal, rounding): the changed values the
        dest6 header parity must expect."""

        out = []
        for o in self.outcomes:
            if not o.lands:
                continue
            rules = sorted({b.rule for b in o.bridge if b.scope == "header"})
            if rules:
                out.append(
                    {
                        "invoice_id": o.invoice_id,
                        "rules": rules,
                        "legacy_total": format(o.legacy_total, "f") if o.legacy_total is not None else None,
                        "v2_subtotal": _fmt(o.header.get("subtotal")),
                        "v2_tax_amount": _fmt(o.header.get("tax_amount")),
                        "v2_total": _fmt(o.v2_total),
                    }
                )
        return out

    def parity(self) -> dict[str, Any]:
        """The declared change to the dest6 parity oracle (``supplier_invoices`` / ``supplier_invoice_lines``).

        Up: before B-3 a NULL-expense line never landed (``posting_account_ref`` is required), so lines ``written``
        rises by the NULL-expense lines that now land (fallback + legacy GL account). Down: an invoice B-3 queues
        without landing no longer lands its header, nor the lines dest6 used to land (those with an expense
        account). ``declared_written_delta_lines`` and ``declared_written_delta_headers`` are the NET changes and may be
        negative. Landed headers whose values change (WHT removal, rounding) are counted and itemised."""

        total = fallback = legacy_gl = not_landed_null = 0
        invoices = set()
        dropped_headers = dropped_lines = 0
        for o in self.outcomes:
            if not o.lands:
                dropped_headers += 1
            for k, line in o.lines.items():
                if not line.null_expense:
                    if not o.lands:
                        dropped_lines += 1  # dest6 landed this line; the queued invoice no longer does
                    continue
                total += 1
                invoices.add(o.invoice_id)
                if not o.lands:
                    not_landed_null += 1
                elif any(b.rule == T_EXP_LEGACY and b.line_id == k for b in o.bridge):
                    legacy_gl += 1
                else:
                    fallback += 1
        changed = self.changed_headers()
        return {
            "null_expense_lines": total,
            "null_expense_invoices": len(invoices),
            "landed_on_fallback": fallback,
            "landed_on_legacy_gl_account": legacy_gl,
            "not_landed_queued": not_landed_null,
            "newly_not_landed_headers": dropped_headers,
            "newly_not_landed_lines_dest6_landed": dropped_lines,
            "changed_header_values": len(changed),
            "declared_written_delta_lines": fallback + legacy_gl - dropped_lines,
            "declared_written_delta_headers": -dropped_headers,
        }

    def bridge_lines(self) -> list[dict[str, Any]]:
        return [b.as_dict() for o in self.outcomes for b in o.bridge]

    def queue_items(self) -> list[dict[str, Any]]:
        return [
            {
                "invoice_id": o.invoice_id,
                "reason": o.queue_reason,
                "reasons": list(o.queue_reasons or (o.queue_reason,)),
                "lands": o.lands,
                "legacy_total": format(o.legacy_total, "f") if o.legacy_total is not None else None,
            }
            for o in self.outcomes
            if o.bucket == QUEUE
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "fallback_expense_account_declared": True,
            "partition": self.partition(),
            "rules": self.rules(),
            "queue": self.queue(),
            "parity": self.parity(),
            "itemised_sha256": self.itemised_sha256(),
        }

    def itemised(self) -> dict[str, Any]:
        return {
            "bridge_lines": self.bridge_lines(),
            "queue": self.queue_items(),
            "changed_headers": self.changed_headers(),
            "landed": self.landed(),
            "fallback_account": self.fallback_account,
        }

    def itemised_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.itemised(), sort_keys=True).encode()).hexdigest()

    def write_itemised(self, path: Path) -> None:
        """The full itemised bridge file, owner-only (mode 600). Raises OSError on failure."""

        body = json.dumps(self.itemised(), indent=1, sort_keys=True) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
        except BaseException:
            path.unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class TransformBuild:
    """The lookup maps the supplier specs read, and the ledger that itemises them."""

    line_map: dict[Any, dict[str, Any]]
    header_map: dict[Any, dict[str, Any]]
    ledger: TransformLedger
    classes: dict[str, Any] = field(default_factory=dict)  # tax-account class counts (receipt)


def _key(value: object) -> object:
    return value.hex if isinstance(value, uuid.UUID) else value


def destination_chart(rows: Iterable[Mapping[str, object]]) -> dict[str, DestAccount]:
    """``DEST_ACCOUNT_SQL`` rows -> the destination chart keyed by canonical account id."""

    return {
        canonical(r["id"]) or "": DestAccount(
            canonical(r["tenant_id"]),
            None if r.get("code") is None else str(r["code"]),
            bool(r.get("is_active", True)),
            bool(r.get("posting_allowed", True)),
            None if r.get("account_class") is None else str(r["account_class"]),
        )
        for r in rows
    }


def build_transforms(
    rows: Sequence[Mapping[str, object]],
    ledger_rows: Iterable[Mapping[str, object]],
    global_accounts: Iterable[Mapping[str, object]],
    deferral_rows: Iterable[Mapping[str, object]],
    headers: Iterable[Mapping[str, object]],
    items: Iterable[Mapping[str, object]],
    account_rows: Iterable[Mapping[str, object]],
    params: TransformParams,
    destination_accounts: Iterable[Mapping[str, object]],
) -> TransformBuild:
    """Pure over already-read rows. Raises ``TransformParamsError`` (missing/unusable fallback) before any outcome."""

    accounts = {
        canonical(r["account_id"]) or "": AccountFacts(
            None if r.get("account_code") is None else str(r["account_code"]),
            bool(r.get("is_active", True)),
            bool(r.get("is_posting_allowed", True)),
        )
        for r in account_rows
    }
    chart = destination_chart(destination_accounts)
    fallback = params.validate(chart)
    classes = classify_snapshot(rows, ledger_rows, global_accounts, deferral_rows)
    grouped, _controls, _hdr = group_invoice_rows(rows)
    item_by_line = {canonical(r["line_id"]) or "": str(r["item_id"]) for r in items if r["item_id"] is not None}
    raw_line_ids = {canonical(r["line_id"]): r["line_id"] for r in rows}
    outcomes: list[InvoiceOutcome] = []
    source_ids: list[str] = []
    line_map: dict[Any, dict[str, Any]] = {}
    header_map: dict[Any, dict[str, Any]] = {}
    for h in headers:
        iid = canonical(h["invoice_id"]) or ""
        sub, tax, total = _dec(h["subtotal"]), _dec(h["tax_amount"]), _dec(h["total_amount"])
        raw_header = {"subtotal": h["subtotal"], "tax_amount": h["tax_amount"], "total_amount": h["total_amount"]}
        source_ids.append(iid)
        if iid not in grouped:
            # a line-less invoice: carried unchanged, but still in the partition universe
            header_map[_key(h["invoice_id"])] = dict(raw_header)
            outcomes.append(
                InvoiceOutcome(
                    iid,
                    CARRY,
                    total,
                    total,
                    True,
                    None,
                    {"subtotal": sub, "tax_amount": tax, "total_amount": total},
                    {},
                    (),
                )
            )
            continue
        organization, _journal, lines = grouped[iid]
        facts = InvoiceFacts(
            iid,
            organization,
            str(h["status"] or ""),
            sub,
            tax,
            total,
            tuple(lines),
            classes[iid],
            {k: v for k, v in item_by_line.items() if k in {canonical(line.line_id) for line in lines}},
        )
        outcome = transform_invoice(facts, fallback, accounts)
        outcomes.append(outcome)
        if outcome.lands:
            header_map[_key(h["invoice_id"])] = (
                dict(outcome.header) if _header_changed(outcome, facts) else dict(raw_header)
            )
        else:
            header_map[_key(h["invoice_id"])] = {REFUSAL_KEY: outcome.queue_reason}
        for k, line in outcome.lines.items():
            raw = raw_line_ids[k]
            if not outcome.lands:
                line_map[_key(raw)] = {REFUSAL_KEY: outcome.queue_reason}
            else:
                line_map[_key(raw)] = {
                    "posting_account_ref": line.posting_account_ref,
                    "tax_account_ref": line.tax_account_ref,
                    "line_amount": line.line_amount,
                    "tax_amount": line.tax_amount,
                }
    return TransformBuild(
        line_map,
        header_map,
        TransformLedger(tuple(outcomes), fallback, tuple(source_ids), len(rows)),
        class_counts(classes.values()),
    )


def _header_changed(outcome: InvoiceOutcome, facts: InvoiceFacts) -> bool:
    return (outcome.header.get("subtotal"), outcome.header.get("tax_amount"), outcome.header.get("total_amount")) != (
        facts.subtotal,
        facts.tax_amount,
        facts.total_amount,
    )


def read_and_build(source: sa.Connection, params: TransformParams, destination: sa.Connection) -> TransformBuild:
    """Read the legacy snapshot (read-only, legacy names via the search_path) and the destination chart (read-only),
    and build the transforms."""

    # Refuse on a missing declared input before reading anything.
    if params.fallback_expense_account is None:
        raise TransformParamsError("fallback_expense_account_missing")
    if params.tenant_id is None:
        raise TransformParamsError("fallback_expense_account_tenant_unknown")

    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in source.execute(sa.text(sql)).mappings()]

    return build_transforms(
        rows(INVOICE_LINE_SQL),
        rows(LEDGER_DEBIT_SQL),
        rows(GLOBAL_TAX_ACCOUNT_SQL),
        rows(DEFERRAL_JOURNAL_SQL),
        rows(INVOICE_HEADER_SQL),
        rows(LINE_ITEM_SQL),
        rows(ACCOUNT_SQL),
        params,
        [dict(r) for r in destination.execute(sa.text(DEST_ACCOUNT_SQL)).mappings()],
    )


__all__ = [
    "PARAM_FALLBACK_EXPENSE",
    "REFUSAL_KEY",
    "RULES",
    "AccountFacts",
    "BridgeLine",
    "InvoiceFacts",
    "InvoiceOutcome",
    "LineOutcome",
    "TransformBuild",
    "TransformLedger",
    "TransformParams",
    "TransformParamsError",
    "account_problem",
    "build_transforms",
    "destination_chart",
    "read_and_build",
    "transform_invoice",
]
