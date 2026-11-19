"""The closed exception-class list: exactly the row ids of the EXCEPTIONS-ZERO-PLAN inventory at its bound revision.

E01-E34 plus the lettered sub-classes. ``tests`` compare this constant with the plan table's ids when the plan file
is present. A class is covered when the register holds at least one row for it (a loaded fact, a manually added
Finance item, or a not-applicable declaration); the gate REPORTS ``classes_missing`` (a report, not a gate, under
profile ``as_is_v1``).
"""

from __future__ import annotations

from typing import Final

# AS-IS-MIGRATION-REPLAN (rev 1, 2026-11-19): the plan of record. ``declare_na`` records it.
PLAN_SHA256: Final = "a28adf06752c43cd69481830c1b0e8d02a36f94a3bbe3c2ce35443d46b28de54"
# The EXCEPTIONS-ZERO-PLAN inventory (E01-E34) is bound by its own sha; the as-is plan adds E35-E38.
INVENTORY_PLAN_SHA256: Final = "7160002d479d502df384ea98c6b150c9ceadf2f0b3a5b36e00a71ad8b63be608"
INVENTORY_CLASSES: Final = (
    "E01", "E02", "E03", "E03a", "E03b", "E03c", "E04", "E04b", "E05", "E06", "E06a", "E06b", "E06c", "E06d",
    "E06e", "E06f", "E07", "E08", "E09", "E10", "E11", "E12", "E13", "E14", "E15", "E16", "E16a", "E16b", "E17",
    "E18", "E19", "E20", "E21", "E22", "E23", "E24", "E25", "E26", "E27", "E28", "E29", "E30", "E31", "E32", "E33",
    "E34",
)  # fmt: skip
# As-is additions (plan section 1): E35 held AR rows/partitions, E36a AR customer gaps / unattributed journals,
# E36b billing variances, E37 D-level control differences (info; the flip holding covers them), E38 report items
# (2110 / 2030 / 1455 statutory review).
AS_IS_CLASSES: Final = ("E35", "E36a", "E36b", "E37", "E38")
CLASSES: Final = (*INVENTORY_CLASSES, *AS_IS_CLASSES)
NA_REASON_MAX: Final = 500
