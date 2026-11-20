"""Shared pure helpers for the B-9 migration ledger tools: strict JSON, money, digests, refusals."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

QUANTUM: Final = Decimal("0.000001")
ZERO: Final = Decimal(0)
OPENING_PERIOD: Final = "OPENING"
PERIOD_RE: Final = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SHA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_MONEY_RE: Final = re.compile(r"^-?\d{1,14}(\.\d{1,6})?$")
REF_MAX: Final = 120
APPLY_ENV: Final = "ERP_MIGRATION_LEDGER_APPLY"


class LedgerRefused(ValueError):
    """Fail-closed refusal with a stable ``code`` (no values, no secrets in the code)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def refuse(code: str, detail: str = "") -> Any:
    raise LedgerRefused(code, detail)


def unit_evidence_sha(lines: Any) -> str:
    """THE evidence sha256 of a refused unit's ledger lines (E39 straddlers, E15 sub-kobo): ``sha256(json.dumps(lines,
    sort_keys=True))`` over the line dicts of the B-13 side files. Defined once here; ``importer.history_cutoff``
    re-exports it for B-14."""

    return hashlib.sha256(json.dumps(list(lines), sort_keys=True).encode()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha(body: Any) -> str:
    """THE digest of a JSON body: canonical JSON, sorted keys, no whitespace."""
    return sha256_bytes(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


def strict_json(raw: bytes, code: str = "invalid_json") -> Any:
    """Parse JSON refusing duplicate keys and any JSON float (money is always a string)."""

    def unique(pairs):
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                refuse("duplicate_json_key")
            out[key] = value
        return out

    def no_float(_text: str):
        return refuse("json_float_refused", "money and counts are strings or integers")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_float=no_float, parse_constant=no_float)
    except LedgerRefused:
        raise
    except (ValueError, UnicodeDecodeError):
        return refuse(code)


def money(value: object, code: str = "invalid_amount") -> Decimal:
    if not isinstance(value, str) or not _MONEY_RE.match(value):
        refuse(code)
    return Decimal(value)


def fmt(value: Decimal) -> str:
    return format(value.quantize(QUANTUM), "f")


def uuid_text(value: object, code: str) -> UUID:
    try:
        parsed = UUID(str(value))
    except ValueError:
        return refuse(code)
    if str(parsed) != value:
        refuse(code)
    return parsed


def day(value: object, code: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return refuse(code)
    if parsed.isoformat() != value:
        refuse(code)
    return parsed


def sha_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not SHA_RE.match(value):
        refuse(code)
    return value


def text(value: object, code: str, maximum: int = REF_MAX) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        refuse(code)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        refuse(code)
    return value


def keys_exactly(raw: object, keys: set[str], code: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict) or set(raw) != keys:
        refuse(code)
    return raw
