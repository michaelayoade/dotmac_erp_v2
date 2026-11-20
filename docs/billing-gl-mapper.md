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
| V1 allocation / deallocation / reallocation | POST L (Dr prepayment / Cr 1400); empty effects NO_GL `lane_neutral` |
| V1 refund | POST L + cash |
| V1 reversal | POST L, + cash when the reversed fact is settlement-family; predecessor must be POSTED |
| V1 invoice_issued, credit_note_issued | BLOCK `invoice_mapping_not_built` (rev 2) |
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
