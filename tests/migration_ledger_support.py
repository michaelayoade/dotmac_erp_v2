"""Test helper: derive the #68 itemised file that backs a set of T-ROUND-2DP header bridge items.

Each header item gets the fingerprint of its #68 ``BridgeLine`` (same construction as ``BridgeLine.as_dict``) as its
evidence sha, so the bridge's rounding door can match it against the receipt-bound itemised file.
"""

import hashlib
import json
from decimal import Decimal

from erp_v2.migration_ledger.common import canonical_sha


def line_for(item: dict) -> dict:
    """The #68 header line (positive invoice totals) behind a 2000 net item."""
    legacy, v2 = -Decimal(item["legacy_amount"]), -Decimal(item["v2_amount"])
    body = {
        "rule": "T-ROUND-2DP", "scope": "header", "invoice_id": item["ref"], "line_id": None,
        "legacy_account": None, "v2_account": None, "legacy_amount": format(legacy, "f"),
        "v2_amount": format(v2, "f"), "delta": format(v2 - legacy, "f"), "note": "header total rounded",
    }  # fmt: skip
    body["fingerprint"] = hashlib.sha256(
        json.dumps({k: v for k, v in body.items()}, sort_keys=True).encode()
    ).hexdigest()
    return body


def fingerprint_of(body: dict) -> str:
    plain = {k: v for k, v in body.items() if k != "fingerprint"}
    return hashlib.sha256(json.dumps(plain, sort_keys=True).encode()).hexdigest()


def bind_rounding(items: list[dict], snapshot_sha: str, *, extra_lines=()):
    """Return (items with fingerprint evidence, itemised body, its canonical sha)."""
    out, lines = [], []
    for item in items:
        if item["bucket"] == "transform" and item["rule"] == "T-ROUND-2DP" and item["scope"] == "header":
            line = line_for(item)
            line["fingerprint"] = fingerprint_of(line)
            lines.append(line)
            item = {**item, "evidence_sha256": line["fingerprint"]}
        out.append(item)
    itemised = {"binding": {"snapshot_sha256": snapshot_sha}, "bridge_lines": [*lines, *extra_lines], "queue": []}
    return out, itemised, canonical_sha(itemised)
