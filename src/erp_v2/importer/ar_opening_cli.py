"""AR opening operator boundary: census / mint / plan / apply over dotmac-billing a12's opening window and lane.

Same discipline as the payables historical CLIs: env-only DSNs (`LEGACY_DATABASE_URL`, read only;
`MIGRATION_DATABASE_URL`), receipts created 0600 BEFORE any database action, plan then apply, every step bound to
the snapshot sha, the census sha and the plan sha. Dry-run by default: `apply` needs `ERP_AR_OPENING_APPLY=1` AND
the approved plan sha.

Flow (rev 2 section 4.2):
- `census`  read-only: bucket every legacy invoice/payment exactly once; writes the census receipt.
- `mint`    (apply-gated) one dotmac-customers account per legacy customer + one billing account per customer x
            currency, for the CARRYABLE partitions only; idempotent on deterministic keys.
- `plan`    census -> the ONE manifest -> `preview_opening_account_import_batch_v2` (expect 0 blockers); variance
            lines; plan sha over the manifest fingerprint and every partition's command digest. Writes nothing.
- `apply`   provision the window (as the migration role = app_admin, by billing's trigger), import every partition
            as the restricted `erp_billing_opening_writer`, then seal. Resumable (billing's key is idempotent).
            There is NO hold path: an operational hold (`target_not_pristine`, `import_refused`) or any refusal
            aborts the run for a re-plan. 0 GL: nothing here posts a journal.

Open items kept fail-closed, not decided here (reported to the architect):
- Variances: a12 records them at the SEAL and resolves them after it, through an Approvals maker-checker port.
  So `apply` REFUSES a plan with variances unless `ERP_AR_OPENING_ALLOW_OPEN_VARIANCES=1` (they are then carried
  open for Finance, listed in the receipt).
- The coupled writer watermark (`--coupled-watermark`) is an opaque fence the operator asserts; billing and the
  seal verifier only require the same value on both sides.
- T-AR-OPENING-CREDIT (reclass of Σ available_credit) and the B-9 ties are the B-9 builder's, not built here.
"""

from __future__ import annotations

# The only f-string SQL interpolates closed module literals (the table and role names).
# ruff: noqa: S608
import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer.ar_opening_models import TABLE, WRITER_ROLE

ADDRESS_NOT_RECORDED = "Not recorded in legacy ERP"
CUSTOMER_SQL = (
    "SELECT customer_id AS customer_key, legal_name, trading_name, dotmac_sub_id FROM customer"  # legacy ERP public
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class OperatorRefusal(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise OperatorRefusal(reason)


def customer_account_number(tenant_id: UUID, legacy_key: str) -> str:
    """Deterministic customer account number: uuid5 of the legacy id (never the legacy id itself)."""

    return "LEG-" + uuid5(NAMESPACE_URL, f"erp-v2-ar-customer:{tenant_id}:{legacy_key}").hex.upper()


def customer_snapshot_from(rows):
    """Documentary customer snapshot per legacy customer key (legal name; address is not held by legacy)."""

    from dotmac_billing.contracts import PartyDocumentSnapshotV1, PostalAddressSnapshotV1

    by_key = {}
    for row in rows:
        name = str(row.get("legal_name") or row.get("trading_name") or "").strip()
        if name:
            by_key[str(row["customer_key"])] = PartyDocumentSnapshotV1(
                legal_name=name, address=PostalAddressSnapshotV1(line_one=ADDRESS_NOT_RECORDED, country_code="NG")
            )
    return by_key.get


def plan_sha256(manifest_fingerprint: str, command_digests: dict[str, str]) -> str:
    body = json.dumps(
        {"domain": "erp.ar-opening.plan.v1", "manifest": manifest_fingerprint, "commands": command_digests},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


# --- accounts (mint) ----------------------------------------------------------------------------------------------


def mint_customers_and_accounts(db, *, scope, partitions, customers, precision: int = 2):
    """One customer per legacy customer id, one billing account per customer x currency. Idempotent.

    `partitions` is [(legacy customer key, currency)] (carryable ones only); `customers` maps the legacy key to its
    legacy customer row (legal name, `dotmac_sub_id` kept only as an immutable external reference).
    Returns {(key, currency): billing account id}.
    """

    from dotmac_billing import service as billing
    from dotmac_customers import AccountStatus, PartyReferenceRole
    from dotmac_customers.models import CustomerAccount, CustomerPartyReference

    # Direct owner rows through dotmac-customers' own models: its `create_account` / `link_party_reference`
    # import the kernel's reference runtime (`dotmac_kernel.db`, which requires DATABASE_URL), which a composed ERP
    # process must not construct (same precedent as the accounting a1 seed helpers). The rules mirrored from them:
    # upper-cased account number, required non-empty display name, one reference per (account, system, role).
    minted: dict[tuple[str, str], UUID] = {}
    customer_ids: dict[str, UUID] = {}
    for key, currency in sorted(partitions):
        if key not in customer_ids:
            number = customer_account_number(scope.tenant_id, key).upper()
            row = customers.get(key) or {}
            name = str(row.get("legal_name") or row.get("trading_name") or key).strip()
            existing = db.scalar(
                sa.select(CustomerAccount.id).where(
                    CustomerAccount.tenant_id == scope.tenant_id, CustomerAccount.account_number == number
                )
            )
            if existing is None:
                account = CustomerAccount(
                    tenant_id=scope.tenant_id,
                    account_number=number,
                    display_name=name[:200],
                    status=AccountStatus.ACTIVE,
                )
                db.add(account)
                db.flush()
                references = [("legacy-erp", key)]
                if row.get("dotmac_sub_id") is not None:  # immutable external reference only (v1677: no sync)
                    references.append(("dotmac-sub-external", str(row["dotmac_sub_id"])))
                for system, reference in references:
                    db.add(
                        CustomerPartyReference(
                            tenant_id=scope.tenant_id,
                            account_id=account.id,
                            party_system=system,
                            party_reference=reference,
                            role=PartyReferenceRole.ACCOUNT_HOLDER,
                        )
                    )
                db.flush()
                existing = account.id
            customer_ids[key] = existing
        billing_account = billing.create_billing_account(
            db, scope=scope, external_account_ref=str(customer_ids[key]), currency=currency, minor_units=precision
        )
        minted[(key, currency)] = billing_account.id
    return minted


def existing_account_ids(db, *, scope, partitions) -> dict[tuple[str, str], UUID]:
    """Read-only lookup of the minted billing accounts (the plan needs the real ids)."""

    from dotmac_customers.models import CustomerAccount

    found: dict[tuple[str, str], UUID] = {}
    for key, currency in sorted(partitions):
        customer = db.scalar(
            sa.select(CustomerAccount.id).where(
                CustomerAccount.tenant_id == scope.tenant_id,
                CustomerAccount.account_number == customer_account_number(scope.tenant_id, key),
            )
        )
        if customer is None:
            continue
        account = db.scalar(
            sa.text(
                "SELECT id FROM mod_billing.billing_accounts "
                "WHERE tenant_id = :t AND external_account_ref = :ref AND currency = :c"
            ),
            {"t": scope.tenant_id, "ref": str(customer), "c": currency},
        )
        if account is not None:
            found[(key, currency)] = account
    return found


# --- plan ----------------------------------------------------------------------------------------------------------


def build_plan(assembly, db, *, scope, migration_authority_ref: str, occurred_at: datetime, params: dict) -> dict:
    """Pure over the assembly plus read-only target lookups. Writes nothing."""

    from dotmac_billing import get_opening_cash_target, opening_account_command_digest_v2
    from dotmac_billing.opening_receivable_validation import preview_opening_account_import_batch_v2

    commands = []
    for partition in assembly.partitions():
        target = get_opening_cash_target(db, scope=scope, billing_account_id=partition.account_id)
        commands.append(
            assembly.command(
                partition, target=target, migration_authority_ref=migration_authority_ref, occurred_at=occurred_at
            )
        )
    batch = preview_opening_account_import_batch_v2(commands, verifier=None)
    manifest_blockers = [(b.code, b.count) for b in batch.manifest_blockers]
    partition_blockers = sorted(
        (c.partition.source_account_key, b.code, b.count)
        for c, item in zip(commands, batch.items, strict=True)
        for b in item.partition_blockers
    )
    digests = {str(c.partition.account_id): opening_account_command_digest_v2(c) for c in commands}
    fingerprint = assembly.fingerprint()
    manifest = assembly.manifest()
    variances = [
        {
            "partition": v.partition.source_account_key if v.partition else None,
            "reason": v.reason_code,
            "basis": v.basis,
            "amount_minor": v.amount_minor,
            "source_key": v.source.key,
        }
        for v in manifest.variances
    ]
    return {
        "kind": "ar-opening-plan",
        "params": params,
        "snapshot_sha256": assembly.census.snapshot_sha256,
        "census_sha256": assembly.census.sha256,
        "manifest_fingerprint": fingerprint,
        "plan_sha256": plan_sha256(fingerprint, digests),
        "command_digests": digests,
        "partitions": len(commands),
        "excluded_partitions": len(assembly.census.excluded),
        "receivable_items": len(manifest.receivables),
        "cash_lots": len(manifest.cash_lots),
        "open_ar_minor": sum(c.open_ar_minor for c in manifest.controls),
        "available_credit_minor": sum(c.available_cash_credit_minor for c in manifest.controls),
        "variances": variances,
        "manifest_blockers": manifest_blockers,
        "partition_blockers": partition_blockers,
        "preview_ok": not manifest_blockers and not partition_blockers,
    }


# --- apply ---------------------------------------------------------------------------------------------------------


def _set_tenant(db, tenant_id: UUID) -> None:
    db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})


def role_oid(db, name: str = WRITER_ROLE) -> int:
    oid = db.scalar(sa.text("SELECT oid::bigint FROM pg_catalog.pg_roles WHERE rolname = :n"), {"n": name})
    _require(oid is not None, "restricted_role_missing")
    return int(oid)


def apply_opening(
    engine,
    *,
    assembly,
    authority,
    plan: dict,
    approved_plan_sha256: str,
    occurred_at: datetime,
    applied_by: str,
    coupled_watermark: str,
    env: dict | None = None,
) -> dict:
    """Provision -> import every partition -> seal. Raises OperatorRefusal on any refusal (aborts for a re-plan)."""

    from dotmac_billing import get_opening_cash_target, get_opening_window, get_opening_window_admissions
    from dotmac_billing import service as billing
    from dotmac_billing.errors import BillingError
    from dotmac_billing.opening_lane import import_opening_lane_partition, seal_opening_lane
    from dotmac_billing.opening_window_contracts import OpeningWindowV1, ProvisionOpeningWindowV1, SealOpeningWindowV1

    env = os.environ if env is None else env
    _require(env.get("ERP_AR_OPENING_APPLY") == "1", "explicit_apply_authorization_required")
    _require(_HEX64.fullmatch(approved_plan_sha256 or "") is not None, "approved_plan_sha_required")
    _require(plan["plan_sha256"] == approved_plan_sha256, "approved_plan_sha_mismatch")
    _require(plan["census_sha256"] == assembly.census.sha256, "census_sha_mismatch")
    _require(plan["manifest_fingerprint"] == assembly.fingerprint(), "manifest_fingerprint_mismatch")
    _require(plan["preview_ok"] is True, "plan_has_blockers")
    _require(
        not plan["variances"] or env.get("ERP_AR_OPENING_ALLOW_OPEN_VARIANCES") == "1",
        "variances_need_post_seal_resolution",
    )
    scope = assembly.scope
    manifest = assembly.manifest()
    window_id = uuid5(NAMESPACE_URL, f"erp-ar-opening-window:{approved_plan_sha256}")
    epoch = uuid5(NAMESPACE_URL, f"erp-ar-opening-epoch:{assembly.census.sha256}")
    receipt: dict = {"kind": "ar-opening-apply", "plan_sha256": approved_plan_sha256, "admitted": [], "sealed": False}

    # 1. provision (billing's trigger insists current_user = 'app_admin'; never the restricted principal)
    with Session(engine) as db, db.begin():
        _set_tenant(db, scope.tenant_id)
        existing = get_opening_window(db, scope=scope)
        if existing is None:
            db.execute(sa.text("SET LOCAL ROLE app_admin"))
            window = OpeningWindowV1(
                scope=scope,
                window_id=window_id,
                epoch=epoch,
                manifest_version=2,
                manifest_fingerprint=assembly.fingerprint(),
                manifest=manifest,
                principal_name=WRITER_ROLE,
                principal_oid=role_oid(db),
                provision_authority_ref=authority.migration_authority_ref,
                recorded_at=occurred_at,
            )
            billing.provision_opening_window(
                db, scope=scope, command=ProvisionOpeningWindowV1(window), verifier=authority.provision_verifier()
            )
            receipt["window"] = "provisioned"
        else:
            _require(
                existing.window_id == window_id and existing.manifest_fingerprint == assembly.fingerprint(),
                "existing_window_differs",
            )
            receipt["window"] = "resumed"
    receipt["window_id"] = str(window_id)

    # 2. import every partition as the restricted principal (idempotent: a re-run replays the stored receipt)
    for partition in assembly.partitions():
        try:
            with Session(engine) as db, db.begin():
                _set_tenant(db, scope.tenant_id)
                target = get_opening_cash_target(db, scope=scope, billing_account_id=partition.account_id)
                command = assembly.command(
                    partition,
                    target=target,
                    migration_authority_ref=authority.migration_authority_ref,
                    occurred_at=occurred_at,
                    idempotency_key=f"erp-ar-opening:{approved_plan_sha256[:16]}:{partition.account_id}",
                )
                from dotmac_billing import opening_account_command_digest_v2

                digest = opening_account_command_digest_v2(command)
                db.execute(sa.text(f"SET LOCAL ROLE {WRITER_ROLE}"))
                result = import_opening_lane_partition(db, scope=scope, command=command, authority=authority)
                db.execute(sa.text("RESET ROLE"))
                db.execute(
                    sa.text(
                        f"INSERT INTO public.{TABLE} (id, tenant_id, source_account_key, currency, billing_account_id, "
                        "billing_receipt_id, posting_group_id, window_id, command_digest, manifest_fingerprint, "
                        "census_sha256, snapshot_sha256, plan_sha256, applied_by) VALUES "
                        "(:id, :t, :k, :c, :a, :r, :g, :w, :d, :m, :cs, :ss, :p, :by) "
                        "ON CONFLICT (tenant_id, billing_account_id) DO NOTHING"
                    ),
                    {
                        "id": uuid5(NAMESPACE_URL, f"erp-ar-opening-admission:{result.receipt_id}"),
                        "t": scope.tenant_id,
                        "k": partition.source_account_key,
                        "c": partition.currency,
                        "a": result.billing_account_id,
                        "r": result.receipt_id,
                        "g": result.posting_group_id,
                        "w": window_id,
                        "d": digest,
                        "m": assembly.fingerprint(),
                        "cs": assembly.census.sha256,
                        "ss": assembly.census.snapshot_sha256,
                        "p": approved_plan_sha256,
                        "by": applied_by,
                    },
                )
                receipt["admitted"].append(
                    {
                        "partition": partition.source_account_key,
                        "currency": partition.currency,
                        "receipt_id": str(result.receipt_id),
                        "posting_group_id": str(result.posting_group_id),
                        "command_digest": digest,
                    }
                )
        except BillingError as exc:  # operational hold or any refusal: abort for a re-plan, no hold path
            receipt["aborted"] = {"partition": partition.source_account_key, "code": exc.code}
            raise OperatorRefusal(f"import_refused:{exc.code}") from exc

    # 3. seal with the coupled watermark and the full admissions population
    with Session(engine) as db, db.begin():
        _set_tenant(db, scope.tenant_id)
        window = get_opening_window(db, scope=scope)
        _require(window is not None and window.window_id == window_id, "window_missing_at_seal")
        admissions = get_opening_window_admissions(db, scope=scope)
        command = SealOpeningWindowV1(
            scope=scope,
            window_id=window_id,
            epoch=epoch,
            manifest_fingerprint=assembly.fingerprint(),
            admissions=admissions,
            coupled_watermark=coupled_watermark,
            command_key=f"erp-ar-opening-seal:{approved_plan_sha256[:16]}",
            authority_ref=authority.migration_authority_ref,
            occurred_at=occurred_at,
        )
        db.execute(sa.text(f"SET LOCAL ROLE {WRITER_ROLE}"))
        try:
            seal = seal_opening_lane(db, scope=scope, command=command, authority=authority)
        except BillingError as exc:
            raise OperatorRefusal(f"seal_refused:{exc.code}") from exc
        db.execute(sa.text("RESET ROLE"))
        receipt["sealed"] = True
        receipt["seal_population_digest"] = seal.population_digest
        receipt["seal_population_count"] = seal.population_count
    return receipt


# --- command line --------------------------------------------------------------------------------------------------


def _receipt_fd(path: Path) -> int:
    _require(path.parent.resolve(strict=True) == Path(os.path.abspath(path.parent)), "receipt_symlink_forbidden")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_inputs(args, source_url: str):
    """Read the three legacy tables + customers on a READ ONLY connection and build the census."""

    from erp_v2.importer.ar_census import build_census, load_legacy_rows
    from erp_v2.importer.identity_cli import _manifest, _safe_engine

    manifest = _manifest(Path(args.manifest).read_bytes())
    _require(_sha(Path(args.snapshot_file)) == manifest.snapshot_sha256, "snapshot_digest_mismatch")
    _require(len(manifest.tenant_extent) == 1, "single_tenant_extent_required")
    cutoff = datetime.fromisoformat(args.cutoff)
    engine = _safe_engine(source_url)
    try:
        with engine.connect() as source, source.begin():
            source.execute(sa.text("SET TRANSACTION READ ONLY"))
            source.execute(sa.text("SET LOCAL row_security = off"))
            _require(
                source.scalar(sa.text("SELECT current_database()")) == manifest.source_database,
                "source_database_mismatch",
            )
            rows = load_legacy_rows(source, cutoff)
            customers = [dict(r) for r in source.execute(sa.text(CUSTOMER_SQL)).mappings()]
    finally:
        engine.dispose()
    from dotmac_billing.contracts import PartyDocumentSnapshotV1, PostalAddressSnapshotV1

    seller = PartyDocumentSnapshotV1(
        legal_name=os.environ.get("ERP_SELLER_LEGAL_NAME", ""),
        address=PostalAddressSnapshotV1(line_one=os.environ.get("ERP_SELLER_ADDRESS_LINE", ""), country_code="NG"),
    )
    _require(bool(seller.legal_name) and bool(seller.address.line_one), "seller_identity_required")
    snapshot = customer_snapshot_from(customers)
    census = build_census(
        snapshot_sha256=manifest.snapshot_sha256,
        cutoff=cutoff,
        invoices=rows["invoices"],
        payments=rows["payments"],
        allocations=rows["allocations"],
        customer_snapshot=snapshot,
        seller=seller,
    )
    return manifest, rows, customers, census, snapshot, seller


def _capture(manifest, params):
    from dotmac_billing.opening_contracts import OpeningCaptureV1

    from erp_v2.importer.billing_opening import MAPPING_VERSION, SOURCE_SYSTEM

    cutoff = datetime.fromisoformat(params["cutoff"])
    return OpeningCaptureV1(
        source_system=SOURCE_SYSTEM,
        source_repository_sha=manifest.candidate_revision,
        installation_id=manifest.source_database,
        schema_heads=tuple(h for h in params["source_schema_heads"].split(",") if h),
        snapshot_id=manifest.snapshot_sha256,
        captured_at=datetime.fromisoformat(params["captured_at"]),
        economic_cutoff=cutoff,
        recorded_cutoff=cutoff,
        isolation="repeatable_read",
        population_version="1",
        source_authority_ref=params["source_authority_ref"],
        account_mapping_version=MAPPING_VERSION,
        cash_mapping_version=MAPPING_VERSION,
    )


def _run(args) -> dict:
    from dotmac_kernel.cache import TenantScope

    from erp_v2.importer.ar_assembly import ArAssembly, ErpOpeningAuthority
    from erp_v2.importer.ar_census import normalise_rows
    from erp_v2.importer.identity_cli import _safe_engine

    source_url = os.environ.pop("LEGACY_DATABASE_URL", "")
    destination_url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    _require(bool(source_url), "operator_database_materials_required")
    _require(
        "DATABASE_URL" not in os.environ and "PLATFORM_DATABASE_URL" not in os.environ,
        "serving_database_materials_forbidden",
    )
    manifest, rows, customers, census, snapshot, seller = _load_inputs(args, source_url)
    if args.command == "census":
        return census.receipt()
    _require(bool(destination_url), "operator_database_materials_required")
    tenant = UUID(manifest.tenant_extent[0])
    scope = TenantScope(tenant)
    params = {
        "cutoff": args.cutoff,
        "captured_at": args.captured_at,
        "source_schema_heads": args.source_schema_heads,
        "source_authority_ref": args.source_authority_ref,
        "migration_authority_ref": args.migration_authority_ref,
        "occurred_at": args.occurred_at,
    }
    engine = _safe_engine(destination_url)
    try:
        carry = list(census.carry_partitions)
        if args.command == "mint":
            _require(os.environ.get("ERP_AR_OPENING_APPLY") == "1", "explicit_apply_authorization_required")
            by_key = {str(c["customer_key"]): c for c in customers}
            with Session(engine) as db, db.begin():
                _set_tenant(db, tenant)
                minted = mint_customers_and_accounts(db, scope=scope, partitions=carry, customers=by_key)
            return {"kind": "ar-opening-mint", "census_sha256": census.sha256, "accounts": len(minted)}
        with Session(engine) as db, db.begin():
            _set_tenant(db, tenant)
            db.execute(sa.text("SET TRANSACTION READ ONLY")) if args.command == "plan" else None
            accounts = existing_account_ids(db, scope=scope, partitions=carry)
        _require(len(accounts) == len(carry), "accounts_not_minted")
        ni, np_, na = normalise_rows(rows["invoices"], rows["payments"], rows["allocations"])
        assembly = ArAssembly(
            tenant_id=tenant,
            capture=_capture(manifest, params),
            census=census,
            invoices=ni,
            payments=np_,
            allocations=na,
            account_ids=accounts,
            customer_snapshot=snapshot,
            seller=seller,
        )
        occurred_at = datetime.fromisoformat(args.occurred_at)
        with Session(engine) as db, db.begin():
            _set_tenant(db, tenant)
            db.execute(sa.text("SET TRANSACTION READ ONLY"))
            plan = build_plan(
                assembly,
                db,
                scope=scope,
                migration_authority_ref=args.migration_authority_ref,
                occurred_at=occurred_at,
                params=params,
            )
            oid = role_oid(db)
        if args.command == "plan":
            return plan
        authority = ErpOpeningAuthority(
            assembly=assembly,
            migration_authority_ref=args.migration_authority_ref,
            role_oid=oid,
            expected_coupled_watermark=args.coupled_watermark,
        )
        return apply_opening(
            engine,
            assembly=assembly,
            authority=authority,
            plan=plan,
            approved_plan_sha256=args.approved_plan_sha256,
            occurred_at=occurred_at,
            applied_by=args.applied_by,
            coupled_watermark=args.coupled_watermark,
        )
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.importer.ar_opening_cli")
    parser.add_argument("command", choices=("census", "mint", "plan", "apply"))
    for name in ("manifest", "snapshot-file", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--cutoff", required=True, help="capture cutoff (ISO-8601 with offset), e.g. the opening date")
    parser.add_argument("--captured-at", default=datetime.now(UTC).isoformat())
    parser.add_argument("--occurred-at", default=datetime.now(UTC).isoformat())
    parser.add_argument("--source-schema-heads", default="")
    parser.add_argument("--source-authority-ref", default="")
    parser.add_argument("--migration-authority-ref", default="", help="dotmac-debt-register v<N> of Michael's go")
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument("--coupled-watermark", default="")
    parser.add_argument("--applied-by", default="ar_opening_cli")
    args = parser.parse_args(argv)
    try:
        if args.command in ("mint", "apply"):
            _require(os.environ.get("ERP_AR_OPENING_APPLY") == "1", "explicit_apply_authorization_required")
        fd = _receipt_fd(args.receipt)
    except Exception:
        print("ar opening refused: authorization or private receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            if args.command in ("plan", "apply"):
                _require(
                    bool(args.migration_authority_ref) and bool(args.source_authority_ref), "authority_refs_required"
                )
            if args.command == "apply":
                _require(bool(args.coupled_watermark), "coupled_watermark_required")
            payload, code = _run(args), 0
            if args.command == "plan" and not payload["preview_ok"]:
                code = 3
        except OperatorRefusal as exc:
            payload, code = {"refusal": exc.reason}, 2
        except Exception:
            payload, code = {"refusal": "ar_opening_command_refused"}, 2
        json.dump(payload, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(f"ar opening {args.command}: " + ("refused" if code == 2 else "done"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
