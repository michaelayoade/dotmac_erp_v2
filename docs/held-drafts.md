# Held drafts: governed decisions for the FY2026 legacy DRAFT journals (B-2, `e2_0027_held_draft_decisions`)

Michael's ruling (register v1537): a rule-matched void counts as "reviewed against source". Every FY2026 legacy
DRAFT journal (the 14,271 held drafts) receives exactly one decision: **posted**, **voided** or **queued** for
Finance. The operator CLI plans, then applies; there is no UI and no runtime write path.

## The rules (`erp_v2/held_drafts/rules.py`; `RULES_SHA256` binds their exact text)

Held population: destination DRAFT journals owned by the legacy import, dated in the fiscal year, whose legacy
record is DRAFT, SUBMITTED or APPROVED. A destination draft with another legacy status is not held (counted in the
receipt as `excluded_legacy_status_not_held`).

| Rule | Draft | Outcome |
|---|---|---|
| `R-BANK-FEE` | legacy BANKING / BANK_FEE with exactly one line on a bank cash GL. Key = (bank cash GL, posting date, signed amount). S = bank statement lines on the key, P = POSTED BANK_FEE journals on the key (including any this tool already posted), D = drafts on the key. | S = 0: queue `no_statement_evidence`. P > S: void every draft `duplicate_of_posted_fee_excess` and raise an **E17 finding** for the key. Otherwise `postable = min(D, max(0, S - P))`: the first `postable` drafts (by number, id) post through the B2 legacy-held path; the rest are voided `duplicate_of_posted_fee`. Not a single bank line: queue `not_single_bank_line`. A key on a shared or re-pointed cash GL queues `bank_gl_unresolved` (the refusal semantics of `recon_service.resolve_gl_account`). |
| `R-AR-TWIN` | legacy AR / INVOICE or CUSTOMER_PAYMENT with a document id | A POSTED twin (same legacy module, type, document id, POSTED in v2) with equal per-account (debit, credit) functional totals: void `already_posted_duplicate`. Different totals: queue `twin_amount_differs`. No twin: `twin_missing`. No document id: `no_document_reference`. |
| `R-AR-UNAPPROVED-REVERSAL` | an AR draft that is the EXACT sign mirror of a POSTED twin (same accounts, debit and credit swapped) | Legacy document PAID, POSTED or PARTIALLY_PAID: void `unapproved_reversal_of_live_document`, evidence = twin id, document id and status, mirror proof (account count, totals, digest). Document not live or not found: queue `reversal_document_not_live`. Not an exact mirror: queue (`twin_amount_differs`). The equal-amount rule is tried first. |
| `R-QUEUE` | everything else (BANK_RECONCILIATION, EXPENSE, PAYROLL, AP, no legacy record, a draft already in the B2 workflow) | queue (`no_published_rule`, `no_legacy_record`, `legacy_record_mismatch`, `workflow_in_progress`) |

The document status is read from the legacy `ar.invoice (invoice_id, status)` and
`ar.customer_payment (payment_id, status)` tables; these names are taken from the legacy AR schema notes and must be
confirmed against the g9 snapshot before a real plan.

**E17 finding.** For every key where P > S the plan and receipt carry `e17_findings`: key digest, statement lines S,
posted fee journals P, `excess_posted_fee_journals` = P - S, a digest of the ids of all P posted journals, and the
number of drafts voided. The excess journals cannot be told apart from the matched ones, so Finance reviews the set.

**Evidence ids.** Per-draft evidence stores counts plus sha256 digests of the sorted statement-line and posted
journal ids, never a truncated list.

A draft that would post is pre-checked against the B2 guards (the fiscal period must be OPEN, no control account
2000/2040/1410 on the draft, and, when `filed_vat_months` names its month, no VAT account); a failure queues it as
`post_blocked` with the blocker. A refusal by the B2 or a2 path at apply also queues it (`posted_refused:<code>`,
`voided_refused:<Type>`); nothing is improvised. Pre-fiscal-year drafts (Tier C history) are out of scope and are
only counted in the receipt.

**Filed VAT months (ruled treatment, not built).** A draft that would post in a filed VAT month touching VAT
accounts stays `post_blocked`. The ruled treatment is to void it as `superseded_by_forward_correction` and post a
system-built journal in the first open period. The g9 data has none; build it only if any appear.

## Operator flow

```
python -m erp_v2.held_drafts.cli plan  --spec S.json --receipt P.json --operator-ref <who>
ERP_HELD_DRAFTS_APPLY=1 python -m erp_v2.held_drafts.cli apply --spec S.json --plan-receipt P.json \
    --approved-plan-sha256 <plan_sha256 in P.json> --receipt A.json --operator-ref <who>
```

- Spec: `{"version": 1, "tenant_id", "fiscal_year_start", "fiscal_year_end", "rules_sha256", "evidence_ref",
  "requester_party_id", "approver_party_id", "filed_vat_months": ["YYYY-MM"], "expected_held_total": <int>}`. The
  spec is refused when its `rules_sha256` differs from the code's.
- DSNs: `MIGRATION_DATABASE_URL` (destination, operator role) and `LEGACY_DATABASE_URL` (read-only legacy snapshot;
  reads `gl.journal_entry` only: `source_module`, `source_document_type`, `source_document_id`). Both leave the
  environment before the assembly is imported. Plan runs read-only.
- Receipts are 0600, created exclusively before any read or write, and carry journal ids, counts and sha256 only
  (no tenant id, DSN or account code). The plan receipt holds one bridge line per draft (rule id, outcome, reason,
  amount, evidence sha) and the partition proof. Voids and posts are not undoable: the rollback is not to apply.
- Apply recomputes the plan and refuses on any drift (`plan_drift`), then decides each draft in its own savepoint
  and commits in batches of 500. It is resumable: decided drafts are never touched again; a re-run is a no-op. An
  advisory lock refuses a concurrent apply.
- Voids use the accounting a2 `void_draft_journal` (JOURNAL_VOID period event; idempotency key per journal;
  requester `held-drafts-requester:<planner>`, approver `held-drafts-approver:<applier>`, so the owner's
  requester != approver rule holds even for one operator). Posts use `request_legacy_post` and `approve_and_post`
  with the supplied requester and approver parties.

## Dual control (per-tenant policy, default off)

Read from `erp_v2.governance`. Off: one operator and one party may plan, apply, request and approve. In that case
the same operator appears under both role prefixes (`held-drafts-requester:` and `held-drafts-approver:`) in the a2
void audit (the period event and the evidence); the prefixes only keep the owner's requester != approver rule true. On: apply
refuses `dual_control_same_operator` when the applying operator is the planning operator and
`dual_control_same_party` when requester and approver parties are the same; B2 enforces its own separation on the
posted journals (row stamp), and the decision row stamps the policy and a CHECK (`planned_by <> applied_by`).

## The decision table and the partition proof

`erp_held_draft_decisions`: one immutable row per draft (unique per tenant and journal): journal id, rule id,
outcome, reason code, amount, evidence (statement-line ids or the twin journal id, key sha and counts), evidence
sha, rules sha, plan sha, plan receipt sha, B2 workflow row for posts, the policy stamp, planned by, applied by.

The partition proof (in both receipts) shows, by journal id, `posted + voided + queued = held`. The held set and
its total come from the **legacy snapshot** (journals the legacy ERP holds as DRAFT, SUBMITTED or APPROVED in the fiscal
year; 14,271 on g9), not from a re-read of the destination, and the total must also equal the spec's
`expected_held_total`. The proof also reports decisions not in the legacy set and legacy held journals without a
decision (both must be 0), disjoint buckets, amount totals and a digest of each bucket's ids. After apply, a status
check confirms every voided decision is VOID, every posted one POSTED and every queued one still DRAFT.

## Not covered

- Filed-VAT-month drafts are queued, not superseded (see above); the forward-correction journal is not built.
- Queued drafts are resolved one by one in the B2 held-journal screens; their decision rows are not updated.
- After an interrupted apply, re-plan: decided drafts keep their recorded decisions, and the posts already made count
  in P, so a resume cannot post a key twice. The new plan sha differs from the old one, so the apply needs the new one.
