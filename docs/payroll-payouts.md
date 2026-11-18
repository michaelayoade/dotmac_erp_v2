# Payroll payouts (B5)

Starter payroll 0.1.0a5 owns the liabilities and their settlements; the ERP composes batching,
approval, the optional bank file, the role gates and the ledger posting. The ERP never calls a bank:
recording is a named person's assertion, backed by a bank evidence reference, that the money left
through the bank portal.

## What a batch is

A batch is a set of WHOLE liabilities of ONE finalized run (amounts are never edited), paid from one
active bank account in the tenant functional currency, with a typed bank value date.

- `net_pay`: liabilities with key `employee-net-pay` and beneficiary type employee only. A deduction,
  employer liability or any component liability can never be selected (`liability_not_net_pay`).
  DRAFT -> APPROVED -> [FILE_ISSUED] -> RECORDED (the file step is optional: files are default OFF).
- `statutory`: `component:*` liabilities with an external beneficiary, one destination per batch
  (PAYE, pension, NHIS; W19). DRAFT -> APPROVED -> RECORDED. Never a file (`statutory_no_file`).
- Internal recoveries are excluded: a liability account that is not a payable (asset class, for
  example 1410) is refused (`internal_recovery_excluded`). From payroll a4 the liability carries
  `settlement_mode`; the check `settlement_mode_allows_payout` turns on by itself when the attribute
  exists and then requires `external` (net pay and remittances), so the a4 pin PR needs no code change
  here (covered by tests that simulate the attribute).

## Roles (all routes dormant until granted; admin and staff hold none)

| Step | Routes | Permission | Role |
| --- | --- | --- | --- |
| Prepare | `/payroll/payouts` | `erp.finance.payroll_payouts.prepare` | finance_preparer |
| Approve / cancel | `/payroll/payout-approvals` | `erp.finance.payroll_payouts.approve` | finance_approver |
| Bank file | `/payroll/payout-treasury/{id}/bank-file` | `erp.treasury.disbursement_files.generate` | treasury |
| Record as paid | `/payroll/payout-treasury` | `erp.treasury.payroll_payouts.record` | treasury |

Preparer, approver, file issuer and recorder are four distinct people (service, and CHECK constraints
in `e2_0019`). The approver and the file issuer must not be the proposer or decider of any net-pay
line's CURRENT payee bank instruction (migrated legacy instructions exempt); re-checked at approve,
file and record. Statutory batches keep preparer, approver and recorder distinct.

## Recording

One savepoint: for each line `record_liability_settlement`, the settlement consequence
(Dr liability account, normally 2040 / Cr the selected bank account's cash GL, posting date = the
bank value date) and the P0 bridge. Payroll a3 has no accounting-receipt table, so each line stores
its settlement id, journal id and consequence fingerprint (the ERP-side receipt). One journal per
line: the owner's consequence builder works per settlement. Any refusal rolls the whole batch back:
a closed period, a liability no longer outstanding at exactly its amount, a reused bank reference
(normalised: trimmed, whitespace collapsed, upper-cased; unique per bank account), a future value
date, an instruction changed since the file. A replay of a recorded batch is a no-op.

## Bank file (net pay only, default OFF)

Same setting and library as B3 (`treasury_policy/bank_file_generation_enabled`,
`erp_v2.disbursement`, `generic-csv-v1`). One file per batch: a receipt row per line in
`erp_disbursement_file_receipts` (UNIQUE per line; kind `payroll_payout`) plus `file_sha256` and row
count on the batch; a second file is refused (`file_already_issued`). Cancelling after a file needs
the bank's rejection or recall reference, audited. Bytes are never logged or stored; `no-store`.
A cell with a control character, comma, quote or semicolon refuses the whole file (`file_invalid_cell`).

## Returned salary (payroll a5, `e2_0021_payroll_payout_returns`)

When the bank returns one employee's salary credit, an approver records the return on that LINE of the
RECORDED net-pay batch (`/payroll/payout-approvals/{batch}/lines/{line}/return`; inputs: bank return
reference, bank return date, reason). Permission: the existing `erp.finance.payroll_payouts.approve`
(finance_approver; same reuse ruling as cancel/reversal in B3). No new permission, no role profile bump.

- Returner: a finance_approver who is none of the batch's preparer, approver, file issuer or recorder
  (`sod_returner_is_*`; the owner also refuses the settlement's recorder; a trigger repeats the rule).
- One savepoint: owner `reverse_liability_settlement` (whole settlement, liability back to outstanding,
  settlement row never updated), `build_settlement_reversal_consequence` (Dr bank cash GL / Cr 2040,
  keyed on the reversal id), the bridge, and the line marked RETURNED with `active = false` (so the
  liability can go into a new batch). Payroll a5 has no accounting-receipt table: the receipt is on the
  line (reversal id, journal id and number, fingerprint, who, when, reference, reason).
- Bank account: the cash GL credited by the settlement's ORIGINAL posted journal (rebuilt and checked
  against the stored consequence fingerprint; `original_journal_mismatch` otherwise), never the bank
  account's current mapping. If an approved cash GL re-point has since left that GL, the return still
  debits it (the old GL stays postable; only B7 refuses to reconcile it) and the audit event records
  `original_cash_gl_retired: true`. Finance moves the amount to the new account with the step-4 B2
  transfer in `cash-gl-split.md`.
- Dates: the return date is on or after the settlement's posting date and not in the future.
- The return reference is a bank evidence reference: unique per bank account across B3, B5 payouts and
  B5 returns (`bank_reference_reused`).
- Replay with identical evidence is a no-op returning the same reversal and journal; different evidence
  is `already_returned`. Concurrent returns serialise on the batch row lock. Audited:
  `erp.payroll.payout.line_returned` (refusals `erp.payroll.payout.refused`).
- Re-payment is a new batch for the reopened liability; its settlement reference is new (batch number and
  line id), and the owner refuses the old one.
- Statutory lines have no return path (`statutory_no_return`).

## Not done here

No reversal of a recorded payout other than the returned-salary path above (a Finance journal through B2),
no WHT, no non-functional currency,
no bank-specific layouts (W15), no backfill; amounts are never changed.

## Pre-flip validation V3

A net-pay batch needs an approved payee bank instruction for every employee line at approval, and at recording unless a bank file was issued (`payee_instruction_required`), whether or not bank files are enabled. Statutory remittances are excluded: their destination is chosen on the batch, not a person. Legacy-import approved instructions satisfy it.
