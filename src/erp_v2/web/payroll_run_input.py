"""Parse the employee input file for a batch payroll calculation (B4).

The file is data only: it is read into memory, validated, hashed and discarded. It is
never written to disk, never executed and never echoed back (it carries pay amounts).
Format: UTF-8 CSV with exactly the header ``employee_ref,component_code,amount,evidence_ref``,
one row per (employee, input component). Every row must carry an evidence reference.
Amounts are exact decimals at the run's currency scale: nothing is rounded or defaulted.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

# Below Starlette's 1 MiB spool threshold even with form overhead, so an upload (which carries pay
# amounts) is held in memory and never written to a temporary file.
MAX_FILE_BYTES: Final = 512 * 1024
MAX_ROWS: Final = 20_000
HEADER: Final = ("employee_ref", "component_code", "amount", "evidence_ref")
_AMOUNT: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")
MAX_AMOUNT: Final = Decimal("99999999999999")


class InputFileRefused(ValueError):
    """The input file was rejected; ``problems`` are safe, amount-free messages."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class InputRow:
    component_code: str
    amount: Decimal
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class ParsedInput:
    by_employee: dict[str, dict[str, InputRow]]
    sha256: str
    row_count: int


EMPTY = ParsedInput({}, hashlib.sha256(b"").hexdigest(), 0)


def parse_input_file(data: bytes, *, minor_units: int) -> ParsedInput:
    if len(data) > MAX_FILE_BYTES:
        raise InputFileRefused([f"The input file exceeds {MAX_FILE_BYTES // 1024} KiB"])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InputFileRefused(["The input file must be UTF-8 text"]) from None
    if "\x00" in text:
        raise InputFileRefused(["The input file must be CSV text"])
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        raise InputFileRefused(["The input file is not valid CSV"]) from None
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows or tuple(cell.strip() for cell in rows[0]) != HEADER:
        raise InputFileRefused(["The header must be exactly: " + ",".join(HEADER)])
    body = rows[1:]
    if not body:
        raise InputFileRefused(["The input file has no data rows"])
    if len(body) > MAX_ROWS:
        raise InputFileRefused([f"The input file has more than {MAX_ROWS} rows"])
    problems: list[str] = []
    by_employee: dict[str, dict[str, InputRow]] = {}
    for number, row in enumerate(body, start=2):
        if len(row) != len(HEADER):
            problems.append(f"Row {number}: expected {len(HEADER)} columns")
            continue
        employee, code, amount, evidence = (cell.strip() for cell in row)
        if not employee or len(employee) > 240:
            problems.append(f"Row {number}: employee reference is required (maximum 240 characters)")
            continue
        if not code or len(code) > 80:
            problems.append(f"Row {number}: component code is required")
            continue
        if not evidence or len(evidence) > 240:
            problems.append(f"Row {number}: an evidence reference is required (maximum 240 characters)")
            continue
        if len(amount) > 40 or not _AMOUNT.fullmatch(amount):
            problems.append(f"Row {number}: amount must be a plain non-negative decimal number")
            continue
        value = Decimal(amount)
        if -value.as_tuple().exponent > minor_units or value > MAX_AMOUNT:
            problems.append(f"Row {number}: amount exceeds the currency precision or the allowed range")
            continue
        per_employee = by_employee.setdefault(employee, {})
        if code in per_employee:
            problems.append(f"Row {number}: component {code} appears more than once for employee {employee}")
            continue
        per_employee[code] = InputRow(code, value, evidence)
        if len(problems) >= 20:
            break
    if problems:
        raise InputFileRefused(problems)
    return ParsedInput(by_employee, hashlib.sha256(data).hexdigest(), len(body))
