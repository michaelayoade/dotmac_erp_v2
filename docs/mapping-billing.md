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
