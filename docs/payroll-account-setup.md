# Payroll deduction account setup

NHIS employee deductions credit a payroll payable until remitted. Use account 2133, NHIS Payable, in the same active liability category as 2132 NHF Payables. The account starts with no opening balance. An existing account must match the reviewed category, posting kind, normal balance and name; a conflicting code is refused.

Staff-loan principal recovery credits the existing 1410 Staff Loan asset account, reducing the employee receivable. It does not create revenue or a second payable account. The source chart already contains this active, postable debit-balance account. Loan balances and repayment evidence must be reconciled with the loan owner; account setup does not fabricate a loan, repayment or settlement.

Configuration affects future payroll only. Existing published rule snapshots, calculations, journals and legacy data stay unchanged. A new effective payroll revision must consume the validated account UUIDs returned by `resolve_future_deduction_accounts`. NHIS needs a real remittance destination; rates and provider identities are separate inputs.

Account setup alone does not enable the payroll workflow. Released Payroll currently creates an external liability for every deduction, including a recovery mapped to an asset. Before loan recovery is enabled, the Payroll owner and ERP payroll workflow must distinguish internal receivable recovery from outward remittance, apply the recovery once, and prove replay leaves both the staff-loan balance and GL unchanged. No bank file or payment should be produced for a loan recovery.

Missing-account creation requires Accounting 0.1.0a2 or later with the public transaction helper; the older release imports the kernel reference database runtime. Use the owner service to create accounts and refuse the older unsafe creation path. Plan mode is read-only, and apply must use a reviewed tenant/database-bound spec. Verify creation, exact replay, conflicting configuration refusal, and tenant isolation in hosted CI; rehearse the final published image on a disposable copy before cutover.

Source evidence: fresh legacy snapshot `9fc8fb1e2d83953dc365c2cb7c1d0e2b88fb7623a78bf29e503c2ab92213d8df`, read-only run-f census. Accounting principles: [IAS 19 employee benefits](https://www.ifrs.org/issued-standards/list-of-standards/ias-19-employee-benefits/) and [IFRS 9 financial assets](https://www.ifrs.org/issued-standards/list-of-standards/ifrs-9-financial-instruments/). These standards do not prescribe company account numbers.
