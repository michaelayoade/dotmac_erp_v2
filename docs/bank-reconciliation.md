# Bank reconciliation (B7)

ERP v2 only, no Starter release, no new permission and no role-profile bump: it uses the P0 codes
`erp.banking.reconciliation.prepare` (`bank_reconciler`) and `erp.banking.reconciliation.approve`
(`finance_approver`). admin and staff hold neither (W14); holders must also be staff (access_cli).
Migration `e2_0017_bank_reconciliation` is additive (four `erp_bank_*` tables, RLS forced).

## What it does
Matches imported statement lines (B6, banking a1) to posted ledger lines on the bank account's cash GL
account (accounting a1 `posted_ledger_lines`; REVIEW-A5 A5-6: never raw settlement or cash
observations). It reads banking and accounting tables and writes only its own tables and audit events.
**No GL writes, no external calls, no Mono.** The cash GL is `bank_accounts.cash_account_ref`, either a
canonical account id or an account code; the bank account must be in the tenant functional currency.

## Lifecycle (per bank account and statement period)
`open` (workspace) -> `prepared` (snapshot, preparer) -> `approved` (a different person). `prepared`
can be returned to `open` with a reason; `open`/`prepared` can be cancelled (frees the period).
One live reconciliation per account and period end (partial unique index).

* **Matching**: statement lines <-> ledger lines of exactly equal net cash effect (N:M groups, up to 50
  per side). Suggestions are exact-amount, within 7 days, reference-scored, and never auto-accepted.
  Matching is idempotent (same member set returns the existing match); one active match per line is a
  database unique index. A ledger-only group is allowed only for a whole reversal pair. Unmatch needs a
  reason and is refused once an approved reconciliation covers the match.
* **Reconciling items** are typed and bound to a line, so no free amounts exist:
  `deposit_in_transit`, `outstanding_payment` (ledger side, timing) and `unrecorded_bank_charge`,
  `unrecorded_bank_receipt` (statement side). Statement-side items BLOCK preparation: the fix is a B2
  manual journal (never posted from here), after which the new ledger line is matched to the statement
  line.
* **Zero-difference gate** (exact Decimal): statement closing balance == GL balance (to the statement end
  date) + sum of items. Every uncleared in-scope statement line and ledger line must carry exactly one
  item. Prepare refuses otherwise; approve re-evaluates under lock and refuses any difference or drift
  of the prepared snapshot. A match whose partner lies after the period end does not clear that period.
* **SoD**: approver != preparer (service, and CHECK), approver != opener (CHECK), and the approver took no
  part in opening, matching or itemizing the scope (service). Role pairs (`finance_approver` +
  `finance_preparer`/`treasury`, `bank_reconciler` + `treasury`) are blocked at grant time by access_cli.
* **Immutability**: an approved or cancelled reconciliation, its items and the matches it covers cannot
  be changed or deleted (triggers; runtime role has no DELETE except items of an open reconciliation).

## Baseline at the flip date (Michael, register v1310)
Each bank account gets ONE opening reconciliation (`kind = baseline`) at a declared date D (the flip date):
open with `baseline_date` = D, anchored to the statement that ends exactly on D; prepare and approve by two
different people; statement balance at D == cash GL balance at D + typed outstanding timing items
(deposit in transit / outstanding payment) bound to real ledger lines. No plug, no statement-side items,
no coverage of history. Any other gap is fixed first by governed B2 journals. Once approved it is immutable
and unique per account (partial unique index). A periodic reconciliation needs the approved baseline and a
period end after it; its scope is statement lines and ledger lines dated after D plus the baseline's
carried-forward items (those ledger lines still need an item until they clear). Matching lines dated on or
before D is refused (`before_baseline`), except the carried ledger lines, which may be matched to later
statement lines. Periods only move forward and one reconciliation per account is in flight at a time;
a statement whose opening balance differs from the previous approved closing balance is a warning.

## Unrecorded bank items in a closed period (D2)
In an OPEN period an unrecorded bank charge/receipt still blocks preparation (post a back-dated B2 journal,
then match). In a closed (soft-closed/locked) period it is allowed only when linked to a live B2 manual
journal dated in the first open period that books exactly the bank line's effect on this cash GL; the
approver sees the link and the next reconciliation matches the bank line to that journal's ledger line.
No free items, no auto-posting, no reopening.

## Shared cash GL and foreign currency
* Two bank accounts on one cash GL (census: GL 1210 and 1211 are each shared by two accounts) are REFUSED
  (`shared_cash_gl`) for open, baseline and matching: the GL balance mixes both accounts and no allocation
  is invented. Finance splits the cash GL (one per bank account, `docs/cash-gl-split.md`) or reconciles that pair outside ERP.
* Non-functional-currency bank accounts (census: one USD account) stay refused; reconcile them outside ERP.

## Ownership and locking
B7 is the single owner of reconciliation state: banking a1 reconciliation/match/observation aggregates and
`is_matched` are never written or read (guard test). Declared debt: extract B7 into Starter banking after
cutover. Global lock order: bank account row -> reconciliation row -> cash GL account row (prepare/approve
lock the GL account so postings serialise against them); never lock a banking account row after posting.
Reconciling items are insert-only; a governed withdrawal (who/when/reason, audited) replaces deletion and
the runtime role has no DELETE on any B7 table.

## Not covered
* Banking a1's own statement `is_matched`/`reconciliations` tables are not touched.
* Backdated postings after approval surface as uncleared lines in the next reconciliation (soft-close the
  period after approval to prevent them).
