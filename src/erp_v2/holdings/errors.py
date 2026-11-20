"""One refusal type for every B-14 tool: a stable machine-readable ``code``; no values or secrets in the code.

A subclass of the B-9 ``LedgerRefused`` so the shared strict parsers and the operator-CLI plumbing see one family.
"""

from __future__ import annotations

from typing import Any

from erp_v2.migration_ledger.common import LedgerRefused


class HoldingRefused(LedgerRefused):
    pass


def refuse(code: str, detail: str = "") -> Any:
    raise HoldingRefused(code, detail)
