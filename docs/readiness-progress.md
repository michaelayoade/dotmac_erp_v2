# ERP v2 readiness checkpoint — 2026-11-11

Production traffic continues to use the existing ERP. Historical data migration
does not require inventing a new payroll approver policy.

## Verified candidate

Latest qualified candidate: `7e3da63a2d5fb2fd538153ffd159c8105d3a8f1f`,
with released Payables `0.1.0a2`. Its published image is
`ghcr.io/michaelayoade/dotmac_erp_v2@sha256:5489105d5844ea333681bd6a6eeb4bfdf6835d3fa094364179ca36b5f5773cc0`
([build 42950394820](https://github.com/michaelayoade/dotmac_erp_v2/actions/runs/42950394820)).
Merged-source [CI 42932721104](https://github.com/michaelayoade/dotmac_erp_v2/actions/runs/42932721104)
passed 347 tests. Dedicated-server qualification used the published image
without source mounts: all 106 tracked source files matched Git, the exact
30 stamped heads matched, and historical NULL/duplicate-number protection,
new-create validation, identity isolation and guarded readiness passed.

The following records describe earlier qualification and retain its provenance:

- PR #7 merged at 6d3903aec50287b6cbbad6caeb3cfe3fd475650d.
- Merged-source CI passed 313 tests ([run 42829910186](https://github.com/michaelayoade/dotmac_erp_v2/actions/runs/42829910186)).
- Published image: ghcr.io/michaelayoade/dotmac_erp_v2@sha256:207f7cf9b875c14085e23d2fd06056b7e561dd351908246bc31a7181f87f408f
  ([build 42830558772](https://github.com/michaelayoade/dotmac_erp_v2/actions/runs/42830558772)).
- Dedicated-server proof used that image without source mounts: all 102 tracked
  source files matched Git; clean genesis matched the exact 30 stamped heads;
  identity plan/apply/replay, password upgrade, narrow-runtime tenant isolation
  and guarded database readiness passed. Owned proof resources were removed.
- A selected existing staff member entered their password directly in the
  historical preview. Authenticated home, staff-only grant, and supplier
  creation GET refusal (403) passed. That human proof used the earlier image
  with the recorded login-template override; retain its provenance separately
  from the published-image synthetic proof.

Private receipts retain snapshot, manifest, cohort, image and run bindings.
Account identifiers, passwords, credential hashes, session tokens and row-level
exception details do not belong in public documentation or CI output.

## Evidence review workspaces

The payroll readiness GET requires erp.payroll.manage. It compares recorded
calculations with component totals and evidence references and shows gaps.
It does not establish roster completeness or approval authority, create policy,
approve/finalize a run, post a journal or pay anyone.

The banking overlap GET requires the existing financial permission. It compares
receipt-backed lines with other imported lines in the same tenant, bank account,
currency and precision. Shared references and same-date/amount evidence remain
review candidates; neither establishes an automatic merge or provider authority.
Results are bounded and private. No provider receiver or polling is activated.

The review workspaces landed in PR #8 and are included in the latest candidate.
Their published-image PostgreSQL proof passed; read-only review does not
authorize payroll execution or banking activation.

## Historical replay and outstanding payables structures

The separate clean historical replay on the latest image read 1,317,234 rows,
wrote 1,275,724 and retained 66,338 raw exception entries (`complete=False`,
exit 3). Compared with the earlier `6d3903a` replay using the same frozen
snapshot, 4,107 additional rows landed and raw exceptions fell by 4,107.
These are receipt-entry counts, not a count of distinct affected records.

Of 2,204 supplier invoice headers, 1,883 landed, including 1,151 of the
1,208 uncaptured supplier numbers preserved as NULL. Missing headers and
lines were accounted for by exact row or parent exceptions; known invoice
and line amounts, identity, supplier, currency and document numbers were
preserved. No data was adjusted to satisfy destination constraints.

AP open positions remain unresolved. The numeric comparison covered 1,563
landed source POSTED/PARTIALLY_PAID/PAID invoices and found 138 residual
differences. The destination contained **zero payment obligations and zero
settlement observations**. The other 1,425 zero comparisons therefore do
not prove liability structures or historical discharge; APPROVED and
unlanded invoices were outside that comparison. Direct SQL status changes
do not invoke Payables' obligation-creation service. Reordering the existing
settlement import cannot supply missing historical recognition or payment
evidence. A tenant-scoped diagnostic must distinguish missing structures
from monetary differences before an owner-controlled historical admission
can be rehearsed.

Ledger reconciliation matched all 445,950 rows and 1,080 account/period
groups with zero differences. Clean backup restoration reproduced the
ledger and all six AP table fingerprints (four AP tables were empty).
Genesis took 27.72 seconds, import 1,075.96 seconds and recovery 69.93
seconds. The 127,410,016-byte backup has SHA-256
`2b52e6db1d0d6000050ca1ad089821f4795957aa584a313ef7f40e08444ebb17`.
This proves historical baseline restoration, not post-financial-write
recovery, a current production snapshot or production writer fencing.
Owned disposable resources were removed; private receipts and backup remain.

## Historical supplier payment import boundary

The generic importer retains every source supplier payment as the named
`historical_ap_payment_not_settlement_evidence` refusal. Legacy payment states
and allocations cannot insert Payables settlement observations, even when an
invoice already has an obligation. Original payment, allocation and journal
evidence remains in the frozen snapshot for a separate owner admission contract.

The refusal census reads the complete resolved source table independently of
destination mappings and parent filters. It writes no destination rows. Missing,
ambiguous or unreadable sources remain incomplete; a verified empty source is
reported separately. Refused rows count in the import receipt and keep its
exit status at 3. No bank confirmation or liability reduction is inferred.

## Remaining gates

| Area | Required evidence or next action |
| --- | --- |
| Historical replay | The latest historical replay and clean restoration passed the preservation checks above. The receipt remains incomplete; a final frozen production snapshot still needs its own rehearsal and dispositions. |
| Supplier numbers | Payables a2 and ERP PR #9 landed. Preserve uncaptured numbers as NULL; 1,151 landed and the remaining 57 stay named constraint exceptions. No invented numbers or balance adjustments. |
| Supplier balances | Separate missing obligations from numeric residual differences, establish evidence-supported Payables historical admission, and reconcile all scoped AP cohorts before acceptance. No inferred cash settlements or duplicate accounting consequences for imported GL. |
| Journals | Preserve posted history and keep source unposted journals unposted. Retain approval evidence without treating it as a posting instruction. |
| Department/asset/HR gaps | Preserve source UUIDs, references and known facts. Retain unsupported/unclassified records in the reconciled exception register; do not deduplicate departments or invent asset condition. |
| Payroll and tax | Preserve historical evidence. PAYE bindings, net-payable account evidence and actor provenance remain explicit inputs. Preparation/approval roles are needed for new operational approvals, not historical transfer. |
| Payments | Incoming intents/confirmed cash are separate from outbound payouts. Treasury instruction ownership has not been allocated/released; connector transport alone does not authorize payment. |
| Banking | Reconcile manual/provider overlap, establish owner-controlled bindings and scoped machine admission, then prove replay/isolation before activation. |
| Sub-origin books | Keep approved importer exclusions. Integrator observations supply accounting consequences; align the start cursor to the final snapshot cutoff to avoid reposting imported GL history. |
| Inventory | Reconcile quantities/valuation with the existing evidenced costing policy. Do not invent an opening value. |
| Recovery | Prove clean backup restoration and retain its byte digest/timings. This does not prove recovery after accepted financial writes or production writer fencing. |

## Production review packet

Bind one final image/dependency set to one frozen snapshot; inventory and fence
every legacy writer; import into a clean destination; reconcile GL by
account/period, open AR/AP, payroll/YTD liabilities and inventory; disposition
each exception class; prove staff access and denied financial actions; and
record measured downtime, backup/restore evidence and ownership.

State the write boundary. Before accepted v2 writes, recovery can restore the
frozen baseline. After accepted financial writes, returning to the old database
can discard valid business events; recovery requires a reconciled repair/replay
procedure. No production route switch, balance adjustment or live
payroll/payment execution is authorized by this checkpoint.

