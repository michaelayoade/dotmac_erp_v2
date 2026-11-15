"""Pure bank-file builder: format, hygiene, receipts. No database."""

import csv
import hashlib
import io
from datetime import date
from decimal import Decimal

import pytest

from erp_v2.disbursement import file as bank_file
from erp_v2.disbursement.file import DisbursementFileError, DisbursementRow


def row(**over):
    base = dict(
        reference="SP-1",
        value_date=date(2026, 1, 20),
        currency="NGN",
        amount=Decimal("215.00"),
        payee_name="ACME LTD",
        bank_code="058",
        account_number="0123456789",
        narration="Supplier payment SP-1",
        debit_account="payer-1",
    )
    base.update(over)
    return DisbursementRow(**base)


def test_csv_has_header_rows_hash_and_count():
    built = bank_file.build_csv([row(), row(reference="SP-2", amount=Decimal("10.5"))])
    parsed = list(csv.reader(io.StringIO(built.content.decode())))
    assert tuple(parsed[0]) == bank_file.HEADER and len(parsed) == 3
    assert parsed[2][3] == "10.50"
    assert built.row_count == 2 and built.sha256 == hashlib.sha256(built.content).hexdigest()
    assert built.format == bank_file.FORMAT


def test_output_is_deterministic():
    assert bank_file.build_csv([row()]).sha256 == bank_file.build_csv([row()]).sha256


def test_repr_never_carries_account_number_or_bytes():
    built = bank_file.build_csv([row()])
    for text in (repr(row()), repr(built)):
        assert "0123456789" not in text and "payer-1" not in text and "ACME" not in text


@pytest.mark.parametrize("lead", ["=", "+", "-", "@"])
def test_spreadsheet_formulas_are_neutralised(lead):
    built = bank_file.build_csv([row(payee_name=f'{lead}HYPERLINK("x")')])
    parsed = list(csv.reader(io.StringIO(built.content.decode())))
    assert parsed[1][4].startswith("'" + lead)


def test_control_characters_are_removed_from_text():
    built = bank_file.build_csv([row(payee_name="A\x00B\x1bC")])
    assert list(csv.reader(io.StringIO(built.content.decode())))[1][4] == "A B C"


@pytest.mark.parametrize(
    "rows,code",
    [
        ([], "empty_file"),
        ([row(), row()], "duplicate_reference"),
        ([row(amount=Decimal("0"))], "invalid_amount"),
        ([row(amount=Decimal("-1.00"))], "invalid_amount"),
        ([row(amount=Decimal("1.005"))], "invalid_amount"),
        ([row(amount=Decimal("NaN"))], "invalid_amount"),
        ([row(account_number="12AB")], "invalid_destination"),
        ([row(bank_code="")], "invalid_destination"),
        ([row(payee_name=" ")], "invalid_destination"),
    ],
)
def test_invalid_rows_refuse(rows, code):
    with pytest.raises(DisbursementFileError) as exc:
        bank_file.build_csv(rows)
    assert exc.value.code == code


def test_row_limit():
    rows = [row(reference=f"SP-{n}") for n in range(bank_file.MAX_ROWS + 1)]
    with pytest.raises(DisbursementFileError) as exc:
        bank_file.build_csv(rows)
    assert exc.value.code == "too_many_rows"


def test_no_store_headers():
    headers = bank_file.no_store_headers("f.csv")
    assert "no-store" in headers["Cache-Control"] and headers["Pragma"] == "no-cache"
    assert headers["Content-Disposition"] == 'attachment; filename="f.csv"'
    assert headers["X-Content-Type-Options"] == "nosniff"
