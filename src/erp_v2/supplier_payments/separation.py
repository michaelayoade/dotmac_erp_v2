"""Separation-of-duties wiring for dotmac-payables (D-9), prepared behind the payables a7 pin.

dotmac-payables 0.1.0a7 adds a keyword-only ``separation_of_duties: bool = False`` to create, approve, record and
reverse supplier payment, and stamps the policy it was given on the row (``SupplierPayment.separation_enforced``,
immutable, set at preparation). Until a7 is the pinned version those keywords do not exist, so
``payables_kwargs`` returns nothing and the a6 behaviour is unchanged. Once a7 is pinned the same call sites pass
the tenant policy and the stamp is read back after every write (see the TODO for the pin-step clean-up).

TODO(payables-a7): at the payables a7 pin (``==0.1.0a7`` in pyproject.toml, uv.lock refreshed) DELETE this
signature-sniffing shim and pass ``separation_of_duties=governance.separation_of_duties(db, tenant.id)``
explicitly at the four call sites, so signature drift becomes a TypeError instead of a silent default OFF; make
``_read_back_stamp`` unconditional. Take a read-only count at the pin of payments that pa_0007 stamps true (it
backfills existing rows ``separation_enforced = true``): with the policy OFF, every later step on those rows
writes a ``sod_stamp_flagged`` event and still needs two people, and replaying an old prepare raises Conflict.
``tests/test_payables_a7_separation_pg.py`` stops skipping on the pin.

Stamp read-back (never silently accepted):

* at preparation the stamp must equal the policy that was passed; otherwise the payment is refused
  (``sod_stamp_mismatch``) and the savepoint rolls the row back;
* at a later step a stamp that differs from the policy is flagged with an audit event. A relaxed row
  (stamp false) while the policy is ON is still enforced by payables because the policy is passed again and
  the stricter value wins. A stricter row (stamp true) while the policy is OFF is the documented "a row keeps
  the stamp it was created with".
"""

from __future__ import annotations

import inspect
from enum import StrEnum
from typing import Final
from uuid import UUID

from dotmac_payables import service as payables
from sqlalchemy.orm import Session

from erp_v2.governance import policy as governance

KEYWORD: Final = "separation_of_duties"
GOVERNED_FUNCTIONS: Final = (
    "create_supplier_payment",
    "approve_supplier_payment",
    "record_supplier_payment",
    "reverse_supplier_payment",
)


def payables_separation_supported() -> bool:
    """True when the installed dotmac-payables takes the keyword on every governed function."""
    for name in GOVERNED_FUNCTIONS:
        function = getattr(payables, name, None)
        if function is None or KEYWORD not in inspect.signature(function).parameters:
            return False
    return True


def payables_kwargs(db: Session, tenant_id: UUID) -> dict[str, bool]:
    """``separation_of_duties=<tenant policy>`` for a governed payables call (empty before payables a7)."""
    if not payables_separation_supported():
        return {}
    return {KEYWORD: governance.separation_of_duties(db, tenant_id)}


class StampVerdict(StrEnum):
    MATCH = "match"
    RELAXED_UNDER_POLICY_ON = "relaxed_under_policy_on"
    STRICTER_THAN_POLICY = "stricter_than_policy"


def stamp_verdict(*, stamp: bool, policy: bool) -> StampVerdict:
    if stamp == policy:
        return StampVerdict.MATCH
    return StampVerdict.STRICTER_THAN_POLICY if stamp else StampVerdict.RELAXED_UNDER_POLICY_ON
