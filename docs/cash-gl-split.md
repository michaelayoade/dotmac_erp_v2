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
debit-normal ASSET account in the bank account's currency with no journal line at all, posted or
draft (`target_has_activity`: re-point FIRST, then post the reclassification journals); it is not already any bank account's cash
GL; the bank account has no approved or in-flight B7 reconciliation (re-point happens before
baselines); and no B3 supplier payment (DRAFT, APPROVED) or B5 payout batch (draft, approved,
file_issued) for that bank account is unposted. Approval also refuses if the bank account's reference
changed since the proposal. Approval writes `mod_banking.bank_accounts.cash_account_ref` as the
canonical account id and records the request as `approved` in the same transaction; both decisions are
audited (`erp.banking.cash_gl_repoint.*`). The request table is insert-only for the runtime role
(decision columns only, no DELETE).

B3 reads the payer references (`cash_account_ref`) when a payment is PROPOSED and stores the cash
account on the payment; recording and any later reversal use that stored reference, not the bank
account's current one. B5 reads the bank account's reference at proposal and again when a batch is
recorded. Because a re-point is refused while any B3 payment or B5 batch for that bank account is
unposted, no in-flight record can straddle it, and every new proposal picks up the new account. The
payer lookup takes a FOR SHARE lock on the bank account row, so a payment cannot be proposed against
the old reference while an approval commits (lock order: bank account row, then GL account rows).
RECORDED payments keep the account they posted to. The payroll a5 return path (a payout returned by
the bank) should mirror the ORIGINAL GL of the payout it reverses, not the bank account's current one;
until that is built, a return of a pre-split payout lands on the old shared GL and is handled by step 4.

Both banks of each shared pair (1210 and 1211) are re-pointed, each to its own new account. B7
refuses a GL that any approved re-point left (`retired_cash_gl`), so the partner left on a
half-split GL cannot be reconciled, and a retired GL cannot be chosen as a re-point target
(`target_retired`).

## Steps 3 to 5: Finance, at the baseline date D (not built here)

Order: re-point first (step 2, the new accounts are empty), then the journals below. Open an approved
B7 baseline for a bank account only after its reclassification (step 3) and the joint reconciliation
(step 3b).

3. Corrections first. Bank-side items missing from the books (ruling D1: a bank charge or receipt
   the books never recorded) are posted as B2 journals to the SHARED GL, dated on or before D, before
   any reclassification, so the shared GL at D is complete.
   Then, for each re-pointed bank account, Finance posts one B2 reclassification journal at D for that
   bank's statement balance (debit the new account, credit the shared one), with evidence, so the
   baseline reconciles with zero reconciling items.
3b. Joint reconciliation of the shared GL at D. Evidence for each pair: a reconciliation of the shared
   GL at D that lists the outstanding items (cheques, deposits in transit) of EACH bank, whose sum
   equals exactly the balance left on the shared GL after BOTH reclassifications. Anything unexplained
   is a Finance finding to resolve before baselines; no plug and no allocation by software.
4. Items stay on the shared GL until they clear. Outstanding items at D (and postings after D) stay on
   the shared GL and Finance moves each with a B2 transfer journal as it clears, i.e. when a bank line
   for it appears on the new account's bank statement. This also covers postings after D:
   - reversals of pre-split payments (they post to the old GL; transfer only when a bank line appears on
     the new account), and
   - held legacy drafts (W13) on 1210/1211, posted one at a time and transferred the same way.
5. The residual on the old shared GL must run to zero. Finance reviews it monthly; it is a finding, not
   a plug.
