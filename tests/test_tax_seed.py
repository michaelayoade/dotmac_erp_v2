"""Pure spec/band validation for the tax seed operator CLI."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from erp_v2.tax_seed import OperatorRefusal, parse_seed_spec, select_bands

SNAPSHOT = "a" * 64
CANDIDATE = "b" * 40


def _spec(codes):
    return json.dumps(
        {
            "version": 1,
            "snapshot_sha256": SNAPSHOT,
            "candidate_revision": CANDIDATE,
            "codes": codes,
        }
    ).encode()


def _paye(**overrides):
    entry = {
        "code": "PAYE",
        "name": "PAYE (NTA 2025)",
        "tax_kind_code": "INCOME_TAX",
        "fact_kind": "income_tax",
        "recognition_basis_code": "payroll",
        "transaction_side": "withholding",
        "calculation_method": "progressive",
        "treatment_code": "standard_rated",
        "calculation_base_code": "source_amount",
        "band_names": ["NTA 2025 - 0%", "NTA 2025 - 15%"],
    }
    entry.update(overrides)
    return entry


def test_parse_seed_spec_accepts_reviewed_shape():
    codes = parse_seed_spec(_spec([_paye()]), snapshot_sha256=SNAPSHOT, candidate_revision=CANDIDATE)
    assert codes[0].code == "PAYE"
    assert codes[0].band_names == ("NTA 2025 - 0%", "NTA 2025 - 15%")


def test_parse_seed_spec_refuses_percentage_and_extras():
    with pytest.raises(OperatorRefusal, match="progressive_seed_only"):
        parse_seed_spec(
            _spec([_paye(calculation_method="percentage")]),
            snapshot_sha256=SNAPSHOT,
            candidate_revision=CANDIDATE,
        )
    with pytest.raises(OperatorRefusal, match="invalid_seed_code_schema"):
        parse_seed_spec(
            _spec([_paye(unexpected="x")]),
            snapshot_sha256=SNAPSHOT,
            candidate_revision=CANDIDATE,
        )
    with pytest.raises(OperatorRefusal, match="seed_snapshot_mismatch"):
        parse_seed_spec(_spec([_paye()]), snapshot_sha256="c" * 64, candidate_revision=CANDIDATE)


def _band(name, sequence, low, high, rate, eff=date(2026, 1, 1), to=None):
    return {
        "name": name,
        "sequence": sequence,
        "min_amount": Decimal(low),
        "max_amount": None if high is None else Decimal(high),
        "rate": Decimal(rate),
        "effective_from": eff,
        "effective_to": to,
    }


def test_select_bands_orders_and_proves_contiguity():
    rows = [
        _band("NTA 2025 - 15%", 2, "800000", None, "0.15"),
        _band("NTA 2025 - 0%", 1, "0", "800000", "0"),
    ]
    effective, bands = select_bands(rows, ("NTA 2025 - 0%", "NTA 2025 - 15%"))
    assert effective == date(2026, 1, 1)
    assert [b.rate for b in bands] == [Decimal("0"), Decimal("0.15")]
    assert bands[1].upper_bound is None


def test_select_bands_refuses_gaps_and_missing():
    rows = [_band("NTA 2025 - 0%", 1, "0", "800000", "0")]
    with pytest.raises(OperatorRefusal, match="legacy_band_missing"):
        select_bands(rows, ("NTA 2025 - 15%",))
    gapped = rows + [_band("NTA 2025 - 15%", 2, "900000", None, "0.15")]
    with pytest.raises(OperatorRefusal, match="legacy_band_not_contiguous"):
        select_bands(gapped, ("NTA 2025 - 0%", "NTA 2025 - 15%"))
