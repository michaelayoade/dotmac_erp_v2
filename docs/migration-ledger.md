# Migration ledger: opening, IAS 8, bridge, exceptions register (B-9, `e2_0028_migration_ledger`)

Rulings: Tier C closed years (FY2025 and earlier) land as a reviewed opening balance with IAS 8 corrections forward
against 3100; legacy history stays read-only. The bridge is itemised per account x period with no materiality
threshold; Michael signs each sha-bound version. The flip gate is: open exceptions = 0 AND the latest bridge signed.
Rounding residues (ruling v1640) have no tolerance: each T-ROUND-2DP header delta is a bridge line and is cleared by one
governed correction per period.

Sign convention everywhere: net = debit - credit (assets positive, liabilities negative). All money in files is a
string (a JSON number is refused); duplicate JSON keys are refused. DSN: `MIGRATION_DATABASE_URL` only. Every write
needs the flag `--apply` AND `ERP_MIGRATION_LEDGER_APPLY=1`; dry run is the default. Receipts are 0600, created
exclusively before any read, and carry shas, counts and tie-outs.

## Opening and IAS 8 (`python -m erp_v2.migration_ledger.opening_cli plan|apply`)

* Inputs, each bound by sha in the spec: the reviewed TB at the FY2025 close (`as_of`, one row per account),
  the declared subledger openings for 2000, 2040, 1410 and 1400 (each with its source receipt sha), and the
  itemised IAS 8 items (`item_id`, restated account, signed movement, evidence sha, description).
* ONE system-built opening journal, owner `erp.opening`, one line per account (the spec says "one opening journal
  per account"; a single-account journal cannot balance, so this is one journal with one line per account).
* Each IAS 8 item is its own journal, owner `erp.ias8`: the item's movement on its account, the opposite on 3100,
  dated the first day of the first OPEN, non-adjustment period after the opening (D-3).
* REFUSES, before any database access, unless Σ net = 0 AND for every control account
  `TB net + Σ IAS 8 movement on it = subledger opening`. A gap is therefore always an itemised IAS 8 item, never netted.
* The opening period must be OPEN when the opening posts (accounting rejects posting to a closed period); soft-close and
  lock it afterwards. A second, different TB is refused; a repeat is a replay. Provenance is `erp_opening_journals`.
* Both journals are posted through accounting a2 (create + post) under their own source owners, not through the B2
  manual-journal path, so the manual control guard is not the door; the control equality above is.

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
(`posted`, `admitted`, `voided`, `written_off`, `no_action`) and an evidence sha. `gate` exits 0 only when the open count
is 0 AND the latest bridge version is signed.

## Not covered (by design or follow-up)

* The legacy side of the bridge and the subledger openings are DECLARED inputs with sha provenance; the tool does not
  read the legacy ledger or the subledgers itself.
* The v2 side of FY2026 months (carry and transform) is taken from the items, not recomputed from the v2 ledger.
* Receipts of other builds (B-1 plan receipts, settled admission) enter through the admission-review rows only.
