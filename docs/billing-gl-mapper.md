# Live billing -> GL mapper (B-15, e2_0031)

An in-process consumer of ERP's own `mod_billing` accounting facts. There is no Integrator: ERP's billing module is the
system of record. Each fact becomes at most one journal through the posting bridge, or a recorded non-posting
disposition. Nothing here reads a legacy system or creates a billing fact.

## Run

    DATABASE_URL=<runtime dsn> python -m erp_v2.billing_gl run --tenant-id <uuid> [--apply] [--enable-once]

- Runs as the RUNTIME role (W4, approved by Michael 2026-11-19); the migration and legacy DSNs are refused.
- **Dry run by default**: without `--apply` AND `ERP_BILLING_GL_APPLY=1` the run happens in one transaction that is
  rolled back and only reports.
- `billing_gl.enabled` defaults to false: the mapper is idle and main stays deployable. `--enable-once` runs one pass
  with the flag still false: it dispositions every fact (opening facts become SKIP for the flip gate) and posts
  nothing. A window holding a fact it could not post is NOT closed, so enabling later revisits it.
- Schedule: every minute. ERP has no scheduled-job mechanism of its own (the cron/timer census is the deployment's), so
  the schedule is an external one-minute timer invoking the command in the existing image.
- Output is counts only (by reason); no identifier or amount is printed. Exit 0 ok, 2 refused, 3 integrity alarm.

## Posting identity

`post_consequence`: owner `billing`, kind `accounting_fact`, document_id = fact id, version = fact version,
fingerprint = fact digest, approval_ref = `billing_gl:<map_digest>`, actor `erp.billing_gl`. `map_digest` is the sha of the
effective mapping settings (accounts, timezone, closed-period policy).

## Dispatch (code constant `billing_gl/rules.py::DISPATCH`)

Lane rule L: `receivable` d>0 Dr 1400, d<0 Cr 1400; `available_credit` d>0 Cr prepayment liability, d<0 Dr;
`prepaid_funding` the same on `ar.prepaid_funding_account` (unset: BLOCK `prepaid_funding_unmapped`). Cash counterpart:
the residual line on the settlement source system's cash account.

| Fact | Disposition |
|---|---|
| V1 settlement_accepted | POST L + cash (Dr cash / Cr prepayment) |
| V1 allocation / deallocation / reallocation | POST L (Dr prepayment / Cr 1400) + the VAT release / un-release (rev 2); empty effects and no VAT NO_GL `lane_neutral` |
| V1 refund | POST L + cash |
| V1 reversal | POST L, + cash when the reversed fact is settlement-family; predecessor must be POSTED. A void of an invoice or credit note is the exact negation of the predecessor's posted journal and VAT movements (rev 2) |
| V1 invoice_issued | POST: Dr 1400 grand total / Cr revenue per line / Cr deferral per treatment (rev 2) |
| V1 credit_note_issued | POST: Dr revenue / Dr deferral min(t, R) / Dr current the rest / Cr 1400; closes out the deferral when open reaches 0 (rev 2) |
| V2, V3 | BLOCK `fact_contract_unreadable` (no public getter) |
| V4/V5 imported | SKIP `opening_gl_carried` if committed at or before the seal, else BLOCK `opening_fact_after_seal` |
| V4/V5 import reversed | SKIP before the seal, BLOCK `opening_unwind_post_seal` after |
| V4 applied, draw reversed | POST L |
| V4 refunded, refund corrected | POST L + cash on `opening_refund` |
| V6 any | REFUSED `v6_ledger_lane_unmapped` (never decoded, so both approval shapes behave the same) |
| unknown (version, kind) | BLOCK `unknown_fact_kind` |

A non-functional currency BLOCKs `billing_fx_unsupported`. An unknown pair, an unmapped account or an unresolvable
settlement BLOCKs with a stable code. To add a mapping, replace one `DISPATCH` entry and, if the lines are not the lane
rule, register a builder in `BUILDERS`.

## Ordering, blocking, healing

Facts are processed in the billing index order. A BLOCK applies per billing account: that account's later facts WAIT
(`waiting_predecessor`) while other accounts continue. Each run retries pending facts first, so a configuration fix heals
them with no staff action. Windows are UTC days up to now - `settle_seconds`; a window closes only when billing's window
summary (count + chained digest) equals the mapper's running `fact_index_line` digest; closed windows of the last
`resummarize_days` are re-summarised every run and a mismatch re-pages (idempotent).

Posting date: `occurred_at` in `posting_timezone`. If that period is not OPEN: `next_open_period` (default) posts on the
first open date after it with the original date in the description; `hold` BLOCKs `period_closed`.

## Completeness, locking and terminal rows (architect review, 2026-11-19)

- **One run at a time.** Each run takes a per-tenant session advisory lock on a dedicated AUTOCOMMIT connection (never idle in transaction); a second run exits
  with `locked: true` and does nothing, so two overlapping runs can never process the same facts.
- **Retry pages everything.** Pending facts are retried per timestamp slice, paging to the end (opening facts can share
  one timestamp), in billing index order.
- **Nothing is silently skipped.** Besides re-summarising recent windows, every run re-summarises the `rescan_batch`
  oldest-checked closed windows, a rolling full rescan, so a backdated fact in an old window is found. Period close
  (`soft_close_period` / `lock_period`) and the gate refuse while ANY UTC day up to the settle horizon has no closed window
  checked within `rescan_days`, whether or not a window row exists for it. `history_floor` cannot hide early days: a period
  starting before the floor is refused, and the gate refuses a floor later than 2000-01-01.
- **Period close waits for the period end plus settle.** Every UTC day of the period, including the days after the
  settle horizon (which have no window yet), must have a fresh closed window. The guard bites from F8 on (once any
  window or fact row exists for the tenant), so the runbook re-runs `--enable-once`, which rescans EVERY stale window
  (the rolling batch only runs while the mapper is live), just before the gate.
- **Runbook.** Each `--enable-once` pass is capped at 1000 windows (`max_windows`), so loop `--enable-once --apply` until the report's
  `windows_remaining` is 0 (about 10 passes over history from 2000), then once more just before the gate.
- **Terminal rows are the mapper's only.** The runtime role can INSERT (W4), so the trigger also checks INSERT and UPDATE:
  POSTED needs the posting bridge's own journal for that fact, SKIP is only for opening import/unwind kinds, NO_GL only for V1.

## Settings (domain `billing_gl`, entered in the UI)

`enabled`, `rescan_days` (7), `rescan_batch` (24), `cash_account` (JSON: source system -> account id; `opening_refund` for opening refunds), `posting_timezone`,
`closed_period_policy`, `settle_seconds`, `resummarize_days`, `history_floor`. Read by key from B-14:
`ar.prepayment_liability_account`, `ar.prepaid_funding_account` (account id, code or exact name).

## Tables (W4)

`erp_billing_fact_postings` and `erp_billing_fact_windows`, FORCE RLS. `app_user` has SELECT, INSERT and a column-limited
UPDATE; no DELETE. Triggers: identity and `fact_digest` immutable (`billing_fact_digest_changed`); POSTED, SKIP and
NO_GL terminal; BLOCKED/WAITING move only to a terminal state or to each other; REFUSED only to POSTED; no DELETE;
window bounds immutable. The downgrade refuses while rows exist.

## Period close

`soft_close_period` and `lock_period` refuse `billing_facts_incomplete` while an unclosed window overlaps the period or a
BLOCKED/WAITING fact occurred in it.

## Gate (`billing_gl.checks.ready`, wired by the B-9r follow-up)

Every window up to now - settle closed; 0 BLOCKED/WAITING; 0 REFUSED V6; every pre-seal fact SKIP; no POSTED row before
F10.

## Rev 2: invoices, credit notes and cash-basis output VAT (e2_0033)

Ruling (2026-11-18): output VAT is deferred on 1455 at issue and released to 1440 on payment AND on application of an
advance. All of it is in the same one-journal-per-fact door; there is no new posting kind.

- **Accounts.** Revenue: ERP's `erp_ar_line_accounts` per (document, line), no fallback (missing:
  BLOCK `revenue_account_unmapped`). VAT: setting `billing_gl.tax_accounts` (treatment -> {current 1440, deferral 1455});
  a treatment with tax that is unmapped blocks `tax_account_unmapped`. Cash for source `erp_v2.ar` is
  `erp_ar_receipts.cash_account_id` of the settlement; other sources use `billing_gl.cash_account`.
- **Invoice.** Before posting, the line accounts must equal the document fact exactly, line by line and in total
  (`line_accounts_mismatch`); no rounding is absorbed. It creates a VAT position per treatment (D = tax, open = total).
- **Release.** A settlement or advance application of d against a document releases
  `min(R, HALF_EVEN(d x D / T))` from 1455 to 1440; the settlement that brings the document to 0 takes all of R.
  A deallocation un-releases exactly that allocation's net release (full) or the same formula capped at it (partial);
  a credit-note close-out is reversed first.
- **Credit note.** Dr 1455 min(t, R), Dr 1440 the rest; if the invoice's open amount reaches 0 the remaining deferral
  is closed out to 1440 (D-A6-9).
- **Invariants** (database trigger backstop plus an in-transaction assertion; the mapper never clamps): 0 <= R <= D,
  0 <= open <= T, open = 0 implies R = 0 (`vat_invariant`).
- **Tables (W6).** `erp_billing_vat_positions` / `erp_billing_vat_movements`: append-only, FORCE RLS, `app_user` SELECT
  and INSERT only. The downgrade refuses while rows exist.
- **Migrated invoices (Q1).** `python -m erp_v2.billing_gl vat-opening plan|apply` (migration role, 0600 receipt, plan
  sha) loads each carried open invoice's remaining deferral = legacy 1455 credit less the attributed reclass. A result
  below 0, above the tax or unattributable is an E38 report item and is not loaded; a payment on such an invoice (or on any
  imported document without a position) blocks `vat_position_missing`. The legacy extraction that feeds the input file is a
  separate reader step.
- **Gate.** `checks.ready` also requires `billing_gl.tax_accounts` to map every treatment to a current and a deferral
  account and 0 BLOCKED VAT facts.

### Rev 2 review amendments (architect, 2026-11-19)

- **Release rate (spec 4.3 amendment).** The release denominator is the gross the deferral still covers: `T` for a
  native invoice, the remaining gross at the opening (`open_initial`) for a migrated, part-paid invoice, because the
  loaded `D` is the remaining deferral. `r = min(R, HALF_EVEN(d x D / basis))`.
- **Opening-lot advances.** A V4 fact's `opening_cash_effects` `application` / `application_inverse` rows are settlements
  of their document, exactly like settlement allocations.
- **Ordering.** An allocation that sorts before its native invoice's issue fact is WAITING `waiting_document`; it does
  not block the account (the invoice must post) and the next run's retry re-evaluates it. `vat_position_missing` is for
  imported documents only.
- **Bank from structure.** An `erp_v2.ar` settlement needs exactly one `erp_ar_receipts` row for its cash account, else
  BLOCK `receipt_cash_unresolved`; there is no fallback to `billing_gl.cash_account`.
- **Line accounts.** A credit note is compared with the document by SUMS only (B-16 writes one row per credit-note line,
  billing one line per credit note). An unknown, inactive or non-postable revenue account blocks `revenue_account_invalid`.
- **Void of a credit note** mirrors only the close-out still in force (a later deallocation may already have reversed it).
- **Persist.** Journal and VAT movements are written in one savepoint; a database `vat_invariant` refusal blocks that one
  fact, it does not abort the run.
- **Opening loader.** `plan` cross-checks each item against billing (document exists, this tenant's imported open item,
  total = original grand total, exposure = `open_initial`); a zero-tax carried invoice loads a zero position;
  an E38 item can be resolved by a `finance_override` (stated deferral, evidence sha, reference) in a new, sha-bound plan.
  The runtime role cannot create `origin = opening` positions or `fact_version = 0` movements.

### Q1 legacy VAT extraction (`vat-extract`, P-B16-e)

    python -m erp_v2.billing_gl vat-extract --tenant-id T --organization-id O --cutoff D --snapshot-sha256 S \
        --out ITEMS.json --receipt R.json [--overrides OVERRIDES.json] [--treatment-map MAP.json]

Read-only on both sides (env-only `MIGRATION_DATABASE_URL` and `LEGACY_DATABASE_URL`, `SET TRANSACTION READ ONLY`, rehearsal
snapshot only). Both output files are created exclusively with mode 0600 before any read. The items file is the exact
`vat-opening` input in canonical JSON, so a re-run on the same snapshot gives the same sha; the receipt is counts and
aggregate tie amounts only.

- **Ledger truth** is `gl.posted_ledger_line` on the deferral accounts (the `deferral_pair_account_id` of each VAT/GST code's
  collected account) up to the cutoff. Reversal journals keep their original's source key, so reversed and voided
  invoices and bounced payments net out. Legacy account ids are the v2 account ids.
- **Invoice credit** = the net of INVOICE, AR_INVOICE_VAT_DEFERRAL and AR_INVOICE_RESTORE lines per invoice (the snapshot
  shows all three on 1455). It must equal the deferrable tax of the invoice's lines, else the invoice is unattributable (E38).
- **Reclass** = legacy's own prorate (`(allocated x line tax / invoice total)` to 0.01, HALF_EVEN) per allocation, only for
  payments with a net CUSTOMER_PAYMENT_VAT_RECLASS journal; a payment whose recomputed total differs from its journal makes
  its invoices unattributable.
- **Link** to billing is the #72 import receipt (`opening_receivable_bases` of the receipts in `erp_ar_opening_admissions`):
  never an invoice number. The admissions' snapshot sha must equal `--snapshot-sha256`, and the bases must equal the
  imported open items (`carried_count_mismatch`).
- Every carried invoice appears exactly once per treatment (zero-tax invoices as a zero position `none`); NGN only.
- **Tie (independent).** The legacy GL of the deferral accounts at the cutoff must equal ERP v2's own B-9r opening balance
  of those accounts (`tie_failed`), and the AP-module lines must equal minus the payables a6 remaining input deferral
  (`ap_positions_mismatch`). Everything no carried invoice owns is a NAMED residue in the receipt (AR lines with no carried
  owner by source, credit notes, non-carried invoice remainders, payments whose reclass does not re-derive, manual
  non-AR/non-AP journals), with a `residue_sha256`. Finance acknowledges it by passing `--acknowledge-residue <sha>`; until
  then the run writes no items file.
- **Credit notes.** Legacy posts a credit note's 1455 under the credit note's OWN id and keeps no link to the invoice it
  reduces, so that amount is residue and every invoice touched by a payment that also allocates to a credit note is E38.
- **Overrides** are a separate Finance-named input (`named_by`, evidence sha and reference per item), bound by sha in the
  receipt. Only E38 items may be overridden (`override_on_loadable_item`); an override that matches no item refuses.
- The receipt also carries the treatment-map sha and a legacy fingerprint (counts and max ids of `gl.posted_ledger_line`,
  `ar.invoice`, `ar.payment_allocation`) so the output can be re-derived from it.
