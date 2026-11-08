"""Transform-to-fit importer for ERP v2 (never table copies).

Rows move from legacy `dotmac_erp` storage into kernel/module/`e2`
storage through explicit per-table mappings; a legacy table never
re-enters as a local model for a MODULE/KERNEL surface. Contents:
wave pairings (1-6), the dry-run planner, column-level specs, the
executor, and the aggregating pipeline. Writes happen only through
`run_import` on caller-owned connections; snapshot binding and the
operator CLI are the remaining importer-gate work (`docs/cutover.md`).
"""

from erp_v2.importer.pipeline import ImportReceipt, run_import
from erp_v2.importer.plan import (
    ImportPlan,
    PlanRefusal,
    TableMapping,
    build_plan,
)
from erp_v2.importer.wave1 import WAVE1_MAPPINGS
from erp_v2.importer.waves import SOURCE_TABLES, build_wave_mappings
from erp_v2.importer.waves36 import WAVE3_PAIRINGS, WAVE4_PAIRINGS, WAVE56_PAIRINGS

__all__ = [
    "SOURCE_TABLES",
    "WAVE1_MAPPINGS",
    "WAVE3_PAIRINGS",
    "WAVE4_PAIRINGS",
    "WAVE56_PAIRINGS",
    "ImportPlan",
    "ImportReceipt",
    "PlanRefusal",
    "TableMapping",
    "build_plan",
    "build_wave_mappings",
    "run_import",
]
