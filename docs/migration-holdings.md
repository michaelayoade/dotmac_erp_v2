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

## Review hardening (architect review of 6b4adec)

* F1: T1 and the holding plan anchor the census to the `erp_ar_opening_admissions` rows (census digest = the receipt's own
  `census_sha256`, one manifest fingerprint, admitted partitions == census parties carrying items or cash lots); T1 also
  compares `BillingReader.window_manifest_fingerprint` (refuses `manifest_fingerprint_unavailable` until the a13 reader).
* F2: `window_already_applied` once any `migration_holding` identity of the window is posted from another plan.
* F3: apply re-reads the owner subledgers inside its transaction. F4: a release refuses `release_dual_control_unbuilt` while D-9 is ON.
* F6: every explained (held) row must match an OPEN register exception, with an equal amount where the source records one.
* L3/L4/L6: TRUNCATE refused; a `migration_holdings` item cannot be resolved with an outstanding amount; resolved holding accounts are
  persisted (`erp_migration_holding_accounts`) and guarded even with no rows. L5: verify checks the prepayment account has not moved.

## Unit reproduction: straddle (E39) and sub-kobo (E15)

`holding straddle|subkobo plan|apply|verify` (module `holdings/units.py`) reproduces the legacy journals the importer
refuses as UNITS. Input is #74's own side file, exactly as written (`<receipt>.straddlers.json`, `<receipt>.subkobo.json`;
top level `{history_cutoff, class, reason, journals}`), bound in the run-spec as `receipts.straddle` / `receipts.subkobo`
together with the import receipt `receipts.import_receipt`: the side-file sha must equal
`history_cutoff_census.{straddlers|subkobo}.side_file_sha256`, the cutoff equals the receipt's and the posted opening's as-of
date, the journal count (E39) or set (E15) equals the receipt's, `evidence_sha256` is recomputed through the shared evidence
function, and nets, type, `s_j` are recomputed from the lines.

* **straddle** (`migration_straddle`): one balanced journal per ledger-line day at the legacy account, amount and date, never
  suspense. Refuses a legacy journal that is not POSTED, S1 or any line on or before the cutoff, a non-zero net (total or per
  day), control, holding (persisted or run-spec), tax-role and prepaid-funding accounts, a non-functional currency, and a day
  whose period is not OPEN. Item: E39 OPEN (review only).
* **subkobo** (`migration_subkobo`): every legacy line re-posted exactly (6 dp) plus ONE residue line on
  `migration.rounding_account` (run-spec param), on the legacy day. Refuses lines on more than one day, lines on or before
  the cutoff, a net outside 0 < |net| < 0.01, `subkobo_control_line`, a closed period. Item: E15 RESOLVED with #74's evidence.
* Window closure: `<mode>_already_applied` if any journal of the kind exists from a different plan.
* The e2_0028 exceptions table carries a close-guard trigger from e2_0030 (a `migration_holdings` item with an outstanding
  amount cannot be resolved).

## Not in this change

The gate wiring (`holdings.ties.holding_tie` and the `verify` receipt as gate items 2/3, class rows), the control-code
tightening and the supplier-invoice V1 guard adoption (S9), the AR release (R8).
