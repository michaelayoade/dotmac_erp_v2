"""Separation-of-duties stamp verdict for dotmac-payables a7 (D-9).

dotmac-payables 0.1.0a7 takes a keyword-only ``separation_of_duties: bool = False`` on create, approve, record and
reverse supplier payment, and stamps the policy it was given on the row (``SupplierPayment.separation_enforced``,
immutable, set at preparation). ``supplier_payments.service`` passes
``separation_of_duties=governance.separation_of_duties(db, tenant.id)`` explicitly at all four call sites (signature
drift is a TypeError, not a silent default OFF) and reads the stamp back after every write.

pa_0007 backfills existing rows ``separation_enforced = true``: with the policy OFF, every later step on such a row
writes a ``sod_stamp_flagged`` event and still needs two people, and replaying an old prepare raises Conflict.
``python -m erp_v2.governance.sod_verification`` reports them read-only.

Stamp read-back (never silently accepted):

* at preparation the stamp must equal the policy that was passed; otherwise the payment is refused
  (``sod_stamp_mismatch``) and the savepoint rolls the row back;
* at a later step a stamp that differs from the policy is flagged with an audit event. A relaxed row
  (stamp false) while the policy is ON is still enforced by payables because the policy is passed again and
  the stricter value wins. A stricter row (stamp true) while the policy is OFF is the documented "a row keeps
  the stamp it was created with".
"""

from __future__ import annotations

from enum import StrEnum


class StampVerdict(StrEnum):
    MATCH = "match"
    RELAXED_UNDER_POLICY_ON = "relaxed_under_policy_on"
    STRICTER_THAN_POLICY = "stricter_than_policy"


def stamp_verdict(*, stamp: bool, policy: bool) -> StampVerdict:
    if stamp == policy:
        return StampVerdict.MATCH
    return StampVerdict.STRICTER_THAN_POLICY if stamp else StampVerdict.RELAXED_UNDER_POLICY_ON
