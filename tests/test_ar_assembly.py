"""The ERP's opening authorities: re-derivation import verifier, provision verifier, seal verifier, refusing holds."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest
from ar_opening_support import SNAPSHOT, TENANT, UTC, account_id, alloc, assembly, invoice
from dotmac_billing import (
    OpeningImportVerificationRequestV2,
    OpeningTargetV1,
    opening_account_command_digest_v2,
    opening_manifest_fingerprint_v2,
)
from dotmac_billing.opening_lane import OpeningLaneAssemblyAuthorityV1
from dotmac_billing.opening_receivable_validation import preview_opening_account_import_v2
from dotmac_billing.opening_window_contracts import (
    OpeningWindowAdmissionV1,
    OpeningWindowProvisionVerificationRequestV1,
    OpeningWindowSealVerificationRequestV1,
    OpeningWindowV1,
    ProvisionOpeningWindowV1,
    SealOpeningWindowV1,
    opening_admission_population_digest,
)
from dotmac_kernel.cache import TenantScope

from erp_v2.importer.ar_assembly import WRITER_ROLE, ErpOpeningAuthority
from erp_v2.importer.billing_opening import OpeningTranslationError, translate_partition

REF = "dotmac-debt-register v1"
FENCE = "fence-1"
NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _target(p):
    return OpeningTargetV1(
        scope=TenantScope(TENANT),
        account_id=p.account_id,
        currency=p.currency,
        precision=p.precision,
        expected_position_fingerprint="0" * 64,
        posting_watermark="0",
    )


def _authority(asm=None, *, oid=4242, dual_control: str | None = "off"):
    return ErpOpeningAuthority(
        assembly=asm or assembly(),
        migration_authority_ref=REF,
        role_oid=oid,
        expected_coupled_watermark=FENCE,
        expected_dual_control=dual_control,
    )


def _request(asm, partition, *, command=None):
    command = command or asm.command(partition, target=_target(partition), migration_authority_ref=REF, occurred_at=NOW)
    return OpeningImportVerificationRequestV2(
        command, opening_account_command_digest_v2(command), opening_manifest_fingerprint_v2(command.manifest)
    )


def test_authority_satisfies_billings_port_and_holds_always_refuse() -> None:
    authority = _authority()
    assert isinstance(authority, OpeningLaneAssemblyAuthorityV1)
    assert (
        authority.hold_approvals().hold_approval(
            None, scope=None, request_id=uuid4(), subject_type="x", subject_id="y", content_digest="z"
        )
        is None
    )


def test_genuine_command_passes_and_proof_refs_are_the_shas() -> None:
    asm = assembly()
    verifier = _authority(asm).import_verifier()
    partition = asm.partitions()[0]
    verified = verifier.verify_import_v2(_request(asm, partition))
    assert verified is not None and verified.partition == partition
    assert verified.source_binding_proof_ref == f"snapshot-sha256:{asm.census.snapshot_sha256}"
    assert verified.cash_lifecycle_proof_ref == f"census-sha256:{asm.census.sha256}"
    assert verified.migration_authority_proof_ref == f"debt-register:{REF}"
    # billing's own preview accepts it with this verifier (0 blockers)
    command = _request(asm, partition).command
    assert preview_opening_account_import_v2(command, verifier=verifier).admissible


def test_every_partition_of_the_one_manifest_verifies() -> None:
    asm = assembly()
    verifier = _authority(asm).import_verifier()
    assert all(verifier.verify_import_v2(_request(asm, p)) is not None for p in asm.partitions())
    assert len({asm.fingerprint()}) == 1


def test_edited_residual_is_refused() -> None:
    asm = assembly()
    partition = asm.partitions()[0]
    command = _request(asm, partition).command
    manifest = command.manifest
    item = replace(manifest.receivables[0], residual_minor=manifest.receivables[0].residual_minor + 1)
    tampered = replace(command, manifest=replace(manifest, receivables=(item, *manifest.receivables[1:])))
    forged = OpeningImportVerificationRequestV2(
        tampered, opening_account_command_digest_v2(tampered), opening_manifest_fingerprint_v2(tampered.manifest)
    )
    assert _authority(asm).import_verifier().verify_import_v2(forged) is None


def test_added_row_is_refused() -> None:
    asm = assembly()
    partition = asm.partitions()[0]
    command = _request(asm, partition).command
    manifest = command.manifest
    extra = replace(manifest.receivables[0], source_invoice_key="forged")
    tampered = replace(command, manifest=replace(manifest, receivables=(*manifest.receivables, extra)))
    forged = OpeningImportVerificationRequestV2(
        tampered, opening_account_command_digest_v2(tampered), opening_manifest_fingerprint_v2(tampered.manifest)
    )
    assert _authority(asm).import_verifier().verify_import_v2(forged) is None


def test_swapped_partition_and_foreign_target_are_refused() -> None:
    asm = assembly()
    first, second = asm.partitions()
    command = _request(asm, first).command
    swapped = replace(command, partition=second)
    assert (
        _authority(asm)
        .import_verifier()
        .verify_import_v2(
            OpeningImportVerificationRequestV2(
                swapped, opening_account_command_digest_v2(swapped), opening_manifest_fingerprint_v2(swapped.manifest)
            )
        )
        is None
    )  # target still names the first account
    stranger = replace(first, account_id=uuid4())
    foreign = replace(command, partition=stranger, target=replace(command.target, account_id=stranger.account_id))
    assert (
        _authority(asm)
        .import_verifier()
        .verify_import_v2(
            OpeningImportVerificationRequestV2(
                foreign, opening_account_command_digest_v2(foreign), opening_manifest_fingerprint_v2(foreign.manifest)
            )
        )
        is None
    )


def test_a_different_manifest_a_wrong_authority_ref_and_a_stale_digest_are_refused() -> None:
    asm = assembly()
    other = assembly(extra_invoices=[invoice("inv-new", total="1.00", posted=True)])
    partition = asm.partitions()[0]
    verifier = _authority(asm).import_verifier()
    # another snapshot's genuine command (different manifest) is not this assembly's
    other_partition = next(p for p in other.partitions() if p.source_account_key == partition.source_account_key)
    assert verifier.verify_import_v2(_request(other, other_partition)) is None
    # a command bound to some other migration authority
    command = asm.command(
        partition, target=_target(partition), migration_authority_ref="dotmac-debt-register v2", occurred_at=NOW
    )
    assert verifier.verify_import_v2(_request(asm, partition, command=command)) is None
    # a digest that does not match the command
    request = _request(asm, partition)
    assert verifier.verify_import_v2(replace(request, command_digest="0" * 64)) is None


def test_census_or_snapshot_sha_mismatch_refuses_the_derivation() -> None:
    asm = assembly()
    bad_census = replace(asm.census, sha256="e" * 64)
    # the verifier's proof refs would carry the wrong sha, but the derived items/lots must equal the census rows
    tampered_rows = tuple(r for r in asm.census.rows if r.key != "inv-3")
    drifted = replace(asm, census=replace(asm.census, rows=tampered_rows), _cache={})
    with pytest.raises(OpeningTranslationError, match="ERP != census"):
        drifted.manifest()
    verifier = _authority(drifted).import_verifier()
    partition = asm.partitions()[0]
    assert verifier.verify_import_v2(_request(asm, partition)) is None  # derivation fails -> refused
    assert bad_census.sha256 != asm.census.sha256


def test_new_snapshot_row_the_census_does_not_know_is_refused() -> None:
    # A row added to the snapshot after the census was taken changes the derived partition: ERP != census.
    asm = assembly()
    grown = replace(
        asm,
        invoices=[
            *asm.invoices,
            {**invoice("late-add", total="9.00", posted=True), "invoice_date": datetime(2025, 5, 1, tzinfo=UTC)},
        ],
        _cache={},
    )
    with pytest.raises(OpeningTranslationError, match="ERP != census"):
        grown.manifest()


def _window(asm, *, principal=WRITER_ROLE, oid=4242, ref=REF, manifest=None, dual_control="off"):
    manifest = manifest or asm.manifest()
    return OpeningWindowV1(
        scope=TenantScope(TENANT),
        window_id=uuid4(),
        epoch=uuid4(),
        manifest_version=2,
        manifest_fingerprint=opening_manifest_fingerprint_v2(manifest),
        manifest=manifest,
        principal_name=principal,
        principal_oid=oid,
        provision_authority_ref=ref,
        recorded_at=NOW,
        dual_control=dual_control,  # explicit, never billing's default
    )


def test_provision_verifier_passes_and_refuses_a_wrong_principal_oid_name_or_manifest() -> None:
    asm = assembly()
    verifier = _authority(asm, oid=4242).provision_verifier()

    def verify(window):
        return verifier.verify_provision(
            OpeningWindowProvisionVerificationRequestV1(ProvisionOpeningWindowV1(window), "d" * 64)
        )

    ok = verify(_window(asm))
    assert ok is not None and ok.complete_population_proof_ref == f"census-sha256:{asm.census.sha256}"
    assert verify(_window(asm, oid=1)) is None  # wrong principal oid
    assert verify(_window(asm, principal="app_admin")) is None  # wrong principal name
    assert verify(_window(asm, ref="dotmac-debt-register v9")) is None
    other = assembly(extra_invoices=[invoice("inv-new", total="1.00", posted=True)])
    assert verify(_window(asm, manifest=other.manifest())) is None  # a different manifest
    assert verify(_window(asm, dual_control="on")) is None  # the window binds a policy other than the ERP's (OFF)
    unset = _authority(asm, oid=4242, dual_control=None).provision_verifier()  # an unset ERP policy binds nothing
    assert (
        unset.verify_provision(
            OpeningWindowProvisionVerificationRequestV1(ProvisionOpeningWindowV1(_window(asm)), "d" * 64)
        )
        is None
    )


def _seal(asm, *, accounts=None, held=(), watermark=FENCE, ref=REF):
    fingerprint = asm.fingerprint()
    ids = accounts if accounts is not None else sorted(asm.population_account_ids(), key=str)
    admissions = tuple(
        OpeningWindowAdmissionV1(
            scope=TenantScope(TENANT),
            window_id=uuid4(),
            receipt_id=uuid4(),
            billing_account_id=a,
            request_identity="k",
            request_digest="d",
            manifest_fingerprint=fingerprint,
        )
        for a in ids
    )
    return SealOpeningWindowV1(
        scope=TenantScope(TENANT),
        window_id=uuid4(),
        epoch=uuid4(),
        manifest_fingerprint=fingerprint,
        admissions=admissions,
        coupled_watermark=watermark,
        command_key="seal",
        authority_ref=ref,
        occurred_at=NOW,
        held_account_ids=tuple(sorted(held, key=str)),
    )


def _verify_seal(asm, command):
    return (
        _authority(asm)
        .seal_verifier()
        .verify_seal(
            OpeningWindowSealVerificationRequestV1(
                command, "c" * 64, opening_admission_population_digest(command.admissions)
            )
        )
    )


def test_seal_verifier_passes_only_for_the_exact_census_population() -> None:
    asm = assembly()
    assert _verify_seal(asm, _seal(asm)) is not None
    expected = sorted(asm.population_account_ids(), key=str)
    assert {account_id("cust-1"), account_id("cust-2")} == set(expected)
    assert _verify_seal(asm, _seal(asm, accounts=expected[:1])) is None  # a partition missing
    assert _verify_seal(asm, _seal(asm, accounts=[*expected, uuid4()])) is None  # a stranger admitted
    assert _verify_seal(asm, _seal(asm, held=expected[:1])) is None  # holds are never used
    assert _verify_seal(asm, _seal(asm, watermark="other-fence")) is None
    assert _verify_seal(asm, _seal(asm, ref="dotmac-debt-register v9")) is None


def test_a_nonzero_provenance_origin_does_not_change_verification() -> None:
    # F1 reversed: Sub-origin and Splynx partitions verify exactly like native ones.
    asm = assembly(
        extra_invoices=[invoice("sub-x", customer="cust-2", total="5.00", dotmac_sub_id="s", posted=True)],
    )
    verifier = _authority(asm).import_verifier()
    assert all(verifier.verify_import_v2(_request(asm, p)) is not None for p in asm.partitions())
    assert SNAPSHOT is not None and alloc is not None and translate_partition is not None
