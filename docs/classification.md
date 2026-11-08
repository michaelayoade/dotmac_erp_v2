# ERP new composition — classification v1 (2026-11-08)

Ruling (Michael, 2026-11-08): ERP moves to a NEW Starter-composed app,
`dotmac_erp_v2`, replace-not-adapt (thin-client rule, 2026-10-16), data
by transform-to-fit + one downtime flip (Academy pattern). Legacy
`dotmac_erp` @ `74fa788a` stays untouched as rollback until the flip.

Basis: legacy tree census (841 service files in 96 groups, 357 model
files / 426 tables, 105 api + 87 web + 36 task + 75 schema files, 449
alembic versions, 1,006 test files, 924 templates), Starter package
READMEs/EXTRACTION boundaries in `dotmac_starter_mt/packages`, and the
pins legacy ERP already consumes. File-level mechanical dispositions:
`file-dispositions.csv` (1,501 rows). SPLIT/VERIFY rows are ruled
per-file at port time against the module's public surface — this v1 is
a group-level ruling, not a port.

Counts (files): MODULE 535 · SPLIT 427 · PORT 265 · EXTRACT 110 ·
KERNEL 50 · VERIFY 114.

## Dispositions

- **KERNEL** — consume `dotmac-kernel`; delete local store. Identity,
  sessions, authorization/RBAC, audit, settings (legacy
  `domain_settings` dies; values re-enter via kernel `set_many`),
  search, tenancy (`tenancy="multi"`, as legacy `product_assembly.py`),
  Party/PartyPerson. SMTP and other credentials env-only from day one.
- **MODULE** — Starter owns it; pin the released wheel, delete the
  local implementation, migrate data into `mod_*` storage.
- **EXTRACT** — Starter owner exists but is unreleased/unconsumed;
  qualification/extraction in Starter is a prerequisite wave. Never
  port a local copy into erp_v2 meanwhile.
- **PORT** — ERP-unique domain; the only code erp_v2 owns locally,
  under `src/erp_v2/` product packages, ported slice by slice.
- **SPLIT** — module core + ERP presentation/policy residue; split
  ruled per file at port time.
- **VERIFY** — glue/admin/init with no topic owner yet; ruled when its
  owning slice ports. Must be zero before the flip rehearsal.

## MODULE — pin and delete local

| ERP surface | Starter owner | Note |
|---|---|---|
| finance/gl, posting | dotmac-accounting | CoA, fiscal periods, journals, ledger (`ac`, `mod_accounting`) |
| finance/ar (invoices, receipts, aging) | dotmac-billing | Operational receivables (`mod_billing`); customer/contract residue → customers/commercial-agreements, ruled per file |
| finance/ap (supplier invoices, payments) | dotmac-payables | Liabilities/obligations (`pa`, `mod_payables`); PO side → procurement |
| finance/banking | dotmac-banking | Statements, matching, reconciliation (`bk`) |
| finance/payments, remita | dotmac-payments + connectors | remita/paystack/flutterwave connectors; ERP keeps no PSP transport |
| finance/tax | dotmac-tax | Already pinned (a4) in legacy ERP |
| expense, finance/exp | dotmac-expenses | Claims, limits, reimbursement eligibility (`ex`) |
| payroll (calc, structures, liabilities) | dotmac-payroll | (`py`, `mod_payroll`); tax policy from dotmac-tax, settlement via banking/payments — assembly glue ports, calc does not |
| procurement requisition→PO | dotmac-procurement | Budget/approval enter as digest-bound facts |
| approvals | dotmac-approvals | Decides approval only; subject transitions stay with owners |
| support | dotmac-ticketing | README names ERP as consumer; product declares reasons only |
| files, imports, numbering | dotmac-files/-imports/-numbering | Already pinned in legacy ERP |
| people directory/org/positions | dotmac-people | Narrow directory only (`pe`, `mod_people`), over kernel Party |
| fixed-asset subledger/depreciation | dotmac-finance | Physical custody → dotmac-assets |
| asset custody/lifecycle | dotmac-assets | ERP is qualifying source; verify released pin before genesis |
| forms | dotmac-forms | Verify surface at port time |

## EXTRACT — qualify in Starter first (blocks its wave, not genesis)

Updated 2026-11-08 (released-tag census): both packages below carry
release tags and are now MOUNTED at those pins. They remain
EXTRACT-class for DATA: their first-consumption parity proof is the
wave rehearsal, not the mount.

| ERP surface | Starter package | Remaining gate |
|---|---|---|
| inventory (SKU, movements, valuation) | dotmac-inventory 0.2.0a1 (released tag) | First-consumption parity vs ERP quantities/valuation at rehearsal; wheel carries the Rust/libmpdec native |
| pm/projects | dotmac-projects 0.1.0a1 (released tag) | First-consumption proof; project finance/SLA glue stays PORT |
| billing gaps found at AR port | dotmac-billing | Register direction: extract Sub/ERP code behind contracts, do not rewrite locally. Billing M1/M6/M8 rulings (Sub first) still govern the billing flip order |

## PORT — ERP-unique (erp_v2 local code)

- **HR suite** beyond the people directory: leave, attendance,
  discipline, appraisals/performance, recruitment + careers,
  training records, onboarding, self-service. (dotmac-workforce
  covers shifts/capacity only — SPLIT for scheduling.)
- **Public sector / IPSAS**: appropriations, commitments, funds,
  virements, IPSAS reporting. No Starter owner.
- **Consolidation** (cons) and **leases**: no Starter owner today;
  lease is an extraction candidate if a second consumer appears.
- **Fleet operations**: fuel, reservations, incidents, maintenance
  finance over dotmac-assets identity + connector-traccar GPS.
- **Coach**: prompts, domain context, consequences over the
  dotmac-ai-operations advisory protocol.
- **Integration adapters**: dotmac_sub sync, Academy sync, mailcow,
  nextcloud, staff/backoffice sync — as Integrator-contract adapters
  (Academy ERP-adapter pattern); provider transport stays outside.
- **Presentation/policy residue** of every SPLIT: dashboards, reports
  (rpt), notifications/email policy, help centre, analytics readers.
- **Project finance/SLA** glue over dotmac-projects once qualified.

## Models / data (426 legacy tables)

Genesis is the composed lineage — kernel head plus each adopted
module's head (`ac`, `pe`, `py`, `bk`, `pa`, `ex`, billing, tax,
numbering, files, imports, …) plus erp_v2's own `e2` lineage for
PORT tables only. Legacy tables never re-enter as local models for a
MODULE/KERNEL surface; their rows transform into module storage.
Opening/GL history enters accounting as typed opening facts, AR/AP
as billing/payables facts — never as table copies. Per-table mapping
is the importer wave's deliverable (see `docs/cutover.md` gates);
exceptions go to a manual-review queue, never silent defaults.
ERP→Academy and ERP→Sub sync facts (ACK/watermarks) stay separate
product facts, as in Academy v2.

## Waves (each: pin → port slice → transform rehearsal → parity)

0. **Genesis shell** (this step): kernel a107 + ui a9, multi-tenant,
   health/readiness, kernel-only genesis, no product traffic.
1. **Platform** (mounted 2026-11-08): files a7, imports 0.2.0a1,
   numbering a3 pinned and composed; table rulings in
   `docs/mapping-wave1.md`. Settings/identity/RBAC consumption
   proofs land with the first consumer slice of wave 2.
2. **People + payroll**: people directory, HR-suite port, payroll
   module + tax/banking glue. Payroll parity is a hard gate
   (run-total equality per legacy regression tests).
3. **Finance core**: accounting, banking, tax, expenses, payables,
   procurement, approvals, fixed assets (finance+assets).
4. **Billing/AR**: after Starter billing gap plan (Sub M1 first);
   customers/contracts split ruled here.
5. **Inventory + projects**: gated on EXTRACT qualification above.
6. **Satellites**: fleet, support/ticketing, coach, analytics,
   public-sector/IPSAS, consolidation, leases, integrations.
7. **Importer + flip**: full transform rehearsed on a production
   snapshot (separate approval), then one downtime flip (separate
   approval). Legacy ERP remains rollback until the flip completes.

## Open rulings for Michael (do not block wave 0)

1. Billing wave order vs Sub M1 — confirm ERP AR waits for the
   dotmac-billing gap plan rather than driving it.
2. Inventory native-build gate — accept Rust/libmpdec qualification
   as wave-5 prerequisite, or rule a pure-Python fallback requirement
   into the Starter package first.
3. Public-sector/IPSAS + consolidation + leases — keep ERP-unique,
   or commission Starter extractions now?
4. HR suite — keep ERP-unique as one product package, or split
   leave/attendance into a Starter `workforce` extension?
5. Legacy-table retention after flip: read-only archive extent
   (closed GL periods, payslip history) for statutory queries.
