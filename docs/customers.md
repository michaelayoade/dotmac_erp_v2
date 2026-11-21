# Customer screen (F-F): add and edit a customer, legacy parity

Spec: the architect's customer screen spec of 2026-11-20 (private). Ruling: build before the flip; match legacy, add
nothing. This PR is stacked on #78 (B-16, `e2_0032`) and the B-15 rev 2 chain (`e2_0033`); the coordinator re-chains it.

## What it is

Routes under `/finance/customers` (server-rendered, CSRF on every POST):

| Route | Permission |
|---|---|
| `GET /finance/customers` (list, search, status filter, paging), `GET /finance/customers/{id}` (detail) | `erp.web.access` |
| `GET/POST /finance/customers/new`, `GET/POST /finance/customers/{id}/edit`, `POST /finance/customers/{id}/status` | `erp.finance.customers.manage` (finance_preparer) |

There is no two-person control (master data; single-operator ruling) and no feature flag: the routes are dormant until a
party holds the permission. Admin and staff hold no write permission.

## Where each field lives

* dotmac-customers (the owning module, single writer): account number, display name, status, notes (profile). This
  package only calls its service commands (`create_account`, `transition_account`, `set_profile`, `rename_account`).
  A source-grep test asserts no other ERP code writes `mod_customers`.
* ERP table `erp_customer_finance` (`e2_0035`): trading name, tax id, payment terms code, primary contact (name, email,
  phone) and billing address. Keyed by the plain customer account id (UNIQUE per tenant), never financial identity: billing
  keys off the customer account id. No JSON, no free metadata.
* The billing accounts panel is read-only: the accounts whose `external_account_ref` is this customer's id (one per
  currency). Structural; never edited here.

## Behaviour

* New customers start `ACTIVE` (legacy default) or `PROSPECT`. A blank account number is taken from the numbering series
  named by the tenant setting `customers.series.account_number` (entered in the UI, audited); a duplicate number is
  refused `account_number_taken`. Create carries a hidden `request_id` (kernel idempotency: a retry returns the original,
  a changed payload on the same id is `idempotency_conflict`).
* Inactive = `SUSPENDED` (reversible); legacy delete becomes `CLOSED` (terminal, needs an explicit confirmation).
  **No delete exists.**
* The payment terms code must be a key of `ar.payment_terms` at save (`payment_terms_unknown`). A customer's code is
  at most 40 characters (`payment_terms_code_too_long`). Tax id: trimmed, printable, at most 50, format not validated
  (as legacy). Email: basic shape check.
* Each POST runs in one savepoint and writes one audit event with field names only (never a contact, address, tax id,
  note or name): `erp.finance.customers.created | updated | status_changed | refused`.
* Rename calls customers a2 `rename_account` directly. A CLOSED customer is refused `customer_closed` for a rename, a
  finance-field edit and a notes edit (close is terminal); nothing is saved. Concurrent edits of one customer serialise
  on the finance row (`SELECT ... FOR UPDATE`).
* Create calls customers a2 `create_account` directly (a2 imports `dotmac_kernel.transactions`, which a composed ERP
  process allows).

## B-16 hook (read side, ready now)

`customers_web.terms.code_for(db, tenant_id, customer_account_id)` returns the customer's terms code or None.
`ar.service.default_terms_for_customer` pre-selects it when it is still one of the configured terms, else
`ar.default_payment_terms_code`. A draft stores its own code, so a later customer edit never changes a draft or an
issued invoice (PG-tested). `ar.service._customer_document_snapshot` builds the customer snapshot on a document from
the finance row: legal name, trading name, billing address (or the unrecorded marker) and the TIN identifier when present.

## Gate check

`customers_web.checks.ready(db, tenant_id)` reports `customers_setup_incomplete:customers.series.account_number` and
`customers_finance_missing:<n>` (customers without a finance row; the wave-4 importer transform lands them). Wiring it
into the flip gate is a follow-up.

## Migration `e2_0035_customer_finance`

One table, FORCE RLS; a trigger makes id, tenant_id, customer_account_id and created_at immutable and refuses DELETE even
for the owner. **W10** (approved 2026-11-20): `app_user` gets SELECT, INSERT and a column-limited UPDATE (trading name, tax
id, terms code, contact columns, billing address, `updated_at`, `updated_by`); no DELETE. The downgrade refuses while rows
exist. `e2_0034` stays reserved for the WHT follow-up.
