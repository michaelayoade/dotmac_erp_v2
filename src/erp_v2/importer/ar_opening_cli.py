"""AR opening operator boundary: census / mint / plan / apply over dotmac-billing's opening window and lane.

Same discipline as the payables historical CLIs: env-only DSNs (`LEGACY_DATABASE_URL`, read only;
`MIGRATION_DATABASE_URL`), receipts created 0600 BEFORE any database action, plan then apply, every step bound to
the snapshot sha, the census sha and the plan sha. Dry-run by default: `apply` needs `ERP_AR_OPENING_APPLY=1` AND
the approved plan sha.

Flow (AS-IS migration replan, section 2):
- `census`  read-only: bucket every legacy invoice/payment exactly once; writes the census receipt (the input of the
            register source `ar_census`: E35 partitions and documents, unapplied credit notes, E36b variances).
- `mint`    (apply-gated) one billing account per customer x currency for the CARRYABLE partitions. It REUSES the
            wave-4 `customer_accounts` (the importer preserves the legacy customer id as the account id) and
            refuses `customer_not_imported`; it never creates a customer.
- `plan`    census -> the ONE manifest -> `preview_opening_account_import_batch_v2` (expect 0 blockers); every
            variance listed and sha-bound; plan sha over the manifest fingerprint, the variances and every
            partition's command digest. Writes nothing.
- `apply`   provision the window (as app_admin, by billing's trigger) with the ERP two-person policy passed
            explicitly (`dual_control`, a13), import every partition as the restricted `erp_billing_opening_writer`
            (operator `SET ROLE` membership is granted by Michael for the window only), then seal. Resumable.
            There is NO hold path: an operational hold or any refusal aborts the run for a re-plan. 0 GL.

Variances (stored-balance differences, memo basis: no accounting fact, never suspense; billing a13 P-3) are
recorded by billing at the seal. `apply` proceeds only when every variance is listed in the plan (and so
sha-bound); each one becomes an E36b work item. Recognise and confirm are post-flip, as app_user, NOT in this run.

Seams (not built here): T1 `ar_opening_credit` reclass journal and the holding reclass (B-14, e2_0030), the live
mapper T2/T3 (B-15). `t1_credit_by_currency` gives B-14 the sealed-manifest amounts (T1 anchoring).
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

from erp_v2.importer import ar_opening_role as role_guard
from erp_v2.importer.ar_census import CensusError
from erp_v2.importer.ar_opening_models import TABLE, WRITER_ROLE

FUNCTIONAL_CURRENCY = "NGN"  # P-4/P-7: non-functional AR is refused until ruled on
ADDRESS_NOT_RECORDED = "Not recorded in legacy ERP"
CUSTOMER_SQL = (
    "SELECT customer_id AS customer_key, legal_name, trading_name, dotmac_sub_id FROM customer"  # legacy ERP public
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class OperatorRefusal(ValueError):
    def __init__(self, reason: str, receipt: dict | None = None):
        self.reason = reason
        self.receipt = receipt  # apply: what was admitted before the refusal (an abort never loses its evidence)
        super().__init__(reason)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise OperatorRefusal(reason)


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


def plan_sha256(
    manifest_fingerprint: str,
    command_digests: dict[str, str],
    variances: list[dict] | None = None,
    dual_control: str = "on",
    gl_attribution_sha256: str = "",
) -> str:
    """The plan sha binds the manifest, every command digest, every variance, the window's two-person policy and the
    sha of the census `gl_attribution` block (the per-party split B-14 itemises)."""

    body = json.dumps(
        {
            "domain": "erp.ar-opening.plan.v4",
            "manifest": manifest_fingerprint,
            "commands": command_digests,
            "variances": variances or [],
            "dual_control": dual_control,
            "gl_attribution": gl_attribution_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def manifest_variances(manifest) -> list[dict]:
    """Every variance of the manifest as plain data (the plan lists them; the E36b work items come from this)."""

    return sorted(
        (
            {
                "partition": v.partition.source_account_key if v.partition else None,
                "currency": v.currency,
                "reason": v.reason_code,
                "basis": v.basis,
                "amount_minor": v.amount_minor,
                "source_key": v.source.key,
            }
            for v in manifest.variances
        ),
        key=lambda v: (str(v["partition"]), v["source_key"], v["basis"]),
    )


def t1_credit_by_currency(manifest) -> dict[str, int]:
    """T1 seam: the Dr 1400 / Cr prepayment amount per currency = sum of cash-lot `residual_minor` of the admitted
    partitions, read from the manifest the window sealed (never from operator input). The B-14 `ar_opening_credit`
    journal kind consumes this; it may be cross-checked against R1 `available_credit_minor` ONLY between the seal
    and the opening of the app (writer fence held). Built here as a pure function; no journal is posted."""

    out: dict[str, int] = {}
    for lot in manifest.cash_lots:
        out[lot.partition.currency] = out.get(lot.partition.currency, 0) + lot.residual_minor
    return dict(sorted(out.items()))


# --- accounts (mint) ----------------------------------------------------------------------------------------------


def wave4_account_id(db, *, scope, legacy_customer_key: str) -> UUID:
    """The wave-4 `customer_accounts` row of a legacy customer (the importer preserves the legacy id as the account
    id). A key that is not a UUID or has no account is `customer_not_imported`: this CLI never creates a customer."""

    from dotmac_customers.models import CustomerAccount

    try:
        candidate = UUID(str(legacy_customer_key))
    except ValueError:
        raise OperatorRefusal("customer_not_imported") from None
    found = db.scalar(
        sa.select(CustomerAccount.id).where(
            CustomerAccount.tenant_id == scope.tenant_id, CustomerAccount.id == candidate
        )
    )
    if found is None:
        raise OperatorRefusal("customer_not_imported")
    return found


def mint_billing_accounts(db, *, scope, partitions, precision: int = 2):
    """One billing account per customer x currency over the imported wave-4 customer. Idempotent.

    Returns {(key, currency): billing account id}. Refuses `customer_not_imported` for any partition whose customer
    the wave-4 import did not land (checked for ALL partitions before any account is created)."""

    from dotmac_billing import service as billing

    customer_ids = {key: wave4_account_id(db, scope=scope, legacy_customer_key=key) for key, _c in sorted(partitions)}
    minted: dict[tuple[str, str], UUID] = {}
    for key, currency in sorted(partitions):
        billing_account = billing.create_billing_account(
            db, scope=scope, external_account_ref=str(customer_ids[key]), currency=currency, minor_units=precision
        )
        minted[(key, currency)] = billing_account.id
    return minted


def existing_account_ids(db, *, scope, partitions) -> dict[tuple[str, str], UUID]:
    """Read-only lookup of the minted billing accounts (the plan needs the real ids)."""

    found: dict[tuple[str, str], UUID] = {}
    for key, currency in sorted(partitions):
        try:
            customer = wave4_account_id(db, scope=scope, legacy_customer_key=key)
        except OperatorRefusal:
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


def build_plan(
    assembly,
    db,
    *,
    scope,
    migration_authority_ref: str,
    occurred_at: datetime,
    params: dict,
    dual_control: str,
    gl_attribution_sha256: str,
) -> dict:
    """Pure over the assembly plus read-only target lookups. Writes nothing.

    Fail closed on FX (replan T5): any partition whose currency is not the functional one is refused BEFORE anything
    is imported or sealed (the seal is irreversible); lifted only by an explicit P-4/P-7 ruling."""

    if any(p.currency != FUNCTIONAL_CURRENCY for p in assembly.partitions()):
        raise OperatorRefusal("ar_opening_fx_unsupported")

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
    variances = manifest_variances(manifest)
    return {
        "kind": "ar-opening-plan",
        "params": params,
        "snapshot_sha256": assembly.census.snapshot_sha256,
        "census_sha256": assembly.census.sha256,
        "manifest_fingerprint": fingerprint,
        "dual_control": dual_control,
        "gl_attribution_sha256": gl_attribution_sha256,
        "plan_sha256": plan_sha256(fingerprint, digests, variances, dual_control, gl_attribution_sha256),
        "command_digests": digests,
        "partitions": len(commands),
        "excluded_partitions": len(assembly.census.excluded),
        "collection_block": assembly.census.collection_block(assembly.account_ids),
        "unapplied_credit_notes": sum(1 for r in assembly.census.rows if r.bucket == "UNAPPLIED_CREDIT_NOTE"),
        "receivable_items": len(manifest.receivables),
        "cash_lots": len(manifest.cash_lots),
        "open_ar_minor": sum(c.open_ar_minor for c in manifest.controls),
        "available_credit_minor": sum(c.available_cash_credit_minor for c in manifest.controls),
        "variances": variances,
        "t1_credit_by_currency": t1_credit_by_currency(manifest),
        "manifest_blockers": manifest_blockers,
        "partition_blockers": partition_blockers,
        "preview_ok": not manifest_blockers and not partition_blockers,
    }


# --- apply ---------------------------------------------------------------------------------------------------------


def _set_tenant(db, tenant_id: UUID) -> None:
    db.execute(sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})


def _verify_window_dual_control(window, wanted: str) -> None:
    """Read-back: the window's bound `dual_control` (billing a13, immutable once provisioned) must equal the ERP
    policy."""

    _require(window is not None, "window_missing")
    _require(window.dual_control == wanted, "window_dual_control_differs_from_policy")


def role_oid(db, name: str = WRITER_ROLE) -> int:
    oid = db.scalar(sa.text("SELECT oid::bigint FROM pg_catalog.pg_roles WHERE rolname = :n"), {"n": name})
    _require(oid is not None, "restricted_role_missing")
    return int(oid)


def _cash_lot_split(manifest) -> dict[str, int]:
    """Per billing account: sum of the cash-lot residuals (the T1 split B-14 needs)."""

    out: dict[str, int] = {}
    for lot in manifest.cash_lots:
        key = str(lot.partition.account_id)
        out[key] = out.get(key, 0) + lot.residual_minor
    return dict(sorted(out.items()))


def apply_opening(engine, **kwargs) -> dict:
    """Provision -> import every partition -> seal. A refusal or failure keeps the partial receipt (what was admitted)
    on the raised OperatorRefusal, so an abort never loses its evidence."""

    receipt: dict = {"kind": "ar-opening-apply", "admitted": [], "sealed": False}
    try:
        return _apply_opening(engine, receipt=receipt, **kwargs)
    except OperatorRefusal as exc:
        exc.receipt = receipt
        raise
    except role_guard.WriterRoleRefused as exc:
        raise OperatorRefusal(str(exc), receipt) from exc
    except Exception as exc:
        raise OperatorRefusal(f"apply_failed:{type(exc).__name__}", receipt) from exc


def _apply_opening(
    engine,
    *,
    receipt: dict,
    assembly,
    authority,
    plan: dict,
    approved_plan_sha256: str,
    occurred_at: datetime,
    applied_by: str,
    coupled_watermark: str,
    dual_control: bool | None = None,
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
    # Every variance must be listed in the plan (so sha-bound, plan_sha256 covers them) and equal the manifest's own.
    _require(plan["variances"] == manifest_variances(assembly.manifest()), "variances_not_listed_in_plan")
    _require(
        plan["plan_sha256"]
        == plan_sha256(
            plan["manifest_fingerprint"],
            plan["command_digests"],
            plan["variances"],
            plan["dual_control"],
            plan["gl_attribution_sha256"],
        ),
        "plan_sha_not_self_consistent",
    )
    scope = assembly.scope
    manifest = assembly.manifest()
    window_id = uuid5(NAMESPACE_URL, f"erp-ar-opening-window:{approved_plan_sha256}")
    epoch = uuid5(NAMESPACE_URL, f"erp-ar-opening-epoch:{assembly.census.sha256}")
    receipt.update(
        {
            "plan_sha256": approved_plan_sha256,
            "variances": plan["variances"],  # each is an E36b work item; recognise/confirm are post-flip (app_user)
            "t1_credit_by_currency": t1_credit_by_currency(manifest),
            "cash_lot_residual_by_account": _cash_lot_split(manifest),
        }
    )

    # 1. provision (billing's trigger insists current_user = 'app_admin'; never the restricted principal)
    with Session(engine) as db, db.begin():
        _set_tenant(db, scope.tenant_id)
        # The ERP two-person policy (#62; default OFF per tenant) is bound at provision and immutable afterwards
        # (billing a13 `dual_control`). Read it here and pass it EXPLICITLY; never rely on billing's default.
        if dual_control is None:
            from erp_v2.governance.policy import dual_control as read_dual_control

            dual_control = read_dual_control(db, scope.tenant_id)
        wanted = "on" if dual_control else "off"
        receipt["dual_control"] = wanted
        _require(
            plan["dual_control"] == wanted, "plan_dual_control_differs_from_policy"
        )  # policy flipped since approval
        _require(authority.expected_dual_control == wanted, "authority_dual_control_differs_from_policy")
        # The writer role is re-checked now, not trusted from the migration: no SET/INHERIT member except this
        # session's operator (SET only), no owned objects, exact R5 privileges.
        role_guard.preflight(
            db.connection(),
            WRITER_ROLE,
            operator=db.scalar(sa.text("SELECT session_user")),
        )
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
                dual_control=wanted,  # explicit: never billing's default
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
        _verify_window_dual_control(get_opening_window(db, scope=scope), wanted)
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
        receipt["window_manifest_fingerprint"] = get_opening_window(db, scope=scope).manifest_fingerprint
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


def _load_inputs(args, source_url: str, ar_account_ids=None):
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
            gl_rows = None
            if ar_account_ids:
                from erp_v2.importer.ar_attribution import load_gl_rows

                gl_rows = load_gl_rows(source, ar_account_ids, cutoff)
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
    return manifest, rows, customers, census, snapshot, seller, gl_rows


def _ar_control_account_ids(destination_url: str, tenant: UUID) -> list[str]:
    """Ids of the destination account coded 1400 (legacy account ids are v2 account ids). Read only."""

    from dotmac_accounting.models import Account

    from erp_v2.importer.identity_cli import _safe_engine

    engine = _safe_engine(destination_url)
    try:
        with Session(engine) as db, db.begin():
            db.execute(sa.text("SET TRANSACTION READ ONLY"))
            _set_tenant(db, tenant)
            return [
                str(i)
                for i in db.scalars(sa.select(Account.id).where(Account.tenant_id == tenant, Account.code == "1400"))
            ]
    finally:
        engine.dispose()


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
    ar_accounts = None
    if destination_url:
        from erp_v2.importer.identity_cli import _manifest as _read_manifest

        extent = _read_manifest(Path(args.manifest).read_bytes()).tenant_extent
        _require(len(extent) == 1, "single_tenant_extent_required")
        ar_accounts = _ar_control_account_ids(destination_url, UUID(extent[0]))
    manifest, rows, _customers, census, snapshot, seller, gl_rows = _load_inputs(args, source_url, ar_accounts)
    attribution: dict = {}
    if gl_rows is not None:  # B-14 input: per-customer legacy 1400 attribution (needs the destination chart)
        from erp_v2.importer.ar_attribution import build_attribution

        lines, owners = gl_rows
        try:
            attribution = build_attribution(
                census,
                gl_lines=lines,
                journal_owners=owners,
                invoice_types={str(i["invoice_id"]): str(i.get("invoice_type") or "") for i in rows["invoices"]},
                functional_currency=args.functional_currency,
            )
        except CensusError as exc:
            raise OperatorRefusal(str(exc).split(":")[0]) from exc
    if args.command == "census":
        receipt = census.receipt()
        receipt.update(attribution)  # gl_attribution and its sha
        return receipt
    _require(bool(attribution), "gl_attribution_required")  # plan and apply are bound to the per-party split
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
            with Session(engine) as db, db.begin():
                _set_tenant(db, tenant)
                minted = mint_billing_accounts(db, scope=scope, partitions=carry)
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
            from erp_v2.governance.policy import dual_control as read_dual_control

            policy_dual_control = read_dual_control(db, tenant)
            plan = build_plan(
                assembly,
                db,
                scope=scope,
                migration_authority_ref=args.migration_authority_ref,
                occurred_at=occurred_at,
                params=params,
                dual_control="on" if policy_dual_control else "off",  # bound into the plan sha
                gl_attribution_sha256=attribution["gl_attribution_sha256"],  # bound into the plan sha
            )
            oid = role_oid(db)
        if args.command == "plan":
            return plan
        authority = ErpOpeningAuthority(
            assembly=assembly,
            migration_authority_ref=args.migration_authority_ref,
            role_oid=oid,
            expected_coupled_watermark=args.coupled_watermark,
            expected_dual_control="on" if policy_dual_control else "off",
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
            dual_control=policy_dual_control,
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
    parser.add_argument("--functional-currency", default="NGN", help="currency of journals no document owns")
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
            if exc.receipt:
                payload["partial_receipt"] = exc.receipt  # what was admitted before the abort
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
