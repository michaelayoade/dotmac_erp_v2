# ruff: noqa: S608  (every interpolated fragment is a module constant; values are bound)
"""The pinned owner-resolution rules for the 2000 / 2040 / 1410 GL attribution receipts (GL-ATTRIBUTION-RECEIPTS-SPEC).

The mapping is CLOSED and pinned to the P-ATTR receipt (``P_ATTR_RECEIPT_SHA256``): the read-only census of the g9
legacy source copy that grouped every ``(source_document_type, lines, journals, net)`` per code and the count resolved
by each rule. A source document type that is not in these tables is NEVER guessed: its journals fall to
``unattributed``.
Free text (``description``, ``reference``) is never an owner key (metadata-as-identity is rejected); only exact-equality
joins from a system-written column to a primary key are used.

A legacy journal's owner set is the union of the owners resolved by every rule below, from ANY line of the journal and
from the document tables that reference the journal (and the reversal of such a journal). Rules (``rule`` ids appear
in the manifest ``by_source_type``):

* 2000 (party ``supplier`` / legacy supplier_id): ``J:ap_invoice`` and ``J:ap_payment`` (document journal), their
  reversals, ``L:SUPPLIER_INVOICE`` and ``L:SUPPLIER_PAYMENT`` (line ``source_document_id``), and K3
  ``L:SUPPLIER_PAYMENT_ALLOC`` (an orphan payment line whose surviving allocations name invoices of exactly ONE
  supplier).
* 2040 (party ``employee`` / legacy employee_id): ``J:salary_slip`` (+ reversal), ``J:claim``,
  ``J:claim_reimbursement`` (+ reversal), ``L:EXPENSE_CLAIM`` / ``L:EXPENSE_REIMBURSEMENT`` / ``L:RECLASS`` (line
  ``source_document_id`` is a claim id) and ``L:PAYROLL_ENTRY`` (a payroll run: its slips' employees, so multi-employee
  and shared). PAYROLL_DISBURSEMENT and BANK_RECONCILIATION carry no key: unattributed. K2 removes backfill reclass
  pairs as zero units (never assigns money to a party).
* 1410 (party ``employee``): the legacy GL has no loan document journal columns, so every 1410 journal is
  unattributed (24 STANDARD journals at g9).
"""

from __future__ import annotations

from typing import Final

from erp_v2.holdings.models import KIND_SUBKOBO_POST

P_ATTR_RECEIPT_SHA256: Final = "0daa2c133a52fc0f2d77c50191fa128462c71f483e817842337f75f626087aed"
SPEC_ID: Final = "GL-ATTRIBUTION-RECEIPTS-SPEC a7272e01 amended 1206b40e (K2, K3)"

CODES: Final = ("2000", "2040", "1410")
PARTY_KIND: Final = {"2000": "supplier", "2040": "employee", "1410": "employee"}
OWNER_RULES: Final = {
    "2000": (
        "J:ap_invoice",
        "J:ap_invoice_reversal",
        "J:ap_payment",
        "J:ap_payment_reversal",
        "L:SUPPLIER_INVOICE",
        "L:SUPPLIER_PAYMENT",
        "L:SUPPLIER_PAYMENT_ALLOC",
    ),
    "2040": (
        "J:salary_slip",
        "J:salary_slip_reversal",
        "J:claim",
        "J:claim_reimbursement",
        "J:claim_reversal",
        "L:EXPENSE_CLAIM",
        "L:EXPENSE_REIMBURSEMENT",
        "L:RECLASS",
        "L:PAYROLL_ENTRY",
    ),
    "1410": (),
}

# K2 (2040 only): the legacy backfill job's correlation id is 'EXPENSE-RECLASS-BACKFILL-JE-' || <source journal id>.
K2_PREFIX: Final = "EXPENSE-RECLASS-BACKFILL-JE-"
K2_CODES: Final = ("2040",)

# v2 non-legacy journal kinds (owner / source_document_kind) that the receipt knows.
# S8's claim-family reclass (owner erp.migration, ``KIND_CLAIM_RECLASS``) is consumed on 2040 only; sub-kobo journals
# (``KIND_SUBKOBO_POST``) count with the legacy-owner journals.
SUBKOBO_KINDS: Final = (KIND_SUBKOBO_POST,)

# --- legacy SQL (read-only; ``:d`` capture, ``:cut`` the history cutoff) -----------------------------------------

_POSTED = (
    "({a}.status IN ('POSTED', 'REVERSED') AND {a}.posting_date <= :d AND NOT EXISTS ("
    "SELECT 1 FROM gl.journal_entry r WHERE r.journal_entry_id = {a}.reversal_journal_id "
    "AND r.status IN ('POSTED', 'REVERSED') AND r.posting_date <= :d))"
)


def posted_by(alias: str) -> str:
    return _POSTED.format(a=alias)


# Every legacy ledger line on the code up to capture, summed per journal (GL-signed: debit - credit).
JOURNAL_NETS_SQL: Final = (
    "SELECT l.journal_entry_id::text AS j, coalesce(l.source_document_type, '-') AS t, "
    "sum(l.debit_amount - l.credit_amount) AS net, "
    "sum(CASE WHEN l.posting_date <= :cut THEN l.debit_amount - l.credit_amount ELSE 0 END) AS net_cut, "
    "count(*) AS n FROM gl.posted_ledger_line l "
    "WHERE l.account_id::text = ANY(:accounts) AND l.posting_date <= :d GROUP BY 1, 2"
)
UNDATED_SQL: Final = (
    "SELECT count(*) FROM gl.posted_ledger_line l WHERE l.account_id::text = ANY(:accounts) AND l.posting_date IS NULL"
)

# Owner events: (journal id, party key, rule). One statement per code; ``:ids`` restricts to the journals on the code.
OWNER_SQL: Final = {
    "2000": (
        "SELECT journal_entry_id::text, supplier_id::text, 'J:ap_invoice' FROM ap.supplier_invoice "
        "WHERE journal_entry_id::text = ANY(:ids) "
        "UNION ALL SELECT d.reversal_journal_id::text, i.supplier_id::text, 'J:ap_invoice_reversal' "
        "FROM ap.supplier_invoice i JOIN gl.journal_entry d ON d.journal_entry_id = i.journal_entry_id "
        "WHERE d.reversal_journal_id::text = ANY(:ids) "
        "UNION ALL SELECT journal_entry_id::text, supplier_id::text, 'J:ap_payment' FROM ap.supplier_payment "
        "WHERE journal_entry_id::text = ANY(:ids) "
        "UNION ALL SELECT d.reversal_journal_id::text, p.supplier_id::text, 'J:ap_payment_reversal' "
        "FROM ap.supplier_payment p JOIN gl.journal_entry d ON d.journal_entry_id = p.journal_entry_id "
        "WHERE d.reversal_journal_id::text = ANY(:ids) "
        "UNION ALL SELECT l.journal_entry_id::text, i.supplier_id::text, 'L:SUPPLIER_INVOICE' "
        "FROM gl.posted_ledger_line l JOIN ap.supplier_invoice i ON i.invoice_id = l.source_document_id "
        "WHERE l.source_document_type = 'SUPPLIER_INVOICE' AND l.journal_entry_id::text = ANY(:ids) "
        "UNION ALL SELECT l.journal_entry_id::text, p.supplier_id::text, 'L:SUPPLIER_PAYMENT' "
        "FROM gl.posted_ledger_line l JOIN ap.supplier_payment p ON p.payment_id = l.source_document_id "
        "WHERE l.source_document_type = 'SUPPLIER_PAYMENT' AND l.journal_entry_id::text = ANY(:ids) "
        # K3: an orphan payment line (no ap.supplier_payment) owned through the payment's surviving allocations,
        # only when EVERY allocation row resolves to an invoice and all invoices belong to exactly one supplier.
        "UNION ALL SELECT o.j, o.s, 'L:SUPPLIER_PAYMENT_ALLOC' FROM ("
        "SELECT l.journal_entry_id::text AS j, min(i.supplier_id::text) AS s "
        "FROM gl.posted_ledger_line l JOIN ap.payment_allocation a ON a.payment_id = l.source_document_id "
        "LEFT JOIN ap.supplier_invoice i ON i.invoice_id = a.invoice_id "
        "WHERE l.source_document_type = 'SUPPLIER_PAYMENT' AND l.journal_entry_id::text = ANY(:ids) "
        "AND NOT EXISTS (SELECT 1 FROM ap.supplier_payment p WHERE p.payment_id = l.source_document_id) "
        "GROUP BY l.journal_entry_id, l.source_document_id "
        "HAVING count(*) = count(i.supplier_id) AND count(DISTINCT i.supplier_id) = 1) o"
    ),
    "2040": (
        "SELECT journal_entry_id::text, employee_id::text, 'J:salary_slip' FROM payroll.salary_slip "
        "WHERE journal_entry_id::text = ANY(:ids) "
        "UNION ALL SELECT d.reversal_journal_id::text, s.employee_id::text, 'J:salary_slip_reversal' "
        "FROM payroll.salary_slip s JOIN gl.journal_entry d ON d.journal_entry_id = s.journal_entry_id "
        "WHERE d.reversal_journal_id::text = ANY(:ids) "
        "UNION ALL SELECT journal_entry_id::text, employee_id::text, 'J:claim' FROM expense.expense_claim "
        "WHERE journal_entry_id::text = ANY(:ids) "
        "UNION ALL SELECT reimbursement_journal_id::text, employee_id::text, 'J:claim_reimbursement' "
        "FROM expense.expense_claim WHERE reimbursement_journal_id::text = ANY(:ids) "
        "UNION ALL SELECT d.reversal_journal_id::text, c.employee_id::text, 'J:claim_reversal' "
        "FROM expense.expense_claim c JOIN gl.journal_entry d "
        "ON d.journal_entry_id IN (c.journal_entry_id, c.reimbursement_journal_id) "
        "WHERE d.reversal_journal_id::text = ANY(:ids) "
        "UNION ALL SELECT l.journal_entry_id::text, c.employee_id::text, 'L:' || l.source_document_type "
        "FROM gl.posted_ledger_line l JOIN expense.expense_claim c ON c.claim_id = l.source_document_id "
        "WHERE l.source_document_type IN ('EXPENSE_CLAIM', 'EXPENSE_REIMBURSEMENT', 'RECLASS') "
        "AND l.journal_entry_id::text = ANY(:ids) "
        "UNION ALL SELECT l.journal_entry_id::text, s.employee_id::text, 'L:PAYROLL_ENTRY' "
        "FROM gl.posted_ledger_line l JOIN payroll.salary_slip s ON s.payroll_entry_id = l.source_document_id "
        "WHERE l.source_document_type = 'PAYROLL_ENTRY' AND l.journal_entry_id::text = ANY(:ids)"
    ),
}

# K2: R references S by exact equality of the correlation id built from S's id (no parsing); both posted by capture.
K2_SQL: Final = (
    "SELECT r.journal_entry_id::text AS r, s.journal_entry_id::text AS s, "
    f"({posted_by('r')}) AS r_posted, (s.journal_entry_id IS NOT NULL AND {posted_by('s')}) AS s_posted "
    "FROM gl.journal_entry r LEFT JOIN gl.journal_entry s "
    f"ON r.correlation_id = '{K2_PREFIX}' || s.journal_entry_id::text "
    f"WHERE left(r.correlation_id, {len(K2_PREFIX)}) = '{K2_PREFIX}'"
)

# Per-invoice legacy residual at capture (the sub_2000 formula: functional amount less allocations dated <= D whose
# payment is posted by D). Supplier id and posted-by-D come along. Credit notes are stored negative.
INVOICE_RESIDUAL_SQL: Final = (
    "SELECT i.invoice_id::text AS inv, i.supplier_id::text AS sup, "
    f"({posted_by('j')}) AS posted, i.functional_currency_amount - coalesce(("
    "SELECT sum((a.allocated_amount + a.discount_taken) * (CASE WHEN i.currency_code = :functional THEN 1 "
    "ELSE i.exchange_rate END)) FROM ap.payment_allocation a "
    "JOIN ap.supplier_payment p ON p.payment_id = a.payment_id "
    "JOIN gl.journal_entry pj ON pj.journal_entry_id = p.journal_entry_id "
    f"WHERE a.invoice_id = i.invoice_id AND a.allocation_date <= :d AND {posted_by('pj')}), 0) AS residual "
    "FROM ap.supplier_invoice i LEFT JOIN gl.journal_entry j ON j.journal_entry_id = i.journal_entry_id "
    "WHERE i.invoice_id::text = ANY(:ids)"
)

# --- AP admission: the closed reason -> class map (EXCEPTIONS-ZERO) ---------------------------------------------
# A reason outside the map is NOT explained: the supplier's gap (E37 ``partygap``) carries it and the manifest lists it
# under ``reasons_unclassified``. Never guessed, never blocking. ``legacy_evidence:`` prefixes (settled plan) are
# stripped before the lookup. The names are the verifier reason codes of ``importer.payables_history`` and
# ``legacy_ap.settled_verifier``.
ADMISSION_CLASS: Final = {
    "unpaid": {  # unpaid-refused -> E02 (journal owner / line totals / scale / status pair); deferral -> E08
        **dict.fromkeys(
            (
                "source_journal_missing",
                "source_journal_status",
                "source_journal_unbalanced",
                "source_journal_shared",
                "source_ledger_line",
                "source_ledger_line_count",
                "source_ledger_line_duplicate",
                "source_amount_scale",
            ),
            "E02",
        ),
        **dict.fromkeys(("deferral_journal_ledger", "destination_deferral_journal_status"), "E08"),
    },
    "settled": {  # allocation anomaly -> E03c; overpaid / not partially paid -> E11; deferral -> E08
        **dict.fromkeys(
            (
                "allocation_amount_scale",
                "allocation_exceeds_total",
                "allocation_invoice",
                "allocation_payment_gl_unproven",
                "allocation_sum_mismatch",
            ),
            "E03c",
        ),
        "invoice_not_partially_paid": "E11",
        **dict.fromkeys(("deferral_journal_ledger", "destination_deferral_journal_status"), "E08"),
    },
}
# E07 (journal-linked APPROVED-unposted) carries no GL: it has no refusal reason in either plan, so it is not mapped.
SRC_UNPAID: Final = "unpaid"
SRC_SETTLED: Final = "settled"
