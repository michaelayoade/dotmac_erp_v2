"""This assembly's migration prerequisite and plane decisions.

Each adopted module wave adds its plane selection here together with
its released pin; decisions are never derived from installed package
order. Wave 1 mirrors legacy ERP's selections (`app/migration_planes.py`)
exactly, so the destination storage shape matches the source the
transform reads — the per-module rationales there remain authoritative:

- numbering: TENANT only. The vendor control plane composes the
  platform plane in its own deployment.
- files: TENANT + PLATFORM. Legacy production carries live grants on
  `mod_files.platform_stored_files` and no row-count oracle proves it
  empty; the full set keeps `fi_0002` from ever being handed a table
  it could legally drop, and retains the destination for those rows.
- imports declares no selectable plane set (tenant-only); no entry.
"""

from __future__ import annotations

from typing import Final

from dotmac_kernel.planes import ModulePlane, ModulePlaneSelection
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    PARTY_PERSON_CATALOG_V1,
    PLATFORM_AUDIT_LOG_V1,
    TENANT_AUDIT_LOG_V1,
    TENANT_SCOPE_CATALOG_V1,
    PrerequisiteBinding,
)

ASSEMBLY_PREREQUISITE_BINDINGS: Final[tuple[PrerequisiteBinding, ...]] = (
    PrerequisiteBinding(
        prerequisite=TENANT_SCOPE_CATALOG_V1.name,
        provider_revision="0001_initial_tenant_schema",
        provider_owner="kernel",
    ),
    PrerequisiteBinding(
        prerequisite=MODULE_DATABASE_ROLES_V1.name,
        provider_revision="0001_initial_tenant_schema",
        provider_owner="kernel",
    ),
    # Name the revision that supplies the whole ledger, not the lineage root.
    PrerequisiteBinding(
        prerequisite=IDEMPOTENCY_LEDGER_V1.name,
        provider_revision="0018_idempotency_one_owner",
        provider_owner="kernel",
    ),
    # Party/PartyPerson replaces the former Person table at kernel 0003
    # (people, expenses, party require the catalog effect).
    PrerequisiteBinding(
        prerequisite=PARTY_PERSON_CATALOG_V1.name,
        provider_revision="0003_party_identity",
        provider_owner="kernel",
    ),
    # The outbox relay completes at 0012 (0008 table, 0011 lease/roles,
    # 0012 platform peer; payments/approvals/billing/... require it).
    PrerequisiteBinding(
        prerequisite=OUTBOX_RELAY_V1.name,
        provider_revision="0012_platform_outbox",
        provider_owner="kernel",
    ),
    # Append-only platform audit completes at 0026; tenant audit shape
    # at 0029 (agreements requires the platform effect).
    PrerequisiteBinding(
        prerequisite=PLATFORM_AUDIT_LOG_V1.name,
        provider_revision="0026_platform_audit_log",
        provider_owner="kernel",
    ),
    PrerequisiteBinding(
        prerequisite=TENANT_AUDIT_LOG_V1.name,
        provider_revision="0029_audit_history_shape",
        provider_owner="kernel",
    ),
)

ASSEMBLY_MODULE_PLANES: Final[tuple[ModulePlaneSelection, ...]] = (
    # Selectable modules take TENANT only in this product assembly; the
    # vendor control plane composes their platform planes separately.
    # files is the one exception (see module docstring).
    ModulePlaneSelection(module="approvals", planes=(ModulePlane.TENANT,)),
    ModulePlaneSelection(module="billing", planes=(ModulePlane.TENANT,)),
    ModulePlaneSelection(module="collections", planes=(ModulePlane.TENANT,)),
    ModulePlaneSelection(module="durable_timers", planes=(ModulePlane.TENANT,)),
    ModulePlaneSelection(module="files", planes=(ModulePlane.TENANT, ModulePlane.PLATFORM)),
    ModulePlaneSelection(module="numbering", planes=(ModulePlane.TENANT,)),
    ModulePlaneSelection(module="ticketing", planes=(ModulePlane.TENANT,)),
)

__all__ = [
    "ASSEMBLY_MODULE_PLANES",
    "ASSEMBLY_PREREQUISITE_BINDINGS",
]
