# Staff loan entries (PR-3)

No Starter owner holds a loan balance, so `erp_staff_loan_entries` (migration `e2_0023_staff_loan_entries`)
is the per-employee subledger behind the 1410 Staff Loan control account. Service level only: no routes,
templates or permission profile in this PR.

## Kinds

| Kind | GL (through the bridge, `("erp","staff_loan_entry")`) | Subledger |
|---|---|---|
| `advance` | Dr 1410 / Cr the paying bank account's cash GL | + amount |
| `opening` | none (the GL already holds it) | + amount |
| `adjustment` (signed) | Dr 1410 / Cr offset, or Dr offset / Cr 1410 | + signed amount |
| `control_difference` | Dr offset / Cr 1410 | none (names no employee) |

Every kind goes `draft -> approved -> recorded` (prepare, approve, record). The approver and recorder may be
the preparer: there is no compulsory second person; the per-tenant policy that can add one is PR-4.

## Reused

* `supplier_payments.service.payer_references`: cash GL from the selected bank account, never typed.
* `disbursement.bank_reference.claim` (now also sees loan advances): one bank reference per account across
  B3, B5 and the loans.
* V3: an approved `employee` payee instruction, at approve and again at record.
* The B3 value-date rule: an entry cannot be recorded before its value date.

## Rules

* **Opening bound:** the sum of all employee balances after the opening must not exceed GL(1410).
  All of an employee's openings share one value date (Finance's schedule date), and an opening cannot
  follow an earlier-dated advance or adjustment of that employee.
* **Control difference** (the 805,000 clean-up): at most `GL(1410) - subledger` and only while that is
  positive. Finance attributes the rest to employees (more openings) or writes it off here.
* **Offsets** may not be a control account (2000, 2040, 1410) or a bank account's cash GL.
* **Employee balance** = recorded entries - payroll internal recoveries on 1410 (payroll a5 liabilities of
  finalized runs, read-only). An opening is a balance as at its date, so only recoveries of runs whose period
  ends after that date count.
* **B4 check:** `loan_recovery_exceeds_balance` at run approve and finalize: an employee's internal recovery
  may not exceed the loan balance before the run. Before any opening or advance is recorded, a run with an
  internal recovery is therefore refused.

The refusal codes are stable: `payee_instruction_required`, `opening_exceeds_gl`, `opening_date_mismatch`,
`opening_after_entries`, `entry_before_opening`, `adjustment_exceeds_balance`, `control_difference_exceeds`,
`offset_is_control_account`, `offset_is_cash_account`, `offset_not_postable`, `bank_reference_reused`,
`value_date_future`, `request_conflict`, plus `posting_*` from the bridge.

The migration branches from `e2_0021` with a distinct id; PR-4 also takes `e2_0022`, so the heads are
rebased when the two meet. The downgrade refuses while any entry exists.
