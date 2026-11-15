# B2: manual journals, held legacy journals, fiscal periods, functional currency

Scope: ERP v2 only (no Starter release, no billing/Sub code, no scripts). Permissions are P0's
(`erp.finance.journals.prepare|approve`, `erp.finance.periods.manage`); admin and staff hold none of
them (W14). Routes are dormant until a named party is granted the role through `access_cli`.

## Flow

`prepare` (finance_preparer; mandatory evidence reference, e.g. the Sub report id for an Option B
summary journal) -> `request post` -> `approve + post` (finance_approver; approver is neither the
preparer nor the requester) -> optionally `request reversal` -> `approve reversal` (approver is neither
the requester nor the preparer). Every journal is created, posted and reversed through accounting a1's
`create_journal` / `post_journal` / `reverse_journal`; maker-checker state lives in
`erp_manual_journals` (e2_0014), whose CHECK constraints also refuse a self-approval. The approvals
module is not consulted (no tenant approval policy is published for this workflow); the audit trail is
the kernel audit log (`erp.journal.*`, `erp.period.*`) plus accounting's period events.

Manual journals are in the tenant functional currency at rate 1 and only into an OPEN, non-adjustment
period (an adjustment period takes ADJUSTMENT journals). REOPENED periods are never offered.

## Held legacy journals (W13)

Imported DRAFT journals appear on `/finance/journals/held` (read-only, paged, no actions). Each is
opened individually, needs a Finance review reference, and then follows the same request/approve+post
path. There is no bulk route or command. Their source identity stays on the journal and is snapshotted
on the workflow row. Only journals whose source owner is the declared legacy import owner (`legacy-erp`) qualify; another module's DRAFT is refused. A rejected held request can be re-requested with a new review reference and requester (SoD re-checked; the prior rejection stays in the audit trail); a rejected manual journal is terminal. The soft-close screen shows the count of held drafts in the period. Approval screens warn (never block) when a journal touches subledger control accounts 2000/2040. A DB trigger freezes recorded preparer/requester/approver evidence and allows only declared state transitions; the e2_0014 downgrade refuses when rows exist.

## Periods

`/finance/periods`: open (FUTURE only), soft-close (accounting close evidence: no unresolved manual
journals + a Finance review reference), lock. Reopen, and creating fiscal years/periods/accounts, are
not exposed (accounting a1 `create_*` helpers carry the `_flush_unique` defect; fix is accounting a2).

## Functional currency

Tenant-scoped typed setting `accounting_policy/functional_currency` (no inheritance, no default).
Imported by `importer.minting.mint_functional_currency_settings` from legacy
`organization.functional_currency_code` (never overwrites). No route writes it; the provisioning
function `policy.set_functional_currency` refuses any different value once a journal beyond DRAFT
exists. A tenant without a value cannot prepare journals. ERP posting paths (B4) read
`policy.functional_currency`; no code names a currency.
