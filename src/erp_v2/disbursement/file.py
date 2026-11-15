"""Pure bank-file builder: rows in, bytes + sha256 + row count out. No database, no I/O.

Format ``generic-csv-v1`` is a neutral, header-first CSV. Bank-specific layouts (legacy
Zenith/Access/GTBank, W15) are not decided and are deliberately not implemented. A cell holding a
line break, tab, control character, comma, double quote or semicolon is REFUSED (never rewritten);
a leading ``= + - @`` is neutralised so a payee name cannot inject a formula.
The account number appears only in the returned bytes; ``repr`` of a row and the receipt never contain it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

FORMAT: Final = "generic-csv-v1"
MAX_ROWS: Final = 500
HEADER: Final = (
    "reference",
    "value_date",
    "currency",
    "amount",
    "payee_name",
    "bank_code",
    "account_number",
    "narration",
    "debit_account",
)
_FORMULA_LEAD = re.compile(r"^[=+\-@]")
# Refused, never rewritten: line breaks and tabs, C0/C1 controls (including U+0085), the Unicode line and
# paragraph separators, and the delimiters comma, double quote and semicolon.
_FORBIDDEN = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029,";]')
_DIGITS = re.compile(r"^[0-9]{3,34}$")


class DisbursementFileError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class DisbursementRow:
    reference: str
    value_date: date
    currency: str
    amount: Decimal
    payee_name: str
    bank_code: str
    account_number: str = field(repr=False)
    narration: str
    debit_account: str = field(repr=False)

    def __repr__(self) -> str:
        return f"DisbursementRow({self.reference})"


@dataclass(frozen=True)
class DisbursementFile:
    """Bytes plus the only things that may be retained: format, row count and sha256."""

    content: bytes = field(repr=False)
    row_count: int
    sha256: str
    format: str = FORMAT

    def __repr__(self) -> str:
        return f"DisbursementFile({self.format}, rows={self.row_count}, sha256={self.sha256[:12]})"


def _cell(value: str, field_name: str) -> str:
    text = str(value)
    if _FORBIDDEN.search(text):
        raise DisbursementFileError(
            "invalid_cell", f"The {field_name} contains a character that is not allowed in a bank file"
        )
    text = text.strip()
    return "'" + text if _FORMULA_LEAD.match(text) else text


def no_store_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }


def build_csv(rows: list[DisbursementRow]) -> DisbursementFile:
    if not rows:
        raise DisbursementFileError("empty_file", "A bank file needs at least one row")
    if len(rows) > MAX_ROWS:
        raise DisbursementFileError("too_many_rows", f"A bank file holds at most {MAX_ROWS} rows")
    seen: set[str] = set()
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(HEADER)
    for row in rows:
        if row.reference in seen:
            raise DisbursementFileError("duplicate_reference", "Two rows carry the same reference")
        seen.add(row.reference)
        if (
            not isinstance(row.amount, Decimal)
            or not row.amount.is_finite()
            or row.amount <= 0
            or row.amount != row.amount.quantize(Decimal("0.01"))
        ):
            raise DisbursementFileError("invalid_amount", "File amounts must be positive with at most two decimals")
        if not _DIGITS.match(row.account_number) or not row.bank_code or not row.payee_name.strip():
            raise DisbursementFileError("invalid_destination", "A row has an incomplete payee destination")
        writer.writerow(
            [
                _cell(row.reference, "reference"),
                row.value_date.isoformat(),
                _cell(row.currency, "currency"),
                f"{row.amount:.2f}",
                _cell(row.payee_name, "payee name"),
                _cell(row.bank_code, "bank code"),
                _cell(row.account_number, "account number"),
                _cell(row.narration, "narration"),
                _cell(row.debit_account, "debit account"),
            ]
        )
    content = buffer.getvalue().encode("utf-8")
    return DisbursementFile(content=content, row_count=len(rows), sha256=hashlib.sha256(content).hexdigest())
