# Staff loan entries (PR-3)

No Starter owner holds a loan balance, so `erp_staff_loan_entries` (migration `e2_0023_staff_loan_entries`,
after `e2_0022_dual_control_policy`) is the per-employee subledger behind the 1410 Staff Loan control
account. Service level only: no routes, templates or permission profile in this PR.

## Which way round the 805,000 is

The register census settles the direction: **GL 1410 holds a net debit of 805,000** (24 FY2025 journals),
while the active loan records prove only about 0.01 (one 2026 loan of 100,000, since repaid). So the GL is
larger than the subledger, not the other way round. (Two Finance packets state it reversed; they are
corrected separately. Spec section 0 and 3d are right.)

The route is therefore:

1. **Per-employee openings, within the GL.** Finance enters an opening for each employee the records
   prove. Openings are subledger only (the GL already holds the amount) and can never take the sum of
   balances above GL(1410) (`opening_exceeds_gl`).
2. **A capped `control_difference` write-off of the remainder.** Dr an offset (expense) / Cr 1410, at most
   `GL(1410) - subledger`, and only while that is positive. It names no employee and never touches the
   subledger. Afterwards the subledger ties to the GL exactly.

`tests/test_staff_loans_pg.py::test_the_805000_route_...` runs this end to end (GL 805,000, three openings,
the cap refusal, the write-off, subledger = GL).

## Kinds

| Kind | GL (through the bridge, `("erp","staff_loan_entry")`) | Subledger |
|---|---|---|
| `advance` | Dr 1410 / Cr the paying bank account's cash GL | + amount |
| `repayment` | Dr the receiving bank account's cash GL / Cr 1410 | - amount (never below zero) |
| `opening` | none (the GL already holds it) | + amount |
| `adjustment` (signed) | Dr 1410 / Cr offset, or Dr offset / Cr 1410 | + signed amount |
| `control_difference` | Dr offset / Cr 1410 | none (names no employee) |

States: `draft -> approved -> recorded`, or `draft`/`approved` `-> cancelled`. A cancellation needs a reason
and is audited; the state CHECK and the trigger make `recorded` and `cancelled` final. A recorded entry is
corrected by an adjustment, never cancelled.

The tenant's dual-control policy (`erp_v2.governance`, default off) is stamped on the row at prepare. Off: one
person may prepare, approve and record. On: approver is not the preparer (nor the employee's instruction
proposer/decider for an advance) and the recorder is neither preparer nor approver; CHECKs back this.

## Reused

* `supplier_payments.service.payer_references`: cash GL from the selected bank account, never typed.
* `disbursement.bank_reference.claim` (also sees loan advances and repayments): one bank reference per
  account across B3, B5 and the loans.
* V3: an approved `employee` payee instruction for an advance, at approve and again at record. A repayment
  receives money and needs none.
* The B3 value-date rule: an entry cannot be recorded before its value date.

## Rules

* **Opening bound:** the sum of all employee balances after the opening must not exceed GL(1410).
  All of an employee's openings share one value date (Finance's schedule date), and an opening cannot
  follow an earlier-dated advance, repayment or adjustment of that employee.
* **Control difference and openings:** a write-off is at most the measured difference; both it and an opening are refused while any employee balance is below
  zero (`employee_balance_negative`), because the difference is then not measurable.
* **Repayment** may not exceed the employee's balance (`repayment_exceeds_balance`).
* **Offsets** may not be a control account (2000, 2040, 1410) or a bank account's cash GL.
* **Employee balance** = recorded entries - payroll internal recoveries on 1410 (payroll a5 liabilities of
  finalized runs, read-only). An opening is a balance as at its date, so only recoveries of runs whose period
  ends after that date count.
* **B4 checks** at run approve and finalize:
  * `loan_recovery_exceeds_balance`: an employee's internal recovery may not exceed the loan balance before
    the run. A run with an internal recovery is refused until the employee's opening or advance is recorded.
  * `recovery_before_loan_opening`: a run whose period ends on or before the employee's opening date, but is
    finalized after it, is refused. Its recovery is already inside the opening, so it is neither deducted
    twice nor silently ignored.

Stable refusal codes: `payee_instruction_required`, `opening_exceeds_gl`, `opening_date_mismatch`,
`opening_after_entries`, `entry_before_opening`, `adjustment_exceeds_balance`, `repayment_exceeds_balance`,
`control_difference_exceeds`, `employee_balance_negative`, `offset_is_control_account`,
`offset_is_cash_account`, `offset_not_postable`, `bank_reference_reused`, `value_date_future`,
`request_conflict`, `request_cancelled`, `not_cancellable`, `reason_required`, `sod_*` (policy on), plus
`posting_*` from the bridge.

The migration follows `e2_0022_dual_control_policy`. The downgrade refuses while any entry exists.
