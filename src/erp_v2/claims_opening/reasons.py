"""The closed list of claim refusal reasons (S8 section 2). Pure: no heavy imports (the register adapter uses it).

Every reason is an E37 register row. An unknown reason is refused by the adapter (``unknown_queue_reason``)."""

from __future__ import annotations

from typing import Final

CLASS_CODE: Final = "E37"
FAMILY: Final = ("2040", "2030", "2110")  # the legacy claim family; the only allowed carried accounts
FUNCTIONAL: Final = "NGN"

EXTRACT_REASONS: Final = ("claim_net_payable_missing",)
VERIFIER_REASONS: Final = (
    "legacy_claim_gl_unattributable",
    "claim_carried_split",
    "claim_carried_outside_family",
    "reimbursement_not_posted",
    "settlements_sum_mismatch",
    "claim_advance_unproven",
    "claim_currency_unsupported",
    "claim_without_employee",
    "destination_claim_missing",
    "source_claim_duplicate",
    "journal_undated",
    "journal_straddles_history_cutoff",
    "cutoff_opening_not_posted",
    "destination_journal_missing",
)
OWNER_CODES: Final = (
    "historical_claim_not_open",
    "legacy_gl_does_not_hold_claim",
    "claim_already_accepted",
    "claim_without_employee",
    "conflict",
)
OWNER_REASONS: Final = tuple(f"owner_refused:{c}" for c in OWNER_CODES)
REASONS: Final = frozenset((*EXTRACT_REASONS, *VERIFIER_REASONS, *OWNER_REASONS))


class ClaimsRefusal(ValueError):
    """An operator-level refusal (not a per-claim E37 row): stable ``reason``, no values."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
