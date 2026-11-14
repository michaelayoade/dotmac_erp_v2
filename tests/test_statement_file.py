"""Statement file parser: formats, layouts, refusals. Pure, offline, no database."""

import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from erp_v2.banking.statement_file import (
    MAX_FILE_BYTES,
    MAX_ROWS,
    StatementFileError,
    parse_statement,
)

TYPE_CSV = (
    "transaction_date,transaction_type,amount,description,reference\n"
    "2026-01-15,credit,50000.00,Salary,TRF-001\n"
    '2026-01-16,debit,"2,500.00",Rent,CHQ-1\n'
)
DC_CSV = (
    "Date,Narration,Debit,Credit,Balance\n"
    "2026-01-01,Opening balance,,,1000.00\n"
    "15/01/2026,Deposit,,300.50,1300.50\n"
    "16/01/2026,Fee,10.00,,1290.50\n"
)


def csv_bytes(text: str) -> bytes:
    return text.encode()


def test_type_layout_csv_parses_amounts_and_directions():
    parsed = parse_statement(csv_bytes(TYPE_CSV), "csv")
    assert not parsed.errors and parsed.layout == "type"
    assert [(line.direction, line.amount) for line in parsed.lines] == [
        ("credit", Decimal("50000.00")),
        ("debit", Decimal("2500.00")),
    ]
    assert parsed.lines[0].external_ref == "TRF-001"
    assert [line.line_number for line in parsed.lines] == [1, 2]
    assert len(parsed.sha256) == 64


def test_debit_credit_layout_dmy_dates_skips_balance_only_row():
    parsed = parse_statement(csv_bytes(DC_CSV), "csv", date_format="dmy")
    assert not parsed.errors and parsed.layout == "debit_credit"
    assert parsed.skipped_balance_rows == 1
    assert parsed.lines[0].transaction_date == date(2026, 1, 15)
    assert parsed.lines[0].external_ref.startswith("gen:")


def test_dmy_dates_refused_unless_selected():
    parsed = parse_statement(csv_bytes(DC_CSV), "csv", date_format="iso")
    assert parsed.errors and not parsed.lines


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_delimiters_are_detected(delimiter):
    text = TYPE_CSV.replace(",", delimiter).replace(f'"2{delimiter}500.00"', "2500.00")
    parsed = parse_statement(csv_bytes(text), "csv")
    assert not parsed.errors and len(parsed.lines) == 2


def test_bom_and_alias_headers():
    text = "﻿Txn Date,Txn Type,Amt,Particulars,Ref\n2026-02-01,Credit,5,x,r1\n"
    parsed = parse_statement(csv_bytes(text), "csv")
    assert not parsed.errors and parsed.lines[0].description == "x"


def test_identical_rows_without_reference_get_distinct_stable_identities():
    text = "date,debit,credit,narration\n2026-03-01,5.00,,Fee\n2026-03-01,5.00,,Fee\n"
    one = parse_statement(csv_bytes(text), "csv")
    two = parse_statement(csv_bytes(text), "csv")
    assert not one.errors and len({line.external_ref for line in one.lines}) == 2
    assert [line.external_ref for line in one.lines] == [line.external_ref for line in two.lines]


def test_same_reference_twice_in_one_file_is_refused():
    text = "date,debit,credit,reference\n2026-03-01,5.00,,R1\n2026-03-01,5.00,,R1\n"
    parsed = parse_statement(csv_bytes(text), "csv")
    assert any("duplicate" in e for e in parsed.errors)


@pytest.mark.parametrize(
    "row,fragment",
    [
        ("2026-03-01,-5,,x", "plain positive"),
        ("2026-03-01,5,5,x", "both debit and credit"),
        ("2026-03-01,,,x", "no debit or credit"),
        ("2026-03-01,1e3,,x", "plain positive"),
        ("2026-03-01,NaN,,x", "plain positive"),
        ("2026-03-01,999999999999999,,x", "out of range"),
        ("2026-13-45,5,,x", "invalid"),
        ("not-a-date,5,,x", "recognised date"),
        ("2026-03-01,5,,x" + "y" * 600, "longer than"),
    ],
)
def test_malformed_rows_are_collected_as_errors(row, fragment):
    parsed = parse_statement(csv_bytes("date,debit,credit,narration\n" + row + "\n"), "csv")
    assert not parsed.lines
    assert any(fragment in e for e in parsed.errors), parsed.errors


def test_bad_type_value_is_refused():
    text = "date,type,amount\n2026-01-01,transfer,5\n"
    assert any("must be 'debit' or 'credit'" in e for e in parse_statement(csv_bytes(text), "csv").errors)


@pytest.mark.parametrize(
    "data,fmt",
    [
        (b"", "csv"),
        (b"\xff\xfe bad", "csv"),
        (b"a,b\x00\n", "csv"),
        (b"date,foo\n2026-01-01,1\n", "csv"),
        (b"foo,bar\n1,2\n", "csv"),
        (b"date,debit,dr\n2026-01-01,1,2\n", "csv"),  # two columns map to debit
        (b"not a zip", "xlsx"),
        (b"x", "xls"),
    ],
)
def test_unusable_files_are_refused(data, fmt):
    with pytest.raises(StatementFileError):
        parse_statement(data, fmt)


def test_size_and_row_limits():
    with pytest.raises(StatementFileError, match="size limit"):
        parse_statement(b"a" * (MAX_FILE_BYTES + 1), "csv")
    rows = "".join(f"2026-01-01,1.00,,r{i}\n" for i in range(MAX_ROWS + 1))
    with pytest.raises(StatementFileError, match="more than"):
        parse_statement(("date,debit,credit,reference\n" + rows).encode(), "csv")


def test_header_only_file_has_no_transactions():
    parsed = parse_statement(b"date,debit,credit\n", "csv")
    assert parsed.errors and not parsed.lines


# ---------------------------------------------------------------------- XLSX


def make_xlsx(rows, *, date_cols=(), extra_members=None, doctype=False) -> bytes:
    """rows: list of lists of str | int | float | None; date_cols: column indexes styled as dates."""

    def ref(r, c):
        return f"{chr(65 + c)}{r + 1}"

    strings: list[str] = []
    xml_rows = []
    for r, row in enumerate(rows):
        cells = []
        for c, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, str):
                strings.append(value)
                cells.append(f'<c r="{ref(r, c)}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                style = ' s="1"' if (c in date_cols and r > 0) else ""
                cells.append(f'<c r="{ref(r, c)}"{style}><v>{value}</v></c>')
        xml_rows.append(f'<row r="{r + 1}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = f'<worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    sst = "".join(f"<si><t>{s}</t></si>" for s in strings)
    prefix = '<!DOCTYPE x [<!ENTITY a "b">]>' if doctype else ""
    members = {
        "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>',  # noqa: E501
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/></Relationships>',  # noqa: E501
        "xl/worksheets/sheet1.xml": prefix + sheet,
        "xl/sharedStrings.xml": f'<sst xmlns="{ns}">{sst}</sst>',
        "xl/styles.xml": f'<styleSheet xmlns="{ns}"><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>',  # noqa: E501
    }
    members.update(extra_members or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in members.items():
            z.writestr(name, body)
    return buffer.getvalue()


def test_xlsx_with_text_numbers_and_date_serials():
    data = make_xlsx(
        [["Date", "Narration", "Debit", "Credit"], [46037, "Deposit", None, 300.5], ["2026-01-16", "Fee", 10, None]],
        date_cols=(0,),
    )
    parsed = parse_statement(data, "xlsx")
    assert not parsed.errors, parsed.errors
    assert parsed.lines[0].transaction_date == date(2026, 1, 15)
    assert parsed.lines[0].amount == Decimal("300.5")
    assert parsed.lines[1].transaction_date == date(2026, 1, 16)


def test_xlsx_float_noise_is_normalised_but_text_dates_still_work():
    data = make_xlsx([["Date", "Debit", "Credit"], ["2026-01-16", None, 1234.5600000000001]])
    assert parse_statement(data, "xlsx").lines[0].amount == Decimal("1234.56")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"doctype": True},
        {"extra_members": {"xl/vbaProject.bin": "x"}},
        {"extra_members": {"xl/externalLinks/externalLink1.xml": "x"}},
    ],
)
def test_hostile_xlsx_is_refused(kwargs):
    data = make_xlsx([["Date", "Debit", "Credit"], ["2026-01-16", 1, None]], **kwargs)
    with pytest.raises(StatementFileError):
        parse_statement(data, "xlsx")


def test_xlsx_numeric_date_without_date_style_is_not_a_date():
    data = make_xlsx([["Date", "Debit", "Credit"], [46037, 5, None]])
    parsed = parse_statement(data, "xlsx")
    assert parsed.errors and not parsed.lines


def test_xlsx_zip_bomb_is_refused():
    big = "A" * (21 * 1024 * 1024)
    with pytest.raises(StatementFileError, match=r"expands|too large"):
        parse_statement(make_xlsx([["Date", "Debit"]], extra_members={"xl/junk.bin": big}), "xlsx")


@pytest.mark.parametrize(
    "amount,expected", [("1,500.50", "1500.50"), ("1500.50", "1500.50"), ("12,345,678", "12345678")]
)
def test_commas_are_thousands_separators_only(amount, expected):
    parsed = parse_statement(f'date,debit,credit\n2026-01-01,,"{amount}"\n'.encode(), "csv")
    assert not parsed.errors and parsed.lines[0].amount == Decimal(expected)


@pytest.mark.parametrize("amount", ["1500,50", "1,50", "1,5000", "15,00,000", ",500", "1,500,5"])
def test_decimal_comma_and_bad_grouping_are_refused(amount):
    parsed = parse_statement(f'date,debit,credit\n2026-01-01,,"{amount}"\n'.encode(), "csv")
    assert parsed.errors and not parsed.lines


@pytest.mark.parametrize("fmt", ["dmy", "mdy"])
def test_day_month_ambiguous_file_is_refused_unless_a_row_disambiguates(fmt):
    ambiguous = "date,debit,credit\n03/04/2026,1.00,\n05/06/2026,,2.00\n"
    parsed = parse_statement(ambiguous.encode(), "csv", date_format=fmt)
    assert any("cannot be verified" in e for e in parsed.errors)
    resolved = ambiguous + ("25/06/2026,1.00,\n" if fmt == "dmy" else "06/25/2026,1.00,\n")
    assert not parse_statement(resolved.encode(), "csv", date_format=fmt).errors


def test_iso_dates_are_never_ambiguous():
    assert not parse_statement(b"date,debit,credit\n2026-03-04,1.00,\n", "csv", date_format="dmy").errors


def test_utf16_doctype_in_xlsx_member_is_refused():
    sheet = (
        '<!DOCTYPE x [<!ENTITY a "b">]><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
    )
    data = make_xlsx([["Date", "Debit"]], extra_members={"xl/worksheets/sheet1.xml": sheet.encode("utf-16")})
    with pytest.raises(StatementFileError, match="DTD"):
        parse_statement(data, "xlsx")
