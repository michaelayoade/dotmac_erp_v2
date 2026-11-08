"""Wave-1 table mappings: files, imports, numbering.

Legacy ERP already composes these three lineages, so their live rows
already sit in module storage (`mod_files`, `mod_imports`,
`mod_numbering`) — those carry across by transform into the identical
destination shape (same lineage heads), preserving UUIDs. The one
genuine transform is the pre-module `numbering_sequence` allocator:
ERP's legacy allocators never switched (series-by-series cutover was
deferred in legacy), so its live series must enrol into
`mod_numbering` at the flip — an open ruling, flagged below so no
plan can be built until it is answered. Detail: docs/mapping-wave1.md.
"""

from __future__ import annotations

from typing import Final

from erp_v2.importer.plan import TableMapping

WAVE1_MAPPINGS: Final[tuple[TableMapping, ...]] = (
    # files (dotmac-files a7, fi -> fi_0002): byte storage itself is an
    # operator concern (file backend path/bucket); rows carry, bytes are
    # verified by checksum at the rehearsal gate.
    TableMapping(
        source="mod_files.stored_files",
        destination="mod_files.stored_files",
        kind="carry",
    ),
    TableMapping(
        source="mod_files.platform_stored_files",
        destination="mod_files.platform_stored_files",
        kind="carry",
    ),
    # imports (dotmac-imports 0.2.0a1, im -> im_0001)
    TableMapping(
        source="mod_imports.import_runs",
        destination="mod_imports.import_runs",
        kind="carry",
    ),
    TableMapping(
        source="mod_imports.import_run_rows",
        destination="mod_imports.import_run_rows",
        kind="carry",
        depends_on=("mod_imports.import_runs",),
    ),
    TableMapping(
        source="mod_imports.import_partitions",
        destination="mod_imports.import_partitions",
        kind="carry",
        depends_on=("mod_imports.import_runs",),
    ),
    # numbering (dotmac-numbering a3, nu -> nu_0001; tenant plane only,
    # matching the assembly's plane selection)
    TableMapping(
        source="mod_numbering.number_series",
        destination="mod_numbering.number_series",
        kind="carry",
    ),
    TableMapping(
        source="mod_numbering.series_counters",
        destination="mod_numbering.series_counters",
        kind="carry",
        depends_on=("mod_numbering.number_series",),
    ),
    TableMapping(
        source="mod_numbering.allocation_receipts",
        destination="mod_numbering.allocation_receipts",
        kind="carry",
        depends_on=("mod_numbering.number_series",),
    ),
    TableMapping(
        source="mod_numbering.series_repairs",
        destination="mod_numbering.series_repairs",
        kind="carry",
        depends_on=("mod_numbering.number_series",),
    ),
    # Pre-module legacy allocator. Live ERP document series still draw
    # from this table; enrolment semantics (current value -> counter,
    # collision handling vs already-enrolled mod_numbering series) must
    # be ruled before the rehearsal. a4's enrolment API is unreleased,
    # so on a3 this is an importer-side transform needing its own spec.
    TableMapping(
        source="public.numbering_sequence",
        destination="mod_numbering.number_series+series_counters",
        kind="transform",
        depends_on=("mod_numbering.number_series",),
        needs_ruling=(
            "enrolment spec for live legacy series on numbering a3 "
            "(collision rules, counter seeding, series freeze window)"
        ),
    ),
    # ERP-local files bookkeeping: operational evidence, not module
    # storage. Cleanup runs do not move; deletion evidence is part of
    # the post-flip archive-extent ruling.
    TableMapping(
        source="public.file_orphan_cleanup_runs",
        destination="retire (archive-extent ruling)",
        kind="retire",
    ),
    TableMapping(
        source="public.file_orphan_cleanup_deletions",
        destination="retire (archive-extent ruling)",
        kind="retire",
        depends_on=("public.file_orphan_cleanup_runs",),
    ),
)

__all__ = ["WAVE1_MAPPINGS"]
