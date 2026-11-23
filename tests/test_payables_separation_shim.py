"""Payables a7 preparation (D-9) that is valid before and after the pin: the shim and the verdict."""

from __future__ import annotations

from uuid import uuid4

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


def test_before_payables_a7_the_shim_passes_nothing(monkeypatch):
    monkeypatch.setattr(separation, "payables_separation_supported", lambda: False)
    monkeypatch.setattr(separation.governance, "separation_of_duties", lambda db, t: pytest.fail("must not read"))
    assert separation.payables_kwargs(object(), uuid4()) == {}


@pytest.mark.parametrize("policy", [True, False])
def test_with_payables_a7_the_shim_passes_the_tenant_policy_by_keyword(monkeypatch, policy):
    monkeypatch.setattr(separation, "payables_separation_supported", lambda: True)
    monkeypatch.setattr(separation.governance, "separation_of_duties", lambda db, t: policy)
    assert separation.payables_kwargs(object(), uuid4()) == {"separation_of_duties": policy}


def test_support_detection_requires_the_keyword_on_every_governed_function(monkeypatch):
    class Stub:
        def create_supplier_payment(self, *, separation_of_duties=False): ...
        def approve_supplier_payment(self, *, separation_of_duties=False): ...
        def record_supplier_payment(self, *, separation_of_duties=False): ...
        def reverse_supplier_payment(self): ...

    stub = Stub()
    monkeypatch.setattr(separation, "payables", stub)
    assert separation.payables_separation_supported() is False
    stub.reverse_supplier_payment = lambda *, separation_of_duties=False: None
    assert separation.payables_separation_supported() is True


def test_the_pin_and_the_support_detection_agree():
    """Pinned a6 -> unsupported; once the pin moves to a7 this must flip with it (no code change needed)."""
    from importlib.metadata import version

    from packaging.version import Version

    pinned = version("dotmac-payables")
    assert separation.payables_separation_supported() == (Version(pinned) >= Version("0.1.0a7")), pinned
