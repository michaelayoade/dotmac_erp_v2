# Snapshot rehearsal runbook (Gate 4 operator sequence)

Implements Gate 4 of `docs/cutover.md` against the dedicated test
server. Nothing here runs without the Gate 4 approval naming the
production source and test-server target. Legacy ERP stays
untouched (read-only `pg_dump`); rollback is "do not flip".

## Inputs checklist (assemble before starting)

| Input | Source |
|---|---|
| Production read-only DSN (dump role) | ops, env-only |
| Test-server destination DSN (isolated DB) | ops, env-only |
| Snapshot manifest JSON (`snapshot_sha256`, source/destination DB names, `candidate_revision`, `tenant_extent`) | written at dump time |
| `ERP_ENROLMENT_ACTOR_ID` | enrolment actor UUID (kernel user) |
| `ERP_PAYROLL_NET_PAYABLE_ACCOUNT_REF` | tenant payroll-payable GL account ref |
| Tax rule policy JSON (legacy `tax_type` -> module code fields `fact_kind`, `recognition_basis_code`, `transaction_side`, `calculation_method`, `treatment_code`, `calculation_base_code`) | finance ruling |
| Tax band binding JSON (legacy band `name` -> legacy `tax_code`) | finance ruling |
| `ERP_SELLER_LEGAL_NAME` / `ERP_SELLER_ADDRESS_LINE` (billing-plan only) | document snapshot authority |
| HR document sensitivity/handling ruling | Michael (blocks `hr_document` import until named) |

## Sequence

All commands from the repo root. Receipts are private evidence:
write them outside Git (`.cutover-private/` or the ops evidence
store). Exit codes: 0 ok, 2 refusal (named), 3 incomplete receipt.

1. **Dump + restore** (ops): read-only `pg_dump` of production;
   restore into the isolated test-server database; disable
   outbound SMTP/PSP/integration writers there. Record
   `snapshot_sha256` of the dump file in the manifest.

2. **Genesis on the destination** (first live proof):

   ```
   MIGRATION_DATABASE_URL=<dest> uv run python -m erp_v2.migrate upgrade heads
   ```

   Compare the exact observed Alembic revision set with the candidate
   `EXPECTED_STAMPED_HEADS` in `erp_v2.migrate` (current e2 lineage
   ends at `e2_0012_academy_sync`). A matching count alone is insufficient.
   Any revision-set mismatch stops the rehearsal.

3. **Census** (no DB): `uv run python -m erp_v2.importer census --receipt <path>`
   - 426 legacy tables, one destination each.

4. **Plan against the restored snapshot**:

   ```
   LEGACY_DATABASE_URL=<restored-source> \
   uv run python -m erp_v2.importer plan --manifest <manifest.json> --receipt <path>
   ```

   Review the receipt before proceeding: `source_resolution`
   (declared `public.*` sources resolved to real legacy schemas —
   `ambiguous` entries need a ruling), blocked specs with named
   missing columns, `enrolment_maps_required` vs
   `enrolment_maps_without_builder` (must be empty).

5. **Dry-run execute with minting** (writes roll back):

   ```
   ERP_IMPORT_APPROVED=1 LEGACY_DATABASE_URL=<restored-source> \
   MIGRATION_DATABASE_URL=<dest> ERP_ENROLMENT_ACTOR_ID=<uuid> \
   ERP_PAYROLL_NET_PAYABLE_ACCOUNT_REF=<ref> \
   uv run python -m erp_v2.importer execute --mint \
     --manifest <manifest.json> --receipt <path> \
     --tax-rule-policy-json <policy.json> --tax-band-binding-json <binding.json>
   ```

   Read the receipt as the exception queue: every unresolved
   exception is a rehearsal finding, never edited away.
   `mints[].refused` names missing rulings/config. Note dry-run
   fidelity: destination-reading enrolment maps see an empty
   destination (spec writes roll back), so map-dependent rows
   over-report exceptions in dry-run - the apply run is the true
   measure for those families.

6. **Apply** (same command + `--apply`, additionally
   `ERP_IMPORT_APPLY_OK=1`). Apply commits per spec and transition;
   it is NOT one atomic destination transaction. Failure or timeout can
   leave a partially populated destination. Preserve the receipt and
   logs, discard only the explicitly bound disposable destination,
   rebuild it cleanly, and restart from the bound snapshot. Never rerun
   apply into a partially imported destination. Exit 3 means incomplete,
   not accepted: every exception class needs an explicit disposition
   before a flip; see `docs/deployment.md`.

7. **Billing (per partition, never executes here)**:
   `billing-plan` from read-only AR exports, then
   `billing-preflight` with the destination-observed
   `--expected-position-fingerprint`/`--posting-watermark` and the
   named `--migration-authority-ref`. Expect
   `verifier_unavailable` until the Sub-first M1 signed-grant
   verifier is injected — that is the honest state.

8. **Parity verification** before any flip discussion:
   GL balances by account/period, AR/AP open positions, payroll
   run totals + year-to-date liabilities, inventory
   quantities/valuation (costing replay for `value_after`),
   per-table read/written vs receipt, exception decisions
   recorded per row class. Record total duration
   (dump + restore + genesis + transform + verification) as the
   downtime estimate.

9. **Smoke**: boot the candidate against the rehearsal
   destination (`ERP_WEB_ENABLED` per packet), sign in, load
   People/Finance/Payroll/Inventory workspaces.

## Known blocked specs at rehearsal start (5)

`tax_returns` (regenerate-vs-archive ruling), expense lifecycle
`actor_reference`, expense request `needed_by`, inventory
`value_after` (costing replay), HR document
sensitivity/handling (classification ruling). Their rows stay in
legacy as the system of record until ruled — they block Gate 5,
not Gate 4 findings collection.

## Cutover DDL: ledger journal index (Gate 4 finding)

The released accounting module has no index on
`mod_accounting.posted_ledger_lines (tenant_id, journal_id)`, but its
posting trigger aggregates ledger lines by journal on every
DRAFT -> POSTED transition. At production scale (446k ledger lines)
each posting seq-scans the ledger (~27k cost units); 197k journal
transitions would take hours. Until a released module migration
adds the index (reported upstream), the cutover procedure creates
it on the destination right after genesis — additive DDL, stamped
heads unaffected:

```sql
CREATE INDEX IF NOT EXISTS ix_cutover_ledger_tenant_journal
ON mod_accounting.posted_ledger_lines (tenant_id, journal_id);
```

Measured at Gate 4: journal transitions 251s total with the index
vs still unfinished after 14 minutes without it.

The index must exist BEFORE `execute` starts: creating it
mid-run does not help, because the posting trigger's cached
plan never picks it up (Gate 5 clean re-run, 2026-11-09: ~12k
postings in 25 minutes without a usable plan; the run was
killed, rolled back cleanly, and relaunched with the index
created right after genesis).

## Gate 5 family-fix re-rehearsal (2026-11-09, commits af60bf5..f3f8ff6)

Clean genesis + execute at `f3f8ff6`: written
1,342,544/1,401,472 (95.8%), 83,965 named exceptions (Gate 4:
184,891), `complete=False` exit 3 by design. Parity is now
exact: journal entries 217,600/217,600, journal lines
477,789/477,789, posted ledger 445,950/445,950 with debit =
credit = 11,234,343,218.68 on both sides. Transitions: POSTED
201,694, REVERSED 3,946, VOID 1,263; 201,697 approval stamps
and 206 posted_by stamps receipt-counted; 601 zero-total
headers recomputed from their own lines. Tax mint landed 6/6
rules published. Tenant smoke green incl. verified
`erp.dotmac.io`; unknown host returns 404.

The previous run's 9 missing ledger lines (D 18,296,921.25)
belonged to JE0001/JE0002 — balanced payroll journals whose
legacy ledger rows reference phantom `journal_line_id`s that
never existed as entry lines, so the FK guard dropped the
ledger evidence and POST refused on "requires complete
balanced ledger evidence" (corrected 2026-11-09; an earlier
revision of this section wrongly blamed empty
`approval_reference`). Fix: the `journal_line` enrolment map
resolves a phantom only to the unique entry line in the same
journal with the same account and amounts (all 9 resolve
uniquely, verified pair-for-pair against SQL); ambiguous
phantoms stay named exceptions. JE0001/JE0002 now post:
`approval_reference = legacy:gl.journal_entry:<id>@2026-01-31`.

Approval provenance is ruled (Michael, 2026-11-09): migrated
journals need no approver — they post through the import path
with their legacy journal ID and posting date as the source.
Three POSTED rejects remain: legacy journals unbalanced by
0.000001 in the source itself; they stay named exceptions (no
amounts are adjusted).

Two operational findings from the clean run:

- A savepoint probe on the shared source connection crashes
  once executor streaming has put it in psycopg server-cursor
  mode (`DECLARE CURSOR FOR SAVEPOINT`). Existence probes on
  that connection must be error-free SELECTs (`to_regclass`),
  never savepoints.
- The cutover ledger index (previous section) must be created
  right after genesis, before `execute`.

Tax minting now follows the module publication lifecycle
(`tx_0004`): insert unpublished, attach bands, then a
publish-only `UPDATE` flips `is_published` (a deferred trigger
requires published by commit). Validated live: 6 rules
created+published. PAYE bands (NTA 2025) stay an explicit
blocker — legacy has no PAYE `tax_code` to bind bands to.
