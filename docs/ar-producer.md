# AR producer (B-16)

Customer receipts, allocations, invoices and credit notes, on dotmac-billing commands. This carries an existing
legacy feature (legacy parity, not a new feature); GL posting is the B-15 mapper's job, never this package's.
Spec: `B16-SPEC.md` (sections 1-3, 5, 6, 8; section 4 is B-15 rev 2).

Code: `src/erp_v2/ar/` (`service`, `routes`, `queries`, `settings`, `terms`, `numbering`, `checks`, `models`,
`constants`), templates `templates/erp_v2/receivables/`, migration `e2_0032_ar_producer`.

## Billing surface

Built against the dotmac-billing **a13** command surface. a13 is unreleased: tests put the local Starter source on
`PYTHONPATH`; the pin change belongs to the composition PR (S9). The billing flows in `tests/test_ar_producer_pg.py`
skip on an older release; the e2_0032 object tests (RLS, grants, triggers, downgrade guard) run everywhere.

## Surface

All under `/finance/receivables`; every POST is CSRF-protected. When `ar.producer.enabled` is false every POST
refuses `ar_producer_disabled` and writes nothing (not even an audit row); GETs work, so main stays deployable.

| Route | Permission |
| --- | --- |
| `GET /drafts` (open drafts), `GET /documents/{id}`, `GET /receipts/{id}` | `erp.web.access` |
| `GET /drafts/{id}` | `erp.finance.receivables.read` (finance_preparer and finance_approver, so a poster can review a draft) |
| `GET/POST /invoices/new`, `GET/POST /drafts/{id}`, `POST /drafts/{id}/discard`, `GET/POST /credit-notes/new?invoice=`, `GET/POST /receipts/new` | `erp.finance.receivables.prepare` (finance_preparer) |
| `POST /drafts/{id}/issue`, `POST /documents/{id}/void`, `POST /receipts/{id}/post`, `POST /receipts/{id}/reverse`, `POST /accounts/{id}/apply-credit`, `POST /allocations/{id}/deallocate`, `POST /draws/{group}/reverse` | `erp.finance.receivables.post` (finance_approver) |

admin and staff hold neither permission. The billing `actor_ref` is always the authenticated party.

## Behaviour (one savepoint per action; any refusal rolls back every billing and ERP row)

- **Issue invoice**: validate (amounts > 0, revenue account active, postable and not control / tax-role / holding,
  functional currency, date <= today in an OPEN non-adjustment period), `create_billing_account` (ref = the customer
  account id), one `accept_rated_obligation` per line (source `erp_v2.ar_invoice_line`, fact id `<draft>:<n>`,
  one `AppliedTaxSnapshotV1` per taxed line from the tax code's dotmac-tax rule), one-line `create_draft_document` or
  multi-line `create_multi_line_draft_document`, `issue_document` (VERIFIED due-date basis, series
  `ar.series.invoice`), one `erp_ar_line_accounts` row per line, optional auto-apply of customer credit.
- **Credit note**: against ONE issued invoice of the same customer, at most its outstanding (billing refuses
  `credit_exceeds_receivable`). No standalone credit note. Revenue accounts are pre-filled from the invoice's rows.
  The form carries the F-D notice: legacy credit notes are cleared by R8; do not re-issue.
- **Receipt**: `accept_settlement` (funding lane `available_credit`, always; evidence `erp_v2.ar.receipt_recorded`)
  then `allocate_settlement` per line; the remainder is customer credit. The receiving bank's cash GL account is
  snapshotted from the bank account (never typed); the bank reference is unique per bank account.
- **Apply credit / deallocate / reverse receipt / void**: `apply_available_credit`, `deallocate_settlement` (an
  opening-lot draw is restored through `reverse_cash_credit_draw`), deallocate-all then `reverse_posting_group`
  (reason `void` or `bounced`), `void_document` (billing refuses allocations, credits, withholding and migrated
  invoices; a migrated invoice is corrected by a credit note).
- Every billing key derives from the draft, document or request id (a double POST returns the original); a changed
  payload under one key is refused `idempotency_conflict`. A test asserts every key is within 200 characters.

## Authorisation (D-9, default OFF)

Each draft stamps `dual_control` from the tenant policy at creation. Stamped ON, issue / post refuse `same_person` for
the preparer (a DB CHECK backs it). Void / apply / deallocate / reverse read the live policy and then need a different
person from the original issuer. OFF: one person holding both roles may do everything.

## Settings (domain `ar_producer`, entered in the UI)

`ar.producer.enabled` (false), `ar.series.invoice|credit_note|receipt`, `ar.payment_terms`,
`ar.default_payment_terms_code`, `ar.default_revenue_account` (pre-fill only, never a mapper fallback),
`ar.seller_snapshot`, `ar.payment_instructions`, `ar.document_profile_code|version`, `ar.auto_apply_credit_on_issue`
(true). The confirmation literal, the source kind and the funding lane are code constants, not settings.

## Tables (e2_0032, W5)

`erp_ar_drafts` (column-limited UPDATE, no DELETE; draft -> issued | deleted, both terminal; billing ids set once),
`erp_ar_draft_lines` (full DML for the runtime role, a trigger bounds it to drafts still `draft`),
`erp_ar_line_accounts` and `erp_ar_receipts` (SELECT and INSERT only; append-only triggers bind the owner too).
FORCE RLS on all four. The downgrade refuses while any row exists. `revenue_account_id` is a plain id (no FK into
`mod_accounting`, the manual-journal precedent); the service resolves every account in the tenant.

## Before R9 (WHT and non-cash)

Only V1 facts are emitted. There is no WHT, VAT-withheld or stamp-duty field: when a customer deducts WHT, record the
cash actually received; the WHT part stays outstanding on the invoice. A test greps that no WHT, non-cash or refund
command is called anywhere in `src/erp_v2`, and that only this package (and the importer's opening commands) calls
billing write commands.

## Gate check

`erp_v2.ar.checks.ready(db, tenant_id)` reports: producer enabled or any row in the four tables before F10; every
required setting key; each series enrolled in a collision domain (needs a numbering release with collision domains;
at the a3 pin it reports `ar_setup_incomplete:<key>:not_enrolled`). Wiring it into the flip gate waits for B-9r.

## Review changes (architect review of 423d8e8)

- A credit note carries ONE line for now (`credit_note_single_line_only`): billing writes one line per note and the
  mapper compares per-line rows until #77 compares sums. A taxed credit line must use a VAT treatment the original
  invoice carries (`credit_tax_treatment_mismatch`; migrated originals: billing's own tax snapshots).
- Payment terms are per draft (`erp_ar_drafts.payment_terms_code`, selected from `ar.payment_terms`, default
  `ar.default_payment_terms_code`). Per-customer pre-fill needs customer terms data the customers module lacks (F-F).
- A draw that includes opening cash is one unit: it is reversed whole from the invoice page (`/draws/{group}/reverse`);
  deallocating one row of it is refused (`opening_draw_unit`). Proof on a real opening import is owed at S11.
- Every dated action (apply, deallocate, void, reverse) needs an OPEN, non-adjustment period.
- D-9 ON: only the preparer may edit or discard a draft (`not_preparer`).
- The F-D notice is a structural SQL filter (`detail.party[0]` = customer account id) with no row cap.
- Deallocate key is `dealloc:{allocation}:{request}`; deallocate and apply-credit replays return the original without a
  second audit row. Role profile `erp.web` is version 6.
