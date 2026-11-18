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

## Not covered here

Starter's own checks (payables a5 approve/record/reverse, payroll a5 creator/approver/finalizer/reverser,
`ck_supplier_payments_sod`) are untouched and ride in the payables a6 and payroll a6 releases. Until those are
pinned the owner still refuses, for example, an approver who prepared the same supplier payment, so a single person
cannot yet complete B3, B4 or B5 end to end even with the policy off; the ERP-side codes no longer fire.
