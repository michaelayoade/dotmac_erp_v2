# ERP v2 deployment packet (Gate 5)

Status: packet prepared 2026-11-09 after Gate 4 terminal receipt
(candidate `4a38b05`). The traffic switch itself needs Michael's
explicit flip-day confirmation after the readiness list below.

Beside-legacy staging DONE 2026-11-09, restaged 2026-11-10 at
merged main (candidate `7cc60ea`, PR #1+#2 landed; restage
receipt `restage-receipt.json` beside the original
`prod-receipt.json`, outcomes identical: written 1,342,827,
exact parity). The guarded launcher now also requires the
runtime materials in `/root/erp-v2-candidate/materials.env`
(platform DSN via a narrow `platform_api` login, root domain,
32-byte secrets, trusted hosts, seeding off). Original
staging record (candidate `0721ce9`):
`erp_v2` database and `dotmac_erp_v2_app` run on the prod host,
loopback `127.0.0.1:8013` only, imported from snapshot
`legacy-20261109T104205Z.dump` (sha256 `b5e75399...`, receipt
at `/root/erp-v2-candidate/prod-receipt.json` on the host:
written 1,342,827, exact ledger parity, JE0001/JE0002 POSTED).
nginx and legacy are untouched; the staging copy goes stale
from the snapshot moment and is discarded at the flip.
Operator files live in `/root/erp-v2-candidate/` (env files
600, compose copy, dump, logs). Prod-specific mechanics proven
there: genesis needs `erp_owner` to be a member of
`app_admin`/`app_user` (migrations ALTER ... OWNER TO them);
runtime connects as dedicated login `erp_v2_runtime`
(NOBYPASSRLS, member of `app_user`) so the shared `app_user`
password is never reused; database traffic goes over the
`dotmac_default` compose network by container name because
host-gateway to the published PG port is firewall-blocked; the
snapshot copy has RLS disabled so the least-privilege reader
role can read it (legacy RLS untouched).

## Current production topology (surveyed read-only 2026-11-09)

- Host `dotmac-erp` (149.102.158.167), nginx `erp.dotmac.io` ->
  upstream `dotmac_erp` -> `127.0.0.1:8003` -> container
  `dotmac_erp_app` (`ghcr.io/michaelayoade/dotmac_erp`, port 8002).
- Writers: `dotmac_erp_app` (web), `dotmac_erp_worker` (Celery),
  `dotmac_erp_beat` (Celery beat). Compose project at `/root/dotmac`
  (`docker-compose.yml` + override).
- Database: `dotmac_pg_local` (postgis 16) published on `9001`;
  legacy DB `dotmac_erp` (9.2GB, single DOTMAC tenant
  `00000000-0000-0000-0000-000000000001`).
- Neighbours that must not be touched: `dotmac_traccar(+_db)`,
  `dotmac_erp_redis`, `dotmac_erp_vmagent`, `promtail`, host
  postgres/mariadb/redis on loopback.

## Candidate shape

- One web service: `uvicorn erp_v2.main:app` (see repo
  `Dockerfile`). There is no Celery worker in erp_v2 today; module
  async work runs through kernel mechanisms in-process. Legacy
  Celery jobs (payroll runs, Sub/Academy sync, banking sync) have
  no erp_v2 equivalent yet — they stop at the flip and are named
  in the surface-gap list.
- Database: new database `erp_v2` in `dotmac_pg_local` (legacy
  `dotmac_erp` untouched as rollback). Roles mirror rehearsal:
  migration owner `erp_owner` (CREATEROLE+BYPASSRLS,
  non-superuser, member of `app_admin`/`app_user`) for
  genesis/import only; runtime `erp_v2_runtime` (dedicated
  login, NOBYPASSRLS, member of `app_user`; posture enforced at
  startup by `erp_v2.database`). `MIGRATION_DATABASE_URL` never
  enters the app container env.
- Tenant host: `erp.dotmac.io` resolves via a verified
  `tenant_domains` row (custom domain), proven at Gate 4 + packet
  prep. Root-domain default (`localhost`) is not relied on.
- Cutover DDL right after genesis (see `docs/rehearsal.md`):
  `ix_cutover_ledger_tenant_journal` index.

## Env inventory (names only; values via host env file, never Git)

- `DATABASE_URL` (app_user), `APP_PORT` (container 8000 default;
  compose publishes loopback only), `ERP_WEB_ENABLED=true`.
- Kernel/platform: `PLATFORM_ROOT_DOMAIN`, session/secret keys
  per kernel config, `ENVIRONMENT=production`.
- Import-time only (flip shell, never app env):
  `MIGRATION_DATABASE_URL`, `LEGACY_DATABASE_URL`,
  `ERP_IMPORT_APPROVED`, `ERP_IMPORT_APPLY_OK`,
  `ERP_ENROLMENT_ACTOR_ID`, `ERP_PAYROLL_NET_PAYABLE_ACCOUNT_REF`.
- Integration secrets (SMTP/PSP/Sub/Academy) stay unset at flip
  until their v2 surfaces are wired; outbound writers off.
- Academy completion receiver (`POST /integration/academy-sync/completions`)
  authenticates with a kernel machine credential (`X-Api-Key`), exact scope
  `academy_sync.completion:write`, `source_application`
  `dotmac_academy_app`. Names only: `ACADEMY_SYNC_TENANT_ID` (non-secret
  tenant UUID), `ACADEMY_SYNC_SOURCE_SCOPE_REF` (default `dotmac`),
  `MACHINE_CREDENTIAL_HMAC_KEY` (the kernel's held verification secret,
  >= 32 bytes, distinct from the session secrets; optional, and unset keeps
  the route closed with `not_configured`). The former shared-secret
  `ACADEMY_SYNC_WEBHOOK_SECRET` and the HMAC `X-Webhook-Signature-256` path
  are removed. Cut-over: issue the credential (kernel `issue_credential`),
  switch Academy's sender to `X-Api-Key`, then delete the old secret from the
  host env file. The receiver was unconfigured at flip, so no deployed
  caller depends on the old path.

## Flip-day sequence (ready packet + explicit flip-day go required)

22m56s measures importer execute only. Measure the full window including
fencing/draining, dump, restore, clean genesis, index, import, reconciliation,
identity/sign-in, switch and rollback allowance. Set a latest abort time.

1. Bind the image digest, candidate revision, source/scratch/destination
   database identities and tenant extent. Check the actual dump checksum
   and observed DB identities against the manifest; the manifest alone
   does not independently establish them. Verify exact stamped revisions
   against the candidate `EXPECTED_STAMPED_HEADS`, not just a head count.
2. Put ingress in maintenance, fence inbound integrations and suppress
   writer restarts. Drain in-flight web/Celery work and define queued/retry
   disposition. Stop legacy beat, worker and app; prove no remaining
   writer sessions or external writers before the final dump. Preserve
   Redis and neighbour services. Maintenance/write fencing must be tested.
3. Stop the staging candidate and preserve its evidence. Explicitly
   identify and dispose ONLY the stale staging destination and scratch
   source, never legacy `dotmac_erp`. Recreate the isolated destination
   empty; migration upgrade does not clear an already imported database.
4. Take the final read-only dump after fencing, checksum it, and create
   the manifest. Restore into the isolated scratch source and verify
   restore completion and observed source identity.
5. Genesis the clean destination from the exact image. Verify stamped
   revisions, create `ix_cutover_ledger_tenant_journal` BEFORE execute,
   and install the verified tenant-domain mapping.
6. Execute the bound import with minting and reviewed inputs. Unexpected
   exits, timeouts, or new unreviewed findings abort. Exit 3 is incomplete,
   not accepted: require a documented disposition and reconciled impact
   for every exception class, blocked/skipped spec, missing/ambiguous
   source and refused mint. Apply commits per spec/transition; failure
   can leave partial data. Preserve evidence and rebuild cleanly before
   retrying; never blindly rerun into a partially populated target.
7. Reconcile GL by account/period, debit/credit, AR/AP open positions,
   payroll run/YTD liabilities, inventory quantities/value, identities,
   grants and receipt counts. Aggregate ledger equality alone does not
   establish completeness. Unexplained differences abort.
8. Verify runtime settings without printing values: guarded
   `python -m erp_v2.runtime`, `ENVIRONMENT=production`, required
   JWT/session/CSRF materials, explicit trusted hosts/root domain,
   disabled seeding, least-privilege app/platform DSNs and no migration
   or source credentials in the app process. Compose `--env-file` alone
   does not forward unlisted variables. First prove on the test server.
9. Prove identity/credential/grant enrolment and real existing-staff login
   against the rebuilt destination. Party import and login redirects
   are insufficient. Exercise authorized workspaces, unauthorized denial,
   unknown-host rejection and readiness in database mode.
10. While public access and business writes remain fenced, test nginx
    config, switch upstream, reload and perform operator-only HTTPS smoke.
11. Record the go/no-go and only then explicitly open public access and
    business writes. Preserve the final receipt, manifest, digest and times.

### Rollback boundary

Before any v2 business write: fence/stop v2, restore and test the legacy
nginx upstream, prove legacy DB unchanged, restart legacy app/worker/beat
under the recorded queue policy, smoke and reopen ingress.

After any v2 business write (including leave/integration writes): fence v2
and preserve its DB, receipt and audit facts. Never drop it or simply
restart legacy, which would lose accepted facts. Reconcile/replay writes
through supported contracts or forward-fix before reopening writers.
Rehearse this post-write recovery procedure before enabling writes.

## Surface gaps accepted at flip (read before confirming)

erp_v2 serves read workspaces (People, Finance, Payroll,
Inventory) plus leave write flows and the Academy completion
receiver. It does NOT yet serve: legacy operational writes for
invoicing/AR collections, AP payments, procurement execution,
payroll run execution, banking sync (Mono), Sub staff sync,
ERPNext sync, FCM — their data is imported and visible, their
write workflows land in later waves. Flipping means staff work
in v2 for reads/leave while those writes pause or return via
rollback. This is the deliberate replace-not-adapt consequence;
it must be a conscious operations decision, not a surprise.

## Gate 5 readiness still owed

- Rulings: `approval_reference` provenance decided 2026-11-09
  (migrated journals post with legacy journal ID + posting
  date as the source; stamps stay receipt-counted);
  14,265 APPROVED/SUBMITTED journals held in DRAFT; 1,208
  supplier invoices without supplier document numbers; 7 fintech
  bank institutions (Paystack/Flutterwave/TAJ) enrolment;
  attendance `MANUAL` source mapping; 5 blocked specs
  (tax_returns regenerate-vs-archive, expense
  actor_reference/needed_by, inventory `value_after` costing
  replay, HR document sensitivity/handling).
- Inputs: real enrolment actor UUID, payroll-payable account
  ref, tax rule policy + band binding JSONs.
- Gate 1 reproducibility: DONE 2026-11-09. `FORGEJO_READ_TOKEN`
  set, `uv.lock` committed (80 packages, exact Forgejo wheel
  hashes), CI green with `uv sync --locked` (119 tests).
  Candidate image builds from the same lock
  (`ghcr.io/michaelayoade/dotmac_erp_v2:sha-0721ce9`,
  digest `sha256:dc09acf0...`); the GHCR package is public
  (anonymous pull verified on test-server and prod).
- Billing command import stays Sub-M1-gated (no execution).

## Cleanup after a successful flip

- Remove `/root/dotmac_erp_g4.dump` (Gate 4 rehearsal dump) from
  the production host.
- Archive the flip receipt + manifest outside Git with the
  Gate 4 rehearsal receipt.

## Readiness reconciliation — 2026-11-09

**NO-GO for traffic switch.** Ordinary migration mechanics do not require
new accounting approval. Missing evidence and new accounting treatments do.
Michael requested parallel development of invoicing/AR, AP payments,
payroll execution and banking sync. Acceptance of a pause is not established.

- Latest reviewed CI: `011a1dde4a63b87cbbcdee6f3f556855893a771f`, run
  42608622488, released-wheel sync/lint/tests passed. Image run 42600697261
  passed at `0721ce9b47855c5cc2cf77b481e7daaeed17d07b`.
- Baseline source/lock/Python version/Dockerfile were identical between
  those revisions (only compose/docs differed). Recorded image digest:
  `sha256:dc09acf0b12fbf7bc29908b9a6cb047e23cb33e9fbc7679f5105ea40516432c9`.
  A fresh anonymous `sha-0721ce9` registry verification returned HTTP 404;
  GitHub package metadata returned 403, missing `read:packages`. Earlier
  pull evidence is historical; re-establish pullability/digest. No access
  expansion was made.
- This change fixes the image launcher and production env forwarding.
  A NEW image/build and dedicated-test-server boot proof are required;
  existing staging is not claimed to use these fixes.
- Saved staging evidence: 10:42Z snapshot; 1,342,827 written rows;
  84,034 exceptions; 446,020 ledger rows and exact debit/credit equality
  of 11,235,151,209.31. This session has not reobserved the host.
  Aggregate parity does not accept the exceptions.
- Attendance MANUAL mapping already preserves its method label as residue
  with unknown actor NULL; fintech enrolment code exists. Reconcile fresh
  receipts rather than treating these as unimplemented code.
- Open technical gates: guarded image boot/effective production settings,
  credential/grant migration + real staff login, verified writer fencing,
  exact exception dispositions and post-write recovery rehearsal.
- Required inputs: real enrolment actor UUID; evidenced confirmation of
  payroll payable account 2040; tax policy/band JSONs; a proper PAYE
  code/rule for NTA-2025 bands. Do not substitute arbitrary defaults.

## Approved migration treatments — Michael, 2026-11-09

These are control recommendations, not IFRS-mandated software statuses.
Confirm the entity's reporting framework before applying IFRS policies.
Preserve original IDs/statuses, mapping versions and exception evidence.

| Issue | Recommended treatment | Acceptance evidence |
|---|---|---|
| APPROVED/SUBMITTED but unposted journals | Preserve original status/provenance; keep target DRAFT outside posted ledger. Approval is not evidence of posting. | By-account/period reconciliation and owned unposted queue; no duplicates or omissions. |
| 1,208 supplier invoices without document numbers | Preserve liabilities/evidence in a controlled migration register; recover originals or use a separately labelled migration reference ONLY if supported by the released contract. Never fabricate supplier-issued numbers. | AP control/open-position reconciliation and supported import route; no silent exclusion. |
| Three journals unbalanced by 0.000001 | Preserve amounts; quarantine rejects and quantify aggregate impact. Assess materiality; any correction must be separately traceable and balanced. | Explicit rounding/error policy and reconciled effect. No automatic suspense plug or silent rounding. |
| Genuine prior-period errors | Correct separately from migration using the applicable error policy; material prior-period errors require retrospective correction where practicable under IAS 8. | Period/materiality analysis and correction audit trail; no balancing through current profit merely to complete migration. |
| Inventory value_after absent | Replay the existing evidenced costing policy and reconcile quantities/value; assess net realisable value separately. | Reproducible costing replay and reconciled control accounts; no new cost method chosen for importer convenience. |
| Duplicate departments / missing asset condition | Merge only on evidenced identity equivalence; retain mappings. Preserve unknown condition for review. | Reconciled employee/asset links; no arbitrary duplicate winner or guessed condition. |
| Tax returns / PAYE mapping gaps | Preserve filed history; regenerate only with validated period-specific rules. Never attach PAYE bands to an unrelated tax code. | Actual configuration and period reconciliation; filed evidence retained. |
| Missing expense actor/date / HR handling | Preserve unknown facts; do not invent actors/dates. Restrict HR evidence until classification/handling is implemented. | Supported target representation or preserved exception route; no silent loss. |

IAS 8 requires retrospective correction of material prior-period errors
unless impracticable. IAS 2 measures inventory at the lower of cost and
net realisable value and prescribes appropriate cost formulas. Neither
standard mandates fabrication of source evidence to satisfy a database.
Queue/reference treatments above are implementation recommendations.

Sources checked 2026-11-09:
- [IAS 8](https://www.ifrs.org/issued-standards/list-of-standards/ias-8-basis-of-preparation-of-financial-statements/)
- [IAS 2](https://www.ifrs.org/issued-standards/list-of-standards/ias-2-inventories/)

## Parallel business workflow implementation

Michael approved the migration policy in this packet on 2026-11-09;
this does not authorize adjusting balances or switching traffic.

- Supplier invoice draft capture/submission and AP evidence are the first
  payables slice. Approval, posting and outbound supplier execution are
  still outstanding: payments a3 does not provide an outbound executor.
- Receivables views expose owner documents and confirmed receipt evidence,
  not inferred outstanding/available balances. Native invoice issuance
  still needs rated tax, commercial and verified due-date evidence;
  billing command-import remains Sub-M1-gated.
- Payroll first slice creates draft runs and calculates individual employees
  using released a2 typed inputs and existing assignments. Batch salary
  generation, approval/finalization, GL handoff and payout remain separate.
- Banking adds a trusted-binding/dedupe service and read workspace. The HTTP
  observation adapter remains unmounted until destination tenant resolution,
  scoped service auth and descriptor trust are established. Account mappings
  and imported provider-transaction overlap must be reconciled before live
  polling. Provider HTTP/secrets remain Integrator-owned.
- Financial writes require admin-only dedicated permissions, not the broad
  staff portal permission. Reprovision the versioned role-grant profile on
  an isolated test destination and verify denial/CSRF before host adoption.
- New lineage head is `e2_0013_banking_observations`; the old staging receipt
  at `e2_0012_academy_sync` is not proof of this schema. Fresh exact-wheel
  CI, image build and test-server genesis/runtime proof remain mandatory.

Compose adds `127.0.0.1` to the explicitly declared trusted hosts so the
container liveness probe passes host filtering. The e2 banking revision
depends on `bk_0001_banking`; its stamped head is absorbed by that lineage.

Mounted browser tests exposed and fixed two existing prerequisites: the
locked runtime now includes python-multipart for form/CSRF parsing, and
ERP declares browser display/branding SettingSpecs through the public
kernel registry without seeding runtime settings rows.

The first exact-image clean PostgreSQL proof caught an invalid composite
credential FK: the released kernel exposes only the credential's global
primary key. The draft migration now references that supported key; trusted
principal tenant/credential matching remains mandatory in the banking
service, and bank-account/statement relationships retain composite tenant
FKs. Explicit app_user SELECT plus UPDATE(updated_at) (binding lock), and SELECT/INSERT
(receipt append) grants are part of genesis; runtime cannot create bindings.
