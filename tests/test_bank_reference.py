"""Shared bank-reference normalisation and lock key (no database)."""

from uuid import UUID

import pytest

from erp_v2.disbursement import bank_reference as bank_ref

T = UUID("00000000-0000-0000-0000-0000000000a1")
A = UUID("00000000-0000-0000-0000-0000000000b1")


def test_normalises_trim_collapse_and_upper_case():
    assert bank_ref.normalize("  ref   one \n") == "REF ONE"


@pytest.mark.parametrize(
    "bad", ["AB\x00CD", "AB\x1fCD", "AB\x7fCD", "AB\x80CD", "AB\x85CD", "AB\x9fCD", "\x85ABC", "ABC\x9f"]
)
def test_control_characters_including_c1_are_refused(bad):
    with pytest.raises(bank_ref.BankReferenceInvalid) as exc:
        bank_ref.normalize(bad)
    assert exc.value.reason == "control"


@pytest.mark.parametrize("bad", [None, "", "  ", "ab", "x" * 256])
def test_length_is_enforced_after_normalising(bad):
    with pytest.raises(bank_ref.BankReferenceInvalid) as exc:
        bank_ref.normalize(bad)
    assert exc.value.reason == "length"


def test_lock_key_is_stable_signed_64_bit_and_keyed_on_all_three_parts():
    key = bank_ref.lock_key(T, A, "REF ONE")
    assert key == bank_ref.lock_key(T, A, "REF ONE")
    assert -(2**63) <= key < 2**63
    assert key != bank_ref.lock_key(T, A, "REF TWO")
    assert key != bank_ref.lock_key(A, T, "REF ONE")
    assert key != bank_ref.lock_key(T, UUID(int=2), "REF ONE")
