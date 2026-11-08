# Waves 2-6 transform census

Legacy tables per destination (code: `src/erp_v2/importer/waves.py`).
All transforms are `needs_ruling` until their wave rehearsal writes
the column-level spec; `e2` = ERP-unique PORT storage (HR suite,
IPSAS/public sector, leases, consolidation, fleet ops, help, ...).

| Destination | Legacy tables |
|---|---:|
| kernel | 14 |
| people | 38 |
| payroll | 11 |
| tax | 13 |
| accounting | 21 |
| banking | 8 |
| payables | 7 |
| payments | 13 |
| expenses | 12 |
| procurement | 13 |
| approvals | 4 |
| finance | 4 |
| assets | 12 |
| billing | 9 |
| commercial_agreements | 5 |
| customers | 1 |
| numbering | 1 |
| sales | 2 |
| inventory | 13 |
| projects | 5 |
| ticketing | 10 |
| analytics | 4 |
| ai_operations+PORT | 1 |
| documents | 5 |
| forms | 10 |
| inbox+PORT | 5 |
| workflow_runtime+PORT | 5 |
| PORT-adapters | 3 |
| e2 | 177 |
| **total** | **426** |

Wave order for data (composition already mounts all released modules):
2 people/payroll/tax → 3 accounting/banking/payables/payments/expenses/
procurement/approvals/finance/assets → 4 billing/customers/agreements/
sales/collections/party → 5 inventory/projects → 6 ticketing/analytics/
ai-operations/documents/forms/inbox/workflow + e2 PORT slices.
