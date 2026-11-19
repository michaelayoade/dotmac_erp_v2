"""Code constants for the AR producer. A setting must never widen any of these (W7 is a code decision)."""

from __future__ import annotations

from typing import Final

SOURCE_SYSTEM: Final = "erp_v2"
SOURCE_KIND: Final = "erp_v2.ar_invoice_line"
ACCEPTED_SOURCE_KINDS: Final = frozenset({SOURCE_KIND})
# W7: an operator assertion by a named holder of erp.finance.receivables.post, backed by a bank reference that is
# unique per bank account.
CONFIRMATION_EVIDENCE: Final = "erp_v2.ar.receipt_recorded"
ACCEPTED_CONFIRMATION_EVIDENCE: Final = frozenset({CONFIRMATION_EVIDENCE})
SETTLEMENT_SOURCE_SYSTEM: Final = "erp_v2.ar"
SETTLEMENT_SOURCE_VERSION: Final = "1"
CHARGE_COMPONENT: Final = "manual"
PRICE_VERSION: Final = "erp_v2.ar:1"
CONTRACT_LINE_REF: Final = "erp_v2.ar.manual"
CONTRACT_VERSION: Final = "1"
DERIVATION_POLICY: Final = "erp_v2.ar.net_days"
DERIVATION_VERSION: Final = "1"
TIMEZONE: Final = "Africa/Lagos"
LOCALE: Final = "en-NG"
AR_CONTROL_CODE: Final = "1400"
ADDRESS_NOT_RECORDED: Final = "Not recorded in legacy ERP"
MAX_LINES: Final = 100
