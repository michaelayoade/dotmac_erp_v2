"""Customer add/edit screen (F-F, legacy parity): list, new, edit, status, notes and the finance fields.

The owning module (dotmac-customers) stays the single writer of its own tables: this package calls its service
commands and never UPDATEs ``mod_customers``. The finance fields legacy kept on the customer (trading name, tax id,
payment terms, primary contact, billing address) live in the ERP-owned ``erp_customer_finance`` (e2_0035). See
``docs/customers.md``.
"""
