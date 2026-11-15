# Payroll run: batch calculate, approve, finalize and post (B4)

Three permission-gated browser commands over the released Starter payroll owner (0.1.0a3) and the
posting bridge. They are dormant until named parties hold the role (`python -m erp_v2.access_cli`);
`admin` and `staff` hold neither approve nor finalize.

| Step | Route | Permission | Role |
|---|---|---|---|
| Batch calculate all active assignments | `/payroll/runs/{id}/batch-calculate` | `erp.payroll.manage` | admin (as for draft creation) |
| Approve | `/payroll/runs/{id}/approve` | `erp.payroll.runs.approve` | `payroll_approver` |
| Finalize and post | `/payroll/runs/{id}/finalize` | `erp.payroll.runs.finalize` | `payroll_finalizer` |

Creator, approver and finalizer must be three different people (the owner enforces creator != approver
and approver != finalizer; this workflow also refuses creator == finalizer). Role-level SoD
(`payroll_approver` + `payroll_finalizer` on one person) is blocked at grant time.

## Batch calculation

Every active assignment effective at the run's period end is calculated with the released
`calculate_employee_payroll` (proration 1; use the single-employee form first when an employee needs proration,
the batch skips employees that already have a calculation). It is all or nothing in one savepoint.
Employees whose structure has input rules need rows in an uploaded UTF-8 CSV
`employee_ref,component_code,amount,evidence_ref`; the evidence reference is mandatory on every row, amounts are
exact at the currency scale (nothing is rounded), a missing, surplus or unknown row refuses the whole batch.
The file is never stored or echoed; the audit event keeps only its sha256 and row count.

## Guards (batch, approve and finalize)

- Currency: the run currency must equal the tenant functional currency (the `accounting_policy/functional_currency`
  setting, imported from the legacy organisation). An unset setting refuses; there is no default.
- Overlap: the period must not overlap any other non-cancelled payroll run. Imported legacy runs carry no marker in
  the payroll tables, so every existing run counts (imported runs arrive finalized).
- Roster and evidence: every active assignment has a calculation, no calculation lacks an active assignment, and
  every component line has an evidence reference (re-checked at approval and finalization).
- Mappings (Finance input, never created here): every earning/employer-liability line has an expense account,
  every deduction/employer-liability line has a liability account and destination, every revision has a net payable
  account, and each reference resolves to exactly one active, postable tenant account in a declared form
  (account UUID, or the code `2040` for the net payable). The net payable must be account 2040. Anything else refuses
  with `missing_account_mapping` (listing the component codes) or a `mapping_*` code.

## Finalize and post (one savepoint)

`finalize_payroll_run` (liabilities) -> `build_run_accounting_consequence` -> bridge post. A refusal anywhere
rolls all of it back. The journal is `payroll/payroll_run/<run id>/1`, dated the run's period end:
Dr earning and employer-liability expense, Cr deduction and employer-liability accounts, Cr 2040 Payroll Payable =
total net pay (asserted before posting). A repeat finalize returns the same journal and writes nothing.
A finalized run with no journal is refused and referred to the exceptions review; nothing is repaired.

## Loan recovery and account setup (PR #42)

Account setup (2133 NHIS Payable, reuse of 1410 Staff Loan) and the validated UUIDs for new pay-structure
revisions belong to the payroll account-setup command; this workflow consumes the resulting mappings and does not
create accounts. A deduction credited to an ASSET-class account such as 1410 is an internal receivable recovery, but
payroll 0.1.0a3 creates an external liability row for every deduction that a later remittance or payout could pay.
Such a run is therefore refused (`internal_recovery_unsupported`) until the payroll owner distinguishes internal
recovery. That is a payroll owner change, not an ERP workaround.
