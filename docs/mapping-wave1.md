# Wave-1 transform mapping — files, imports, numbering

Source of truth (code): `src/erp_v2/importer/wave1.py`. This document
is the operator-facing summary; if they disagree, the code wins and
this file is the defect.

## Pins and destination heads (all released)

| Module | Pin | Head | Planes here |
|---|---|---|---|
| dotmac-files | 0.1.0a7 | `fi_0002_selectable_planes` | TENANT + PLATFORM |
| dotmac-imports | 0.2.0a1 | `im_0001_import_runs` | tenant-only (no selection) |
| dotmac-numbering | 0.1.0a3 | `nu_0001_numbering` | TENANT |

Numbering a4 exists in Starter source but its changelog marks it
UNRELEASED/unpublished; a3 is the latest released and has no
migration change vs legacy ERP's a2. Files a7 and imports 0.2.0a1 are
the latest release tags and match (files) legacy ERP's pins.

## Table rulings (12)

- **Carry (9)** — legacy ERP already composes these lineages, so rows
  already sit in module storage and move into the identical shape,
  UUID-preserving: `mod_files.stored_files`,
  `mod_files.platform_stored_files`, `mod_imports.import_runs` /
  `import_run_rows` / `import_partitions`,
  `mod_numbering.number_series` / `series_counters` /
  `allocation_receipts` / `series_repairs`.
- **Transform (1, BLOCKED)** — `public.numbering_sequence`, the
  pre-module allocator live ERP document series still draw from,
  must enrol into `mod_numbering` at the flip. Needs Michael's
  ruling: enrolment spec on a3 (current-value → counter seeding,
  collision rules against already-enrolled series, freeze window),
  or wait for a released a4 enrolment API. The planner refuses to
  build a wave-1 plan until this is ruled.
- **Retire (2)** — `public.file_orphan_cleanup_runs` /
  `file_orphan_cleanup_deletions` are ERP-local operational
  evidence, not module storage; they fall under the post-flip
  archive-extent ruling.

File **bytes** are not rows: the stored-file backend location moves
as an operator step and is checksum-verified at the rehearsal gate
(`docs/cutover.md` Gate 4). Mounting these modules changes no
runtime behaviour until a consumer slice reads through this app.
