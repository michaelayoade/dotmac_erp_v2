# Sub-origin policy for the general importer (F1 part 2)

ERP v2 is never the authority for a Sub-originated record. Legacy ERP
tags mirrored Sub records with `dotmac_sub_id`. Code:
`src/erp_v2/importer/sub_origin.py`.

- Every `Pairing` whose legacy source table carries `dotmac_sub_id`
  must declare `sub_origin` (`exclude` or `not_applicable`). The tagged
  tables are the checked-in constant `LEGACY_SUB_ORIGIN_TABLES`
  (customer, customer_payment, invoice; dotmac_erp @ 74fa788a). A
  structural test fails any undeclared pairing.
- `exclude`: the executor reads only `dotmac_sub_id IS NULL` rows (a
  blank string is NOT NULL, hence excluded, as in legacy). The receipt
  carries `sub_origin.specs[*].census` (source = imported + excluded +
  refused; unbalanced = incomplete run) and the sorted excluded keys
  (capped at 500, flagged when truncated).
- A spec declared `exclude` over a source without the column, or a
  source with the column and no declared policy, is NOT executable.

| Pairing | Source | Policy | Reason |
|---|---|---|---|
| mod_payments.payment_confirmations | customer_payment | exclude | Sub-origin cash belongs to Sub's Billing |
| mod_customers.customer_accounts | customer | include | as-is ruling: the AR opening reuses these accounts |
| mod_customers.customer_profiles | customer | include | as-is ruling |

The enrolment mint `sales_leads` (one lead per legacy customer) also
reads only `dotmac_sub_id IS NULL` customers. Invoices and payments for
Billing go through `billing_opening` (the COMMAND-IMPORT pairing).

The receipt also reports `sub_origin.customers_referenced_by_native`:
sorted keys of Sub-tagged customers referenced by ERP-native invoices or
payments. These customers are not imported (M11).

- `include`: the column is present and Sub-tagged rows are imported like any other (origin stays provenance).
  Added by the AS-IS rework of the AR opening (replan section 2.4); it replaces `exclude` on the wave-4 customer
  pairings. A spec declared `include` over a source without the column is not executable.

## AR opening: F1 reversed for Billing (v1677, rev 2)

For the Billing opening (`importer/billing_opening.py`, `ar_census.py`, `ar_opening_cli.py`) the Sub-origin
exclusion and the Splynx-era block are REVERSED: every open 1400 item (native, Sub-origin, Splynx) is admitted, and
origin is provenance in the evidence digest, never an identity or a rule. The AR opening REUSES the wave-4
`customer_accounts` (id = legacy customer id; policy `include` above) and only creates the billing accounts
(`ar_opening_cli mint` refuses `customer_not_imported`). The `customer_payment` row above is unchanged.
