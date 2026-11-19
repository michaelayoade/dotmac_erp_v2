"""The #68 adapter: T-ROUND-2DP header lines -> bridge items (B-9 section 12.6).

Reads the receipt-bound #68 itemised file (digest = the import receipt's ``itemised_sha256``; snapshot sha equal to
the bridge's) and, for each header line, takes the PERIOD and the ACCOUNT from the snapshot:

* period = ``YYYY-MM`` of ``gl.journal_entry.posting_date`` of the invoice's ``journal_entry_id``
  (``ap.supplier_invoice`` has no posting date; the journal is where 2000 carries the legacy amount);
* account = ``ap_control_account_id``, mapped by id (legacy ids are v2 ids); it must resolve in the tenant's chart
  and its CURRENT code must be 2000, else the adapter refuses.

An invoice that was not legacy-posted (no journal or no ledger lines) has no GL residue: its delta is subledger-only
(v2 posts it later at 2 dp), so it is EXCLUDED from the rounding items and only counted. Items are in 2000 net terms
(the credit is negative); ``evidence_sha256`` is the #68 line fingerprint and ``ref`` the legacy journal id.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.migration_ledger import legacy_ledger
from erp_v2.migration_ledger.bridge import AP_CONTROL, ROUND_RULE
from erp_v2.migration_ledger.common import canonical_sha, fmt, refuse


def check_binding(itemised: dict[str, Any], receipt_sha: str, snapshot_sha: str) -> None:
    if canonical_sha(itemised) != receipt_sha:
        refuse("import_itemised_digest_mismatch")
    binding = itemised.get("binding")
    if not isinstance(binding, dict) or binding.get("snapshot_sha256") != snapshot_sha:
        refuse("import_itemised_snapshot_mismatch")


def rounding_items(
    itemised: dict[str, Any], receipt_sha: str, snapshot_sha: str, legacy: sa.Connection, db: Session, tenant: UUID
) -> dict[str, Any]:
    check_binding(itemised, receipt_sha, snapshot_sha)
    codes = legacy_ledger.account_codes(db, tenant)
    items: list[dict[str, Any]] = []
    excluded = 0
    for line in itemised.get("bridge_lines") or []:
        if not isinstance(line, dict) or line.get("rule") != ROUND_RULE or line.get("scope") != "header":
            continue
        invoice = str(line.get("invoice_id"))
        found = legacy.execute(
            sa.text(
                "SELECT si.journal_entry_id::text, si.ap_control_account_id::text, je.posting_date "
                "FROM ap.supplier_invoice si LEFT JOIN gl.journal_entry je "
                "ON je.journal_entry_id = si.journal_entry_id "
                "WHERE si.invoice_id = CAST(:i AS uuid)"
            ),
            {"i": invoice},
        ).first()
        if found is None:
            refuse("rounding_invoice_not_in_snapshot")
        journal_id, control_id, posting_date = found
        if control_id is None or codes.get(control_id) != AP_CONTROL:
            refuse("rounding_invoice_control_account_not_2000")
        has_lines = journal_id is not None and legacy.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM gl.posted_ledger_line WHERE journal_entry_id = CAST(:j AS uuid))"),
            {"j": journal_id},
        )
        if not has_lines or posting_date is None:
            excluded += 1  # subledger-only: no GL residue
            continue
        legacy_total, v2_total = Decimal(line["legacy_amount"]), Decimal(line["v2_amount"])
        items.append(
            {
                "item_id": f"round:{line['fingerprint'][:32]}",
                "bucket": "transform",
                "ref": journal_id,
                "account": AP_CONTROL,
                "period": posting_date.strftime("%Y-%m"),
                "rule": ROUND_RULE,
                "scope": "header",
                "legacy_amount": fmt(-legacy_total),
                "v2_amount": fmt(-v2_total),
                "evidence_sha256": line["fingerprint"],
                "source_invoice_id": invoice,
            }
        )
    return {"items": sorted(items, key=lambda i: i["item_id"]), "excluded_not_legacy_posted": excluded}
