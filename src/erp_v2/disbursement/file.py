"""Pure bank-file builder: rows in, bytes + sha256 + row count out. No database, no I/O.

Format ``generic-csv-v1`` is a neutral, header-first CSV. Bank-specific layouts (legacy
Zenith/Access/GTBank, W15) are not decided and are deliberately not implemented. Cell text that a
spreadsheet could execute (leading ``= + - @`` or control characters) is neutralised, so a payee
name cannot inject a formula. The account number appears only in the returned bytes; ``repr`` of a
row and the receipt never contain it.
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
_FORMULA_LEAD = re.compile(r"^[=+\-@\t\r]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
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


def _cell(value: str) -> str:
    text = _CONTROL.sub(" ", str(value)).strip()
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
                _cell(row.reference),
                row.value_date.isoformat(),
                _cell(row.currency),
                f"{row.amount:.2f}",
                _cell(row.payee_name),
                _cell(row.bank_code),
                _cell(row.account_number),
                _cell(row.narration),
                _cell(row.debit_account),
            ]
        )
    content = buffer.getvalue().encode("utf-8")
    return DisbursementFile(content=content, row_count=len(rows), sha256=hashlib.sha256(content).hexdigest())
