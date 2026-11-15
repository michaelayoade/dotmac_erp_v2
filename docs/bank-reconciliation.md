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

## Not covered / carried forward
* Banking a1's own statement `is_matched`/`reconciliations` tables are not touched; B7 keeps its own
  state. Statement lines imported from legacy (18,187) arrive unmatched: they need matching or items
  against legacy-imported ledger lines before the first approval.
* Foreign-currency bank accounts are refused (ledger functional legs only).
* Reconciliations are not forced to be sequential by period.
