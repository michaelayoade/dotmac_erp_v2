"""Batch payroll input file parsing: strict, evidence-mandatory, amount-exact, never echoed."""

from decimal import Decimal
from pathlib import Path

import pytest

from erp_v2.web import payroll_run_input as inp

HEADER = "employee_ref,component_code,amount,evidence_ref\n"


def parse(body: str | bytes, minor_units: int = 2):
    data = body if isinstance(body, bytes) else body.encode()
    return inp.parse_input_file(data, minor_units=minor_units)


def test_valid_file_groups_rows_by_employee_and_hashes_the_bytes():
    parsed = parse(HEADER + "E1,BASIC,100000.00,hr:c1\nE1,BONUS,5,hr:c2\nE2,BASIC,50000,hr:c3\n")
    assert parsed.row_count == 3 and len(parsed.sha256) == 64
    assert parsed.by_employee["E1"]["BASIC"].amount == Decimal("100000.00")
    assert parsed.by_employee["E1"]["BONUS"].evidence_ref == "hr:c2"
    assert set(parsed.by_employee) == {"E1", "E2"}


def test_bom_blank_lines_and_surrounding_spaces_are_tolerated():
    parsed = parse(b"\xef\xbb\xbf" + (HEADER + "\n E1 , BASIC , 10.5 , hr:c1 \n\n").encode())
    assert parsed.by_employee["E1"]["BASIC"].amount == Decimal("10.5")


@pytest.mark.parametrize(
    "body",
    [
        "",
        HEADER,  # no data rows
        "employee,code,amount,evidence\nE1,BASIC,1,x\n",  # wrong header
        HEADER + "E1,BASIC,1.00\n",  # short row
        HEADER + "E1,BASIC,1.00,x,extra\n",  # long row
        HEADER + "E1,BASIC,1.00,\n",  # evidence mandatory
        HEADER + "E1,BASIC,1.00,   \n",
        HEADER + ",BASIC,1.00,x\n",
        HEADER + "E1,,1.00,x\n",
        HEADER + "E1,BASIC,-1.00,x\n",  # negative
        HEADER + "E1,BASIC,1e3,x\n",  # exponent
        HEADER + "E1,BASIC,NaN,x\n",
        HEADER + "E1,BASIC,1.005,x\n",  # more places than the currency: never rounded
        HEADER + "E1,BASIC,1,000.00,x\n",  # unquoted thousands separator
        HEADER + "E1,BASIC,100000000000000,x\n",  # above the range
        HEADER + "E1,BASIC,1.00,x\nE1,BASIC,2.00,y\n",  # duplicate employee/component
        HEADER + "E1,BASIC,1.00," + "x" * 241 + "\n",
    ],
)
def test_malformed_files_are_refused_with_safe_messages(body):
    with pytest.raises(inp.InputFileRefused) as exc:
        parse(body)
    assert exc.value.problems
    assert "100000000000000" not in str(exc.value)  # amounts are never echoed


def test_currency_scale_is_exact_per_run():
    assert parse(HEADER + "E1,BASIC,1000,x\n", minor_units=0).by_employee["E1"]["BASIC"].amount == 1000
    with pytest.raises(inp.InputFileRefused):
        parse(HEADER + "E1,BASIC,1000.5,x\n", minor_units=0)


def test_size_encoding_and_binary_limits():
    with pytest.raises(inp.InputFileRefused):
        inp.parse_input_file(b"x" * (inp.MAX_FILE_BYTES + 1), minor_units=2)
    with pytest.raises(inp.InputFileRefused):
        parse(b"\xff\xfe\x00bad")
    with pytest.raises(inp.InputFileRefused):
        parse(HEADER + "E1,BASIC,1.00,x\x00y\n")


def test_row_limit(monkeypatch):
    monkeypatch.setattr(inp, "MAX_ROWS", 2)
    with pytest.raises(inp.InputFileRefused):
        parse(HEADER + "E1,A,1,x\nE2,A,1,x\nE3,A,1,x\n")


def test_run_step_template_compiles():
    import jinja2

    root = Path(inp.__file__).resolve().parents[1] / "templates"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(root), autoescape=True)
    for name in ("payroll/run_step.html", "payroll/actions.html"):
        env.get_template(name)
