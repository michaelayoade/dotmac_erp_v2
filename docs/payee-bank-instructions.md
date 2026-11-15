# Payee bank instructions (B8): two-person approval, import, declared parity delta

Rulings: register W10 (ERP v2 e2 table, two-person approval for any change, import legacy bank
details) and W14 (admin gains nothing). Constraints: REVIEW-P0 F7 (treasury never approves bank
details). Prerequisite of B3 supplier-payment and B5 payroll-payout bank files.

## Model

`payee_bank_instructions` (migration `e2_0015_payee_bank_instructions`, additive, one table).
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
- Strict approval validation (also the test for a complete legacy row): bank code 3-6 digits, account number 10 digits (NUBAN format; the
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

## Review changes (REVIEW-B8, applied on PR #39)

- R2: the payee routes are NOT facet entry routes (entry routes skip the `erp.web.access`
  admission check). A finance role without staff/admin is refused on every route (tested).
- R3: `finance_approver + treasury` is non-waivable (`NON_WAIVABLE_SOD_PAIRS`): an allowlist
  `sod_exceptions` entry for it is refused at parse (`sod_pair_not_waivable`).
- R4: ruled by Michael (v1294): legacy bank details are migrated as approved when complete, with
  no human approval and no verification note (see Legacy import).
- R5: a human proposal replaces an open `legacy_import` proposal in the same transaction (old row
  `rejected`, note `replaced by <id>`, audited with `replaced_by`); the replacement still needs a
  second person. An open manual proposal is never replaced.
- R6: payee identity comes from the systems of record, never free text: employees from
  `mod_people.employees` (tenant, canonical dashed UUID; label = code + person name), suppliers
  from the payables supplier reference set (`supplier_invoices.supplier_ref`; a supplier with no
  invoice cannot yet have an instruction because v2 has no supplier master). Unknown payees are
  refused at propose and again at approve; the approver sees the system-of-record name.
- R7: Postgres tests run as a NOBYPASSRLS member of `app_user` (lifecycle, UPDATE of content and
  DELETE/TRUNCATE denied, other tenant sees nothing). Run 'g' must read back
  `has_column_privilege`/`has_table_privilege` for the real runtime role.
- L1: reveal commits the audit row before rendering the number.
- L5: the importer's deterministic row id collides if a legacy row was rejected and the importer
  is rerun on the same destination (fresh destinations only today).

## Account number handling

Stored in the account_number column (PII). It never appears in `repr`, logs, audit details,
refusal text, importer blockers or the importer receipt. UI shows `******1234`; the full number
is shown only by the audited `reveal` action (reason required, `no-store`, finance_approver).
Audit actions `erp.payees.bank_details.{proposed,approved,rejected,revoked,unmasked_read}` carry
payee ref, last four digits and (for approvals) the superseded id and a shared-account count.
Not done: field-level encryption at rest (needs key custody; open question).

## Legacy import (importer mint step `payee_bank_instructions`)

Michael's ruling (register v1294): legacy-to-v2 bank details are a data migration; approval is
not needed. Sources (retained fields): `employee.bank_name`, `bank_account_number`,
`bank_account_name` and `supplier.bank_details` (JSON: bank_code, bank_name, account_name,
account_number).

- COMPLETE rows (bank code 3-6 digits, 10-digit account number, account name) land directly as
  `approved`, origin `legacy_import`, decided by the declared enrolment actor (`MintContext.actor_id`,
  required; used as the party id if it is a UUID, else a stable uuid5), no human proposer or
  approver. Provenance `source_ref` (`legacy:<source>:<id>`), a sha256 `source_fingerprint` and one
  audit event each (`erp.payees.bank_details.migrated`, last four digits only). The audit write
  is fail-closed: if it fails the whole mint step rolls back and no approved row remains.
- INCOMPLETE rows (every legacy employee row: it has a branch code, never a bank code) land
  `proposed`/`legacy_import` and are never active. Finance corrects them through the normal
  two-person path: a human proposal replaces the open legacy row (R5) and a different person
  approves.
- Every change after migration is two-person: the migration actor is a recorded decider only, not
  a role anyone can use afterwards. No bulk path.
- Employees only if their destination `mod_people.employees` row was imported (blocker by id).
- Supplier rows with only `account_number_last4` mint nothing (no full number).
- Payees with an existing proposed/approved row are counted `existing`; reruns are idempotent and
  write no second audit event. Receipts and blockers carry legacy ids and counts only; the mint
  receipt entry has `details {approved, proposed_incomplete}`.
- Supplier `payee_ref` = legacy supplier id, canonical dashed lowercase UUID; resolver for later
  proposals is the payables supplier reference set (a migrated supplier without invoices cannot
  receive a new proposal until it has one).

## Declared delta to the step-3 parity baseline

Baseline (dest6, docs/rehearsal.md): written 1,344,782, exceptions 65,118, GL exact (108 accounts,
ledger 446,679), AP reconciliation byte-identical.

Declared delta for the re-freeze run 'g' (dest6 + B8):

1. Destination heads: the e2 head moves from `e2_0014_manual_journals` (B2) to `e2_0015_payee_bank_instructions` (EXPECTED_HEADS; the migration's down_revision is `e2_0014_manual_journals`).
2. Spec-row `written` and `exceptions` are unchanged: the payee rows come from a mint step and
   are reported in the receipt `mints` list (step `payee_bank_instructions`), not in `written`.
3. New expected receipt evidence: mint step `payee_bank_instructions` with
   `details.approved` = count of COMPLETE legacy rows (suppliers with a full `bank_details`
   number, valid bank code and account name; no employee row is complete today) and
   `details.proposed_incomplete` = count of incomplete rows (all employees with an account number
   whose destination row exists, plus incomplete supplier rows). `created` = approved + proposed.
   Approved rows are all origin `legacy_import`, decided by the declared migration actor, with
   source fingerprint, and each has one `erp.payees.bank_details.migrated` audit event. Exact N
   is a census result at run 'g'; blockers count employees not yet imported / over-long fields.
4. GL, AP reconciliation, identity and payroll parity are unaffected (no accounting or payables
   writes). No payment, bank call or external effect.
