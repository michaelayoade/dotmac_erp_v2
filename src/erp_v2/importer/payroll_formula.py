"""Legacy payroll formula -> typed calculation-rule conversion.

Legacy salary-structure lines carry `amount`, `amount_based_on_formula`
and a free-text `formula` (Frappe-style, components referenced by
abbreviation). The payroll module accepts only typed methods:
`input`, `fixed`, `percentage` (amount = sum(bases) x rate).

Conversion is deliberately narrow - the exact subset that can be
represented without changing what a run pays:

- not formula-based, amount present  -> fixed
- not formula-based, no amount       -> input (per-employee value)
- `ABBR`                             -> percentage, rate 1
- `EXPR * N`, `N * EXPR`             -> percentage, rate N
- `EXPR * N / 100` (and mirror)      -> percentage, rate N/100
  where EXPR is one abbreviation or several joined by `+`,
  optionally parenthesised.

ANYTHING else (conditionals, function calls, subtraction inside the
basis, unknown abbreviations, a legacy `condition` guard) does not
convert: the caller exception-queues the rule for a human ruling.
Rounding or approximating a pay formula is never acceptable here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_ABBR = r"[A-Za-z][A-Za-z0-9_]*"
_NUM = r"\d+(?:\.\d+)?"
_EXPR = rf"\(?\s*{_ABBR}(?:\s*\+\s*{_ABBR})*\s*\)?"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"^(?P<expr>{_EXPR})\s*\*\s*(?P<num>{_NUM})\s*/\s*100$"), "pct100"),
    (re.compile(rf"^(?P<num>{_NUM})\s*/\s*100\s*\*\s*(?P<expr>{_EXPR})$"), "pct100"),
    (re.compile(rf"^(?P<expr>{_EXPR})\s*\*\s*(?P<num>{_NUM})$"), "frac"),
    (re.compile(rf"^(?P<num>{_NUM})\s*\*\s*(?P<expr>{_EXPR})$"), "frac"),
    (re.compile(rf"^(?P<expr>{_ABBR})$"), "bare"),
)


@dataclass(frozen=True)
class RuleConversion:
    method: str  # input | fixed | percentage
    rate: Decimal | None
    basis_abbrs: tuple[str, ...]


def convert_rule(
    *,
    amount: object,
    amount_based_on_formula: object,
    formula: object,
    condition: object,
    known_abbrs: set[str],
) -> RuleConversion | None:
    """Convert one legacy rule row, or None when not exactly representable."""

    if condition not in (None, "", 0, False):
        return None  # a conditional guard has no typed equivalent
    if not amount_based_on_formula:
        if amount is not None:
            return RuleConversion("fixed", None, ())
        return RuleConversion("input", None, ())
    text = str(formula or "").strip()
    if not text:
        return None
    for pattern, kind in _PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        abbrs = tuple(a.strip() for a in match.group("expr").strip("() ").split("+"))
        if any(a not in known_abbrs for a in abbrs):
            return None
        try:
            if kind == "bare":
                rate = Decimal(1)
            elif kind == "pct100":
                rate = Decimal(match.group("num")) / Decimal(100)
            else:
                rate = Decimal(match.group("num"))
        except InvalidOperation:
            return None
        if rate < 0:
            return None
        return RuleConversion("percentage", rate, abbrs)
    return None


__all__ = ["RuleConversion", "convert_rule"]
