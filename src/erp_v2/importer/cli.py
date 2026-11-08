"""Operator CLI for the transform-to-fit importer.

Commands:
  census   Mapping census receipt (no database access).
  plan     Snapshot-bound plan: reflects live source columns when a
           source DSN env is given, builds every wave spec, and
           reports executable/blocked counts with named gaps.
  execute  Runs executable specs through the pipeline. Triple-locked:
           snapshot manifest + ERP_IMPORT_APPROVED=1 + destination
           DSN env; dry-run unless --apply is also passed.

Every command writes a JSON receipt; nothing prints row data.
Exit codes: 0 ok, 2 refusal (named on stderr), 3 incomplete receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid as _uuid
from collections.abc import Sequence
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timezone as _timezone
from decimal import Decimal as _Decimal
from pathlib import Path

import sqlalchemy as sa

from erp_v2.importer.e2 import E2_PAIRINGS, FLEET_PAIRINGS, LEASE_PAIRINGS, PUBLIC_SECTOR_PAIRINGS
from erp_v2.importer.enrolment import EnrolmentMaps, required_maps, unbuilt_maps
from erp_v2.importer.minting import MintContext, MintResult
from erp_v2.importer.pipeline import run_import
from erp_v2.importer.snapshot import ManifestError, SnapshotManifest
from erp_v2.importer.spec import TransformSpec
from erp_v2.importer.wave1 import WAVE1_MAPPINGS
from erp_v2.importer.wave2 import WAVE2_PAIRINGS, Pairing
from erp_v2.importer.waves import SOURCE_TABLES
from erp_v2.importer.waves36 import ALL_WAVE_PAIRINGS, build_specs
from erp_v2.migrate import EXPECTED_HEADS

_ALL_PAIRINGS = (
    WAVE2_PAIRINGS + ALL_WAVE_PAIRINGS + E2_PAIRINGS + FLEET_PAIRINGS + PUBLIC_SECTOR_PAIRINGS + LEASE_PAIRINGS
)


class CliRefusal(RuntimeError):
    """A named operator refusal (exit 2)."""


def _destination_tables() -> dict[str, object]:
    """All composed metadata tables keyed schema.name (kernel Base)."""

    # Importing the assembly registers every module's models on Base.
    from dotmac_kernel.models import Base

    import erp_v2.assembly
    import erp_v2.fleet
    import erp_v2.hr.appraisals
    import erp_v2.hr.attendance
    import erp_v2.hr.discipline
    import erp_v2.hr.feedback
    import erp_v2.hr.lifecycle
    import erp_v2.hr.models
    import erp_v2.hr.recruitment
    import erp_v2.hr.training
    import erp_v2.leases
    import erp_v2.public_sector  # noqa: F401

    return dict(Base.metadata.tables)


_STASHED_ENV: dict[str, str] = {}


def _stash_forbidden_env() -> None:
    """Move owner/source DSN env vars out of the process environment.

    The runtime database binding refuses MIGRATION_/LEGACY_
    DATABASE_URL in a runtime process - and this CLI's defaults use
    exactly those names, while _destination_tables imports the
    assembly (which builds the runtime binding at import). Stashing
    at CLI entry keeps the guard's intent: the credentials live
    only in this operator process's memory, never in the env of
    anything it imports or spawns.
    """

    for name in ("MIGRATION_DATABASE_URL", "LEGACY_DATABASE_URL"):
        if name in os.environ:
            _STASHED_ENV[name] = os.environ.pop(name)


def _take_env(name: str | None) -> str:
    """Read a DSN env var (stashed first) and remove it from the env."""

    if not name:
        return ""
    if name in _STASHED_ENV:
        return _STASHED_ENV.pop(name)
    return os.environ.pop(name, "")


def _resolve_sources(engine: sa.Engine, pairings: Sequence[Pairing]):
    """Resolve declared pairing sources against the real snapshot schemas.

    Pairings declare legacy tables as `public.<name>` (the census
    convention), but the legacy database is multi-schema
    (hr/ar/ap/inv/...). Resolution is by bare table name across
    every snapshot schema: exactly one holder rewrites the pairing
    source to the actual qualified name; zero holders is a named
    missing source; more than one is a named ambiguity and the
    pairing is excluded - never a guessed schema.

    Returns (resolved_pairings, columns_by_resolved_source, report,
    source_schemas) where source_schemas feeds the connection
    search_path enrolment SQL relies on.
    """

    from dataclasses import replace as _dc_replace

    inspector = sa.inspect(engine)
    schemas = [s for s in inspector.get_schema_names() if s not in ("pg_catalog", "information_schema")]
    tables_by_schema = {s: set(inspector.get_table_names(schema=s)) for s in schemas}
    resolution: dict[str, str | None] = {}
    ambiguous: set[str] = set()
    columns: dict[str, tuple[str, ...]] = {}
    for declared in sorted({p.source for p in pairings if p.source}):
        bare = declared.rpartition(".")[2]
        holders = [s for s in schemas if bare in tables_by_schema[s]]
        if len(holders) == 1:
            actual = f"{holders[0]}.{bare}"
            resolution[declared] = actual
            columns[actual] = tuple(c["name"] for c in inspector.get_columns(bare, schema=holders[0]))
        elif len(holders) > 1:
            resolution[declared] = None
            ambiguous.add(declared)
        else:
            resolution[declared] = None
    resolved_pairings = tuple(
        _dc_replace(p, source=resolution[p.source]) if p.source and resolution.get(p.source) else p
        for p in pairings
        if not p.source or resolution.get(p.source)
    )
    report = {
        "resolved": {d: a for d, a in sorted(resolution.items()) if a and a != d},
        "ambiguous": sorted(ambiguous),
        "missing": sorted(d for d, a in resolution.items() if a is None and d not in ambiguous),
    }
    used_schemas = sorted({a.partition(".")[0] for a in resolution.values() if a})
    return resolved_pairings, columns, report, used_schemas


def _set_search_path(connection: sa.Connection, schemas: Sequence[str]) -> None:
    """Point unqualified enrolment SQL at the legacy schemas (Postgres)."""

    if connection.dialect.name == "postgresql" and schemas:
        connection.execute(sa.text(f"SET search_path = {', '.join(schemas)}, public"))


def _spec_report(specs: Sequence[TransformSpec]) -> dict[str, object]:
    blocked = [
        {"destination": s.destination_table, "missing": list(s.missing_required)} for s in specs if not s.executable
    ]
    return {
        "specs_total": len(specs),
        "specs_executable": sum(s.executable for s in specs),
        "specs_blocked": blocked,
        "residue_columns": sum(len(s.residue) for s in specs),
        "enrolment_maps_required": sorted(required_maps(specs)),
        "enrolment_maps_without_builder": sorted(unbuilt_maps(specs)),
    }


def _census_receipt() -> dict[str, object]:
    generated = [p for p in _ALL_PAIRINGS if p.source is None]
    return {
        "kind": "census",
        "destination_heads": sorted(EXPECTED_HEADS),
        "legacy_tables_named": sum(len(v) for v in SOURCE_TABLES.values()),
        "legacy_tables_by_destination": {k: len(v) for k, v in sorted(SOURCE_TABLES.items())},
        "pairings_total": len(_ALL_PAIRINGS),
        "pairings_generated_or_command": len(generated),
        "wave1_mappings": len(WAVE1_MAPPINGS),
    }


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def _load_manifest(args: argparse.Namespace) -> SnapshotManifest:
    if not args.manifest:
        raise CliRefusal("a snapshot manifest is required (--manifest PATH)")
    try:
        return SnapshotManifest.load(Path(args.manifest))
    except ManifestError as exc:
        raise CliRefusal(str(exc)) from exc


def _translate_billing_args(args: argparse.Namespace):
    """Shared billing-plan/preflight input path: JSON rows -> translation."""

    manifest = _load_manifest(args)
    from dotmac_billing.contracts import (  # lazy: billing contracts are heavy
        PartyDocumentSnapshotV1,
        PaymentInstructionsSnapshotV1,
        PostalAddressSnapshotV1,
    )
    from dotmac_billing.opening_contracts import OpeningPartitionV1

    from erp_v2.importer.billing_opening import (
        OpeningTranslationContext,
        translate_partition,
    )

    def _rows(path: str | None) -> list[dict[str, object]]:
        if not path:
            return []
        data = json.loads(Path(path).read_text())
        if not isinstance(data, list):
            raise CliRefusal(f"{path} must contain a JSON list of rows")
        return data  # type: ignore[return-value]

    def _coerce(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for row in rows:
            converted: dict[str, object] = {}
            for key, value in row.items():
                if key in ("subtotal", "tax_amount", "total_amount", "amount", "amount_allocated"):
                    converted[key] = _Decimal(str(value))
                elif key in ("invoice_date", "payment_date", "allocated_at"):
                    converted[key] = _datetime.fromisoformat(str(value))
                elif key == "due_date" and value:
                    converted[key] = _date.fromisoformat(str(value))
                else:
                    converted[key] = value
            out.append(converted)
        return out

    seller = PartyDocumentSnapshotV1(
        legal_name=str(os.environ.get("ERP_SELLER_LEGAL_NAME", "")),
        address=PostalAddressSnapshotV1(
            line_one=str(os.environ.get("ERP_SELLER_ADDRESS_LINE", "")),
            country_code=str(os.environ.get("ERP_SELLER_COUNTRY", "NG")),
        ),
    )
    if not seller.legal_name:
        raise CliRefusal("billing-plan requires ERP_SELLER_LEGAL_NAME (document snapshot authority)")
    invoices = _coerce(_rows(args.invoices_json))
    customers: dict[str, PartyDocumentSnapshotV1] = {}
    for invoice in invoices:
        key = str(invoice.get("customer_key", ""))
        legal = invoice.get("customer_legal_name")
        if key and legal and key not in customers:
            customers[key] = PartyDocumentSnapshotV1(
                legal_name=str(legal),
                address=PostalAddressSnapshotV1(
                    line_one=str(invoice.get("customer_address_line", "")),
                    country_code=str(invoice.get("customer_country", "NG")),
                ),
            )
    currencies = sorted({str(i.get("currency_code")) for i in invoices})
    if len(currencies) != 1:
        raise CliRefusal(f"billing-plan translates one currency per partition; got {currencies}")
    context = OpeningTranslationContext(
        partition=OpeningPartitionV1(
            source_account_key=str(args.source_account_key),
            account_id=_uuid.UUID(str(args.account_id)),
            currency=currencies[0],
            precision=int(args.precision),
        ),
        seller=seller,
        customer_snapshot=customers.get,
        payment_instructions=PaymentInstructionsSnapshotV1(method_code="bank_transfer"),
    )
    result = translate_partition(
        context=context,
        invoices=invoices,
        allocations=_coerce(_rows(args.allocations_json)),
        payments=_coerce(_rows(args.payments_json)),
    )
    return manifest, context, result


def cmd_billing_plan(args: argparse.Namespace) -> int:
    """Translate exported legacy AR rows (JSON) into an opening receipt.

    Exports are read-only operator extracts; this command writes
    nothing anywhere except the receipt file.
    """

    manifest, _context, result = _translate_billing_args(args)
    payload: dict[str, object] = {
        "kind": "billing-plan",
        "manifest": manifest.as_dict(),
        "items": len(result.items),
        "open_ar_minor": result.open_ar_minor,
        "cash_lots": len(result.cash_lots),
        "blockers": [{"source_key": b.source_key, "reason": b.reason} for b in result.blockers],
        "dispositions": [
            {"key": d.identity.key, "disposition": d.disposition, "residual_minor": d.residual_minor}
            for d in result.dispositions
        ],
    }
    _write_receipt(Path(args.receipt), payload)
    return 0 if not result.blockers else 3


def cmd_billing_preflight(args: argparse.Namespace) -> int:
    """Verifier round-trip for one partition. Pure: executes nothing.

    The target fingerprint/watermark come from the destination
    observation at rehearsal; the migration authority ref is the
    named approval. Without an injected signed-grant verifier the
    module preview reports verifier_unavailable - the honest state
    until Starter billing M1 lands Sub-first.
    """

    from dotmac_billing import OpeningTargetV1
    from dotmac_billing.opening_contracts import OpeningCaptureV1
    from dotmac_kernel.cache import TenantScope

    from erp_v2.importer.billing_opening import (
        MAPPING_VERSION,
        OpeningTranslationError,
        build_manifest,
        preflight_opening_import,
    )

    manifest, context, result = _translate_billing_args(args)
    if len(manifest.tenant_extent) != 1:
        raise CliRefusal("billing-preflight requires a single-tenant snapshot extent")
    tenant_id = _uuid.UUID(str(manifest.tenant_extent[0]))
    economic_cutoff = _datetime.fromisoformat(str(args.economic_cutoff))
    recorded_cutoff = _datetime.fromisoformat(str(args.recorded_cutoff))
    capture = OpeningCaptureV1(
        source_system="legacy-erp",
        source_repository_sha=manifest.candidate_revision,
        installation_id=manifest.source_database,
        schema_heads=tuple(h.strip() for h in str(args.source_schema_heads).split(",") if h.strip()),
        snapshot_id=manifest.snapshot_sha256,
        captured_at=_datetime.now(_timezone.utc),
        economic_cutoff=economic_cutoff,
        recorded_cutoff=recorded_cutoff,
        isolation=str(args.isolation),
        population_version="1",
        source_authority_ref=str(args.source_authority_ref),
        account_mapping_version=MAPPING_VERSION,
        cash_mapping_version=MAPPING_VERSION,
    )
    partition = context.partition
    target = OpeningTargetV1(
        scope=TenantScope(tenant_id),
        account_id=partition.account_id,
        currency=partition.currency,
        precision=partition.precision,
        expected_position_fingerprint=str(args.expected_position_fingerprint),
        posting_watermark=str(args.posting_watermark),
    )
    try:
        opening_manifest = build_manifest(tenant_id=tenant_id, capture=capture, translation=result)
        preflight = preflight_opening_import(
            translation=result,
            manifest=opening_manifest,
            target=target,
            migration_authority_ref=str(args.migration_authority_ref),
            occurred_at=recorded_cutoff,
        )
    except OpeningTranslationError as exc:
        raise CliRefusal(str(exc)) from exc
    payload: dict[str, object] = {
        "kind": "billing-preflight",
        "manifest": manifest.as_dict(),
        "manifest_fingerprint": preflight.manifest_fingerprint,
        "idempotency_key": preflight.idempotency_key,
        "admissible": preflight.admissible,
        "importable": preflight.importable,
        "cash_residual_minor": preflight.cash_residual_minor,
        "receivable_residual_minor": preflight.receivable_residual_minor,
        "preview_blocker_codes": list(preflight.preview_blocker_codes),
        "translation_blockers": [
            {"source_key": b.source_key, "reason": b.reason} for b in preflight.translation_blockers
        ],
    }
    _write_receipt(Path(args.receipt), payload)
    return 0 if preflight.importable else 3


def cmd_census(args: argparse.Namespace) -> int:
    _write_receipt(Path(args.receipt), _census_receipt())
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    manifest = _load_manifest(args)
    receipt: dict[str, object] = {
        "kind": "plan",
        "manifest": manifest.as_dict(),
        "destination_heads": sorted(EXPECTED_HEADS),
    }
    dsn = _take_env(args.source_url_env)
    if not dsn:
        receipt["mode"] = "pairings-only (no source DSN env set)"
        receipt["pairings"] = _census_receipt()
        _write_receipt(Path(args.receipt), receipt)
        return 0
    engine = sa.create_engine(dsn)
    with engine.connect() as connection:
        connection.execute(sa.text("SELECT 1"))
    present, source_columns, resolution, _schemas = _resolve_sources(engine, _ALL_PAIRINGS)
    specs, generated = build_specs(present, source_columns, _destination_tables())
    receipt["mode"] = "reflected"
    receipt["sources_reflected"] = len(source_columns)
    receipt["source_resolution"] = resolution
    receipt["sources_missing_on_snapshot"] = resolution["missing"] + resolution["ambiguous"]
    receipt["generated_destinations"] = len(generated)
    receipt["specs"] = _spec_report(specs)
    _write_receipt(Path(args.receipt), receipt)
    return 0


def _mint_dict(result: MintResult) -> dict[str, object]:
    return {
        "step": result.step,
        "created": result.created,
        "existing": result.existing,
        "blockers": list(result.blockers),
        "refused": result.refused,
    }


def _json_arg(path: str | None, label: str) -> dict | None:
    if not path:
        return None
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise CliRefusal(f"{label} must be a JSON object")
    return data


def _mint_context(args: argparse.Namespace, run_constants: dict[str, object]) -> MintContext:
    tenant = run_constants.get("tenant_id")
    if tenant is None:
        raise CliRefusal("--mint requires a single-tenant snapshot extent (minting is tenant-scoped)")
    return MintContext(
        tenant_id=tenant,
        actor_id=os.environ.get("ERP_ENROLMENT_ACTOR_ID") or None,
        occurred_at=_datetime.now(_timezone.utc),
        payroll_net_payable_account_ref=os.environ.get("ERP_PAYROLL_NET_PAYABLE_ACCOUNT_REF") or None,
        tax_rule_policy=_json_arg(args.tax_rule_policy_json, "--tax-rule-policy-json"),
        tax_band_rule_binding=_json_arg(args.tax_band_binding_json, "--tax-band-binding-json"),
    )


def cmd_execute(args: argparse.Namespace) -> int:
    manifest = _load_manifest(args)
    if os.environ.get("ERP_IMPORT_APPROVED") != "1":
        raise CliRefusal("execute requires ERP_IMPORT_APPROVED=1 (named human approval)")
    source_dsn = _take_env(args.source_url_env)
    dest_dsn = _take_env(args.dest_url_env)
    if not source_dsn or not dest_dsn:
        raise CliRefusal("execute requires source and destination DSN env vars")
    if args.apply and os.environ.get("ERP_IMPORT_APPLY_OK") != "1":
        raise CliRefusal("--apply additionally requires ERP_IMPORT_APPLY_OK=1")
    source_engine = sa.create_engine(source_dsn)
    dest_engine = sa.create_engine(dest_dsn)
    present, source_columns, resolution, source_schemas = _resolve_sources(source_engine, _ALL_PAIRINGS)
    specs, _generated = build_specs(present, source_columns, _destination_tables())
    run_constants: dict[str, object] = {}
    if len(manifest.tenant_extent) == 1:
        # Module tenant_id columns are CHAR(32) hex; a dashed UUID
        # string would not fit (found live at Gate 4).
        run_constants["tenant_id"] = _uuid.UUID(str(manifest.tenant_extent[0])).hex
    executable = tuple(s for s in specs if s.executable and set(s.run_supplied) <= set(run_constants))
    unsupplied = [s.destination_table for s in specs if s.executable and not set(s.run_supplied) <= set(run_constants)]
    mint_context = _mint_context(args, run_constants) if args.mint else None
    post_mints: tuple[MintResult, ...] = ()
    with source_engine.connect() as source_conn, dest_engine.begin() as dest_conn:
        _set_search_path(source_conn, source_schemas)
        enrolment = EnrolmentMaps(source_conn, dest_conn, mint_context=mint_context)
        if mint_context is not None:
            enrolment.mint("kernel_tenants")  # FK parent of every tenant-scoped row
        result = run_import(
            source_conn,
            dest_conn,
            executable,
            enrolment=enrolment,
            run_constants=run_constants,
            dry_run=not args.apply,
        )
        if mint_context is not None:
            # Non-map-feeding mints run after the specs so every parent
            # row they reference is imported (same caller transaction;
            # dry-run rollback below covers them).
            post_mints = tuple(
                enrolment.mint(step)
                for step in ("pay_rule_bases", "tax_rules", "sales_leads", "document_libraries_types")
            )
        if not args.apply:
            dest_conn.rollback()
    payload: dict[str, object] = {
        "kind": "execute",
        "manifest": manifest.as_dict(),
        "dry_run": not args.apply,
        "destination_heads": list(result.destination_heads),
        "read": result.read,
        "written": result.written,
        "complete": result.complete,
        "exceptions": [
            {
                "source_table": e.source_table,
                "source_key": e.source_key,
                "column": e.column,
                "reason": e.reason,
            }
            for e in result.exceptions
        ],
        "specs": _spec_report(specs),
        "specs_skipped_unsupplied_run_constants": unsupplied,
        "source_resolution": resolution,
        "enrolment_maps_built": dict(result.enrolment_maps),
        "mints": [_mint_dict(m) for m in (*result.mints, *post_mints)],
    }
    _write_receipt(Path(args.receipt), payload)
    return 0 if result.complete else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="erp-v2-import", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (
        ("census", cmd_census),
        ("plan", cmd_plan),
        ("execute", cmd_execute),
        ("billing-plan", cmd_billing_plan),
        ("billing-preflight", cmd_billing_preflight),
    ):
        cmd = sub.add_parser(name)
        cmd.add_argument("--receipt", required=True, help="receipt JSON output path")
        cmd.set_defaults(func=fn)
        if name != "census":
            cmd.add_argument("--manifest", help="snapshot manifest JSON path")
            cmd.add_argument("--source-url-env", default="LEGACY_DATABASE_URL", help="env var holding the source DSN")
        if name in ("billing-plan", "billing-preflight"):
            cmd.add_argument("--invoices-json", required=True)
            cmd.add_argument("--allocations-json")
            cmd.add_argument("--payments-json")
            cmd.add_argument("--source-account-key", required=True)
            cmd.add_argument("--account-id", required=True, help="destination billing account UUID")
            cmd.add_argument("--precision", default="2")
        if name == "billing-preflight":
            cmd.add_argument("--expected-position-fingerprint", required=True)
            cmd.add_argument("--posting-watermark", required=True)
            cmd.add_argument("--migration-authority-ref", required=True)
            cmd.add_argument("--source-authority-ref", required=True)
            cmd.add_argument("--economic-cutoff", required=True, help="ISO datetime")
            cmd.add_argument("--recorded-cutoff", required=True, help="ISO datetime")
            cmd.add_argument("--source-schema-heads", required=True, help="comma-separated legacy schema heads")
            cmd.add_argument("--isolation", default="repeatable-read-dump")
        if name == "execute":
            cmd.add_argument(
                "--dest-url-env", default="MIGRATION_DATABASE_URL", help="env var holding the destination DSN"
            )
            cmd.add_argument("--apply", action="store_true", help="write (default dry-run)")
            cmd.add_argument(
                "--mint",
                action="store_true",
                help="mint GENERATED enrolment rows (revisions, authorities, sourcing lines, "
                "leads, document types) in the same transaction",
            )
            cmd.add_argument("--tax-rule-policy-json", help="legacy tax_type -> ruled module codes")
            cmd.add_argument("--tax-band-binding-json", help="legacy tax band name -> legacy tax_code")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _stash_forbidden_env()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CliRefusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
