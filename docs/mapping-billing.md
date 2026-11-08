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
4. Target fingerprints/watermarks (`OpeningTargetV1`) and the
   verifier round-trip before `ImportOpeningAccountV2` executes.
