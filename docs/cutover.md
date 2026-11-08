# ERP v2 cutover gates

Pattern: Academy v2 (`dotmac_academy_v2/docs/cutover.md`), adapted.
No gate is passed by a successful run of a neighbouring gate.

## Gate 1: reproducible candidate

- Private GitHub repository `michaelayoade/dotmac_erp_v2` (to create;
  needs Michael's go before first push).
- uv, Python 3.12, committed lock, exact Starter pins (never caret).
- Repository Actions secret for the Forgejo index, sourced from
  OpenBao; never put its value in source or logs.
- CI: ruff, maintained suite green at the exact candidate revision.
- Every legacy behaviour in the ported wave has a maintained test
  here; copied tests of retired internals do not establish parity.
  Payroll parity includes run-total equality against legacy oracles.

## Gate 2: module waves (per classification wave)

1. Pin the module's released wheel; read its changelog and
   EXTRACTION.toml boundary.
2. Add its plane selection + lineage head (`EXPECTED_HEADS`,
   `migration_bindings.py`) in the same change as the pin.
3. Port only the ERP-unique residue of that surface; delete nothing
   in legacy ERP (it is rollback until the flip).
4. Write the per-table transform mapping for that surface
   (legacy table → module/`e2` storage), including identity
   equivalence and exceptions-to-manual-review rules.
5. EXTRACT surfaces (inventory, projects, billing gaps) do not port:
   their Starter qualification is the wave's work.

## Gate 3: importer (before any production-data rehearsal)

One operator pipeline that composes and verifies, in order:

1. Bind candidate revision, snapshot digest, exact source and
   destination database identities, explicit tenant extent.
2. Identity first: tenants, Party/PartyPerson, credentials, roles —
   UUID-preserving, every mapping reconciled.
3. Module transfers per wave mapping, inside one destination root
   transaction per tenant extent where the module contract requires.
   Refuse the whole import while any exception is unresolved; never
   silently discard rows or substitute semantic defaults.
4. ERP-unique (`e2`) tables in dependency order; Sub/Academy sync
   ACK/watermark facts carried as separate product facts.
5. Settings through validated kernel `set_many`; credentials
   env-only (SMTP, PSP keys, integration secrets).
6. Private receipt: source/destination identities, dump checksum,
   candidate SHA and pins, tenant extent, per-table expected vs
   observed counts, identity equivalence, exception decisions,
   verification and smoke outcomes. No row data, credentials, or
   snapshots in CI artifacts or Git.

Financial invariants at import: GL balances by account/period,
AR/AP open positions, payroll year-to-date liabilities, inventory
quantities/valuation — each reconciled to legacy control totals.

## Gate 4: production snapshot rehearsal (separate approval)

Approval names the production source and the dedicated test-server
target. Read-only `pg_dump` only; no traffic switch, production
writes, or writer stoppage. Restore into a uniquely named isolated
database on the dedicated test server (never a local Docker daemon).
Disable outbound SMTP/PSP/integration writers on the restored
environment. Record duration; the downtime estimate includes dump,
restore, transform, verification, and smoke.

## Gate 5: deployment packet + downtime flip (separate approval)

Record target host, release identity, runtime roles and
least-privilege grants (non-superuser, NOBYPASSRLS — enforced by
`erp_v2.runtime` at startup), worker/scheduler entrypoints, env
secrets, writer inventory and fencing proof (web, Celery, sync,
inbound integrations), health checks, traffic switch, and rollback
(legacy ERP untouched until the flip completes). Billing mutations
stay frozen per the 2026-10-12 rule: no new repair/backfill commands
are written under this cutover; data faults go to manual review.
