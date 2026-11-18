# Supplier payments (B3)

Starter payables 0.1.0a5 owns the payment lifecycle and the obligation settlement; the ERP composes
the UI, the role gates, the payer references and the ledger posting. The ERP never calls a bank or a
payment provider: recording a payment is a named person's assertion, backed by a bank evidence
reference, that the money left through the bank portal.

## Flow and roles

| Step | Route family | Permission | Role |
| --- | --- | --- | --- |
| Propose from open obligations | `/finance/supplier-payments` | `erp.finance.supplier_payments.prepare` | finance_preparer |
| Approve or cancel; reverse a recorded payment | `/finance/supplier-payment-approvals`, `/finance/supplier-payment-reversals` | `erp.finance.supplier_payments.approve` | finance_approver |
| Record as paid (bank evidence reference); optional bank file | `/finance/supplier-payment-treasury` | `erp.treasury.supplier_payments.record` / `erp.treasury.disbursement_files.generate` | treasury |

Every route is dormant until named parties hold these roles (`access_cli`). admin and staff hold
none of them. The routes are not facet entry routes, so a finance role without `staff` is refused.
Segregation of duties is enforced per payment (payables and the ERP service) and per person at grant
time (the P0 role pairs: finance_preparer + finance_approver and finance_approver + treasury are
conflicts; the second is never waivable). Reversal is an approver power; payables refuses the
approver, recorder and preparer of that payment as reverser.

## Ledger posting

Recording runs, in one savepoint, `record_supplier_payment` (settles each obligation), the payables
consequence (Dr the invoice liability account, normally 2000, Cr the cash GL), the P0 bridge and
`record_accounting_receipt`. Any refusal (closed period, inactive account, reused bank reference,
changed payee instruction) rolls all of it back. A reversal does the same with
`reverse_supplier_payment` and the mirror consequence; the reversal date must fall in an open period.
Replays are no-ops. Settlement observations are never edited; reports must net `settlement_reversed`.

The cash GL account and the payer instrument come from the selected bank account record
(`cash_account_ref`, account id), never from a form field.

## Bank file (optional, default OFF)

Tenant setting `treasury_policy/bank_file_generation_enabled` (boolean, default false; absent or
invalid reads as off). When on, treasury may generate a generic CSV (`generic-csv-v1`) from APPROVED
payments of one bank account, using each supplier's approved payee instruction (B8). The bytes are
streamed to the download only: never logged, never stored, `no-store` headers. Only `sha256`, row
count and the payee instruction id are kept (`erp_disbursement_file_receipts`, migration
`e2_0018_disbursement_receipts`, insert-only) plus an audit event. Recording refuses when the payee's
approved instruction changed after the file. Bank-specific layouts (W15) are not implemented. The
library (`erp_v2.disbursement`) is shared with B5.

## Refused, not guessed (decisions open)

- Withholding tax (`wht_unsupported`): a5 carries it, but the WHT payable account source and
  certificate policy (W17) are undecided; the UI offers no WHT and approve/record/file refuse a
  payment that has any.
- Payments not in the tenant functional currency at rate 1 (`fx_unsupported`): realised FX is not modelled.
- Open legacy invoices outside the admitted cohort have no obligation and cannot be paid here.

## Pre-flip validations (V1-V3)

- V3: a supplier payment needs an approved payee bank instruction at approval and at recording, whether or not bank files are enabled (`payee_instruction_required`). With a file issued, the existing `payee_instruction_unavailable` / `payee_instruction_changed` codes apply. Legacy-import approved instructions satisfy it.
- A bank account whose cash GL is a control account (2000/2040/1410) is refused (`cash_account_is_control`).
- V1/V2 apply to the supplier invoice that creates the obligation: a taxed line's tax account must be the input-tax account 1455 and not the line's own account, and the liability must be AP control 2000. No invoice line or tax account may be a control account (`line_account_is_control`), so `Dr 1410 / Cr 2000` cannot be entered as an invoice.
