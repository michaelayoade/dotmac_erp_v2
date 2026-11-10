"""Sub-origin policy for the general legacy importer (F1 part 2).

ERP v2 is never the authority for a Sub-originated record. Legacy ERP
mirrors Sub records and tags them with `dotmac_sub_id`; any legacy
source table carrying that column must declare, per pairing, what the
importer does with tagged rows. Nothing is inferred: a pairing over a
tagged table without a declared policy fails the structural test, and
a source that carries the column without a declared policy is not
executable.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa

SUB_ORIGIN_COLUMN: Final = "dotmac_sub_id"


class SubOriginPolicy(str, enum.Enum):
    # Rows with `dotmac_sub_id IS NOT NULL` are never read for import;
    # the executor counts them (with keys) in the receipt census.
    EXCLUDE = "exclude"
    # The source table does not carry the column.
    NOT_APPLICABLE = "not_applicable"


# Legacy tables that carry a `dotmac_sub_id` column, by bare table name
# (the legacy database is multi-schema; all three live in schema `ar`).
# Source: dotmac_erp @ 74fa788a, `git grep dotmac_sub_id -- app/models`:
#   app/models/finance/ar/customer.py:209          (customer)
#   app/models/finance/ar/customer_payment.py:211  (customer_payment)
#   app/models/finance/ar/invoice.py:318           (invoice)
# and alembic/versions/20260616_add_dotmac_sub_integration.py, which adds
# the column to exactly these three tables. Checked in as a constant:
# the legacy repo is never imported at runtime. Re-derive on a new
# legacy revision.
LEGACY_SUB_ORIGIN_TABLES: Final = frozenset({"customer", "customer_payment", "invoice"})

_KEY_CAP: Final = 500


def is_sub_origin_table(source: str | None) -> bool:
    return source is not None and source.rpartition(".")[2] in LEGACY_SUB_ORIGIN_TABLES


def sub_tagged_customers_referenced_by_native(source: sa.Connection) -> dict[str, object]:
    """Census: Sub-tagged customers that ERP-native invoices or payments reference.

    Read-only. Table names resolve through the connection search_path,
    as enrolment SQL does. These customers are NOT imported (customer
    pairings are `exclude`, pending Michael's M11 ruling).
    """

    def keys(sql: str) -> list[str]:
        return sorted(str(r[0]) for r in source.execute(sa.text(sql)))

    via_invoices = keys(
        "SELECT DISTINCT c.customer_id FROM customer c JOIN invoice i ON i.customer_id = c.customer_id "
        "WHERE c.dotmac_sub_id IS NOT NULL AND i.dotmac_sub_id IS NULL"
    )
    via_payments = keys(
        "SELECT DISTINCT c.customer_id FROM customer c JOIN customer_payment p ON p.customer_id = c.customer_id "
        "WHERE c.dotmac_sub_id IS NOT NULL AND p.dotmac_sub_id IS NULL"
    )
    union = sorted(set(via_invoices) | set(via_payments))
    return {
        "count": len(union),
        "keys": union,
        "via_invoices": via_invoices,
        "via_payments": via_payments,
        "imported": False,
        "pending_ruling": "M11",
    }


def capped(keys: Sequence[str]) -> tuple[list[str], bool]:
    return list(keys[:_KEY_CAP]), len(keys) > _KEY_CAP


__all__ = [
    "LEGACY_SUB_ORIGIN_TABLES",
    "SUB_ORIGIN_COLUMN",
    "SubOriginPolicy",
    "capped",
    "is_sub_origin_table",
    "sub_tagged_customers_referenced_by_native",
]
