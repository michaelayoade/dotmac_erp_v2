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

## Dest3 rehearsal — payables 0.1.0a2 candidate (2026-11-11)

Candidate `7e3da63` (image `sha-7e3da63`, digest
`sha256:5489105d...`) pins Starter `dotmac-payables 0.1.0a2`
(Starter PR #1032, tag `dotmac-payables-v0.1.0a2`). Michael
ruled that a supplier-issued invoice number that was never
captured stays empty: `pa_0002_optional_supplier_doc` makes
`supplier_document_number` nullable for historical/imported
rows, while new documents created through the Payables
contracts still require it. The frozen importer needed no
code change — it already maps the source NULL straight
through.

Fresh genesis into `erp_v2_dest3` from the exact image
stamped 30/30 `EXPECTED_STAMPED_HEADS` including
`pa_0002_optional_supplier_doc`, both columns nullable, with
the cutover ledger index created before `execute`. Full
execute (same approvals/materials as dest2) completed with
the by-design exit 3, `complete=False`
(`dest3-receipt.json` on the test server).

Supplier-document result, accounted row-for-row
(`ap_reconcile.py` + cohort script):

- Source supplier invoices with NULL `supplier_invoice_number`:
  1,208. Landed in `mod_payables.supplier_invoices` with
  `supplier_document_number` NULL: **1,151**. Landed with an
  invented document number: **0**.
- The remaining 57 are all named header exceptions in a
  pre-existing finance family: 56 × `ck_supplier_invoices_total`,
  1 × `ck_supplier_invoices_amounts` (50 PAID, 4 APPROVED,
  2 DRAFT, 1 SUBMITTED). Nothing was adjusted to force them in.
- `supplier_document_number` exceptions in the receipt: **0**
  (dest2: 1,208 NotNullViolation).
- Captured-document cohort is byte-identical to dest2
  (732 landed, 264 missing: 199 supplier-document duplicates,
  totals checks and REJECTED statuses), so the delta is
  isolated to the ruling. Landed-row mismatches across
  document presence/value, totals, currency and status
  mapping: 0.

Everything else matches dest2 except the deliberate F1
Sub-origin exclusion, which first takes effect in this
candidate: read 1,382,524 (dest2 1,401,472), written 1,336,803
(dest2 1,342,544), customer accounts 456 (dest2 5,380;
4,924 Sub-origin excluded with hashed side-file keys),
journal entries 217,600 and posted ledger 445,950 unchanged,
account-level GL reconciliation exact (108 accounts,
0 mismatches). AP lines rose 1,867 → 4,823 with the newly
landed invoices.

Runtime proof on dest3: the candidate image served with
dedicated `erp_v2_runtime`/`erp_v2_platform` logins
(NOBYPASSRLS) reported `payables@0.1.0a2` in its startup
order, `/health/ready` returned database mode, the tenant
host redirected to login and an unknown host returned 400.
Guarded startup also refused sub-32-byte secrets before
serving. The rehearsal app container was stopped; `erp_v2_dest`,
`erp_v2_dest2` and `erp_v2_dest3` all remain for comparison.

## Verified historical unpaid AP admission

The separate historical admission operation requires the released Payables
version containing `admit_historical_unpaid_invoice` and its forward
`pa_0003_historical_invoice` migration. Payables `0.1.0a3` was published by
the PR #1044 integration train: tag `dotmac-payables-v0.1.0a3`, source commit
`ec7af09897c8123067344b57216ff643fafb09d1`, release run `43237641857`.
This assembly adopts that exact version. Retain the publication and subsequent
ERP image qualification receipts; source adoption alone does not qualify data.

This operation covers only STANDARD invoices already posted in the source,
wholly unpaid, with a genuine due date, no payment allocations, withholding,
prepayment or unsupported adjustments, and exact source/destination invoice
and posted-ledger evidence. It preserves missing supplier document numbers and
approval facts. It creates no modern approval, cash settlement or GL posting.
Other historical positions remain in the exception register.

Use a held, read-only restored source and a clean disposable destination on the
dedicated server. Retain the independent restore/role-closure receipt: hashing
a dump and naming a database does not prove that database came from the dump.
The manifest's candidate revision must match the imported rows' recorded
`source_version`. For an upgraded prior rehearsal, retain its original import
manifest and separately record the newer operator image/revision. Do not relabel
old imported data as a fresh run of a newer image.

Create a private cohort JSON with exactly `version` (integer 1), `tenant_id`,
`snapshot_sha256`, `candidate_revision`, and `invoice_ids`. The cohort must name
one manifest tenant and 1–1,000 unique canonical invoice UUIDs. Keep the cohort,
full plan, commands, row-level refusals and receipts outside Git; use private
directories. Each receipt path must be new and have an existing real parent.

Plan with operator materials supplied only to that command:

```sh
python -m erp_v2.importer.payables_history_cli \
  --manifest /private/manifest.json \
  --snapshot-file /private/source.dump \
  --cohort /private/cohort.json \
  --receipt /private/ap-plan.json
```

The command consumes `LEGACY_DATABASE_URL` and `MIGRATION_DATABASE_URL`, removes
them before loading owner code, and rejects serving database materials. Both
connections are bound to the manifest identities. The source transaction and
the plan destination transaction are read-only; source reads refuse RLS
filtering and bind the evidenced AP/GL namespaces explicitly. Execution uses SERIALIZABLE
isolation. Admission uses a separate nonprivileged destination login, with no
superuser, BYPASSRLS, role-management or ownership authority, including through
role membership. Schema upgrades use their own offline migration credentials.
The operator checks the exact released owner version and stamped schema heads,
then binds and verifies the cohort tenant transaction-locally before evidence
reads. Console output contains counts only. Full evidence stays in the private
receipt.

Review every refusal and compare the exact planned commands with the bound
source evidence. Every member of an apply cohort must qualify. If a narrower
cohort is justified, retain the original refused plan, record the exclusions,
and generate a new explicit cohort and plan. Never edit the old plan into an
acceptance result.

Apply the exact reviewed bytes using a new receipt path:

```sh
ERP_IMPORT_APPROVED=1 ERP_IMPORT_APPLY_OK=1 \
python -m erp_v2.importer.payables_history_cli \
  --manifest /private/manifest.json \
  --snapshot-file /private/source.dump \
  --cohort /private/cohort.json \
  --reviewed-plan /private/ap-plan.json \
  --reviewed-plan-sha256 <sha256-of-the-reviewed-plan-bytes> \
  --apply --receipt /private/ap-apply.json
```

The fixed ERP verifier rechecks actual source/destination facts; the plan hash
does not replace verification or migration authorization. Admission takes the
invoice lock and creates the obligation, historical recognition event and
permanent receipt in one owner transaction. A later cohort failure rolls the
whole transaction back. Preserve the destination and inspect its durable
receipts before retrying after any uncertain process/output failure.

Repeat the same command and reviewed plan with another new receipt path to
prove exact replay. Existing receipts must return without recreating objects or
resetting an obligation subsequently settled through legitimate operations.
Do not fabricate payments to exercise replay against historical business data;
the hosted synthetic canaries cover settlement-after-admission behavior.

Reconcile admitted invoice identities, full-face amounts, due dates, outstanding
amounts, original recognition timestamps and permanent receipts. Verify that
invoice fields and complete GL row fingerprints are unchanged. Back up and
restore the admitted result, comparing its AP evidence and ledger fingerprints.
Once historical receipts exist, schema downgrade is refused; use a forward
correction. A successful bounded cohort does not establish full AP parity or
authorize a production switch.

## Dest4 rehearsal — freeze candidate e82c89b, fresh snapshot (2026-11-13)

Freeze candidate is ERP `main` `e82c89b` (Payables a3, Billing a11),
image `sha-e82c89b` digest `sha256:2b9f806e...`. A fresh production
dump taken the same morning (`legacy-fresh-20261113T074551Z.dump`,
sha256 `9fc8fb1e...`) was restored on the test server as
`erp_legacy_fresh` and rehearsed into `erp_v2_dest4`:

- Genesis 33s, stamped 30/30 heads (`pa_0003_historical_invoice`,
  `bi_0014_opening_variance_ledger` included). Execute 14m49s,
  exit 3 by design, `complete=False`.
- Account-level GL reconciliation exact (108 accounts, 0 mismatches)
  against the fresh source. Entries 217,892; ledger 446,679.
- AP: 1,896 invoices landed. Uncaptured cohort: 1,155 of 1,212 landed
  with `supplier_document_number` NULL, 0 invented, 57 accounted
  totals-check exceptions — the same 57 as dest3. Supplier-document
  exceptions: 0.
- Identity (everyone-access ruling): fresh cohort 143 → 106 matched →
  98 eligible → 94 ready. Ready plan zero blockers; apply on dest4
  inserted 94 credentials. The dest4 app serves on the test server
  (host port 18015) for the designated real-login proof.
- Prod loopback staging was restaged to the same image from the same
  fresh snapshot: total restage 14m36s, counts identical to dest4
  (entries 217,892; ledger 446,679; AP 1,896 / 1,155 null), readiness
  database mode, tenant host 302, unknown host 400, legacy untouched.
  Staging credentials remain 0 — identity apply is a flip-window step.
- Timings for the flip window proposal: dump ~3m, restore ~4m,
  genesis <1m, execute ~15m, boot + smoke ~2m. Reconciliation and
  identity apply measured in minutes. A 2-hour window with abort if
  reconciliation is not green by T+75m is the working proposal.


## Historical AP qualification checkpoint — 2026-11-13

The retained source snapshot `327c22df...` was separately restored and qualified
with operator `e82c89b`; this is distinct from the newer Dest4 snapshot above.
The source audit verified 2,204 total invoice headers and 1,913 recognized
STANDARD invoices. Exact application/package bytes, schema heads, all ten AP
owner-table fingerprints and the 445,950-row ledger fingerprint passed. Source
RLS visibility was qualified through a restricted, read-only offline audit role;
destination admission retained its nonprivileged tenant-bound role.

The private historical AP plan requested 23 preliminary invoices and qualified
zero: 22 refused unsupported procurement evidence and one refused source amount
scale. No admission ran. Read-only diagnosis found NULL procurement references
and 64-character receipt fingerprints on those 22 imported headers, with no
source PO/receipt-line links. The source invoice table has no corresponding
receipt-fingerprint column; the generic mapper selected `sha256_row` for that
missing field. A row hash cannot supply the missing business evidence binding.

Row-hash derivations now require reviewed mapping declarations. Unsourced
business proof remains omitted when nullable or an explicit missing requirement
when mandatory. Existing source hash values, including NULL, are preserved.
The retained rehearsal and refused plan remain unchanged. The amount exception
keeps its original precision for separate review. The next historical AP attempt
requires a fresh isolated import using the corrected mapping, a newly reviewed
eligible plan, unchanged financial evidence, exact replay and recovery parity.
Full AP open-position reconciliation remains a cutover gate.

## Dest5 re-freeze rehearsal — 2026-11-13 (candidate `241b1ad`)

PR #22 invalidated the `e82c89b` freeze for AP history: the frozen
importer generated `receipt_evidence_fingerprint` values with no
source column, which the historical verifier refuses. Dest5 reran
the full import from the identical fresh snapshot
(`legacy-fresh-20261113T074551Z.dump`) with the corrected importer
(image `sha-241b1ad`, digest `sha256:2e3fabfe...`), so PR #21/#22
are the only variables against dest4.

- Genesis clean: 30/30 stamped heads match the image, cutover
  index created before execute, 344 tables.
- Execute: exit 3 by design (`complete=False`); read 1,384,806,
  written 1,339,490 — written identical to dest4. Exceptions
  70,594 (dest4: 71,253); the 659 delta is the procurement chain
  (128 purchase orders + 7 lines + 524 receipt observations) now
  named `specs_skipped_parent_not_imported` because
  `mod_procurement.bid_evaluations` is blocked on missing source
  `content_sha256`, instead of 659 per-row failures. Blocked
  specs 5 → 7 (adds bid_evaluations `content_sha256` and
  expense_policy_evaluations `fingerprint`); every unlanded row
  remains named.
- AP: 1,896 landed (741 captured document + 1,155 NULL), zero
  field mismatches, reconciliation byte-identical to dest4.
  Generated receipt fingerprints: 1,896 (dest4) → **0** (dest5).
- GL: exact — 108 accounts, 0 mismatches (ledger 446,679 lines).
- Identity re-derived on dest5 only: cohort 143 → 106 matched →
  98 source-eligible (7 password-change + 1 MFA held) → 94 ready
  (2 party + 2 employee destination mismatches held); plan 0
  blockers; 94 credentials applied on the rehearsal database.
- Runtime: dest5 app serves login 200 on the tenant host,
  unknown host 400; sessions 0 until a real staff login.
- Tax mint blocker unchanged: NTA 2025 bands unminted — legacy
  has no PAYE tax code (SD/VAT/WHT only); Track B finance item.
