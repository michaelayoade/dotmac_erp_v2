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

## Settings (domain `billing_gl`, entered in the UI)

`enabled`, `cash_account` (JSON: source system -> account id; `opening_refund` for opening refunds), `posting_timezone`,
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
