# Billing opening-import design (wave 4)

Code: `src/erp_v2/importer/billing_opening.py`. Governed by the
billing freeze (no repair/backfill commands) and the register's
rulings: billing M1 gap plan for `dotmac-billing` runs Sub-first,
and ERP AR consumes the result rather than driving it.

## Shape

One translation per partition (billing account + currency):

- Legacy `invoice` rows become `OpeningReceivableItemV1`:
  original-document snapshot (seller/customer, subtotal/tax/grand as
  kernel Money), exact historical discharges from
  `invoice_payment_allocation`, and a residual in minor units.
- Customer payments with unallocated residual become
  `OpeningCashLotV1` with their allocation consumptions in order.
- Everything inexact or unprovable becomes a named blocker:
  inexact minor-unit money, missing payment for an allocation,
  over-allocation beyond the invoice total, currency mismatch,
  missing customer document snapshot.
- Collection evidence is honestly `unknown_unverified`: legacy ERP
  cannot prove verified service periods or due-date provenance, so
  `collectible_receivable_minor` stays 0 in manifest controls until
  product evidence exists. Automated collection therefore cannot
  start on migrated balances — by design, not by accident.
- **Sub-origin exclusion (F1).** ERP v2 is never the receivables
  authority for a Sub-originated record: Sub's own Billing owns those
  open items. Invoices (credit notes included) with
  `dotmac_sub_id IS NOT NULL` (blank string counts) get an `excluded`
  disposition with reason `sub_origin`, as do Sub-tagged payments whose
  allocations (if any) are all Sub-origin. An ERP-recorded payment that
  touches a Sub-origin invoice is a blocker for the exceptions review
  (per-allocation evidence in minor units); it is never split or
  re-attributed. An allocation to an invoice that is absent, void or
  blocked blocks its payment. Splynx-era rows (`splynx_id` set,
  `dotmac_sub_id` NULL) are blocked as `splynx_era_pending_ruling`
  (`SPLYNX_ERA_POLICY`, pending the M13 ruling). Required export
  fields, checked on every row (Sub-origin rows included) before
  classification: invoices carry `dotmac_sub_id`, `splynx_id` and
  `customer_dotmac_sub_id`; payments carry `dotmac_sub_id` and
  `splynx_id`. A row missing any is refused, so older exports must be
  re-exported. `customer_dotmac_sub_id` feeds a census only. Optional:
  `invoice_type` (CREDIT_NOTE). Payments are reconciled in the receipt
  census only; the manifest population lists invoices only. ERP cash
  in a partition with no ERP-native invoice is a named blocker. Receipts carry
  `sub_origin_exclusions` and a `census` that must balance per family
  (the run is refused otherwise). A wholly Sub-origin partition makes
  `billing-preflight` report `nothing_to_import` and exit 0.
- Void/draft invoices are excluded dispositions, counted in the
  manifest population; nothing is dropped silently.

## What the rehearsal still owes

1. Partition assignment: which legacy customers map to which
   billing account, and the customer document-snapshot source
   (Party import + document profile values).
2. The M1 gap list: any ERP AR behaviour the released billing
   contracts cannot represent goes back as a Starter extraction,
   never a local workaround (classification EXTRACT rule).
3. Capture binding: `OpeningCaptureV1` from the snapshot manifest
   (source sha, schema heads, cutoffs) — fields are wired in
   `build_manifest`; values come from the rehearsal runbook.
4. ~~Target fingerprints/watermarks (`OpeningTargetV1`) and the
   verifier round-trip~~ DONE 2026-11-08: `preflight_opening_import`
   (CLI `billing-preflight`) builds the manifest, binds
   `OpeningTargetV1` + migration authority, checks the module's
   fingerprint locally, and runs `preview_opening_account_import_v2`.
   With no signed-grant verifier injected the module itself reports
   `verifier_unavailable` and nothing is importable - execution
   still waits on ERP's trusted verifier composition (Sub-first) plus rehearsal/flip
   approvals. Preflight never executes.

Billing `0.1.0a11` publishes the Starter-native M1 verifier factory and
profile contracts (tag `dotmac-billing-v0.1.0a11`, commit
`8be175cc8bc470753e7edbc359f8415564c9f39f`). This assembly adopts its
`bi_0014_opening_variance_ledger` head with the existing tenant plane and
prerequisite bindings; kernel `0.1.0a107` satisfies its `>=0.1.0a105` floor.
ERP's trusted source, custody, grant, window, authorization, time,
revocation and target readers remain unwired. The default preview still
reports `verifier_unavailable` and `importable=False`; adoption alone
does not qualify rehearsal or activate imports. The 2026-10-12 Billing
freeze and no repair/backfill rule remain in force.

## AR opening admission (AS-IS rework of #72): billing a13 pinned (S9 composition)

Pin `dotmac-billing==0.1.0a13` (R1 + `bi_0018` + `bi_0019`, head `bi_0019_opening_dual_control`). The earlier a12
fallbacks are gone: every window provision passes `dual_control` explicitly.

a12/a13 bind ONE window to ONE complete `OpeningManifestV2`; every command carries the whole manifest and its
`partition` (customer x currency) only selects. `ar_opening_cli` runs census -> mint -> plan -> apply (provision as
app_admin, import every partition and seal as the restricted NOLOGIN `erp_billing_opening_writer` created by
`e2_0029`). The opening posts no GL.

Billing owner's a13 answers (paths under Starter `packages/dotmac-billing/src/dotmac_billing/`):

- **P-1 = NO.** A customer x currency partition is the smallest holdable unit: any held or blocking row refuses the
  whole manifest (`manifest_noncarryable_row`, `opening_variance_validation.py:127-132`); no variance reason accepts
  source kind `credit_note` (`opening_variance_contracts.py:107-157`); a `customer_credit` variance is refused for a
  customer with cash lots (`opening_variance_validation.py:149-157`). So no row is held inside billing. An
  **unapplied credit note** (no allocation links) is simply not part of the manifest: its partition is carried, the
  note is a register work item (E35 key `ar_unapplied_credit_note:<id>`: party, document number, amount) and part of
  the itemised per-party GL-minus-subledger difference on 1400 that the B-14 holding reclass moves to the AR holding
  account. A credit note with allocation links, and every other dependency blocker, still excludes the whole
  partition (E35 `partition:`; its GL goes to holding).
- **P-2 = YES.** Open items with `unknown_unverified` collection evidence count in R1 `collectible_receivable_minor`
  (`control_position.py:176-188`); tie them into AR control. No special-casing (no due date, no dunning until
  verified).
- **P-3 = YES.** `stored_balance`, `document_residual` and `rebuild` variances are memo basis: no accounting fact, never
  suspense (`opening_variance.py:173-176`); they resolve only by `evidence_confirmed`. `apply` has no allow-variances
  switch: it proceeds only when every variance is listed in the plan (sha-bound) and equals the manifest's own; each
  becomes an E36b item (register source `ar_census`). Recognise / confirm are post-flip, as app_user.

Other rules: customers are the imported wave-4 `customer_accounts` (`SubOriginPolicy.INCLUDE`; the account id is the
legacy customer id); the mint creates billing accounts only and refuses `customer_not_imported`. The window binds the
ERP two-person policy (#62) as a13's per-window `dual_control`, passed explicitly and read back (policy OFF is the default). T1: `t1_credit_by_currency(manifest)` is the Sigma cash-lot residual of the sealed
manifest per currency (the seam for B-14's `ar_opening_credit`; cross-check against R1 only while the writer fence
is held). T2/T3 are the B-15 mapper, not here. M3 `batch_controls` is NOT built (see the PR description).

Writer role (R5): `e2_0029` creates `erp_billing_opening_writer` (or verifies an existing one: login, superuser,
bypassrls, createrole, createdb, replication or any membership is refused; PostgreSQL below 15 is refused) and grants
exactly the R5 list (20 INSERT tables, 9 SELECT-only, `UPDATE(generation)` on `opening_windows`, `UPDATE(id)` on
`billing_accounts`, kernel `idempotency_records` / `outbox_events` SELECT+INSERT). It grants NO membership, not to the
migrator. The operator's `SET ROLE` membership is a Michael root step for the window only
(`GRANT erp_billing_opening_writer TO <operator> WITH SET TRUE, INHERIT FALSE`), revoked after the seal with the
writer's INSERT grants (runbook F9).

Review af472276 hardening (architect findings):

- PostgreSQL 16 or newer is required (`WITH SET TRUE, INHERIT FALSE` and `REVOKE ... OPTION FOR` are PG16). Any membership
  row on the writer with SET or INHERIT, held by any role, is refused by the migration and again by `apply` (only the
  session operator may hold SET, never INHERIT). A creator's ADMIN-only row is tolerated by the migration only for a role it just created and only when no other role
  reaches the creator (see R1 below); a non-superuser CREATEROLE migrator gets one automatically on PG16; if `createrole_self_grant` also gave the creator SET or INHERIT on a role the migration
  just created, those two options are revoked. A superuser migrator gets no row. An adopted role has every privilege
  stripped and exactly the R5 set re-granted, and the exact set is asserted from the ACLs; owned objects and default
  privileges are refused (`erp_v2.importer.ar_opening_role`).
- The two-person policy (`dual_control`) is bound into the plan sha: a policy flip after approval changes the sha and
  `apply` refuses it (`approved_plan_sha_mismatch`).
- The GL attribution reads the legacy snapshot at the census cutoff D with the census's posting-date and status
  semantics, and REFUSES any 1400 line after D (`ar_gl_after_cutoff`). A non-functional currency on any partition or
  journal owner is refused (`ar_attribution_fx_unsupported`) until P-4/P-7; unowned journals are never forced to the
  functional currency. A census without `gl_attribution` cannot be loaded into the register.
- Nothing legacy recorded is dropped silently. A document with no posted journal (any non-void, non-reversed status,
  invoices and payments) is an H-DOC work item with its status. A document posted by D but dated after it is an E35
  `row:` item with its GL-signed amount held (`row_after_cutoff`). CLOSED is only for residual 0, void or reversed.
- A refused partition is held whole: one `partition_held:<blockers>` item equal to the party's entire legacy GL.
- The apply receipt carries the window's manifest fingerprint read back after the seal, T1 per currency and the
  per-account cash-lot split; an aborted apply keeps what was admitted (`partial_receipt`).
- Runbook: an aborted apply followed by a re-plan yields a new window id (the id derives from the plan sha), so the
  existing window is refused (`existing_window_differs`). The only way forward is a restore of the pre-F6 database and a
  fresh run; do not try to edit or reuse the half-provisioned window.
- Runbook R1 (Michael as root, BEFORE the deploy that carries `e2_0029`, rehearsal first, then production): a superuser
  pre-creates the role so that nobody holds ADMIN on it and the migration takes the ADOPT path (revoke all, grant R5, assert
  exact; it needs no CREATEROLE):
  `CREATE ROLE erp_billing_opening_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;`
  Read back: all attributes false; `SELECT count(*) FROM pg_auth_members WHERE roleid = w.oid OR member = w.oid` is 0;
  `pg_has_role('app_admin', 'erp_billing_opening_writer', 'MEMBER')` is false. Rollback: `DROP ROLE` (only before the
  migration grants; after, REVOKE first; never once a window binds the oid). Why: on PG16 a non-superuser CREATEROLE creator
  gets an ADMIN-only row, and in the probed graph `app_admin` is a member of `erp_owner` (admin, inherit, set), so
  `app_admin` would reach ADMIN on the writer and could grant it to itself. Both the migration and the apply preflight now
  refuse `writer_admin_reachable:<role>` for any non-superuser role that can reach (`pg_has_role(.., 'MEMBER')`, which
  follows SET-only chains too) a role holding ADMIN on the writer. The migration tolerates only its own freshly created
  creator row and only if no other role reaches the creator; apply tolerates nothing. Revoke-after-migrate is NOT a
  substitute (the migration refuses first).
- Operator (window-only login, a widening): Michael creates a dedicated login (for example `erp_ar_opening_operator`) that
  is NOT `app_admin`, NOT `erp_owner` and not reachable from either (anything granted to `erp_owner` flows to `app_admin`).
  `GRANT app_admin TO <op> WITH SET TRUE, INHERIT TRUE` (the CLI reads billing and inserts the admission rows as the
  session user) and `GRANT erp_billing_opening_writer TO <op> WITH SET TRUE, INHERIT FALSE, ADMIN FALSE`. After the seal:
  REVOKE both, plus the writer's INSERT grants (F9), then `ALTER ROLE <op> NOLOGIN` or DROP. The credential is Michael's.
- Non-functional currency: `plan` refuses any partition whose currency is not NGN (`ar_opening_fx_unsupported`) before
  anything is imported or sealed, until the P-4/P-7 ruling.
- Merge precondition (deploy): `e2_0029` runs the role guard on every target. Probe production and rehearsal read-only
  first: PG >= 16; the migrator has CREATEROLE, is not `app_admin`, and `app_admin` is not a member of the migrator;
  `createrole_self_grant` unset; no pre-existing writer role with SET/INHERIT members.
- Before any plan/apply go: a read-only census + attribution dry run on g9 with a 0600 receipt, identical across two
  runs; confirm the distinct payment and invoice statuses per bucket and the GL sign of a positive credit note.
- The sha of the census `gl_attribution` block (`gl_attribution_sha256`, also on the receipt) is bound into the plan sha
  (plan.v4); `plan` and `apply` refuse without the attribution (`gl_attribution_required`) and the register refuses a
  receipt whose block does not match its sha (`gl_attribution_sha_mismatch`). The per-party split cannot be edited
  without detection. The census command therefore needs the destination chart (read only), like plan and apply.
- Migrator (reader facts): the deploy pipeline runs NO alembic step. `mod_billing` and `public` tables, the `mod_billing`
  schema and `alembic_version` are owned by `erp_owner`; the `public` schema is owned by `app_admin`. So `erp_owner` is the
  migrator, and migrations (including `e2_0029`) are run by an operator as `erp_owner`, not by the pipeline.
- R1 and W9 are approved by Michael (he pre-creates the writer as superuser, and creates the window-only operator login);
  the PG tests model R1 (superuser pre-create, adopt) as the expected path.
