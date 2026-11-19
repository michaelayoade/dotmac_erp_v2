"""Shared operator-CLI plumbing for the B-9 tools: private receipts, environment-only DSN, explicit APPLY."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from erp_v2.migration_ledger.common import APPLY_ENV, LedgerRefused, refuse, text


def operator(value: str | None) -> str:
    return text(value, "operator_ref_required")


def require_apply(flag: bool) -> None:
    """Dry-run is the default: a write needs the flag AND the environment switch."""
    if not flag or os.environ.get(APPLY_ENV) != "1":
        refuse("explicit_migration_ledger_apply_required")


def engine():
    import sqlalchemy as sa

    url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    if not url:
        refuse("operator_database_materials_required")
    if "DATABASE_URL" in os.environ or "PLATFORM_DATABASE_URL" in os.environ:
        refuse("serving_database_materials_forbidden")
    eng = sa.create_engine(url, echo=False, hide_parameters=True)
    if eng.dialect.name != "postgresql":
        refuse("postgresql_required")
    return eng


def legacy_engine():
    """The read-only legacy snapshot connection (``LEGACY_DATABASE_URL``), popped from the environment like the DSN."""
    import sqlalchemy as sa

    url = os.environ.pop("LEGACY_DATABASE_URL", "")
    if not url:
        refuse("operator_legacy_materials_required")
    eng = sa.create_engine(url, echo=False, hide_parameters=True)
    if eng.dialect.name != "postgresql":
        refuse("postgresql_required")
    return eng


def install_registries() -> None:
    from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions

    from erp_v2.assembly import assembly

    install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))


def read_bound(path: Path, expect_sha: str | None = None) -> tuple[bytes, str]:
    import hashlib

    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if expect_sha is not None and sha != expect_sha:
        refuse("input_sha_mismatch")
    return data, sha


def run(
    receipt_path: Path, body: Callable[[], dict[str, Any]], label: str, pre: Callable[[], None] | None = None
) -> int:
    """Reserve the 0600 receipt BEFORE any read or write, run, write the outcome. 0 ok, 3 not ready, 2 refused."""
    try:
        if pre is not None:
            pre()
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception:
        print(f"{label} refused: authorization or receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = body()
            code = 0 if payload.get("ready", True) else 3
        except LedgerRefused as exc:
            payload, code = {"ready": False, "refusal": exc.code}, 2
        except Exception as exc:
            payload, code = (
                {
                    "ready": False,
                    "refusal": f"{label.replace(' ', '_')}_command_refused",
                    "error_type": type(exc).__name__,
                },
                2,
            )
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(f"{label} receipt written" if code == 0 else f"{label} not applied; see private receipt")
    return code
