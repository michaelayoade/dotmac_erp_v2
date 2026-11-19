"""AR opening assembly: the one derivation of the manifest, and the ERP's four opening authorities (rev 2, 4.3).

`ArAssembly.manifest()` is the single place the complete manifest is derived from (snapshot rows + census + minted
account ids). The plan uses it to build commands; the import verifier uses it again to RE-DERIVE, so a forged or
edited command or manifest cannot pass. Nothing here talks to a database: DB reads (account ids, role oid) are
injected values.

Authorities supplied to billing's `OpeningLaneAssemblyAuthorityV1`:
- import verifier: re-derivation (`verify_import_v2`); reversal is never qualified (`None`);
- provision verifier: window manifest == the re-derived manifest, principal == the e2_0029 role (name AND oid);
- seal verifier: admitted population == the census carryable set exactly, no holds, the expected coupled fence;
- hold approvals: a port that ALWAYS refuses (holds are never used; an operational hold aborts the run).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from uuid import UUID

from dotmac_billing import (
    ImportOpeningAccountV2,
    OpeningImportVerificationRequestV2,
    OpeningImportVerificationV2,
    OpeningTargetV1,
    opening_account_command_digest_v2,
    opening_manifest_fingerprint_v2,
)
from dotmac_billing.contracts import PartyDocumentSnapshotV1
from dotmac_billing.opening_contracts import OpeningCaptureV1, OpeningPartitionV1
from dotmac_billing.opening_receivable_contracts import OpeningManifestV2
from dotmac_billing.opening_window_contracts import (
    OpeningWindowProvisionVerificationRequestV1,
    OpeningWindowProvisionVerificationV1,
    OpeningWindowSealVerificationRequestV1,
    OpeningWindowSealVerificationV1,
)
from dotmac_kernel.cache import TenantScope

from erp_v2.importer.ar_census import ArCensus, PartitionKey, partition_rows
from erp_v2.importer.billing_opening import (
    MAPPING_VERSION,
    OpeningTranslation,
    OpeningTranslationContext,
    OpeningTranslationError,
    build_complete_manifest,
    build_import_command,
    translate_partition,
)

WRITER_ROLE = "erp_billing_opening_writer"
PROVISION_OPERATION = "billing.opening.window.provision.v1"
SEAL_OPERATION = "billing.opening.window.seal.v1"
IMPORT_OPERATION = "billing.opening.import.v2"


def _ref(prefix: str, value: str) -> str:
    return f"{prefix}:{value}"


def mapping_sha() -> str:
    return hashlib.sha256(MAPPING_VERSION.encode()).hexdigest()


@dataclass(frozen=True)
class ArAssembly:
    """Everything the manifest is derived from. Immutable; derivations are pure and deterministic."""

    tenant_id: UUID
    capture: OpeningCaptureV1
    census: ArCensus
    invoices: Sequence[Mapping[str, object]]  # normalised (ar_census.normalise_rows), loader flags included
    payments: Sequence[Mapping[str, object]]
    allocations: Sequence[Mapping[str, object]]
    account_ids: Mapping[PartitionKey, UUID]  # minted billing account per customer x currency
    customer_snapshot: Callable[[str], PartyDocumentSnapshotV1 | None]
    seller: PartyDocumentSnapshotV1
    precision: int = 2
    _cache: dict[str, object] = field(default_factory=dict, compare=False, repr=False)

    def _partition(self, key: PartitionKey) -> OpeningPartitionV1:
        if key not in self.account_ids:
            raise OpeningTranslationError(f"no billing account minted for partition {key[0]}/{key[1]}")
        return OpeningPartitionV1(
            source_account_key=key[0], account_id=self.account_ids[key], currency=key[1], precision=self.precision
        )

    def translations(self) -> dict[PartitionKey, OpeningTranslation]:
        cached = self._cache.get("translations")
        if cached is not None:
            return cached  # type: ignore[return-value]
        from dotmac_billing.contracts import PaymentInstructionsSnapshotV1

        cutoff = datetime.fromisoformat(self.census.cutoff)
        out: dict[PartitionKey, OpeningTranslation] = {}
        for key in self.census.carry_partitions:
            invs, pays, allocs = partition_rows(key, self.invoices, self.payments, self.allocations, cutoff)
            translation = translate_partition(
                context=OpeningTranslationContext(
                    partition=self._partition(key),
                    seller=self.seller,
                    customer_snapshot=self.customer_snapshot,
                    payment_instructions=PaymentInstructionsSnapshotV1(method_code="bank_transfer"),
                ),
                invoices=invs,
                allocations=allocs,
                payments=pays,
            )
            if not translation.carryable:
                raise OpeningTranslationError(f"census says carryable but partition {key[0]}/{key[1]} is blocked")
            out[key] = translation
        self._check_against_census(out)  # never cache an unproven derivation
        self._cache["translations"] = out
        return out

    def _check_against_census(self, translations: Mapping[PartitionKey, OpeningTranslation]) -> None:
        items = {i.source_invoice_key for t in translations.values() for i in t.items}
        lots = {lot.source_identity.key for t in translations.values() for lot in t.cash_lots}
        census_items = {r.key for r in self.census.rows if r.bucket == "CARRY_ITEM"}
        census_lots = {r.key for r in self.census.rows if r.bucket == "CARRY_LOT"}
        if items != census_items or lots != census_lots:
            raise OpeningTranslationError("derived items and lots differ from the census (ERP != census)")

    def manifest(self) -> OpeningManifestV2:
        cached = self._cache.get("manifest")
        if cached is None:
            cached = build_complete_manifest(
                tenant_id=self.tenant_id, capture=self.capture, translations=tuple(self.translations().values())
            )
            self._cache["manifest"] = cached
        return cached  # type: ignore[return-value]

    def fingerprint(self) -> str:
        return opening_manifest_fingerprint_v2(self.manifest())

    def partitions(self) -> tuple[OpeningPartitionV1, ...]:
        return tuple(sorted((t.partition for t in self.translations().values()), key=lambda p: p.source_account_key))  # type: ignore[arg-type]

    @cached_property
    def scope(self) -> TenantScope:
        return TenantScope(self.tenant_id)

    def command(
        self,
        partition: OpeningPartitionV1,
        *,
        target: OpeningTargetV1,
        migration_authority_ref: str,
        occurred_at: datetime,
        idempotency_key: str | None = None,
    ) -> ImportOpeningAccountV2:
        return build_import_command(
            manifest=self.manifest(),
            target=target,
            migration_authority_ref=migration_authority_ref,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
            partition=partition,
        )

    def population_account_ids(self) -> frozenset[UUID]:
        """Accounts the seal must find admitted: every partition that carries value (T != 0)."""

        from dotmac_billing.opening_variance_validation import carryable_accounts

        return frozenset(carryable_accounts(self.manifest()))  # type: ignore[arg-type]


class RefusingHoldApprovals:
    """Holds are never used (rev 2 section 9, R4): the approval port ALWAYS refuses."""

    def hold_approval(self, db, *, scope, request_id, subject_type, subject_id, content_digest):
        return None


@dataclass(frozen=True)
class ErpOpeningAuthority:
    """The ERP's `OpeningLaneAssemblyAuthorityV1`, plus the provision verifier (admin step, same re-derivation)."""

    assembly: ArAssembly
    migration_authority_ref: str  # "dotmac-debt-register v<N>" of Michael's go
    role_oid: int  # read from pg_roles at run time for WRITER_ROLE
    expected_coupled_watermark: str
    # The ERP #62 two-person policy as billing a13's per-window "off" | "on". Must be explicit: a provision whose
    # window binds anything else (or any value while this is None) is refused. a12 windows carry no such field.
    expected_dual_control: str | None = None

    # --- OpeningLaneAssemblyAuthorityV1 ----------------------------------------------------------------------
    def import_verifier(self) -> ErpImportVerifier:
        return ErpImportVerifier(self.assembly, self.migration_authority_ref)

    def seal_verifier(self) -> ErpSealVerifier:
        return ErpSealVerifier(self.assembly, self.migration_authority_ref, self.expected_coupled_watermark)

    def hold_approvals(self) -> RefusingHoldApprovals:
        return RefusingHoldApprovals()

    # --- provision (billing's `OpeningWindowProvisionVerifier`) ----------------------------------------------
    def provision_verifier(self) -> ErpProvisionVerifier:
        return ErpProvisionVerifier(
            self.assembly, self.migration_authority_ref, self.role_oid, self.expected_dual_control
        )


@dataclass(frozen=True)
class ErpImportVerifier:
    """Re-derivation verifier: the command is accepted only if the ERP rebuilds the SAME manifest and command."""

    assembly: ArAssembly
    migration_authority_ref: str

    def verify_import_v2(self, request: OpeningImportVerificationRequestV2) -> OpeningImportVerificationV2 | None:
        command = request.command
        asm = self.assembly
        try:
            rebuilt = asm.manifest()
        except OpeningTranslationError:
            return None
        expected_fingerprint = opening_manifest_fingerprint_v2(rebuilt)
        if (
            request.manifest_fingerprint != expected_fingerprint
            or opening_manifest_fingerprint_v2(command.manifest) != expected_fingerprint
            or command.manifest != rebuilt
            or command.scope != asm.scope
            or command.migration_authority_ref != self.migration_authority_ref
            or command.manifest.capture.source_system != "legacy-erp"
            or command.partition not in {t.partition for t in asm.translations().values()}
            or command.target.account_id != command.partition.account_id
        ):
            return None
        # Rebuild the command from the re-derived manifest and the command's own non-manifest fields.
        expected = ImportOpeningAccountV2(
            scope=asm.scope,
            manifest=rebuilt,
            partition=command.partition,
            target=command.target,
            migration_authority_ref=self.migration_authority_ref,
            idempotency_key=command.idempotency_key,
            occurred_at=command.occurred_at,
        )
        if opening_account_command_digest_v2(expected) != request.command_digest:
            return None
        census = asm.census.sha256
        snapshot = asm.census.snapshot_sha256
        return OpeningImportVerificationV2(
            operation=IMPORT_OPERATION,
            scope=command.scope,
            command_digest=request.command_digest,
            manifest_fingerprint=expected_fingerprint,
            partition=command.partition,
            target=command.target,
            installation_id=command.manifest.capture.installation_id,
            migration_authority_ref=command.migration_authority_ref,
            source_authority_ref=command.manifest.capture.source_authority_ref,
            source_binding_proof_ref=_ref("snapshot-sha256", snapshot),
            cash_lifecycle_proof_ref=_ref("census-sha256", census),
            receivable_lifecycle_proof_ref=_ref("census-sha256", census),
            documentary_tax_mapping_proof_ref=_ref("mapping-sha256", mapping_sha()),
            historical_discharge_mapping_proof_ref=_ref("mapping-sha256", mapping_sha()),
            migration_authority_proof_ref=_ref("debt-register", self.migration_authority_ref),
        )

    def verify_import_reversal_v2(self, request):
        return None  # an opening import is never reversed through this assembly


@dataclass(frozen=True)
class ErpProvisionVerifier:
    assembly: ArAssembly
    migration_authority_ref: str
    role_oid: int
    expected_dual_control: str | None = None

    def verify_provision(
        self, request: OpeningWindowProvisionVerificationRequestV1
    ) -> OpeningWindowProvisionVerificationV1 | None:
        window = request.command.window
        asm = self.assembly
        try:
            rebuilt = asm.manifest()
        except OpeningTranslationError:
            return None
        fingerprint = opening_manifest_fingerprint_v2(rebuilt)
        if (
            window.manifest_version != 2
            or window.manifest_fingerprint != fingerprint
            or window.manifest != rebuilt
            or window.scope != asm.scope
            or window.principal_name != WRITER_ROLE
            or window.principal_oid != self.role_oid
            or window.provision_authority_ref != self.migration_authority_ref
            or (hasattr(window, "dual_control") and window.dual_control != self.expected_dual_control)
        ):
            return None
        return OpeningWindowProvisionVerificationV1(
            operation=PROVISION_OPERATION,
            scope=window.scope,
            window_id=window.window_id,
            epoch=window.epoch,
            manifest_fingerprint=fingerprint,
            principal_name=window.principal_name,
            principal_oid=window.principal_oid,
            command_digest=request.command_digest,
            authority_ref=window.provision_authority_ref,
            complete_population_proof_ref=_ref("census-sha256", asm.census.sha256),
            authority_proof_ref=_ref("role", f"{WRITER_ROLE}:{self.role_oid}"),
        )


@dataclass(frozen=True)
class ErpSealVerifier:
    assembly: ArAssembly
    migration_authority_ref: str
    expected_coupled_watermark: str

    def verify_seal(self, request: OpeningWindowSealVerificationRequestV1) -> OpeningWindowSealVerificationV1 | None:
        command = request.command
        asm = self.assembly
        try:
            expected_accounts = asm.population_account_ids()
            fingerprint = asm.fingerprint()
        except OpeningTranslationError:
            return None
        admitted = [a.billing_account_id for a in command.admissions]
        if (
            command.scope != asm.scope
            or command.manifest_fingerprint != fingerprint
            or len(admitted) != len(set(admitted))
            or set(admitted) != set(expected_accounts)
            or command.held_account_ids
            or command.operational_holds  # no holds: an operational hold aborts the run for a re-plan
            or command.coupled_watermark != self.expected_coupled_watermark
            or command.authority_ref != self.migration_authority_ref
            or any(a.manifest_fingerprint != fingerprint for a in command.admissions)
        ):
            return None
        return OpeningWindowSealVerificationV1(
            operation=SEAL_OPERATION,
            scope=command.scope,
            window_id=command.window_id,
            epoch=command.epoch,
            manifest_fingerprint=fingerprint,
            command_digest=request.command_digest,
            population_digest=request.population_digest,
            population_count=len(command.admissions),
            coupled_watermark=command.coupled_watermark,
            authority_ref=command.authority_ref,
            complete_population_proof_ref=_ref("census-sha256", asm.census.sha256),
            coupled_watermark_proof_ref=_ref("coupled-watermark", self.expected_coupled_watermark),
            authority_proof_ref=_ref("debt-register", self.migration_authority_ref),
        )


__all__ = [
    "WRITER_ROLE",
    "ArAssembly",
    "ErpImportVerifier",
    "ErpOpeningAuthority",
    "ErpProvisionVerifier",
    "ErpSealVerifier",
    "RefusingHoldApprovals",
]
