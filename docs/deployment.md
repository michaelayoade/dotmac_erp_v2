# ERP v2 deployment packet (Gate 5)

Status: packet prepared 2026-11-09 after Gate 4 terminal receipt
(candidate `4a38b05`). The traffic switch itself needs Michael's
explicit flip-day confirmation after the readiness list below.

Beside-legacy staging DONE 2026-11-09 (candidate `0721ce9`):
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
  per kernel config, `APP_ENV=production`.
- Import-time only (flip shell, never app env):
  `MIGRATION_DATABASE_URL`, `LEGACY_DATABASE_URL`,
  `ERP_IMPORT_APPROVED`, `ERP_IMPORT_APPLY_OK`,
  `ERP_ENROLMENT_ACTOR_ID`, `ERP_PAYROLL_NET_PAYABLE_ACCOUNT_REF`.
- Integration secrets (SMTP/PSP/Sub/Academy) stay unset at flip
  until their v2 surfaces are wired; outbound writers off.

## Flip-day sequence (downtime; Gate 4 measured 22m56s execute)

1. Freeze: stop `dotmac_erp_beat`, `dotmac_erp_worker`, then
   `dotmac_erp_app` (writer fencing). Record stop time.
2. Final read-only `pg_dump -Fc dotmac_erp`; write the snapshot
   manifest (sha256, source/destination DBs, candidate revision,
   tenant extent = the single DOTMAC tenant).
3. Genesis `erp_v2` DB from the candidate image
   (`erp_v2.migrate upgrade heads`, 31 stamped heads), create the
   cutover ledger index, insert the verified `tenant_domains`
   row for `erp.dotmac.io`.
4. Restore the dump to a scratch source DB on the same PG;
   importer `execute --mint --apply --minor-units 2` with the
   flip manifest. Expect exit 3 (incomplete = named exception
   queue); exit anything else aborts the flip.
5. Parity: entries/lines/ledger counts, ledger debit=credit,
   posted-debit delta == rejected-at-POSTED sum, employees and
   invoices counts vs receipt. Any unreconciled delta aborts.
6. Smoke (loopback, Host: erp.dotmac.io): `/health`,
   `/health/ready` 200 database mode; `/` and `/people` 302 to
   `/login`; unknown host 404. Then a real staff sign-in.
7. Switch: point nginx upstream `dotmac_erp` at the candidate
   container port; `nginx -t && systemctl reload nginx`.
8. Post-flip smoke on `https://erp.dotmac.io` (health, login,
   People/Finance workspaces). Start no legacy writers.
9. Rollback (any failure before step 7 completes): restore the
   nginx upstream, start legacy app/worker/beat. Legacy DB was
   never written by the flip.

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
