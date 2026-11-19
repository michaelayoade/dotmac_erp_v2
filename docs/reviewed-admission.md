# Reviewed admission (B-1)

Migration `e2_0026_admission_reviews`. Rulings: D-M3 (Finance may waive one named verifier check per invoice,
with evidence, through a decision file; the GL tie is never waivable) and D-M5 (the FY2025 unposted invoice is
posted as an IAS 8 prior-period item against 3100). Every decision resolves an exception to a terminal state
with evidence: admitted, posted, voided with a reason, or confirmed.

## What it is

* `erp_admission_reviews`: one append-only row per legacy invoice (tenant, legacy invoice id, decision,
  `waived_checks`, `evidence_sha`, `decision_file_sha`, `snapshot_sha256`, `decided_by`, `decided_at`,
  `ias8_account_id`, `operator_ref`, `dual_control`). RLS forced; the runtime role has SELECT only; a trigger
  refuses UPDATE and DELETE; CHECKs hold the closed vocabulary, the one-waiver rule, the sha shapes and the
  separation rule. The downgrade refuses while rows exist.
* `python -m erp_v2.legacy_ap.review_cli`: plan/apply of a sha-bound Finance decision file (below).
* The verifiers read the row. They are unchanged for an invoice with no row.

## Decision file

JSON, at most 1000 rows, canonical lower-case UUIDs, duplicate keys refused, bound by `--decision-sha256`
(recomputed over the bytes) and `--snapshot-sha256`. A tampered file, a different snapshot, or any invalid row
refuses the whole file; nothing is partially accepted.

```
{"version": 1, "tenant_id": "<uuid>", "snapshot_sha256": "<64 hex>", "decision_ref": "<text>",
 "rows": [{"legacy_invoice_id": "<uuid>", "decision": "<see below>", "waived_checks": ["<name>"],
           "evidence_sha": "<64 hex>", "decided_by": "<Finance person>", "decided_at": "<ISO with offset>",
           "ias8_account_id": "<uuid>"}]}
```

Decisions: `admit_residual`, `admit_with_waivers` (exactly one waived check), `void`, `ias8_post` (carries
`ias8_account_id`, which must be the tenant's postable account 3100), `supplier_credit`, `reopen`. This PR
records every decision; the verifiers act on the first three. `void`, `supplier_credit` and `reopen` are
recorded as evidence, and while one is on file the admission verifiers refuse the invoice
(`review_decision_not_admission`); their executing paths (the v2 void, payables a6 supplier credit) are not in
this PR.

Apply needs `--apply`, `ERP_IMPORT_APPROVED=1`, `ERP_IMPORT_APPLY_OK=1` and `--operator-ref`. The DSN comes from
`MIGRATION_DATABASE_URL` only. The receipt is 0600, created exclusively before any work, and holds counts and
digests only. A row identical to a recorded one is a no-op; a different decision for a decided invoice is
refused.

## The waivable-check allowlist

An allowlist, so a new check is strict by default (`erp_v2/importer/admission_waivers.py`):

| Name | Skips |
|---|---|
| `line_totals` | `invoice_line_totals` |
| `journal_owner` | `source_journal_owner`, `source_journal_invoice`, `source_ledger_invoice` (ownership only) |
| `inconsistent_status_pair` | `source_status` (the evidence still reports POSTED; the POSTED legacy journal is verified) |
| `legacy_journal_link_unposted` | `source_status` for an unposted header linked to a legacy journal; the original posting instant is then the journal's |
| `source_allocations` | `source_allocations` (unpaid and B1b paths) |
| `source_amount_scale` | the two-decimal check on header amounts |
| `legacy_gl_tax_treatment_unrecognised` | the A3 refusal |
| `deferral_amount_mismatch` | the A3 refusal when its reason is the deferral mismatch |

Never waivable: `source_journal_shared` (one legacy journal never backs two invoices: refused when another legacy
invoice references the journal or a historical invoice/settlement receipt already names it; the decision file
may not name one journal twice, and the table has a unique index on it), the payment-GL check behind
`source_allocations` (`allocation_payment_gl_effect` / `allocation_payment_gl_unproven`: the waiver is refused when
an allocated payment posted to the control account or the snapshot cannot identify the payment), tenant, snapshot binding, fingerprint, the GL tie (the journal's control credit equals the
invoice total, settled residual against the control account), currency and FX, duplicate admission, the
destination-equals-source journal and ledger proofs, the V1 tax-account rule. A waiver for a check that does
not fail is refused (`waiver_not_needed`), so a stale decision cannot linger. The waived name, the review id's
decision and sha enter the evidence fingerprint, and the audit event of the admission records them.

Admit-with-waiver on a posted invoice carries `legacy_journal_id` in the decision row; the verifier requires it to
equal the invoice's own journal (`review_journal_mismatch`). One waiver per invoice (D-M3) is enforced in the CLI,
the verifier and the database. `line_tax_detail` is not in the vocabulary.

Waivers that leave the GL and the subledger disagreeing on tax (`legacy_gl_tax_treatment_unrecognised`,
`deferral_amount_mismatch`) are flagged `requires_correcting_journal` in the plan/receipt
(`reviewed_admissions`) and in the audit event, so B-9 cannot close the exception without the E08 correcting
journal. The audit event and the private plan carry the readable `legacy_header_status` (status/posting status)
and the waived check name, not only the fingerprint.

Transforms and waivers are distinct. `WaiverGate(review, transform={name: rule_id})` takes the failures an
approved importer transform (#68 transform manifest) already fixed: such a failure needs no waiver and does not
trip `waiver_not_needed`; the evidence records `transforms` and `review` separately. The verifiers take the same
input as `transforms=((name, rule_id), ...)`; it is empty until the #68 manifest is wired in.

## Verifier integration

`HistoricalPayablesVerifier` (historical unpaid admission), `SettledInvoiceVerifier` (settled admission) and
`ApprovedInvoiceVerifier` (B1b) build a `WaiverGate` from the row, read in the destination under the snapshot
manifest. A row bound to another snapshot, another tenant or an unusable decision refuses. B1b also honours
`ias8_post`; the other two refuse it.

## IAS 8 mode (B1b, D-M5)

For an invoice dated in a period that is closed to posting, with a later OPEN period, and a `ias8_post`
decision, B1b recognises the invoice through the owner as usual. The journal that posts is derived from the
owner's consequence:

* posting date = first day of the first OPEN, non-adjustment period (later than the invoice date), else
  `ias8_no_later_open_period`; an invoice whose period is open refuses (`ias8_period_open_use_normal_path`);
* every expense-side debit goes to the decision's account (3100); the input-tax debits and the credit to the
  control account are untouched (V1/V2 rules are not bypassed); the journal still balances at the invoice total;
* the journal description and the approval reference carry the review id; the audit event carries the review,
  the decision file sha and the posting date. The owner's accounting receipt stays against the owner's own
  consequence, and the invoice date is never changed.

## Dual control

A per-tenant policy, default OFF (`erp_v2.governance`). The policy is read when a decision is recorded and
stamped on the row. ON: the applying operator must differ from the person who decided each row (database CHECK
and CLI refusal `dual_control_operator_is_decider`). OFF: one person may do both.

## IAS 8: fiscal year, fingerprint, later credit notes

* IAS 8 mode applies only when the invoice's fiscal year is EARLIER than the fiscal year of the first open period
  (`ias8_same_fiscal_year` otherwise): a soft-closed month of the current year never posts to 3100.
* Fingerprint relationship: the owner's consequence, its fingerprint and the accounting receipt are the standard
  ones (the receipt records the owner's consequence fingerprint). The IAS 8 journal has its own request
  fingerprint (different date and expense account) and is derived deterministically from the owner's
  consequence, the review id and the decision file; the receipt's accounting evidence fingerprint is that
  journal's. The two are tied by the document identity (same source owner, kind and document id) and the review
  id on the journal, not by equal fingerprints.
* A later credit note against an IAS 8 invoice credits current-year P&L (the credit note posts at its own date to
  the expense account of its lines); it does not reverse the 3100 charge. A prior-period write-back needs its own
  IAS 8 decision.
