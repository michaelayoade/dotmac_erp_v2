"""Private operator plan/apply for seeding tax codes from legacy taxes.

Tax code creation is a composable Starter capability (dotmac_tax service),
so destination tax configuration that the importer cannot mint — a code
whose only legacy evidence is payroll bands, e.g. PAYE — is seeded through
that service instead of an importer change or a legacy data edit. Band
rates/bounds/dates are copied exactly from legacy ``payroll.tax_band``;
the code identity and module policy fields come from an explicit reviewed
seed spec, never from defaults invented here.

DSNs are environment-only. Receipts contain codes/counts/digests only.
Plan mode runs the whole ensure flow and rolls back; apply requires
``ERP_TAX_SEED_APPROVED=1`` plus reviewed spec/manifest digests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session


class OperatorRefusal(ValueError):
    pass


def _refuse(code: str):
    raise OperatorRefusal(code)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_POLICY_FIELDS = (
    "fact_kind",
    "recognition_basis_code",
    "transaction_side",
    "calculation_method",
    "treatment_code",
    "calculation_base_code",
)


@dataclass(frozen=True, slots=True)
class SeedCode:
    code: str
    name: str
    tax_kind_code: str
    fact_kind: str
    recognition_basis_code: str
    transaction_side: str
    calculation_method: str
    treatment_code: str
    calculation_base_code: str
    band_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyBand:
    name: str
    sequence: int
    lower_bound: Decimal
    upper_bound: Decimal | None
    rate: Decimal
    effective_from: date
    effective_to: date | None


def parse_seed_spec(raw: bytes, *, snapshot_sha256: str, candidate_revision: str) -> tuple[SeedCode, ...]:
    """Strictly parse the reviewed seed spec; refuse any extra shape."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _refuse("invalid_seed_spec_json")
    if not isinstance(data, dict) or set(data) != {"version", "snapshot_sha256", "candidate_revision", "codes"}:
        _refuse("invalid_seed_spec_schema")
    if data["version"] != 1 or data["snapshot_sha256"] != snapshot_sha256:
        _refuse("seed_snapshot_mismatch")
    if data["candidate_revision"] != candidate_revision:
        _refuse("seed_candidate_mismatch")
    if not isinstance(data["codes"], list) or not 1 <= len(data["codes"]) <= 16:
        _refuse("explicit_bounded_codes_required")
    codes: list[SeedCode] = []
    seen_codes: set[str] = set()
    seen_bands: set[str] = set()
    for entry in data["codes"]:
        expected = {"code", "name", "tax_kind_code", "band_names", *_POLICY_FIELDS}
        if not isinstance(entry, dict) or set(entry) != expected:
            _refuse("invalid_seed_code_schema")
        for key in expected - {"band_names"}:
            if not isinstance(entry[key], str) or not entry[key].strip():
                _refuse(f"invalid_seed_field_{key}")
        if entry["calculation_method"] != "progressive":
            _refuse("progressive_seed_only")
        names = entry["band_names"]
        if not isinstance(names, list) or not 1 <= len(names) <= 64:
            _refuse("explicit_band_names_required")
        if any(not isinstance(n, str) or not n.strip() for n in names):
            _refuse("invalid_band_name")
        if entry["code"] in seen_codes or seen_bands.intersection(names):
            _refuse("duplicate_seed_code_or_band")
        seen_codes.add(entry["code"])
        seen_bands.update(names)
        codes.append(
            SeedCode(
                code=entry["code"],
                name=entry["name"],
                tax_kind_code=entry["tax_kind_code"],
                fact_kind=entry["fact_kind"],
                recognition_basis_code=entry["recognition_basis_code"],
                transaction_side=entry["transaction_side"],
                calculation_method=entry["calculation_method"],
                treatment_code=entry["treatment_code"],
                calculation_base_code=entry["calculation_base_code"],
                band_names=tuple(names),
            )
        )
    return tuple(codes)


def select_bands(rows: list[dict], band_names: tuple[str, ...]) -> tuple[date, tuple[LegacyBand, ...]]:
    """Select named legacy bands and prove they form one progressive scale."""
    by_name = {str(r["name"]): r for r in rows}
    missing = [n for n in band_names if n not in by_name]
    if missing:
        _refuse("legacy_band_missing")
    bands = [
        LegacyBand(
            name=str(r["name"]),
            sequence=int(r["sequence"]),
            lower_bound=Decimal(str(r["min_amount"])),
            upper_bound=None if r["max_amount"] is None else Decimal(str(r["max_amount"])),
            rate=Decimal(str(r["rate"])),
            effective_from=r["effective_from"],
            effective_to=r["effective_to"],
        )
        for r in (by_name[n] for n in band_names)
    ]
    bands.sort(key=lambda b: b.sequence)
    if [b.sequence for b in bands] != list(range(1, len(bands) + 1)):
        _refuse("legacy_band_sequence_not_contiguous")
    if len({b.effective_from for b in bands}) != 1 or any(b.effective_from is None for b in bands):
        _refuse("legacy_band_effective_from_ambiguous")
    if any(b.effective_to is not None for b in bands):
        _refuse("legacy_band_effective_to_unsupported")
    previous_upper: Decimal | None = None
    for expected, band in enumerate(bands, start=1):
        if band.lower_bound < 0 or not 0 <= band.rate <= 1:
            _refuse("legacy_band_invalid")
        if previous_upper is not None and band.lower_bound != previous_upper:
            _refuse("legacy_band_not_contiguous")
        if band.upper_bound is not None and band.upper_bound <= band.lower_bound:
            _refuse("legacy_band_upper_invalid")
        if previous_upper is None and expected > 1:
            _refuse("legacy_band_unbounded_not_last")
        previous_upper = band.upper_bound
    return bands[0].effective_from, tuple(bands)


def _manifest(raw: bytes):
    from erp_v2.importer.snapshot import SnapshotManifest

    data = json.loads(raw)
    required = {"snapshot_sha256", "source_database", "destination_database", "candidate_revision", "tenant_extent"}
    if not isinstance(data, dict) or set(data) != required:
        _refuse("invalid_manifest")
    if not re.fullmatch(r"[a-f0-9]{64}", str(data["snapshot_sha256"])):
        _refuse("invalid_snapshot_digest")
    if data["source_database"] == data["destination_database"]:
        _refuse("distinct_databases_required")
    if not isinstance(data["tenant_extent"], list) or len(data["tenant_extent"]) != 1:
        _refuse("single_tenant_extent_required")
    return SnapshotManifest(
        data["snapshot_sha256"],
        data["source_database"],
        data["destination_database"],
        data["candidate_revision"],
        tuple(data["tenant_extent"]),
    )


def _safe_engine(url: str):
    engine = sa.create_engine(url, echo=False, hide_parameters=True, isolation_level="SERIALIZABLE")
    if engine.dialect.name != "postgresql":
        engine.dispose()
        _refuse("postgresql_required")
    if engine.logger.isEnabledFor(logging.INFO):
        engine.dispose()
        _refuse("unsafe_sql_logging")
    return engine


def _legacy_band_rows(connection) -> list[dict]:
    return [
        dict(r)
        for r in connection.execute(
            sa.text(
                "SELECT name, sequence, min_amount, max_amount, rate, effective_from, effective_to "
                "FROM payroll.tax_band WHERE is_active"
            )
        ).mappings()
    ]


def _run(args) -> dict:
    manifest_bytes = args.manifest.read_bytes()
    seed_bytes = args.seed_spec.read_bytes()
    manifest = _manifest(manifest_bytes)
    if not re.fullmatch(r"[a-f0-9]{40}", manifest.candidate_revision):
        _refuse("full_candidate_revision_required")
    if _sha(args.snapshot_file) != manifest.snapshot_sha256:
        _refuse("snapshot_digest_mismatch")
    codes = parse_seed_spec(
        seed_bytes,
        snapshot_sha256=manifest.snapshot_sha256,
        candidate_revision=manifest.candidate_revision,
    )
    seed_sha = hashlib.sha256(seed_bytes).hexdigest()
    if args.apply and args.approved_seed_sha256 != seed_sha:
        _refuse("seed_review_binding_required")
    if args.apply and args.approved_manifest_sha256 != hashlib.sha256(manifest_bytes).hexdigest():
        _refuse("manifest_review_binding_required")
    source_url = os.environ.pop("LEGACY_DATABASE_URL", "")
    destination_url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    if not source_url or not destination_url:
        _refuse("operator_database_materials_required")
    if "DATABASE_URL" in os.environ or "PLATFORM_DATABASE_URL" in os.environ:
        _refuse("serving_database_materials_forbidden")
    tenant_id = UUID(manifest.tenant_extent[0])

    from dotmac_tax.contracts import TaxRuleBandInput, TaxRuleInput
    from dotmac_tax.service import ensure_tax_code_in_transaction, ensure_tax_rule_in_transaction

    source = _safe_engine(source_url)
    try:
        destination = _safe_engine(destination_url)
        try:
            with source.connect() as source_connection, source_connection.begin():
                source_connection.execute(sa.text("SET TRANSACTION READ ONLY"))
                if source_connection.scalar(sa.text("SELECT current_database()")) != manifest.source_database:
                    _refuse("source_database_mismatch")
                band_rows = _legacy_band_rows(source_connection)
                with Session(destination) as db, db.begin():
                    if db.scalar(sa.text("SELECT current_database()")) != manifest.destination_database:
                        _refuse("destination_database_mismatch")
                    jurisdictions = list(
                        db.execute(
                            sa.text(
                                "SELECT id FROM mod_tax.tax_jurisdictions "
                                "WHERE tenant_id = :t AND status = 'active'"
                            ),
                            {"t": str(tenant_id)},
                        ).scalars()
                    )
                    if len(jurisdictions) != 1:
                        _refuse("single_active_jurisdiction_required")
                    jurisdiction_id = jurisdictions[0]
                    results = []
                    for seed in codes:
                        effective_from, bands = select_bands(band_rows, seed.band_names)
                        code_view = ensure_tax_code_in_transaction(
                            db,
                            tenant_id=tenant_id,
                            jurisdiction_id=jurisdiction_id,
                            code=seed.code,
                            name=seed.name,
                            tax_kind_code=seed.tax_kind_code,
                            description="Seeded from legacy payroll.tax_band (tax_seed)",
                        )
                        rule_view = ensure_tax_rule_in_transaction(
                            db,
                            tenant_id=tenant_id,
                            command=TaxRuleInput(
                                tax_code_id=code_view.tax_code_id,
                                version=1,
                                effective_from=effective_from,
                                effective_to=None,
                                priority=1,
                                fact_kind=seed.fact_kind,
                                recognition_basis_code=seed.recognition_basis_code,
                                transaction_side=seed.transaction_side,
                                calculation_method=seed.calculation_method,
                                rate=None,
                                fixed_amount=None,
                                inclusive=False,
                                recoverable_rate=Decimal(0),
                                bands=tuple(
                                    TaxRuleBandInput(
                                        sequence=b.sequence,
                                        lower_bound=b.lower_bound,
                                        upper_bound=b.upper_bound,
                                        rate=b.rate,
                                    )
                                    for b in bands
                                ),
                                treatment_code=seed.treatment_code,
                                calculation_base_code=seed.calculation_base_code,
                            ),
                        )
                        results.append(
                            {
                                "code": seed.code,
                                "tax_code_id": str(code_view.tax_code_id),
                                "rule_id": str(rule_view.rule_id),
                                "version": rule_view.version,
                                "bands": len(bands),
                                "effective_from": effective_from.isoformat(),
                            }
                        )
                    payload = {
                        "mode": "apply" if args.apply else "plan",
                        "bindings": {
                            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                            "snapshot_sha256": manifest.snapshot_sha256,
                            "seed_sha256": seed_sha,
                            "candidate_revision": manifest.candidate_revision,
                        },
                        "codes": results,
                    }
                    if not args.apply:
                        db.rollback()
                    return payload
        finally:
            destination.dispose()
    finally:
        source.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.tax_seed")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--snapshot-file", required=True, type=Path)
    parser.add_argument("--seed-spec", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-seed-sha256")
    parser.add_argument("--approved-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if args.apply and os.environ.get("ERP_TAX_SEED_APPROVED") != "1":
            _refuse("explicit_tax_seed_apply_required")
        fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception:
        print("tax seed refused: apply authorization or receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = _run(args)
            code = 0
        except OperatorRefusal as exc:
            payload = {"ready": False, "refusal": str(exc)}
            code = 2
        except Exception:
            payload = {"ready": False, "refusal": "tax_seed_command_refused"}
            code = 2
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    print("tax seed receipt written" if code == 0 else "tax seed refused; see private receipt")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
