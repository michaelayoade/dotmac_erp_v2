"""The importer's transform manifest as the verifiers read it (B-3 -> B-1 gate).

The importer (``importer/transforms.py``) records, per landed invoice a transform touched, which rules applied and the
exact values it landed (``TransformLedger.landed``). A verifier that finds the destination differing from the legacy
source accepts the difference ONLY where this manifest names the same values: a transform that does not cover the
failure leaves it refused. Transforms and Finance waivers stay separate: a manifest-explained failure is neither
waived nor does it trip ``waiver_not_needed``, and the evidence records ``transforms`` next to ``review``.

Pure data and comparisons, no database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from erp_v2.importer.tax_accounts import canonical
from erp_v2.importer.transforms import T_ITEM_NONSTOCK, T_ROUND_2DP, InvoiceOutcome, canonical_digest

HEADER_FIELDS: Final = ("subtotal", "tax_amount", "total_amount")
LINE_FIELDS: Final = ("posting_account_ref", "tax_account_ref", "line_amount", "tax_amount")


class TransformManifestError(ValueError):
    """The manifest is malformed; the verifier refuses rather than guess."""


@dataclass(frozen=True)
class InvoiceTransform:
    rules: frozenset[str]
    header: Mapping[str, Decimal] = field(default_factory=dict)  # changed header values as landed
    lines: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # canonical line id -> landed values
    item_lines: frozenset[str] = frozenset()

    @classmethod
    def from_outcome(cls, outcome: InvoiceOutcome) -> InvoiceTransform | None:
        """What the importer's rules say landed for this invoice, or None when no rule changed anything. The verifiers
        RE-DERIVE this from the snapshot and require it to equal the manifest entry and the destination."""
        lines = {
            k: {
                "posting_account_ref": line.posting_account_ref,
                "tax_account_ref": line.tax_account_ref,
                "line_amount": line.line_amount,
                "tax_amount": line.tax_amount,
            }
            for k, line in outcome.lines.items()
            if line.transformed
        }
        items = frozenset(b.line_id for b in outcome.bridge if b.rule == T_ITEM_NONSTOCK and b.line_id)
        if not (outcome.header_changed or lines or items):
            return None
        return cls(
            frozenset(b.rule for b in outcome.bridge),
            {f: v for f, v in outcome.header.items() if v is not None} if outcome.header_changed else {},
            lines,
            items,
        )

    def waivable_checks(self) -> dict[str, str]:
        """Waivable verifier checks this invoice's transforms fix, as ``{check name: transform rule id}``."""
        return {"source_amount_scale": T_ROUND_2DP} if T_ROUND_2DP in self.rules else {}

    def header_values(self, source: Mapping[str, Any]) -> dict[str, Any]:
        """The source header with the transform's landed values (what the destination must equal)."""
        return {**source, **self.header}

    def line_values(self, source: Mapping[str, Any]) -> dict[str, Any]:
        """The source line (keys of the legacy table) with the landed values of a transformed line."""
        landed = self.lines.get(canonical(source["line_id"]) or "")
        if landed is None:
            return dict(source)
        out = dict(source)
        out["expense_account_id"] = landed["posting_account_ref"]
        out["line_amount"] = landed["line_amount"]
        out["tax_amount"] = landed["tax_amount"]
        return out

    def landed_tax_ref(self, line_id: object) -> tuple[bool, str | None]:
        """``(True, ref)`` for a transformed line (the ref the destination must carry), else ``(False, None)``."""
        landed = self.lines.get(canonical(line_id) or "")
        return (False, None) if landed is None else (True, landed["tax_account_ref"])

    def items_explained(self, source_item_line_ids: set[str]) -> bool:
        """Every item-linked source line is covered by a T-ITEM-NONSTOCK application (and none is invented)."""
        return (
            T_ITEM_NONSTOCK in self.rules
            and bool(source_item_line_ids)
            and {canonical(i) or "" for i in source_item_line_ids} == set(self.item_lines)
        )


@dataclass(frozen=True)
class TransformManifest:
    invoices: Mapping[str, InvoiceTransform]
    sha256: str  # canonical_digest over the WHOLE itemised body, the same value as the import receipt's itemised_sha256
    snapshot_sha256: str = ""  # the snapshot the importer ran against (binding)
    fallback_account: str = ""  # the declared fallback expense account the importer used

    def for_invoice(self, invoice_id: object) -> InvoiceTransform | None:
        return self.invoices.get(canonical(invoice_id) or "")

    @classmethod
    def from_itemised(cls, data: Mapping[str, Any]) -> TransformManifest:
        landed = data.get("landed")
        if not isinstance(landed, Mapping):
            raise TransformManifestError("transform_manifest_landed_missing")
        binding = data.get("binding")
        snapshot = binding.get("snapshot_sha256") if isinstance(binding, Mapping) else None
        fallback = canonical(data.get("fallback_account")) if isinstance(data.get("fallback_account"), str) else None
        if not (isinstance(snapshot, str) and re.fullmatch(r"[0-9a-f]{64}", snapshot)) or not fallback:
            raise TransformManifestError("transform_manifest_binding_missing")
        invoices: dict[str, InvoiceTransform] = {}
        try:
            for invoice_id, entry in landed.items():
                lines = {
                    canonical(k) or "": {
                        "posting_account_ref": canonical(v["posting_account_ref"]),
                        "tax_account_ref": canonical(v["tax_account_ref"]),
                        "line_amount": Decimal(v["line_amount"]),
                        "tax_amount": Decimal(v["tax_amount"]),
                    }
                    for k, v in entry["lines"].items()
                }
                invoices[canonical(invoice_id) or ""] = InvoiceTransform(
                    frozenset(entry["rules"]),
                    {f: Decimal(v) for f, v in entry["header"].items()},
                    lines,
                    frozenset(canonical(i) or "" for i in entry["item_lines"]),
                )
        except (KeyError, TypeError, ArithmeticError, ValueError) as exc:
            raise TransformManifestError("transform_manifest_malformed") from exc
        return cls(invoices, canonical_digest(data), snapshot, fallback)

    @classmethod
    def load(cls, path: Path, expected_sha256: str | None) -> TransformManifest:
        """Read the importer's itemised file. ``expected_sha256`` is the import receipt's ``itemised_sha256``: a file
        that does not hash to it (edited, or from another run) is refused."""
        try:
            loaded = cls.from_itemised(json.loads(Path(path).read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            raise TransformManifestError("transform_manifest_unreadable") from exc
        if not (isinstance(expected_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise TransformManifestError("transform_manifest_receipt_sha256_required")
        if loaded.sha256 != expected_sha256:
            raise TransformManifestError("transform_manifest_digest_mismatch")
        return loaded


__all__ = ["InvoiceTransform", "TransformManifest", "TransformManifestError"]
