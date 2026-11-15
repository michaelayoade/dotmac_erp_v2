"""Pure form parsing for the manual-journal screen (no database, no framework)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from dotmac_accounting.contracts import JournalKind

from erp_v2.journals.service import JournalRefused, LineInput, PrepareJournal

FORM_LINES = 12


def _decimal(raw: str, label: str) -> Decimal:
    raw = raw.strip()
    if not raw:
        return Decimal(0)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise JournalRefused("invalid_amount", f"{label} is not a number") from exc
    if not value.is_finite():
        raise JournalRefused("invalid_amount", f"{label} is not a finite number")
    return value


def parse_prepare(values: Mapping[str, str]) -> PrepareJournal:
    try:
        request_id = UUID(values.get("request_id", ""))
    except ValueError as exc:
        raise JournalRefused("invalid_input", "The form has expired; reload it before saving") from exc
    try:
        posting_date = date.fromisoformat(values.get("posting_date", ""))
    except ValueError as exc:
        raise JournalRefused("invalid_input", "Enter the posting date as YYYY-MM-DD") from exc
    try:
        kind = JournalKind(values.get("kind", ""))
    except ValueError as exc:
        raise JournalRefused("invalid_input", "Select a journal kind") from exc
    lines: list[LineInput] = []
    for index in range(1, FORM_LINES + 1):
        account = values.get(f"account_{index}", "").strip()
        debit_raw = values.get(f"debit_{index}", "")
        credit_raw = values.get(f"credit_{index}", "")
        text = values.get(f"line_description_{index}", "").strip()
        if not (account or debit_raw.strip() or credit_raw.strip() or text):
            continue  # blank row
        try:
            account_id = UUID(account)
        except ValueError as exc:
            raise JournalRefused("invalid_input", f"Select an account on line {index}") from exc
        lines.append(
            LineInput(
                account_id=account_id,
                debit=_decimal(debit_raw, f"line {index} debit"),
                credit=_decimal(credit_raw, f"line {index} credit"),
                description=text or None,
            )
        )
    return PrepareJournal(
        request_id=request_id,
        posting_date=posting_date,
        kind=kind,
        description=values.get("description", ""),
        evidence_ref=values.get("evidence_ref", ""),
        lines=tuple(lines),
    )
