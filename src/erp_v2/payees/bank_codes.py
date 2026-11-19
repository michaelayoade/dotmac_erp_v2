"""Bank reference for the bulk payee command (B-11): a sha-bound file supplied and confirmed by Finance.

ERP v2 holds no reusable bank reference: ``dotmac_banking.BankInstitution`` is a per-tenant table (code, name,
clearing code, status) with no NUBAN applicability and nothing in ERP v2 populates it. So the code list comes
from a file Finance confirms and the operator binds by sha256::

    {"version": 1, "banks": [{"code": "058", "name": "<bank name>", "nuban_check": "check_digit|format_only"}]}

``check_digit``: the revised CBN NUBAN check digit, weights 3,7,3,3,7,3,3,7,3,3,7,3,3,7,3 over a six-digit
institution code plus the first nine account digits. A three-digit code is padded with 000 first (identical to the
original scheme for those codes). Definable for three- and six-digit codes only. ``format_only``: ten digits,
nothing more.
A code absent from the file is refused as ``unknown_bank_code``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

_WEIGHTS: Final = (3, 7, 3, 3, 7, 3, 3, 7, 3, 3, 7, 3, 3, 7, 3)  # revised CBN scheme: 6-digit code + 9 serial
_SHA = re.compile(r"[0-9a-f]{64}")
_CODE = re.compile(r"[0-9]{3,6}")
MODES: Final = ("check_digit", "format_only")
MAX_BANKS = 2000


class BankReferenceError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class BankReference:
    sha256: str
    banks: dict[str, tuple[str, str]]  # code -> (name, nuban_check)

    def name(self, code: str) -> str | None:
        found = self.banks.get(code)
        return None if found is None else found[0]

    def valid_nuban(self, bank_code: str, account_number: str) -> bool:
        return valid_nuban(bank_code, account_number, check_digit=self.banks[bank_code][1] == "check_digit")


def nuban_check_digit(bank_code: str, serial9: str) -> int:
    """Revised CBN scheme over a six-digit institution code + nine serial digits (15 weights).

    A three-digit code is padded with ``000`` first; the three zero digits add nothing and the remaining twelve
    weights equal the original NUBAN weights, so the result is identical to the original scheme for them.
    """

    code = "000" + bank_code if len(bank_code) == 3 else bank_code
    if len(code) != 6:
        raise ValueError("check digit is defined for three- or six-digit institution codes")
    total = sum(int(d) * w for d, w in zip(code + serial9, _WEIGHTS, strict=True))
    return (10 - total % 10) % 10


def valid_nuban(bank_code: str, account_number: str, *, check_digit: bool) -> bool:
    if len(account_number) != 10 or not account_number.isascii() or not account_number.isdigit():
        return False
    if not check_digit:
        return True
    return nuban_check_digit(bank_code, account_number[:9]) == int(account_number[9])


def parse_bank_reference(raw: bytes | None, expected_sha256: str | None) -> BankReference:
    if raw is None or not expected_sha256:
        raise BankReferenceError("bank_reference_required")
    if not _SHA.fullmatch(expected_sha256) or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise BankReferenceError("bank_reference_digest_mismatch")

    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise BankReferenceError("duplicate_json_key")
            out[key] = value
        return out

    try:
        data = json.loads(raw, object_pairs_hook=unique)
    except BankReferenceError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise BankReferenceError("invalid_bank_reference") from exc
    if not isinstance(data, dict) or set(data) != {"version", "banks"} or data["version"] != 1:
        raise BankReferenceError("invalid_bank_reference")
    banks = data["banks"]
    if not isinstance(banks, list) or not 1 <= len(banks) <= MAX_BANKS:
        raise BankReferenceError("invalid_bank_reference")
    out: dict[str, tuple[str, str]] = {}
    for bank in banks:
        if not isinstance(bank, dict) or set(bank) != {"code", "name", "nuban_check"}:
            raise BankReferenceError("invalid_bank_reference")
        code, name, mode = bank["code"], bank["name"], bank["nuban_check"]
        if not (isinstance(code, str) and _CODE.fullmatch(code)) or code in out:
            raise BankReferenceError("invalid_bank_reference")
        if not (isinstance(name, str) and name.strip() and name == name.strip() and len(name) <= 120):
            raise BankReferenceError("invalid_bank_reference")
        if mode not in MODES or (mode == "check_digit" and len(code) not in (3, 6)):
            raise BankReferenceError("invalid_bank_reference")
        out[code] = (name, mode)
    return BankReference(expected_sha256, out)
