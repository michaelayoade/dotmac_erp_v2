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
| mod_customers.customer_accounts | customer | exclude | pending M11 |
| mod_customers.customer_profiles | customer | exclude | pending M11 |

The enrolment mint `sales_leads` (one lead per legacy customer) also
reads only `dotmac_sub_id IS NULL` customers. Invoices and payments for
Billing go through `billing_opening` (the COMMAND-IMPORT pairing).

The receipt also reports `sub_origin.customers_referenced_by_native`:
sorted keys of Sub-tagged customers referenced by ERP-native invoices or
payments. These customers are not imported (M11).
