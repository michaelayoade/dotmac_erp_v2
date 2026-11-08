# dotmac_erp_v2

Dotmac ERP, composed thin from Starter. Ruled 2026-11-08 (Michael):
ERP moves to this new composed app and migrates to it by
transform-to-fit + one downtime flip; the legacy `dotmac_erp` remains
untouched as rollback until the flip. Service/model classification and
wave plan: [docs/classification.md](docs/classification.md). Cutover
gates: [docs/cutover.md](docs/cutover.md).

Composition (kernel + 31 released Starter modules, waves 1-6):

- `dotmac-kernel` `0.1.0a107` — tenancy (multi), identity,
  authorization, sessions, settings, audit, `create_app`.
- `dotmac-ui` `0.1.0a9` — shared design system.
- Wave 1 platform: files, imports, numbering. Wave 2: people,
  payroll, tax. Wave 3 finance core: accounting, approvals, assets,
  banking, expenses, finance (fixed assets), payables, payments,
  procurement. Wave 4 revenue: billing, collections, commercial
  agreements, customers, party, sales. Wave 5: inventory, projects.
  Wave 6 satellites: ai-operations, analytics, documents,
  durable-timers, forms, inbox, ticketing, workflow-runtime.
  Exact pins live in [pyproject.toml](pyproject.toml) and are
  asserted by [tests/test_assembly_wiring.py](tests/test_assembly_wiring.py).
  Not mounted: `dotmac-workforce` (unreleased) and connectors
  (adapter libraries, no module manifest).
- `src/erp_v2/` — ERP-unique domain only (HR suite, public
  sector/IPSAS, consolidation, leases, fleet operations, coach
  prompts/context, integration adapters, presentation/policy).
  Anything the kernel or a module owns is consumed, never
  reimplemented.

Current state: **fully composed, no product traffic**. Genesis
composes 32 migration heads (kernel + 31 modules). Mounting is not
a flip: no consumer reads a module through this app until its data
wave rehearses. Table rulings: [docs/mapping-wave1.md](docs/mapping-wave1.md)
and [docs/mapping-waves.md](docs/mapping-waves.md) (all 426 legacy
tables named with exactly one destination).

Genesis is the composed lineage: kernel head + each adopted module's
head + this assembly's `e2` lineage for ERP-unique tables only.
Legacy data arrives by transform-to-fit transfer (exceptions to
manual review), rehearsed on a production snapshot before the flip.

## Run (released wheels)

Requires Forgejo index credentials in the operator environment
(`UV_INDEX_FORGEJO_USERNAME` / `UV_INDEX_FORGEJO_PASSWORD`).

```sh
uv sync --extra test
uv run pytest
uv run python -m erp_v2.runtime --port 8012
```

## Migrate (owner identity, disposable/test database only at wave 0)

```sh
MIGRATION_DATABASE_URL=postgresql+psycopg://app_admin@HOST/DB \
    uv run python -m erp_v2.migrate upgrade heads
```

The migration entrypoint refuses a superuser identity and never falls
back to the runtime `DATABASE_URL`.
