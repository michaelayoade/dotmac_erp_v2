"""Downgrade of the legacy re-entry tables refuses while any permanent evidence exists."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).parents[1] / "src/erp_v2/migrations/versions/e2_0016_legacy_invoice_refs.py"
TABLES = ("legacy_invoice_references", "legacy_never_posted_cohorts", "legacy_never_posted_members")


def _module():
    spec = importlib.util.spec_from_file_location("legacy_refs_migration", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("populated", TABLES)
def test_downgrade_refuses_when_a_table_has_rows(monkeypatch, populated):
    module = _module()
    dropped = []
    bind = SimpleNamespace(scalar=lambda statement: populated in str(statement))
    monkeypatch.setattr(module, "op", SimpleNamespace(get_bind=lambda: bind, drop_table=dropped.append))
    with pytest.raises(RuntimeError, match="forward correction"):
        module.downgrade()
    assert dropped == []


def test_downgrade_drops_empty_tables(monkeypatch):
    module = _module()
    dropped = []
    bind = SimpleNamespace(scalar=lambda statement: False)
    monkeypatch.setattr(module, "op", SimpleNamespace(get_bind=lambda: bind, drop_table=dropped.append))
    module.downgrade()
    assert dropped == ["legacy_never_posted_members", "legacy_never_posted_cohorts", "legacy_invoice_references"]
