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
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
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

SUB_CUSTOMER_REFERENCE_REASON: Final = "references Sub-tagged customer (M11 pending)"
SUB_PAYMENT_INTENT_REASON: Final = "payment_intent rows reference Sub-tagged customer_payment (routing pending)"


@dataclass(frozen=True)
class SubCustomerRef:
    """How a child spec's rows reference a customer (rows referencing a
    Sub-tagged customer become named row exceptions, M11 pending).

    Direct: `column` on the source row IS the customer id. Via: `column`
    is a key of `via_table` (bare name), whose `via_customer` column is
    the customer id (e.g. quote_line -> quote -> customer).
    """

    column: str
    via_table: str | None = None
    via_key: str | None = None
    via_customer: str | None = None


def tagged_customer_reference_sql(ref: SubCustomerRef) -> str:
    """SELECT of the values of `ref.column` that point at Sub-tagged customers."""

    if ref.via_table is None:
        return "SELECT customer_id FROM customer WHERE dotmac_sub_id IS NOT NULL"
    for name in (ref.via_table, ref.via_key, ref.via_customer):
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"unsafe SubCustomerRef identifier {name!r}")
    return (
        f"SELECT v.{ref.via_key} FROM {ref.via_table} v "  # noqa: S608
        f"JOIN customer c ON c.customer_id = v.{ref.via_customer} WHERE c.dotmac_sub_id IS NOT NULL"
    )


def norm_ref(value: object) -> str:
    """Compare ids across hex/dashed UUID renderings."""

    return str(getattr(value, "hex", value)).replace("-", "").lower()


def key_digest(keys: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()


def key_evidence(keys: list[str], side_name: str) -> dict[str, object]:
    ordered = sorted(keys)
    return {"count": len(ordered), "keys_sha256": key_digest(ordered), "keys_file": side_name}


def write_side_file(path: Path, payload: dict[str, list[str]]) -> None:
    """Full sorted key lists, owner-only (mode 600). Raises OSError on failure."""

    body = json.dumps({k: sorted(v) for k, v in sorted(payload.items())}, indent=1) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def payment_intents_on_sub_payments(source: sa.Connection) -> list[str]:
    """Read-only: payment_intent ids whose customer_payment is Sub-tagged."""

    rows = source.execute(
        sa.text(
            "SELECT pi.intent_id FROM payment_intent pi "
            "JOIN customer_payment cp ON cp.payment_id = pi.customer_payment_id "
            "WHERE cp.dotmac_sub_id IS NOT NULL"
        )
    )
    return sorted(str(r[0]) for r in rows)


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


__all__ = [
    "LEGACY_SUB_ORIGIN_TABLES",
    "SUB_CUSTOMER_REFERENCE_REASON",
    "SUB_ORIGIN_COLUMN",
    "SUB_PAYMENT_INTENT_REASON",
    "SubCustomerRef",
    "SubOriginPolicy",
    "is_sub_origin_table",
    "key_digest",
    "key_evidence",
    "norm_ref",
    "payment_intents_on_sub_payments",
    "sub_tagged_customers_referenced_by_native",
    "tagged_customer_reference_sql",
    "write_side_file",
]
