# Cash GL split (one cash GL account per bank account)

Michael's ruling, "Split the GL accounts": GLs 1210 and 1211 are each the cash GL of two of the 13
bank accounts, and B7 refuses to reconcile a shared GL (`shared_cash_gl`). Each bank account gets its
own cash GL account. This release builds steps 1 and 2; steps 3 to 5 are Finance actions through the
existing B2 manual journals and B7 baselines. No code here posts a journal, moves a balance or
backfills anything. B7 keeps its `shared_cash_gl` refusal until the split is done.

## Step 1: create the per-bank cash GL accounts (operator command)

`python -m erp_v2.cash_gl.setup_cli --spec SPEC.json --receipt RECEIPT.json` with
`MIGRATION_DATABASE_URL` set (never the serving DSN). Plan by default: a READ ONLY transaction that
writes nothing and reports `create` or `reuse` per account. Apply needs `--apply`,
`ERP_CASH_GL_SETUP_APPROVED=1` and `--approved-spec-sha256` equal to the sha256 of the reviewed spec.

Spec (version 1): `tenant_id`, `destination_database`, and 1..13 `accounts`, each
`bank_account_id`, `code`, `name`, `category_id` (an active ASSET category), `parent_id` (null or a
non-posting ASSET account) and `currency_code` (null or 3 upper-case letters). Accounts are always
POSTING, DEBIT-normal and postable, created with accounting a2 `create_account` (a1 is refused).
Re-running is idempotent: an account that already matches exactly is reused; any difference, an
unknown or closed bank account, a currency mismatch, or an existing account already used as another
bank account's cash GL refuses the whole run. Creating accounts does not re-point anything.

## Step 2: re-point a bank account (two people)

Page `/finance/banking/cash-gl-repoints`. `finance_preparer` proposes (bank account, new account
code, evidence reference); a different `finance_approver` approves or rejects. New permissions (the
only addition, profile `erp.web` 4 to 5, same P0 role pair as journals, SoD pair
`finance_preparer`/`finance_approver` keeps them two people): `erp.banking.cash_gl_repoint.propose`,
`erp.banking.cash_gl_repoint.approve`. Dormant until roles are granted (`access_cli`).

Both steps (and approval again, under locks) refuse unless: the target is an active, postable,
debit-normal ASSET account in the bank account's currency; it is not already any bank account's cash
GL; the bank account has no approved or in-flight B7 reconciliation (re-point happens before
baselines); and no B3 supplier payment (DRAFT, APPROVED) or B5 payout batch (draft, approved,
file_issued) for that bank account is unposted. Approval also refuses if the bank account's reference
changed since the proposal. Approval writes `mod_banking.bank_accounts.cash_account_ref` as the
canonical account id and records the request as `approved` in the same transaction; both decisions are
audited (`erp.banking.cash_gl_repoint.*`). The request table is insert-only for the runtime role
(decision columns only, no DELETE).

B3 and B5 read the payer references from `cash_account_ref` on every proposal and record, so they use
the new account from the next payment. The payer lookup takes a FOR SHARE lock on the bank account
row, so a payment cannot be proposed against the old reference while an approval commits (lock order:
bank account row, then GL account rows). RECORDED payments keep the account they posted to; a later
reversal of one posts as its own B3 rules say and shows on the old shared GL, to be cleared by step 4.

## Steps 3 to 5: Finance, at the baseline date (not built here)

3. For each re-pointed bank account, Finance posts one B2 reclassification journal at the baseline date
   for that bank's statement balance (debit the new account, credit the shared one), with evidence, so
   the opening B7 baseline reconciles with zero reconciling items.
4. Outstanding items at the baseline date (cheques, deposits in transit) stay on the shared GL. As
   each clears, Finance moves it to the right account with a B2 transfer journal.
5. The residual on the old shared GL must run to zero. Finance reviews it monthly; it is a finding,
   not a plug: no allocation is invented by software.
Open an approved B7 baseline only after step 3 for that bank account.
