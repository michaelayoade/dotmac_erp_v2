"""The closed exception-class list: exactly the row ids of the EXCEPTIONS-ZERO-PLAN inventory at its bound revision.

E01-E34 plus the lettered sub-classes. ``tests`` compare this constant with the plan table's ids when the plan file
is present. A class is covered when the register holds at least one row for it (a loaded fact, a manually added
Finance item, or a not-applicable declaration); the gate refuses ``class_coverage_incomplete`` otherwise.
"""

from __future__ import annotations

from typing import Final

PLAN_SHA256: Final = "7160002d479d502df384ea98c6b150c9ceadf2f0b3a5b36e00a71ad8b63be608"
CLASSES: Final = (
    "E01", "E02", "E03", "E03a", "E03b", "E03c", "E04", "E04b", "E05", "E06", "E06a", "E06b", "E06c", "E06d",
    "E06e", "E06f", "E07", "E08", "E09", "E10", "E11", "E12", "E13", "E14", "E15", "E16", "E16a", "E16b", "E17",
    "E18", "E19", "E20", "E21", "E22", "E23", "E24", "E25", "E26", "E27", "E28", "E29", "E30", "E31", "E32", "E33",
    "E34",
)  # fmt: skip
NA_REASON_MAX: Final = 500
