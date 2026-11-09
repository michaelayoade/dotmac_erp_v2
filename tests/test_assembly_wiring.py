"""Runtime wiring: full released composition, readiness probes the DB."""

from __future__ import annotations

from dotmac_kernel.planes import ModulePlane

from erp_v2 import readiness
from erp_v2.assembly import assembly

# Latest RELEASED version per mounted module (waves 1-6). A pin bump
# updates this dict, pyproject.toml and EXPECTED_HEADS together; never
# bump to a version whose changelog says UNRELEASED (numbering a4,
# accounting a2, billing a11, payments a4, ...).
EXPECTED_MODULE_VERSIONS = {
    "erp_shell.readiness": None,
    "erp_academy_sync": None,
    "erp_web": None,
    "erp_revenue": None,
    "erp_banking": None,
    "erp_payroll": None,
    "accounting": "0.1.0a1",
    "ai_operations": "0.1.0a1",
    "analytics": "0.1.0a1",
    "approvals": "0.1.0a8",
    "assets": "0.1.0a1",
    "banking": "0.1.0a1",
    "billing": "0.1.0a10",
    "collections": "0.1.0a1",
    "commercial_agreements": "0.1.0a4",
    "customers": "0.1.0a1",
    "documents": "0.1.0a1",
    "durable_timers": "0.1.0a1",
    "expenses": "0.1.0a1",
    "files": "0.1.0a7",
    "finance": "0.2.0a1",
    "forms": "0.2.0a1",
    "imports": "0.2.0a1",
    "inbox": "0.1.0a3",
    "inventory": "0.2.0a1",
    "numbering": "0.1.0a3",
    "party": "0.1.0a1",
    "payables": "0.1.0a1",
    "payments": "0.1.0a3",
    "payroll": "0.1.0a2",
    "people": "0.1.0a2",
    "procurement": "0.1.0a1",
    "projects": "0.1.0a1",
    "sales": "0.1.0a1",
    "tax": "0.1.0a7",
    "ticketing": "0.1.0a4",
    "workflow_runtime": "0.1.0a1",
}


def test_all_released_modules_mounted_at_released_pins() -> None:
    versions = {getattr(m, "code", getattr(m, "name", "")): getattr(m, "version", None) for m in assembly.modules}
    assert versions == EXPECTED_MODULE_VERSIONS


def test_plane_selections() -> None:
    # Kernel normalizes plane order; compare as sets. files keeps its
    # platform plane (legacy evidence); other selectable modules are
    # tenant-only here.
    selections = {s.module: set(s.planes) for s in assembly.module_planes}
    assert selections == {
        "approvals": {ModulePlane.TENANT},
        "billing": {ModulePlane.TENANT},
        "collections": {ModulePlane.TENANT},
        "durable_timers": {ModulePlane.TENANT},
        "files": {ModulePlane.TENANT, ModulePlane.PLATFORM},
        "numbering": {ModulePlane.TENANT},
        "ticketing": {ModulePlane.TENANT},
    }


def test_tenancy_is_multi() -> None:
    # ERP is multi-tenant (legacy product_assembly.py declares the same).
    assert assembly.tenancy == "multi"


def test_readiness_probe_shell_mode_without_dsn(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    ok, _ = readiness.readiness_probe()
    assert ok is True


def test_readiness_probe_fails_when_dsn_declared_but_runtime_missing(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:x@127.0.0.1:1/none")

    def _raise():
        from dotmac_kernel.session_runtime import NoDatabaseRuntimeError

        raise NoDatabaseRuntimeError("unbound")

    monkeypatch.setattr(readiness, "resolve_database_runtime", _raise)
    ok, _ = readiness.readiness_probe()
    assert ok is False
