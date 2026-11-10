# ERP v2 readiness checkpoint — 2026-11-10

Production traffic continues to use the existing ERP. Historical data migration
does not require inventing a new payroll approver policy.

## Verified candidate

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

The combined source-mounted regression suite passed 347 tests against the
verified released wheel base. These additional workspaces still require their
own hosted checks and image adoption; the candidate above does not contain them.

## Remaining gates

| Area | Required evidence or next action |
| --- | --- |
| Historical replay | Rebuild a clean disposable destination with the published image, preserve the receipt and compare account/period ledger balances. An incomplete import stays incomplete even if its exit code is expected. |
| Supplier numbers | Preserve uncaptured numbers as NULL under the approved ruling. Released payables a1 still rejects them; upstream PR #1032 and its release must land before updating the ERP pin/lock and repeating genesis/import. |
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

