"""Reviewed admission: the closed vocabulary of Finance decisions and of waivable verifier checks (B-1).

Michael's ruling D-M3: Finance may waive ONE named verifier check per invoice, with evidence, through a
sha-bound decision file. This module is pure data (no database, no imports from the verifiers) so that the
verifiers, the operator CLI, the migration and the tests share one vocabulary.

What is waivable is an allowlist, never a denylist: a check that is not named here cannot be waived, so a
new verifier check is strict by default. The GL tie, the integrity checks and everything the plan calls
"never waivable" are absent on purpose and are listed in ``NEVER_WAIVABLE`` only as documentation and for
the test that proves no waivable name collides with them.
"""

from __future__ import annotations

from typing import Final

DECISIONS: Final = (
    "admit_residual",  # admit at the residual the GL proves; no check waived
    "admit_with_waivers",  # admit with exactly one named verifier check waived
    "void",  # Finance: the invoice is not owed (executed by the v2 void path, not by this module)
    "ias8_post",  # D-M5: post as an IAS 8 prior-period item, expense side to 3100 (B1b IAS 8 mode)
    "supplier_credit",  # E11/E12: a supplier credit (payables a6 path; recorded here)
    "reopen",  # E11: reopen a PAID invoice with a positive residual (a6 path; recorded here)
)

# The check names Finance may waive, and the verifier reasons each name covers. A reason that is not
# listed here is never waived. Each name is consumed in the verifiers by ``WaiverGate.require``.
WAIVABLE_CHECKS: Final = {
    # sum(line amounts / line taxes) != invoice subtotal / tax (R2 "line totals")
    "line_totals": ("invoice_line_totals",),
    # the legacy invoice journal is owned by another document (R2 "journal owner"): the ownership attributes
    # only. The journal must still be POSTED, balanced, equal in v2, and tie to the control account.
    "journal_owner": ("source_journal_owner", "source_journal_invoice", "source_ledger_invoice"),
    # header status / posting status disagree with each other while the legacy journal is POSTED (R2 "status
    # pair"); the status the evidence reports to the payables owner is still POSTED (the GL proves it).
    "inconsistent_status_pair": ("source_status",),
    # E03c: the unpaid header carries payment allocations (legacy anomaly Finance confirmed)
    "source_allocations": ("source_allocations",),
    # the header amounts are not at two decimals (E02 scale; T-ROUND-2DP is the preferred route)
    "source_amount_scale": ("source_amount_scale",),
    # A3: the legacy GL tax treatment is not one the rule recognises (E08 combined unbalanced)
    "legacy_gl_tax_treatment_unrecognised": ("legacy_gl_tax_treatment_unrecognised",),
    # A3 with the specific reason: the deferral journal does not equal the VAT (E08)
    "deferral_amount_mismatch": ("legacy_gl_tax_treatment_unrecognised",),
    # E07: header unposted (e.g. APPROVED, NOT_POSTED) but linked to a legacy journal the GL holds POSTED
    "legacy_journal_link_unposted": ("source_status", "source_posted_at"),
    # E10: line tax detail inconsistent. No verifier check reads line-tax detail today (the GL proof decides),
    # so this name is accepted in a decision file but no verifier consumes it: it is refused at admission as
    # ``waiver_not_needed`` rather than silently honoured. Open question for the architect.
    "line_tax_detail": (),
}

# Documentation of what must stay mandatory; ``tests`` assert none of these is a waivable name.
NEVER_WAIVABLE: Final = (
    "tenant",
    "snapshot_binding",
    "fingerprint",
    "gl_tie",  # source_ap_control_credit, invoice_control_other_postings, payment_gl_control_mismatch
    "residual_vs_control",
    "currency_fx",  # unsupported_currency
    "duplicate_admission",
)

# D-M3 says "one named verifier check per invoice".
MAX_WAIVERS_PER_INVOICE: Final = 1

IAS8_ACCOUNT_CODE: Final = "3100"
