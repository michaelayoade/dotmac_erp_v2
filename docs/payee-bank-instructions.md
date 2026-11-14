# Payee bank instructions (B8): two-person approval, import, declared parity delta

Rulings: register W10 (ERP v2 e2 table, two-person approval for any change, import legacy bank
details) and W14 (admin gains nothing). Constraints: REVIEW-P0 F7 (treasury never approves bank
details). Prerequisite of B3 supplier-payment and B5 payroll-payout bank files.

## Model

`payee_bank_instructions` (migration `e2_0014_payee_bank_instructions`, additive, one table).
One row = one proposed instruction for a supplier or employee. The approved row is the single
active destination for the payee.

```
proposed -> approved -> superseded   (a later proposal for the same payee is approved)
                     -> revoked      (finance_approver, reason required)
proposed -> rejected                 (finance_approver, reason required)
```

- Governed commands only (`erp_v2.payees.service`): propose, approve, reject, revoke,
  reveal_account_number (audited), destination_for_payment (B3/B5 file generation).
- Two-person rule in code AND in the database: `proposer != approver` (service refusal
  `same_person`; CHECK `ck_payee_bank_instructions_two_person`). Approval supersedes the previous
  approved row in the same transaction; until then the old destination stays in force.
- One approved and one proposed row per payee (partial unique indexes).
- Instruction content is immutable: the runtime role `app_user` has SELECT/INSERT and UPDATE on
  lifecycle columns only, no DELETE (asserted from the PostgreSQL catalog in CI). A change is
  always a new proposal.
- Effective date is the approval time; there is no future-dated activation (a future-dated row
  would otherwise leave a payee with no effective destination). Raise it if Finance needs it.
- Strict approval validation: bank code 3-6 digits, account number 10 digits (NUBAN format; the
  check-digit algorithm is not enforced), account name present. Refusals name fields, never values.

## Permissions and roles (profile `erp.web` v3 -> v4)

| Permission | Roles |
|---|---|
| `erp.payees.bank_details.read` (masked list) | finance_preparer, finance_approver, treasury |
| `erp.payees.bank_details.propose` | finance_preparer |
| `erp.payees.bank_details.approve` (also reject) | finance_approver |
| `erp.payees.bank_details.revoke` | finance_approver |
| `erp.payees.bank_details.read_unmasked` (audited, reason) | finance_approver |

admin and staff gain nothing (tests). Treasury can see the masked list only: it cannot propose,
approve, reject, revoke or see a full number, and the role SoD pairs (finance_approver+treasury,
finance_preparer+finance_approver) stop one person holding approve together with treasury or
propose. Routes are dormant until the roles have holders (access_cli).
Treasury's file generation (B3/B5) will read destinations through `destination_for_payment`
under its own `disbursement_files.generate` permission, not through the UI.

## Account number handling

Stored in the account_number column (PII). It never appears in `repr`, logs, audit details,
refusal text, importer blockers or the importer receipt. UI shows `******1234`; the full number
is shown only by the audited `reveal` action (reason required, `no-store`, finance_approver).
Audit actions `erp.payees.bank_details.{proposed,approved,rejected,revoked,unmasked_read}` carry
payee ref, last four digits and (for approvals) the superseded id and a shared-account count.
Not done: field-level encryption at rest (needs key custody; open question).

## Legacy import (importer mint step `payee_bank_instructions`)

Retained legacy fields, no new tables read: `employee.bank_name`, `bank_account_number`,
`bank_account_name` and `supplier.bank_details` (JSON: bank_code, bank_name, account_name,
account_number). Every row lands `proposed`, origin `legacy_import`, no proposer, evidence
`legacy-import:<source>`; a finance_approver approves each one individually (no bulk path).

- Employees only if their destination `mod_people.employees` row was imported (blocker by id).
- Legacy employee rows have a branch code, not a bank code: `bank_code` stays NULL, so such a row
  cannot be approved as imported (`incomplete_instruction`); Finance proposes corrected details.
- Supplier rows with only `account_number_last4` mint nothing (no full number to propose).
- Payees with an existing proposed/approved row are counted `existing`; reruns are idempotent.
- Supplier `payee_ref` = legacy supplier id, canonical dashed lowercase UUID. Confirm this
  spelling equals `mod_payables.supplier_invoices.supplier_ref` on the run-f copy before B3.

## Declared delta to the step-3 parity baseline

Baseline (dest6, docs/rehearsal.md): written 1,344,782, exceptions 65,118, GL exact (108 accounts,
ledger 446,679), AP reconciliation byte-identical.

Declared delta for the re-freeze run 'g' (dest6 + B8):

1. Destination heads gain `e2_0014_payee_bank_instructions` (EXPECTED_HEADS).
2. Spec-row `written` and `exceptions` are unchanged: the payee rows come from a mint step and
   are reported in the receipt `mints` list (step `payee_bank_instructions`), not in `written`.
3. New expected receipt evidence: `payee_bank_instructions` created = (employees with a full
   account number whose destination row exists) + (suppliers with a full account number in
   `bank_details`), all status `proposed`, origin `legacy_import`, approved = 0, rows with an
   approved status after import must be 0. Exact N is a census result at run 'g', not a
   constant here; blockers are counts of employees not yet imported / over-long fields.
4. GL, AP reconciliation, identity and payroll parity are unaffected (no accounting or payables
   writes). No payment, bank call or external effect.
