"""Transform-to-fit importer for ERP v2 (never table copies).

Rows move from legacy `dotmac_erp` storage into kernel/module/`e2`
storage through explicit per-table mappings; a legacy table never
re-enters as a local model for a MODULE/KERNEL surface. Every wave
adds its mappings here together with its released pin. The operator
pipeline (snapshot binding, root transaction, receipt) arrives at
the importer gate in `docs/cutover.md`; this package currently
carries the wave-1 mappings and a dry-run planner only — nothing in
this package writes to a destination database.
"""

from erp_v2.importer.plan import (
    ImportPlan,
    PlanRefusal,
    TableMapping,
    build_plan,
)
from erp_v2.importer.wave1 import WAVE1_MAPPINGS
from erp_v2.importer.waves import SOURCE_TABLES, build_wave_mappings

__all__ = [
    "SOURCE_TABLES",
    "WAVE1_MAPPINGS",
    "ImportPlan",
    "PlanRefusal",
    "TableMapping",
    "build_plan",
    "build_wave_mappings",
]
