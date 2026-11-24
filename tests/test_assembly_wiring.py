"""Runtime wiring: full released composition, readiness probes the DB."""

from __future__ import annotations

from dotmac_kernel.planes import ModulePlane

from erp_v2 import readiness
from erp_v2.assembly import assembly

# Selected released versions per mounted module (waves 1-6). A pin bump
# updates this dict, pyproject.toml, uv.lock and EXPECTED_HEADS together.
# Verify publication using release/tag evidence; changelog headings can lag.
EXPECTED_MODULE_VERSIONS = {
    "erp_shell.readiness": None,
    "erp_academy_sync": None,
    "erp_web": None,
    "erp_revenue": None,
    "erp_receivables_producer": None,
    "erp_customers_web": None,
    "erp_revenue_approval": None,
    "erp_supplier_credit_notes": None,
    "erp_legacy_reentry": None,
    "erp_banking": None,
    "erp_payee_bank": None,
    "erp_supplier_payments": None,
    "erp_payroll_payouts": None,
    "erp_staff_loans": None,
    "erp_journals": None,
    "erp_banking_statements": None,
    "erp_banking_reconciliation": None,
    "erp_cash_gl": None,
    "erp_access": None,
    "erp_governance": None,
    "erp_billing_gl": None,
    "erp_payroll": None,
    "erp_payroll_run": None,
    "accounting": "0.1.0a3",
    "ai_operations": "0.1.0a1",
    "analytics": "0.1.0a1",
    "approvals": "0.1.0a9",
    "assets": "0.1.0a1",
    "banking": "0.1.0a1",
    "billing": "0.1.0a13",
    "collections": "0.1.0a1",
    "commercial_agreements": "0.1.0a4",
    "customers": "0.1.0a2",
    "documents": "0.1.0a1",
    "durable_timers": "0.1.0a1",
    "expenses": "0.1.0a2",
    "files": "0.1.0a7",
    "finance": "0.2.0a1",
    "forms": "0.2.0a1",
    "imports": "0.2.0a1",
    "inbox": "0.1.0a3",
    "inventory": "0.2.0a1",
    "numbering": "0.1.0a3",
    "party": "0.1.0a1",
    "payables": "0.1.0a7",
    "payments": "0.1.0a3",
    "payroll": "0.1.0a7",
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
