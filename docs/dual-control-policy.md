# Dual control as a per-tenant policy (D-9, `e2_0022_dual_control_policy`)

Michael's ruling (2026-11-17 and 2026-11-18, register v1525): **no compulsory two-person**. Every coded
two-person or separation-of-duties check in ERP v2 is an optional, per-tenant policy, **default OFF**. This is an
explicit widening, and it also removes the non-waivable `finance_approver` + `treasury` role pair.

## The policy

- One row per tenant in `erp_governance_policies` (`dual_control boolean`, `set_by`, `set_at`, `evidence_ref`).
  A missing row reads as **off**. FORCE RLS; the runtime role has SELECT only.
- It is written only by the operator CLI. There is no UI or runtime write path.

```
python -m erp_v2.governance.cli plan  --spec S.json --receipt R.json
ERP_GOVERNANCE_APPLY=1 python -m erp_v2.governance.cli apply --spec S.json --receipt R.json \
    --approved-spec-sha256 <sha256 of S.json> --operator-ref <who>
```

`S.json` is `{"version": 1, "tenant_id": "<uuid>", "dual_control": true|false, "evidence_ref": "<why>"}`.
`MIGRATION_DATABASE_URL` only; the receipt is 0600 and never carries the tenant id. Apply writes the row and the
audit event `erp.governance.dual_control_set` (previous and new value, spec sha, evidence) in one transaction;
re-applying the current value is a no-op.

## What it gates (the ten ERP checks)

| # | Check | Gate |
|---|---|---|
| 1 | Role-pair SoD at grant (incl. `finance_approver` + `treasury`) | off: counted `warnings.sod_conflict` in the receipt, grants proceed |
| 2 | B1 submitter != approver | tenant policy read at approval |
| 2b | Supplier credit-note approval (#65): submitter != approver (`sod_submitter_is_approver`) | tenant policy read at approval (no stamp column, so no `pending_stamped_off` entry) |
| 3 | B1b approver != submitter / legacy approver (and the unknown/unresolved refusals) | tenant policy read at plan |
| 4 | B2 manual journals: post and reversal separation | row stamp + CHECKs |
| 5 | B3 supplier payments: approver, recorder, reverser, file issuer, instruction party | tenant policy read at each step |
| 6 | B4 payroll runs: calculator = creator, creator != approver != finalizer | tenant policy read at each step |
| 7 | B5 payouts: four-person set, instruction party, returner | row stamp (batch and line) + CHECK + trigger |
| 8 | B7 reconciliation: approver != preparer, opener or participant | row stamp + CHECK |
| 9 | B8 payee instructions: proposer != approver | row stamp + CHECK |
| 10 | Cash GL re-point: proposer != approver | row stamp + CHECK |

Rows with a database CHECK (manual journals, payee instructions, reconciliations, payout batches, payout lines,
cash GL re-points) carry `dual_control`, stamped from the tenant policy when the row is created. The migration adds
the column with `DEFAULT true`, so every row that exists before `e2_0022` stays strict, and rewrites each CHECK as
`NOT dual_control OR (<existing predicate>)`. Where an old CHECK also held plain integrity terms (an approved row
carries its approver and time; a reconciliation approves only at zero difference; a re-point records its applied
reference) those stay unconditional in a companion `*_integrity` CHECK. The runtime role cannot update the stamp
(column-level grants; a trigger freezes it on manual journals). The B5 returner guard trigger is gated on the line's
stamp.

Consequence worth knowing: a row keeps the stamp it was created with. Switching the policy on later does not
tighten rows already created off; switching it off does not relax rows created on.

Before turning the policy ON, read the plan receipt: `pending_stamped_off` counts the undecided rows (payee
instructions, payout batches, manual journals, reconciliations, cash GL re-points) that were created with the
policy off. They stay lax. To bring them under the policy, **cancel (or reject) and re-propose them** after the
policy is on. The ledger-fault re-verification is not part of the policy and always runs.

The policy read refuses (`GovernancePolicyError`) rather than reading OFF when, on PostgreSQL, the session's RLS
tenant setting is missing or differs from the tenant asked for (roles that bypass RLS, such as the operator CLI,
only refuse on a differing setting).

## Starter's own separation checks (payroll a7 pinned; payables a7 prepared)

dotmac-payroll 0.1.0a7 and dotmac-payables 0.1.0a7 take a keyword-only `separation_of_duties: bool = False`
(non-bool refused). ERP passes the tenant policy; the default is OFF.

1. **Every call passes it explicitly.** `tests/test_separation_call_sites.py` scans `src/erp_v2` with the AST and fails
   on any call to `approve_payroll_run`, `finalize_payroll_run`, `reverse_liability_settlement` or the four
   `*_supplier_payment` functions that does not pass `separation_of_duties=` sourced from
   `governance.separation_of_duties` (payables calls go through `**separation.payables_kwargs(db, tenant_id)`).
2. **Payables stamp read-back** (`supplier_payments/separation.py`, `service._read_back_stamp`): after every payables
   write the row's `separation_enforced` stamp is compared with the policy. At preparation a mismatch refuses
   (`sod_stamp_mismatch`, nothing saved); at a later step it is flagged (`erp.finance.supplier_payment.sod_stamp_flagged`)
   and payables still enforces the stricter value.
3. **Payroll stamp-or-policy**: payroll has no stamp column, so ERP records the value it passed as
   `separation_enforced` in the details of its own run events (`erp.payroll.run.approved`,
   `erp.payroll.run.finalized_posted`, `erp.payroll.payout.line_returned`).
4. **Saved check**: `python -m erp_v2.governance.sod_verification --tenant-id <uuid>` (read-only, exit 3 on findings)
   lists payables rows stamped relaxed and payroll events recorded relaxed that were created while the policy was ON
   (the ON windows come from the `erp.governance.dual_control_set` audit events).

Payables a7 is pinned (`dotmac-payables==0.1.0a7`). `supplier_payments/service.py` passes
`separation_of_duties=governance.separation_of_duties(db, tenant.id)` explicitly at all four call sites (signature drift
is a TypeError; the former signature-sniffing shim is deleted) and the stamp read-back is unconditional.
pa_0007 backfills existing rows `separation_enforced = true`: with the policy OFF those rows write a `sod_stamp_flagged`
event on every later step and still need two people, and replaying an old prepare raises Conflict. The saved check
reports a read-only `pa_0007_stamped_true` count (payments stamped true whose ERP `prepared` event is missing or outside
every ON window); record it in the register at the pin. It is a count, never a repair.

Payroll stamp-or-policy: a later step enforces separation when the live policy is ON **or** an earlier step of the run
recorded `separation_enforced` ON (batch calculated, approved), so turning the policy OFF between steps never relaxes a
run governed while ON. Settlement reversal is stricter than ERP's returner guard: tenant policy OR the line's own stamp.

The saved check refuses (exit 2, `policy_timeline_inconsistent`) when the policy row disagrees with the
`dual_control_set` audit timeline (for example the policy is ON with no event), counts in-window payroll events that
lack the stamp as findings, runs in a READ ONLY transaction and uses the database clock only.
