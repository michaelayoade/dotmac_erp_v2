"""Payables a7 (D-9): the stamp verdict."""

from __future__ import annotations

import pytest

from erp_v2.supplier_payments import separation


@pytest.mark.parametrize(
    ("stamp", "policy", "verdict"),
    [
        (True, True, separation.StampVerdict.MATCH),
        (False, False, separation.StampVerdict.MATCH),
        (False, True, separation.StampVerdict.RELAXED_UNDER_POLICY_ON),
        (True, False, separation.StampVerdict.STRICTER_THAN_POLICY),
    ],
)
def test_stamp_verdict(stamp, policy, verdict):
    assert separation.stamp_verdict(stamp=stamp, policy=policy) is verdict
