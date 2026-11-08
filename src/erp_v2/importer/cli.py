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
from decimal import Decimal as _Decimal
from pathlib import Path

import sqlalchemy as sa

from erp_v2.importer.e2 import E2_PAIRINGS, FLEET_PAIRINGS, LEASE_PAIRINGS, PUBLIC_SECTOR_PAIRINGS
from erp_v2.importer.pipeline import run_import
from erp_v2.importer.snapshot import ManifestError, SnapshotManifest
from erp_v2.importer.spec import TransformSpec
from erp_v2.importer.wave1 import WAVE1_MAPPINGS
from erp_v2.importer.wave2 import WAVE2_PAIRINGS
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


def _reflect_source_columns(engine: sa.Engine) -> dict[str, tuple[str, ...]]:
    inspector = sa.inspect(engine)
    columns: dict[str, tuple[str, ...]] = {}
    wanted = {p.source for p in _ALL_PAIRINGS if p.source}
    for qualified in sorted(wanted):
        schema, _, table = qualified.partition(".")
        try:
            found = inspector.get_columns(table, schema=schema)
        except sa.exc.NoSuchTableError:
            continue
        columns[qualified] = tuple(c["name"] for c in found)
    return columns


def _spec_report(specs: Sequence[TransformSpec]) -> dict[str, object]:
    blocked = [
        {"destination": s.destination_table, "missing": list(s.missing_required)} for s in specs if not s.executable
    ]
    return {
        "specs_total": len(specs),
        "specs_executable": sum(s.executable for s in specs),
        "specs_blocked": blocked,
        "residue_columns": sum(len(s.residue) for s in specs),
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


def cmd_billing_plan(args: argparse.Namespace) -> int:
    """Translate exported legacy AR rows (JSON) into an opening receipt.

    Exports are read-only operator extracts; this command writes
    nothing anywhere except the receipt file.
    """

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
    dsn = os.environ.get(args.source_url_env or "", "")
    if not dsn:
        receipt["mode"] = "pairings-only (no source DSN env set)"
        receipt["pairings"] = _census_receipt()
        _write_receipt(Path(args.receipt), receipt)
        return 0
    engine = sa.create_engine(dsn)
    with engine.connect() as connection:
        connection.execute(sa.text("SELECT 1"))
    source_columns = _reflect_source_columns(engine)
    missing_sources = sorted({p.source for p in _ALL_PAIRINGS if p.source} - set(source_columns))
    present = tuple(p for p in _ALL_PAIRINGS if p.source in source_columns)
    specs, generated = build_specs(present, source_columns, _destination_tables())
    receipt["mode"] = "reflected"
    receipt["sources_reflected"] = len(source_columns)
    receipt["sources_missing_on_snapshot"] = missing_sources
    receipt["generated_destinations"] = len(generated)
    receipt["specs"] = _spec_report(specs)
    _write_receipt(Path(args.receipt), receipt)
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    manifest = _load_manifest(args)
    if os.environ.get("ERP_IMPORT_APPROVED") != "1":
        raise CliRefusal("execute requires ERP_IMPORT_APPROVED=1 (named human approval)")
    source_dsn = os.environ.get(args.source_url_env or "", "")
    dest_dsn = os.environ.get(args.dest_url_env or "", "")
    if not source_dsn or not dest_dsn:
        raise CliRefusal("execute requires source and destination DSN env vars")
    if args.apply and os.environ.get("ERP_IMPORT_APPLY_OK") != "1":
        raise CliRefusal("--apply additionally requires ERP_IMPORT_APPLY_OK=1")
    source_engine = sa.create_engine(source_dsn)
    dest_engine = sa.create_engine(dest_dsn)
    source_columns = _reflect_source_columns(source_engine)
    present = tuple(p for p in _ALL_PAIRINGS if p.source in source_columns)
    specs, _generated = build_specs(present, source_columns, _destination_tables())
    run_constants: dict[str, object] = {}
    if len(manifest.tenant_extent) == 1:
        run_constants["tenant_id"] = manifest.tenant_extent[0]
    executable = tuple(s for s in specs if s.executable and set(s.run_supplied) <= set(run_constants))
    unsupplied = [s.destination_table for s in specs if s.executable and not set(s.run_supplied) <= set(run_constants)]
    with source_engine.connect() as source_conn, dest_engine.begin() as dest_conn:
        result = run_import(
            source_conn,
            dest_conn,
            executable,
            run_constants=run_constants,
            dry_run=not args.apply,
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
    ):
        cmd = sub.add_parser(name)
        cmd.add_argument("--receipt", required=True, help="receipt JSON output path")
        cmd.set_defaults(func=fn)
        if name != "census":
            cmd.add_argument("--manifest", help="snapshot manifest JSON path")
            cmd.add_argument("--source-url-env", default="LEGACY_DATABASE_URL", help="env var holding the source DSN")
        if name == "billing-plan":
            cmd.add_argument("--invoices-json", required=True)
            cmd.add_argument("--allocations-json")
            cmd.add_argument("--payments-json")
            cmd.add_argument("--source-account-key", required=True)
            cmd.add_argument("--account-id", required=True, help="destination billing account UUID")
            cmd.add_argument("--precision", default="2")
        if name == "execute":
            cmd.add_argument(
                "--dest-url-env", default="MIGRATION_DATABASE_URL", help="env var holding the destination DSN"
            )
            cmd.add_argument("--apply", action="store_true", help="write (default dry-run)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CliRefusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
