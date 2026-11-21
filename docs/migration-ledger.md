# Migration ledger: opening, IAS 8, bridge, exceptions register (B-9, `e2_0028_migration_ledger`)

Rulings: Tier C closed years (FY2025 and earlier) land as a reviewed opening balance with IAS 8 corrections forward
against 3100; legacy history stays read-only. The bridge is itemised per account x period with no materiality
threshold; Michael signs each sha-bound version. The flip gate is profile `as_is_v1`
(AS-IS-MIGRATION-REPLAN sections 1 and 4, sha a28adf06...): the latest bridge signed and every gated control tie exact.
The open work-queue count, class coverage, 2030, 2110 and the opening control differences are REPORTS (Michael's as-is
ruling: legacy lands as recorded; Finance corrects after the flip). The profile and its lists are code constants; no
setting can relax them. The B-1 review tools (`legacy_ap/review_cli`, `ias8`) and the B-2 held-draft tools
(`held_drafts/cli`) stay available as post-flip Finance tools and are not in the flip runbook.
Rounding residues (ruling v1640) have no tolerance: each T-ROUND-2DP header delta is a bridge line and is cleared by one
governed correction per period.

Sign convention everywhere: net = debit - credit (assets positive, liabilities negative). All money in files is a
string (a JSON number is refused); duplicate JSON keys are refused. DSN: `MIGRATION_DATABASE_URL` only. Every write
needs the flag `--apply` AND `ERP_MIGRATION_LEDGER_APPLY=1`; dry run is the default. Receipts are 0600, created
exclusively before any read, and carry shas, counts and tie-outs.

## Opening (`python -m erp_v2.migration_ledger.opening_cli gen-tb|plan|apply`; IAS 8 is a post-flip tool)

* plan, apply and `gen-tb` take `--import-receipt` (the applied, complete B-13 importer execute receipt): its
  `history_cutoff` must equal the opening date (`opening_as_of_differs_from_history_cutoff`, refused before any database
  access) and its sha is in the plan sha. Until #74 lands, tests use a fixture in its shape.
* `gen-tb` WRITES the opening TB recomputed from the legacy snapshot (`legacy_ledger.recompute`, P&L rolled into 3100).
  plan/apply REFUSE a declared TB that differs from it (`opening_tb_differs_from_legacy`); the recomputed TB's canonical
  sha is part of the plan sha.
* IAS 8 is empty before the flip: the CLI refuses any item (`ias8_not_allowed_before_flip`). The IAS 8 journal kind
  stays in `apply_opening` as a post-flip Finance tool.
* Inputs, each bound by sha in the spec: the TB at the FY2025 close (`as_of`, one row per account) and the
  itemised IAS 8 items (`item_id`, restated account, signed movement, evidence sha, description).
* ONE system-built opening journal, owner `erp.opening`, one line per account (the spec says "one opening journal
  per account"; a single-account journal cannot balance, so this is one journal with one line per account).
* Each IAS 8 item is its own journal, owner `erp.ias8`: the item's movement on its account, the opposite on 3100,
  dated the first day of the first OPEN, non-adjustment period after the opening (D-3).
* REFUSES, before any database access, only when Σ net is not 0. The control-account difference
  (`TB net + Σ IAS 8 movement - subledger opening`) is REPORT-ONLY: listed per account in `proof.control_differences` as
  E37 info items; the flip holding reclass (B-14) covers them. It never refuses.
* The opening period must be OPEN when the opening posts (accounting rejects posting to a closed period); soft-close and
  lock it afterwards. A second, different TB is refused; a repeat is a replay. Provenance is `erp_opening_journals`.
* Both journals are posted through accounting a2 (create + post) under their own source owners, not through the B2
  manual-journal path, so the manual control guard is not the door; the control equality above is.

* The subledger openings for 2000, 1400, 2040 and 1410 are RECOMPUTED from the legacy snapshot at the opening date
  (`legacy_subledger.py`, `LEGACY_DATABASE_URL`, read-only; recipes in MIGRATION-REVIEWED-CONVERSION 12.3b-addendum:
  "posted by D" journals, allocations dated on or before D, claims net of reimbursements; payroll net pay and staff loans
  are 0 and refused if the snapshot says otherwise). Each GL - subledger difference is reported (E37), not refused. The g9 figures are pinned by a test that skips without a snapshot.
* OPENING P&L roll-up (12.3d): income and expense accounts carry 0 in the bridge's OPENING legacy side and their
  cumulative net to the opening date is rolled into 3100.

## Bridge (`python -m erp_v2.migration_ledger.bridge_report build|sign|rounding`)

Inputs (one file, version 1): the legacy net per account x period (the E1 legacy side), the universe of legacy refs,
and itemised `carry` / `transform` / `queue` items (rule, scope `header` or `line`, legacy and v2 amount, evidence
sha), plus the Finance-named `rounding_account_code`. The `OPENING` period is NOT in the file: its carry is the
posted opening journal's ledger net and its IAS 8 column the posted IAS 8 journals.

    legacy     = carry + transform(legacy) + queue          (partition gap; must be exactly 0)
    v2 planned = carry + transform(v2) + ias8 + rounding
    difference = v2 planned - legacy = Σ transform delta + ias8 + rounding - queue - gap

* Partition proof: every legacy ref appears in exactly one bucket and the items' refs equal the universe.
* `ready` only with no gap (any size), no queue and a proven partition. A not-ready version is stored but cannot be signed.
* A version is the canonical body and its sha256; the itemised file is written 0600 (its sha is in the body).
  `build` stores a new version, replays the latest when the sha is identical, and refuses a sha an older version had.
* `sign` (Michael): only the latest version, sha-bound, `ready`, body re-hashed, and every planned rounding journal
  posted. The database allows only the one-time signature; a signed version is immutable and never deleted. A new
  version always needs a new signature.
* Adapter for #68: its itemised `bridge_lines` carry no period or account for header lines; the feeder maps header
  lines to account 2000 in net terms (credit negative) and adds the invoice period.

## Recompute, not self-attestation (architect addendum, MIGRATION-REVIEWED-CONVERSION s12)

* The legacy side is recomputed from `gl.posted_ledger_line` through `LEGACY_DATABASE_URL` (read-only): `account_id`,
  functional `debit_amount` / `credit_amount`, `posting_date` (period `YYYY-MM`), `journal_entry_id` (the universe of
  FY2026 refs). Keyed by account id, shown by the current v2 code. The item file only CLASSIFIES refs; its sums must tie
  exactly to the recomputed totals or the partition gap shows.
* The v2 side is the whole destination ledger (`ledger_check.v2_posted`). Per account x period the difference
  `recomputed v2 - (planned v2 + v2-native postings)` must be 0: `sign` refuses `v2_ledger_differs_from_bridge`, and so
  does the gate. A header T-ROUND-2DP line changes the subledger only (GL delta 0).
* `bridge_inputs.py` is the #68 adapter: period from the invoice journal's posting date, account by id (must be 2000),
  invoices never legacy-posted are excluded from the residue. Rounding items must fingerprint-match a header line of the
  #68 itemised file bound to the receipt `itemised_sha256` and the snapshot; |delta| < 0.01; |period residue| <=
  0.01 x invoices; the bridge must be `ready` to anchor a correction.

## Gate (`register.gate`)

Profile `as_is_v1`. Passes only when: the latest bridge is signed (its body re-hashes and records
`gate_profile = as_is_v1`); the recomputed v2 ledger equals the bridge and has not moved since signature
(`ledger_moved_since_signature`); and every control account in `GATE_TIE_CODES` (1400, 1410, 2000, 2040, 2045) is tied:
GL vs the live owner subledger for 2000 (payables obligations less approved credit notes), 2040 (payroll liabilities)
and 1410 (staff-loan book position). 1400 and 2045 have no owner subledger yet and REFUSE with
`control_subledger_unavailable:<code>` (1400 needs the billing a13 R1 read, 2045 expenses a2), so the gate cannot pass
in production until those land.

REPORTS (never gate reasons, in the `reports` block of the gate output): open work-queue counts by class, class coverage
(`classes_missing`; `classes.CLASSES` is the E01-E34 inventory plus E35, E36a, E36b, E37, E38), the 2030 balance
(`REPORT_ZERO_CODES`) and the 2110 balance (`REPORT_WHT_CODES`). `profile_checks_pending` lists section-4 checks that
need owners not on main (per-module completeness, holding ties, prepayment tie against R1, keyed-account/period setup,
writer role): every check is an entry of `register.AS_IS_V1_CHECKS` (name -> function, or None while unbuilt; the name set is
pinned to spec section 4 by a test). Each None REFUSES the gate (`profile_checks_unbuilt`); an owner PR replaces its None
with the check. The pending list is: per-module completeness, `control_ties_per_currency` (the inline ties net all
currencies), prepayment tie, holding ties, keyed accounts, control/tax codes exist once, VAT deferral pair, periods,
writer role, `straddle_reproduced` (STRADDLE-SPEC W8: every E39 `journal_straddles_cutoff` journal has its B-14
`migration_straddle` journals; B-14 wires it) and `e15_subkobo_posted` (B-13 review N1: every E15 sub-kobo key has its
2-dp line on the rounding account; the posting itself is not built, see the PR). `migration_straddle` journals (owner
`erp.migration`) are excluded from the bridge's native side. So are `migration_subkobo` journals (the E15 sub-kobo journals,
E15-AND-SIDEFILE-RULING as amended). `bridge_report build --subkobo-side-file F --straddlers-side-file G --import-receipt R
--import-receipt-sha256 <sha>` derives `T-SUBKOBO` (E15) and `T-STRADDLE` (E39) items ONLY from #74's real side files
(`migration_ledger/units.py`): the file's sha must equal the receipt's `history_cutoff_census.{subkobo|straddlers}.
side_file_sha256`, the receipt's own sha is pinned by `--import-receipt-sha256`, the cutoffs must equal the receipt's and
the opening's, and `post_net` (nested `{day: {account_id: net}}`), nets and evidence are rebuilt from the lines. Accounts
are mapped from `account_id`. Per unit: one line item per (account, period), legacy = v2; E15 adds one residue item on
(rounding account, journal period). An E39 unit must be type S2 with s_j 0 and balanced days (else
`straddle_not_simple`). A ref sits in one bucket, so the inputs carry no other item for these refs. The bridge refuses
(`unit_items_forged`, `subkobo_items_differ_from_rounding_cell`, `rounding_account_invalid`) unless the items equal the
derivation and the residues sum to the rounding-cell difference. The signed body records `import_receipt_sha256`,
`subkobo_side_file_sha256` and `straddlers_side_file_sha256`. Built: `post_flip_tools_unused` (the gate refuses `post_flip_tool_used_before_flip` if any B-1 review, any
non-queued B-2 decision or any IAS 8 opening journal exists). Pending probe P-7: the opening posts functional currency
only, so foreign-currency control positions at D need confirming before B-14. The lists are code constants in `ledger_roles`
and recorded in each bridge body; the ledger and register state at signature is stored with the signature.
`sign` takes `approval_reference` = `dotmac-debt-register v<N>` (format-checked only).

## Rounding residue (`bridge_report rounding`; `journals.service.prepare_rounding_correction`)

Per period, residue = Σ T-ROUND-2DP header deltas (v2 - legacy net, all on 2000). One system-built journal per
period, Dr/Cr 2000 against the Finance-named rounding account (required; refused if missing, if it is 2000 or a control
account, or if the bridge names none). Source identity `erp.manual / rounding_residue_correction / <bridge sha256>:<YYYY-MM>`
and a uuid5 request id, so it posts at most once. Dated the start of the invoices' period if open, otherwise the first
open period. It runs the ordinary prepare / request / approve path (dual control per tenant policy, default off). The
control guard admits 2000 for this kind (like the section 3c ledger fault) only after `rounding_verify.verify`
re-proves, at request and approval, that the journal equals what the LATEST bridge version says is due net of
corrections already posted. A superseded bridge, a changed amount or a malformed identity is refused.

## Exceptions register (`python -m erp_v2.migration_ledger.exceptions_register load|resolve|gate`)

`load` reads every listed receipt through its sha: the B-2 APPLY receipt (queued drafts and E17 findings open; voided
and posted drafts resolved; a plan receipt is refused), the importer itemised file (every queued invoice open; an
unknown queue reason is refused, never guessed) and the B-1 admission-review rows. Reconcile: a resolved item stays
resolved while its fingerprint is unchanged and REOPENS when the legacy facts change; an open item that its source no
longer lists is reported as an orphan and stays open. `resolve` closes an item with a terminal outcome
(`posted`, `admitted`, `voided`, `written_off`, `no_action`) and an evidence sha. `gate` exits 0 only when the latest bridge
version is signed and the control ties hold (open counts are reported).

## Not covered (by design or follow-up)

* The v2 side of the 2000/2040/1410 gate ties uses the owner tables; 1400 and 2045 refuse (no owner yet).
* `expense.cash_advance` at the opening date is not queried (the recipe says 0; its column names were not confirmed).
* The recipes were written to the architect's column names and tested on synthetic snapshots; only the g9 pin test (skipped
  without a snapshot) proves them on real data.
* Receipts of other builds (B-1 plan receipts, settled admission) enter through the admission-review rows only.

## History cutoff (B-13) and the importer

`python -m erp_v2.importer execute ... --history-cutoff 2025-12-31` is required whenever the journal pairings run (the
cutover profile always includes them; the CLI refuses before touching either database without it, and the executor
raises without it). The journal, journal-line and posted-ledger-line pairings do not read legacy rows dated on or before
the cutoff: closed years arrive only through the opening journal above (its TB is the cumulative legacy balance to the
cutoff, so copying the same lines too would count them twice). Journals are dated by `COALESCE(posting_date,
entry_date)`, journal lines follow their parent journal, posted ledger lines use their own `posting_date` (the column
the opening TB is computed from). An undated row is not provably closed, so it lands and is counted. A journal whose
ledger lines straddle the cutoff refuses the run. The receipt's `history_cutoff` section is the census:
`legacy = landed + excluded + refused` per pairing, with a sha256 over the excluded legacy keys (the legacy journal ids
for the journal pairing). No destination document table references the journal tables (pinned by a test), so a
pre-cutoff document keeps its legacy journal id as evidence in the retained snapshot and is neither excluded nor
rewritten. PG test: `tests/test_importer_history_cutoff_pg.py` (v2 holds no legacy-owner line on or before the cutoff;
opening + post-cutoff journals = the legacy TB at cutoff + 1 day).

### P-5: the sub-kobo (unbalanced) journals

Ruling: each journal whose posted ledger lines do not balance (the 3 sub-kobo journals, 0.000001) is refused as a UNIT
(journal, journal lines, ledger lines): nothing of it lands. Landing it would leave a DRAFT carrying ledger lines (the
owner trigger forbids posting it and forbids voiding it). It is not a row exception, so F2 does not exit incomplete: it
is itemised (key, debit, credit) in the receipt's `history_cutoff_census` and in the side file under
`E15_unbalanced_journal_unit`, for the E15 register item. Its 2-dp effect and the 6-dp residue go through the rounding
path (B-9r). If such a journal were dated on or before the cutoff, the opening would refuse `opening_tb_not_zero_sum`.

### Receipt and side file

* `history_cutoff` (top level of the execute receipt): the ISO date or null. B-9r requires it and refuses a mismatch with
  the opening as-of date.
* `history_cutoff_census`: per pairing, source rows, excluded count, `excluded_keys_sha256` (keys ordered byte-wise,
  `COLLATE "C"`), excluded by legacy status, undated refusals, unbalanced-unit counts, reversal-target count.
* `<receipt>.history_cutoff.json` (mode 600, sha256 in `history_cutoff_census.side_file_sha256`): pre-cutoff
  non-posted journal keys split by legacy status (`pre_cutoff_journal_draft`, ... for E16b), `E15_unbalanced_journal_unit`,
  `reversal_target_before_cutoff` (post-cutoff reversals whose legacy target is pre-cutoff: they land as is, link empty,
  info register rows, not counted against completeness) and `undated_*`.
* Undated rows (no `posting_date`/`entry_date`) are named row refusals `history_cutoff_undated`;
  `legacy_ledger.recompute` refuses a NULL-dated ledger line instead of omitting it.
* Straddlers (STRADDLE-SPEC, W8): a journal crossing the cutoff is classified from its posted ledger lines (S1: lines on
  both sides; S2: lines on one side, journal date on the other) and excluded as a UNIT (journal, lines, ledger lines) with
  class E39 `journal_straddles_cutoff`, whenever any cutoff pairing runs. It is not a row exception, so F2 completes, and
  it is not counted as a plain pre-cutoff exclusion. The mode-600 side file `<receipt>.straddlers.json` (sha256 in
  `history_cutoff_census.straddlers`) lists, per journal: type, legacy status, journal date, source module/document,
  currency, `pre_net` (already in the opening), `post_net` per day and account, `s_j`, the ledger lines and an evidence
  sha256. The run refuses before any write if the side file cannot be written or if the classifier and the exclusion
  predicate disagree. The reproduction journals (T-B) are B-14; the opening/bridge/gate share is B-9r (the observed straddlers all have
  S_j = 0, so no opening suspense row is needed).
* AP verifiers: a journal the cutoff excluded is proven on the snapshot (same predicate, the receipt cutoff carried in the
  transform manifest binding as `history_cutoff`, all ledger lines on or before it), asserted ABSENT in v2, and a POSTED
  `erp.opening` journal dated exactly the cutoff must exist in v2 (one read, every path).
* E15 side file `<receipt>.subkobo.json` (mode 600, sha in `history_cutoff_census.subkobo.side_file_sha256`): the
  unbalanced journals in the straddlers shape (`{history_cutoff, class: E15, reason: unbalanced_journal_unit, journals}`),
  with full ledger lines, `post_net`, `net`. `history_cutoff.unit_evidence_sha(lines)` is the one shared evidence sha
  (E39 and E15; B-14 imports it).
* A reversal whose target is a cutoff-excluded, E15 or E39 journal lands with the link empty (target kept as evidence).
* A failed post-commit write of the side file is caught: the receipt is still written and the run is marked incomplete.
* A transform manifest with no `import.unposted_wht` in its binding is a pre-B-13 manifest: `remove` is the only legal
  default (the binding is under the manifest sha256).
