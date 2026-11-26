"""Committed route inventory (R0 F1/F2).

`surface_selection` keeps a NEW module, router set or surface from mounting, but
a route added inside an already-selected feature mounts. A pin bump that adds,
removes or renames a mounted route fails here, in the pin PR.

To update after a deliberate change, copy the JSON printed by the failure into
`tests/route_inventory.json` and review the diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotmac_kernel.testing.route_inventory import route_inventory

from erp_v2.main import app

SNAPSHOT = Path(__file__).with_name("route_inventory.json")


def test_mounted_routes_match_the_committed_snapshot() -> None:
    actual = [list(row) for row in route_inventory(app)]
    expected = json.loads(SNAPSHOT.read_text())
    if actual != expected:
        added = [row for row in actual if row not in expected]
        removed = [row for row in expected if row not in actual]
        pytest.fail(
            f"route inventory changed: +{len(added)} -{len(removed)}\n"
            f"ADDED: {json.dumps(added)}\nREMOVED: {json.dumps(removed)}\n"
            f"FULL:{json.dumps(actual)}"
        )
