# Migration holdings (B-14, e2_0030)

Under the as-is ruling (2026-11-19) legacy records land as recorded. Where a legacy control account (2000, 1400, 2040,
2045, 1410, the AR prepayment account) differs from its v2 subledger, the difference is moved at the flip to a
**holding account**, per control code and currency, **itemised per party**, so control == subledger from day one.
Finance clears the items after the flip. Everything is a system-built journal through the posting bridge; nothing
here is a typed amount.

## Configuration

Run-spec `params` (bound in the plan sha), each an account **code or exact name** resolving to exactly one active,
postable account that is not a control or tax-role account:

| Key | Default |
|---|---|
| `migration.holding_account.2000` | AP migration clearing |
| `migration.holding_account.1400` and `.prepayment` | AR migration clearing (shareable with each other only) |
| `migration.holding_account.2040` and `.2045` | Employee liabilities migration clearing (shareable with each other only) |
| `migration.holding_account.1410` | Staff-loan migration clearing |
| `migration.writeoff_offset_codes` | empty = any postable non-control, non-tax-role, non-holding account |

Runtime kernel settings (`erp_v2/ledger_settings.py`, domain `accounting_policy`): `ar.prepayment_liability_account`
(default "Customer prepayments"), `ar.prepaid_funding_account` (unset: T6). Other sharing refuses `holding_account_invalid`.

## Flip commands (`python -m erp_v2.holdings.cli`)

Dry-run by default; apply needs `--apply` AND `ERP_HOLDING_APPLY=1`; DSN is `MIGRATION_DATABASE_URL` only; receipts 0600.

1. `t1 plan|apply` (F7, first): Dr 1400 / Cr prepayment, the census cash-lot residuals per currency; must equal R1
   `available_credit_minor` (`t1_r1_mismatch`); amount 0 posts nothing.
2. `plan`: RR READ ONLY (GL, owner subledgers, R1), READ COMMITTED read-write ROLLED BACK (billing per-account
   positions), RR READ ONLY again (R1 fingerprint and ledger sha unchanged, else `moved_during_plan`). Output: items, X, journal
   lines, posting date, `plan_sha256` over all of it plus params, receipt shas, window id and the census sha.
3. `apply --plan-receipt P --plan-sha S`: re-plans, requires the same sha, then in one transaction registers the items
   (source `migration_holdings`), posts one `migration_holding` journal per code (gross lines) and inserts one `reclass`
   row per item. 2000/2040/2045/1410 assert control == subledger in the transaction. A replay is a no-op; a different
   plan for the same identity refuses `version_conflict`. Dual control follows `governance.policy.dual_control`.
4. `verify`: control == subledger (1400 and prepayment from R1), GL(holding) == sum of e2_0030 rows, item state vs
   outstanding, every `migration_holding` identity posted once.

## Itemisation

`D_P = GL_P - Sub_P` per party. Items: the party's refused (explained) components, `partygap:<code>:<ccy>:<party>`
(E36a for 1400/prepayment, else E37), `gljournal:`, `sharedjournal:` (never split), `opening:`. Explained items are
keyed `held:<code>:<ccy>:<source legacy key>` and keep the source class (E35 census holds, E36b variances, AP
E02/E03c/E07/E08/E11, claims E37). Refusals: `holding_unitemised` unless the items sum to X exactly,
`holding_fx_unsupported` for any non-functional currency (ledger lines carry no transaction currency).

Inputs are sealed receipts: the #72 census `gl_attribution` (1400 and prepayment) and, per other code, a generic
`holding_gl_attribution` v1 receipt (see `holdings/attribution.parse_gl_attribution`).

## Post-flip tools

* `release --exception <key> --owner-command <file>`: ONE REPEATABLE READ transaction (serialization retry x3). Locks the
  item, previews the owner command, posts `holding_release` FIRST, executes the owner command, re-reads the party (actual
  delta == preview), inserts the `release` row with the owner receipt sha, asserts GL == subledger, resolves `admitted` at 0.
  The owner delta (GL-signed) must have the sign of the item's difference and not exceed it.
  Owners: 2000 and 1410 bind through `release.default_adapters` (until bound: `release_owner_unbound:<code>`); 2040/2045
  `release_owner_unavailable`; 1400/prepayment `ar_release_requires_r8`.
* `writeoff plan|apply`: full outstanding only; offset account allowlist; Dr/Cr offset vs holding; `resolve(written_off)`.
  Dual control per the policy.

## Table `erp_migration_holdings` (e2_0030)

Append-only (trigger holds against the owner role): a `release`/`writeoff` needs the exception's `reclass` with the same
code, currency and holding account, the opposite sign, and the outstanding must keep the reclass sign or reach 0. RLS forced;
runtime SELECT only. Downgrade refuses while any row exists.

## Guard

`journals/service._control_guard` refuses any manual-journal or held-draft line on a holding account
(`holding_account_requires_holding_command`), so a bare journal cannot break the holding tie.

## Not in this change

The gate wiring (`holdings.ties.holding_tie` and the `verify` receipt as gate items 2/3, class rows), the control-code
tightening and the supplier-invoice V1 guard adoption (S9), the AR release (R8).
