"""Parse an uploaded bank statement file (CSV or XLSX) into typed, validated lines.

Pure and offline: no database, no network, no execution of file content. The bytes
are only ever decoded as text / read as zip members of a fixed, small allow-list.
Everything fails closed: any malformed row, ambiguous header or limit breach is an
error, and callers refuse to import when ``errors`` is non-empty.

Formats follow the legacy ERP bank statement importer (header aliases, the two
column layouts, delimiter detection), without its tolerance for lossy decoding:

* CSV: UTF-8 (BOM allowed), delimiter one of ``, ; TAB |`` detected from the header.
* XLSX: first worksheet only; shared strings, inline strings, numbers and date cells.
* Layout ``debit_credit`` (a debit and/or credit column) or ``type`` (a
  transaction type column holding debit/credit plus one amount column).
* Legacy ``.xls`` (BIFF) is NOT supported: it needs a third-party parser.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final
from xml.etree import ElementTree as ET
from xml.parsers import expat

MAX_FILE_BYTES: Final = 2 * 1024 * 1024
MAX_ROWS: Final = 5000
MAX_COLUMNS: Final = 100
MAX_ERRORS: Final = 50
MAX_XLSX_MEMBERS: Final = 200
MAX_XLSX_UNCOMPRESSED: Final = 8 * 1024 * 1024
MAX_AMOUNT: Final = Decimal("100000000000000")  # banking stores Numeric(20, 6)

DATE_FORMATS: Final = {
    "iso": (),
    "dmy": ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"),
    "mdy": ("%m/%d/%Y", "%m-%d-%Y"),
}

# Header aliases from the legacy importer (normalised: lower case, spaces -> "_").
_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "transaction_date": (
        "transaction date",
        "date",
        "txn date",
        "posting date",
        "trans date",
        "tran date",
        "book date",
    ),
    "debit": ("debit", "dr", "withdrawal", "withdrawals", "debit amount"),
    "credit": ("credit", "cr", "deposit", "deposits", "credit amount"),
    "transaction_type": ("transaction type", "txn type", "trans type", "tran type", "type"),
    "amount": ("amount", "value", "amt", "transaction amount", "txn amount"),
    "description": ("description", "narration", "narrative", "particulars", "details", "memo", "remark", "remarks"),
    "reference": ("reference", "ref", "ref no", "reference number"),
    "counterparty": ("payee payer", "payee", "payer", "beneficiary", "party", "counterparty"),
    "running_balance": ("running balance", "balance", "closing balance"),
    "value_date": ("value date", "val date"),
    "transaction_id": ("transaction id", "txn id", "trans id"),
    "bank_reference": ("bank reference", "bank ref"),
    "bank_code": ("bank code", "bank category", "category"),
}


def _norm_header(value: object) -> str:
    return re.sub(r"[\s_\-.]+", "_", str(value).strip().lower()).strip("_")


_ALIAS_MAP: Final = {_norm_header(alias): canon for canon, aliases in _ALIASES.items() for alias in aliases}
_ALIAS_MAP.update({canon: canon for canon in _ALIASES})


class StatementFileError(ValueError):
    """The file itself is unusable (format, size, structure); nothing was parsed."""


@dataclass(frozen=True)
class ParsedLine:
    source_row: int
    line_number: int
    transaction_date: date
    direction: str  # "credit" | "debit"
    amount: Decimal
    external_ref: str
    description: str
    reference: str | None
    counterparty: str | None
    value_date: date | None
    bank_code: str | None


@dataclass(frozen=True)
class ParsedStatement:
    sha256: str
    file_format: str  # "csv" | "xlsx"
    layout: str  # "debit_credit" | "type"
    lines: tuple[ParsedLine, ...]
    errors: tuple[str, ...]
    skipped_balance_rows: int
    source_rows: int


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- field parsing

_AMOUNT = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_GROUPED_AMOUNT = re.compile(r"^[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?$")


def _text(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value).strip()


def parse_amount(value: object | None, field: str, row: int) -> Decimal | None:
    """None when blank; refuses negatives, exponents, NaN/Infinity and absurd sizes."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        amount = value
    else:
        text = _text(value).replace(" ", "")
        if not text:
            return None
        # Commas are thousands separators only (valid grouping); "1500,50" is ambiguous.
        if _GROUPED_AMOUNT.match(text):
            text = text.replace(",", "")
        if not _AMOUNT.match(text):
            raise ValueError(f"Row {row}: {field} is not a plain positive decimal number")
        amount = Decimal(text)
    if amount < 0:
        raise ValueError(f"Row {row}: {field} must not be negative")
    if amount >= MAX_AMOUNT:
        raise ValueError(f"Row {row}: {field} is out of range")
    return amount


def parse_date(
    value: object | None, field: str, row: int, date_format: str, seen: list[tuple[int, int]] | None = None
) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        raise ValueError(f"Row {row}: {field} is required")
    candidate = text.replace("/", "-") if re.match(r"^\d{4}/\d{1,2}/\d{1,2}", text) else text
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?$", candidate
    )
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            raise ValueError(f"Row {row}: invalid {field}") from None
    for pattern in DATE_FORMATS[date_format]:
        try:
            parsed = datetime.strptime(text, pattern).date()
        except ValueError:
            continue
        if seen is not None:
            first, second = re.findall(r"\d+", text)[:2]
            seen.append((int(first), int(second)))
        return parsed
    raise ValueError(f"Row {row}: {field} '{text[:30]}' is not a recognised date (format {date_format})")


def _bounded(value: str, limit: int, field: str, row: int) -> str | None:
    if len(value) > limit:
        raise ValueError(f"Row {row}: {field} is longer than {limit} characters")
    return value or None


# ------------------------------------------------------------------------- CSV


def _read_csv(data: bytes) -> list[list[object]]:
    if b"\x00" in data:
        raise StatementFileError("CSV file contains NUL bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise StatementFileError("CSV file must be UTF-8 encoded") from None
    header = next((line for line in text.splitlines() if line.strip()), "")
    counts = {d: header.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=counts.__getitem__)
    delimiter = best if counts[best] else ","
    try:
        return [list(row) for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)]
    except csv.Error as exc:
        raise StatementFileError(f"CSV file is malformed: {exc}") from None


# ------------------------------------------------------------------------ XLSX

_BUILTIN_DATE_FORMATS: Final = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47})
_REFUSED_PARTS: Final = ("xl/vbaProject.bin", "xl/externalLinks/", "xl/embeddings/", "xl/activeX/")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _member(archive: zipfile.ZipFile, name: str, *, required: bool = True) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise StatementFileError(f"XLSX is missing {name}") from None
        return None
    with archive.open(info) as handle:
        raw = handle.read(MAX_XLSX_UNCOMPRESSED + 1)
    if len(raw) > MAX_XLSX_UNCOMPRESSED:
        raise StatementFileError("XLSX member is too large")
    return raw


def _xml(raw: bytes) -> ET.Element:
    def refuse(*_args: object) -> None:
        raise StatementFileError("XLSX contains a DTD or entity declaration")

    # Encoding-independent scan: expat reports declarations whatever the member's encoding.
    scanner = expat.ParserCreate()
    scanner.StartDoctypeDeclHandler = refuse
    scanner.EntityDeclHandler = refuse
    try:
        scanner.Parse(raw, True)
        return ET.fromstring(raw)  # noqa: S314 - DTD/entities refused by the scan above
    except (expat.ExpatError, ET.ParseError):
        raise StatementFileError("XLSX contains malformed XML") from None


def _is_date_format(code: str) -> bool:
    stripped = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.|_.', "", code.lower())
    return bool(re.search(r"[dmyhs]", stripped)) and "general" not in stripped


def _date_styles(archive: zipfile.ZipFile) -> set[int]:
    raw = _member(archive, "xl/styles.xml", required=False)
    if raw is None:
        return set()
    root = _xml(raw)
    custom: dict[int, bool] = {}
    xfs: list[int] = []
    for node in root.iter():
        tag = _local(node.tag)
        if tag == "numFmt":
            try:
                custom[int(node.get("numFmtId", ""))] = _is_date_format(node.get("formatCode", ""))
            except ValueError:
                continue
    for parent in root:
        if _local(parent.tag) == "cellXfs":
            for xf in parent:
                try:
                    xfs.append(int(xf.get("numFmtId", "0")))
                except ValueError:
                    xfs.append(0)
    return {i for i, fmt in enumerate(xfs) if fmt in _BUILTIN_DATE_FORMATS or custom.get(fmt, False)}


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    raw = _member(archive, "xl/sharedStrings.xml", required=False)
    if raw is None:
        return []
    out: list[str] = []
    for si in _xml(raw):
        if _local(si.tag) != "si":
            continue
        out.append("".join(t.text or "" for t in _si_text_nodes(si)))
    return out


def _si_text_nodes(si: ET.Element):
    for child in si:
        name = _local(child.tag)
        if name == "t":
            yield child
        elif name == "r":
            for grand in child:
                if _local(grand.tag) == "t":
                    yield grand


def _column_index(ref: str) -> int:
    letters = re.match(r"^([A-Z]{1,3})[0-9]+$", ref)
    if not letters:
        raise StatementFileError("XLSX contains an invalid cell reference")
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = _xml(_member(archive, "xl/workbook.xml") or b"")
    rel_id = None
    for node in workbook.iter():
        if _local(node.tag) == "sheet":
            rel_id = next((v for k, v in node.attrib.items() if _local(k) == "id"), None)
            break
    if rel_id is None:
        raise StatementFileError("XLSX has no worksheet")
    rels = _xml(_member(archive, "xl/_rels/workbook.xml.rels") or b"")
    for node in rels:
        if node.get("Id") == rel_id:
            target = node.get("Target", "")
            if node.get("TargetMode") == "External":
                raise StatementFileError("XLSX worksheet is an external reference")
            path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if ".." in path.split("/") or not path.startswith("xl/worksheets/"):
                raise StatementFileError("XLSX worksheet path is not allowed")
            return path
    raise StatementFileError("XLSX worksheet relationship is missing")


def _read_xlsx(data: bytes) -> list[list[object]]:
    if not data.startswith(b"PK\x03\x04"):
        raise StatementFileError("File is not an XLSX workbook")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise StatementFileError("XLSX archive is corrupt") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_XLSX_MEMBERS:
            raise StatementFileError("XLSX archive has too many members")
        if sum(i.file_size for i in infos) > MAX_XLSX_UNCOMPRESSED:
            raise StatementFileError("XLSX archive expands beyond the size limit")
        for info in infos:
            if info.flag_bits & 0x1:
                raise StatementFileError("Encrypted XLSX files are not supported")
            if any(info.filename.startswith(part) for part in _REFUSED_PARTS):
                raise StatementFileError("XLSX contains macros, embedded objects or external links")
        shared = _shared_strings(archive)
        date_styles = _date_styles(archive)
        sheet = _xml(_member(archive, _first_sheet_path(archive)) or b"")
    rows: list[list[object]] = []
    for row in sheet.iter():
        if _local(row.tag) != "row":
            continue
        if len(rows) > MAX_ROWS + 1:
            raise StatementFileError(f"Statement has more than {MAX_ROWS} rows")
        cells: dict[int, object] = {}
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            col = _column_index(cell.get("r", ""))
            if col >= MAX_COLUMNS:
                raise StatementFileError("Statement has too many columns")
            cells[col] = _cell_value(cell, shared, date_styles)
        width = (max(cells) + 1) if cells else 0
        rows.append([cells.get(i) for i in range(width)])
    return rows


def _cell_value(cell: ET.Element, shared: list[str], date_styles: set[int]) -> object:
    kind = cell.get("t", "n")
    value_node = next((n for n in cell if _local(n.tag) == "v"), None)
    raw = value_node.text if value_node is not None and value_node.text is not None else None
    if kind == "inlineStr":
        node = next((n for n in cell if _local(n.tag) == "is"), None)
        return "".join(t.text or "" for t in _si_text_nodes(node)) if node is not None else None
    if raw is None:
        return None
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            raise StatementFileError("XLSX shared string reference is invalid") from None
    if kind == "str":
        return raw
    if kind in ("b", "e"):
        raise StatementFileError("XLSX contains boolean or error cells in the statement area")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise StatementFileError("XLSX contains a malformed numeric cell") from None
    if not number.is_finite():
        raise StatementFileError("XLSX contains a non-finite numeric cell")
    try:
        style = int(cell.get("s", "-1"))
    except ValueError:
        style = -1
    if style in date_styles:
        if number < 0 or number >= 2958466:  # Excel 1900 date system, up to 9999-12-31
            raise StatementFileError("XLSX contains an out-of-range date cell")
        return date(1899, 12, 30) + timedelta(days=int(number))
    # Excel stores binary doubles; normalise float noise (e.g. 1234.5600000000001).
    return number.quantize(Decimal("1e-8"))


# ------------------------------------------------------------------- statement


def parse_statement(data: bytes, file_format: str, *, date_format: str = "iso") -> ParsedStatement:
    """Parse and validate. Raises StatementFileError for an unusable file; row-level
    problems are collected in ``errors`` (callers must refuse to import when any)."""

    if file_format not in ("csv", "xlsx"):
        raise StatementFileError("Unsupported statement format (CSV and XLSX only)")
    if date_format not in DATE_FORMATS:
        raise StatementFileError("Unsupported date format option")
    if not data:
        raise StatementFileError("File is empty")
    if len(data) > MAX_FILE_BYTES:
        raise StatementFileError("File exceeds the size limit")
    raw_rows = _read_csv(data) if file_format == "csv" else _read_xlsx(data)
    header_index = next((i for i, r in enumerate(raw_rows) if any(_text(c) for c in r)), None)
    if header_index is None:
        raise StatementFileError("File must include a header row")
    headers = raw_rows[header_index]
    if len(headers) > MAX_COLUMNS:
        raise StatementFileError("Statement has too many columns")
    mapping: dict[int, str] = {}
    for i, h in enumerate(headers):
        canon = _ALIAS_MAP.get(_norm_header(_text(h)))
        if canon:
            mapping[i] = canon
    dupes = [name for name, n in Counter(mapping.values()).items() if n > 1]
    if dupes:
        raise StatementFileError(f"Ambiguous columns (more than one maps to): {', '.join(sorted(dupes))}")
    fields = set(mapping.values())
    if "debit" in fields or "credit" in fields:
        layout = "debit_credit"
    elif "transaction_type" in fields and "amount" in fields:
        layout = "type"
    else:
        raise StatementFileError("Cannot detect layout: need debit/credit columns or transaction type plus amount")
    if "transaction_date" not in fields:
        raise StatementFileError("Missing required column: transaction_date")
    body = raw_rows[header_index + 1 :]
    if len(body) > MAX_ROWS + 50:  # tolerate trailing blank rows, not more
        raise StatementFileError(f"Statement has more than {MAX_ROWS} rows")

    errors: list[str] = []
    lines: list[ParsedLine] = []
    skipped = 0
    source_rows = 0
    occurrences: Counter[tuple] = Counter()
    seen_identity: set[tuple] = set()
    seen_pairs: list[tuple[int, int]] = []
    for offset, cells in enumerate(body):
        row_no = header_index + offset + 2
        values = {mapping[i]: (cells[i] if i < len(cells) else None) for i in mapping}
        if not any(_text(v) for v in values.values()) and not any(_text(c) for c in cells):
            continue
        source_rows += 1
        if source_rows > MAX_ROWS:
            raise StatementFileError(f"Statement has more than {MAX_ROWS} rows")
        try:
            line = _row_to_line(values, row_no, layout, date_format, seen_pairs)
        except ValueError as exc:
            if len(errors) < MAX_ERRORS:
                errors.append(str(exc))
            continue
        if line is None:
            skipped += 1
            continue
        direction, amount, tdate, ext, desc, ref, cp, vdate, code = line
        if not ext:
            key = (tdate, direction, amount.normalize(), desc, ref, cp)
            occurrences[key] += 1
            digest = hashlib.sha256(("|".join(map(str, key)) + f"|{occurrences[key]}").encode()).hexdigest()[:24]
            ext = f"gen:{digest}"
        identity = (tdate, direction, amount.normalize(), ext)
        if identity in seen_identity:
            if len(errors) < MAX_ERRORS:
                errors.append(f"Row {row_no}: duplicate of an earlier row (same date, direction, amount, reference)")
            continue
        seen_identity.add(identity)
        lines.append(ParsedLine(row_no, len(lines) + 1, tdate, direction, amount, ext, desc, ref, cp, vdate, code))
    if seen_pairs and all(a <= 12 and b <= 12 for a, b in seen_pairs):
        errors.append(
            f"Every {date_format} date has day and month both 12 or less, so day/month order cannot be "
            "verified; use ISO dates (YYYY-MM-DD) or a file with an unambiguous date"
        )
    if not lines and not errors:
        errors.append("The statement contains no transaction rows")
    return ParsedStatement(
        sha256=sha256_hex(data),
        file_format=file_format,
        layout=layout,
        lines=tuple(lines),
        errors=tuple(errors),
        skipped_balance_rows=skipped,
        source_rows=source_rows,
    )


def _row_to_line(values: dict[str, object], row: int, layout: str, date_format: str, seen: list):
    tdate = parse_date(values.get("transaction_date"), "transaction_date", row, date_format, seen)
    vdate = values.get("value_date")
    value_date = parse_date(vdate, "value_date", row, date_format, seen) if _text(vdate) else None
    running = parse_amount(values.get("running_balance"), "running_balance", row)
    if layout == "debit_credit":
        debit = parse_amount(values.get("debit"), "debit", row)
        credit = parse_amount(values.get("credit"), "credit", row)
        has_debit, has_credit = bool(debit), bool(credit)
        if has_debit and has_credit:
            raise ValueError(f"Row {row}: both debit and credit are set")
        if not has_debit and not has_credit:
            if running is not None:
                return None  # balance-only row (e.g. opening balance)
            raise ValueError(f"Row {row}: no debit or credit amount")
        direction, amount = ("debit", debit) if has_debit else ("credit", credit)
    else:
        kind = _text(values.get("transaction_type")).lower()
        if kind not in ("debit", "credit"):
            raise ValueError(f"Row {row}: transaction_type must be 'debit' or 'credit'")
        amount = parse_amount(values.get("amount"), "amount", row)
        if not amount:
            raise ValueError(f"Row {row}: amount must be greater than zero")
        direction = kind
    assert amount is not None  # noqa: S101 - narrowed above
    reference = _bounded(_text(values.get("reference")), 200, "reference", row)
    counterparty = _bounded(_text(values.get("counterparty")), 240, "counterparty", row)
    description = _bounded(_text(values.get("description")), 500, "description", row)
    code = _bounded(_text(values.get("bank_code")), 80, "bank_code", row)
    ext = (
        _bounded(
            _text(values.get("transaction_id"))
            or _text(values.get("bank_reference"))
            or _text(values.get("reference")),
            200,
            "transaction reference",
            row,
        )
        or ""
    )
    return (
        direction,
        amount,
        tdate,
        ext,
        description or counterparty or reference or "(no narration)",
        reference,
        counterparty,
        value_date,
        code,
    )
