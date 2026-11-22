"""GL attribution receipts for 2000, 2040 and 1410: the B-14 ``gl_attribution`` inputs (GL-ATTRIBUTION-RECEIPTS-SPEC).

    python -m erp_v2.migration_ledger.attribution_cli --tenant-id T --capture-date D \\
        --snapshot-manifest M --snapshot-sha256 S --cutoff-receipt C [--cutoff-receipt-sha256 X] \\
        --p-attr-receipt P --p-attr-receipt-sha256 X [--import-itemised F --import-itemised-sha256 X] \\
        [--unpaid-plan F --unpaid-plan-sha256 X] [--settled-plan F --settled-plan-sha256 X] \\
        [--s8-receipt F --s8-receipt-sha256 X --s8-fragment F --s8-fragment-sha256 X] --out-dir DIR

Read only on BOTH databases (legacy ``SET TRANSACTION READ ONLY``; destination REPEATABLE READ READ ONLY, an explicit
tenant filter on every v2 read). DSNs come from ``LEGACY_DATABASE_URL`` and ``MIGRATION_DATABASE_URL`` and are popped
from the environment. It writes only files (0600, exclusive create): one receipt per (code, functional currency) in the
exact ``parse_gl_attribution`` v1 shape and the ``gl-attribution-manifest``. It prints the manifest sha and the per-code
shas, nothing else. Integrity faults refuse (fix forward); everything that cannot be attributed becomes an itemised
E37 holding item through the receipt, never a refusal.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.contracts import JournalStatus
from dotmac_accounting.models import Account, JournalEntry, PostedLedgerLine
from sqlalchemy.orm import Session

from erp_v2.claims_opening import reclass
from erp_v2.holdings.attribution import Party
from erp_v2.holdings.models import (
    KIND_HOLDING_POST,
    KIND_RELEASE_POST,
    KIND_STRADDLE_POST,
    KIND_WRITEOFF_POST,
    OWNER,
)
from erp_v2.holdings.plan import OPENING_OWNER
from erp_v2.migration_ledger import attribution_rules as rules
from erp_v2.migration_ledger import cli_common as cli
from erp_v2.migration_ledger import gl_attribution as core
from erp_v2.migration_ledger import register
from erp_v2.migration_ledger.common import (
    ZERO,
    LedgerRefused,
    canonical_sha,
    day,
    refuse,
    sha256_bytes,
    sha_text,
    strict_json,
    uuid_text,
)
from erp_v2.migration_ledger.ledger_check import LEGACY_OWNER, MANUAL_OWNER, OPENING_OWNERS
from erp_v2.migration_ledger.rounding_verify import ROUNDING_KIND

HOLDING_KINDS = (KIND_HOLDING_POST, KIND_RELEASE_POST, KIND_WRITEOFF_POST)
RR_RO = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


def classify_v2(code: str, owner: str, kind: str | None) -> str:
    """The class of a destination journal on the control code, or a refusal (spec section 3)."""
    if owner in OPENING_OWNERS:
        return "opening"
    if owner == LEGACY_OWNER:
        return "legacy"
    if owner == OWNER:
        if kind in HOLDING_KINDS:
            return refuse("holding_already_posted")
        if kind == KIND_STRADDLE_POST:
            return refuse(f"straddle_on_control:{code}")
        if kind in rules.SUBKOBO_KINDS:
            return "legacy"
        if reclass.is_reclass_journal(owner, kind):
            return "claim_reclass"
    if owner == MANUAL_OWNER and kind == ROUNDING_KIND:
        return "rounding"
    return refuse(f"native_posting_before_flip:{code}")


def _v2_journals(db: Session, tenant: UUID, code: str) -> tuple[list[core.V2Journal], Decimal]:
    rows = db.execute(
        sa.select(
            PostedLedgerLine.journal_id,
            JournalEntry.source_owner,
            JournalEntry.source_document_kind,
            sa.func.sum(PostedLedgerLine.debit - PostedLedgerLine.credit),
        )
        .join(Account, (Account.tenant_id == PostedLedgerLine.tenant_id) & (Account.id == PostedLedgerLine.account_id))
        .join(
            JournalEntry,
            (JournalEntry.tenant_id == PostedLedgerLine.tenant_id) & (JournalEntry.id == PostedLedgerLine.journal_id),
        )
        .where(PostedLedgerLine.tenant_id == tenant, Account.tenant_id == tenant, Account.code == code)
        .group_by(PostedLedgerLine.journal_id, JournalEntry.source_owner, JournalEntry.source_document_kind)
    ).all()
    gl = sum((Decimal(n) for *_x, n in rows), ZERO)
    out = [core.V2Journal(str(j), classify_v2(code, owner, kind), Decimal(n)) for j, owner, kind, n in rows]
    return out, gl


def _account_ids(db: Session, tenant: UUID, code: str) -> list[str]:
    ids = [str(i) for i in db.scalars(sa.select(Account.id).where(Account.tenant_id == tenant, Account.code == code))]
    if not ids:
        refuse(f"control_account_missing:{code}")
    return ids


def _legacy_lines(legacy: sa.Connection, accounts: list[str], capture: date, cutoff: date) -> list[core.LegacyLine]:
    if legacy.execute(sa.text(rules.UNDATED_SQL), {"accounts": accounts}).scalar():
        refuse("legacy_ledger_line_undated")
    rows = legacy.execute(sa.text(rules.JOURNAL_NETS_SQL), {"accounts": accounts, "d": capture, "cut": cutoff}).all()
    return [core.LegacyLine(j, t, Decimal(net), Decimal(net_cut), int(n)) for j, t, net, net_cut, n in rows]


def _owners(
    legacy: sa.Connection, code: str, journal_ids: list[str], capture: date
) -> dict[str, dict[Party, frozenset[str]]]:
    sql = rules.OWNER_SQL.get(code)
    if sql is None:
        return {}
    kind = rules.PARTY_KIND[code]
    found: dict[str, dict[Party, set[str]]] = defaultdict(lambda: defaultdict(set))
    for j, key, rule in legacy.execute(sa.text(sql), {"ids": journal_ids, "d": capture}):
        if j is None or key is None:
            continue
        found[str(j)][Party(kind, _canonical(str(key)))].add(str(rule))
    return {j: {p: frozenset(r) for p, r in per.items()} for j, per in found.items()}


def _zero_units(legacy: sa.Connection, lines: list[core.LegacyLine], capture: date) -> tuple[frozenset[str], int]:
    nets: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for ln in lines:
        nets[ln.journal] += ln.net
    rows = [
        core.K2Row(str(r), None if s is None else str(s), bool(rp), bool(sp))
        for r, s, rp, sp in legacy.execute(sa.text(rules.K2_SQL), {"d": capture})
    ]
    return core.zero_units(rows, nets)


def _invoice_facts(
    legacy: sa.Connection, ids: list[str], capture: date, functional: str
) -> dict[str, core.InvoiceFact]:
    if not ids:
        return {}
    out: dict[str, core.InvoiceFact] = {}
    for inv, sup, posted, residual in legacy.execute(
        sa.text(rules.INVOICE_RESIDUAL_SQL), {"ids": ids, "d": capture, "functional": functional}
    ):
        out[str(inv)] = core.InvoiceFact(
            _canonical(str(sup)), core.exact2(Decimal(residual), f"invoice:{inv}"), bool(posted)
        )
    return out


def _party_kinds_2040(db: Session, tenant: UUID) -> None:
    from erp_v2.holdings import positions

    if {p.kind for (p, _c) in positions.sub_2040(db, tenant)} - {"employee"}:
        refuse("party_kind_unexpected")


SOURCE_NAMES = ("import_itemised", "unpaid_plan", "settled_plan", "s8")


class Sources:
    """The sha-bound input bytes of the producer. Every source is supplied OR declared absent (``--absent <name>``):
    an undeclared missing source refuses ``source_required:<name>`` (a silently missing source would leave refused
    items unexplained while their register rows stay open). The S8 source is the receipt plus its fragment, together."""

    def __init__(
        self,
        *,
        import_itemised: bytes | None = None,
        unpaid_plan: bytes | None = None,
        settled_plan: bytes | None = None,
        s8_receipt: bytes | None = None,
        s8_fragment: bytes | None = None,
        absent: Iterable[str] = (),
    ) -> None:
        self.import_itemised, self.unpaid_plan, self.settled_plan = import_itemised, unpaid_plan, settled_plan
        self.s8_receipt, self.s8_fragment = s8_receipt, s8_fragment
        self.absent = frozenset(absent)

    def sha(self, name: str) -> str | None:
        raw = getattr(self, name)
        return None if raw is None else sha256_bytes(raw)

    def require(self) -> None:
        unknown = self.absent - set(SOURCE_NAMES)
        if unknown:
            refuse(f"absent_source_unknown:{sorted(unknown)[0]}")
        for name in SOURCE_NAMES:
            parts = ("s8_receipt", "s8_fragment") if name == "s8" else (name,)
            supplied = [getattr(self, part) is not None for part in parts]
            if name in self.absent:
                if any(supplied):
                    refuse(f"absent_source_supplied:{name}")
            elif not all(supplied):
                refuse(f"source_required:{parts[supplied.index(False)]}")

    @property
    def declared_absent(self) -> list[str]:
        return sorted(self.absent)


def _s8_journals(db: Session, tenant: UUID) -> list[str]:
    """Every claim-family reclass journal of the tenant (any control code it touches)."""
    return reclass.reclass_journal_ids(db, tenant)


def _check_s8_receipt(raw: bytes, fragment_sha: str | None, *, tenant: UUID, snapshot_sha: str, v2_journals: list[str]):
    """The S8 apply receipt is bound by kind, self sha, tenant, snapshot and the v2 reclass journal id."""
    s8 = strict_json(raw, "invalid_s8_receipt")
    if not isinstance(s8, dict) or s8.get("kind") != "claims-opening":
        refuse("s8_receipt_invalid")
    stated = s8.get("receipt_sha256")
    if not isinstance(stated, str) or canonical_sha({k: v for k, v in s8.items() if k != "receipt_sha256"}) != stated:
        refuse("s8_receipt_invalid")
    bindings = s8.get("bindings")
    if not isinstance(bindings, dict) or bindings.get("tenant_id") != str(tenant):
        refuse("s8_receipt_not_bound")
    if bindings.get("snapshot_sha256") != snapshot_sha:
        refuse("s8_receipt_not_bound")
    if s8.get("fragment_2040_sha256") != fragment_sha:
        refuse("s8_fragment_not_bound")
    journal = s8.get("journal")
    posted = journal.get("journal_id") if isinstance(journal, dict) else None
    if len(v2_journals) > 1 or (posted is None) != (not v2_journals) or (v2_journals and str(posted) != v2_journals[0]):
        refuse("s8_receipt_not_bound")


def _legacy_account_check(legacy: sa.Connection, accounts: list[str], code: str) -> None:
    """The producer filters legacy lines by the v2 account ids (legacy id == v2 id): each must exist in legacy
    ``gl.account`` under the same code, else a named refusal instead of a generic ``legacy_import_differs``."""
    found = {
        str(i)
        for (i,) in legacy.execute(
            sa.text("SELECT account_id::text FROM gl.account WHERE account_code = :c AND account_id::text = ANY(:a)"),
            {"c": code, "a": accounts},
        )
    }
    if found != set(accounts):
        refuse(f"legacy_account_not_found:{code}")


def _canonical(key: str) -> str:
    try:
        return str(UUID(key))
    except ValueError:
        return key


def _owner_sub_keys(db: Session, tenant: UUID, code: str, functional: str) -> set[str]:
    """The owner-subledger party keys (real tables), all of which must be canonical UUID text: the legacy keys are
    canonicalised with ``str(UUID(k))``, so a differently written subledger key would split every party in two."""
    from erp_v2.holdings import positions

    keys = {p.key for (p, _ccy), v in positions.owner_sub(db, tenant, code, functional).items() if v != 0}
    if any(_canonical(k) != k for k in keys):
        refuse(f"party_key_not_canonical:{code}")
    return keys


def _check_opening(db: Session, tenant: UUID, cutoff: date) -> None:
    """The cutoff receipt carries no snapshot binding of its own: the v2 B-9 opening must be POSTED with its posting
    date equal to the cutoff (the ``cutoff_evidence`` rule)."""
    rows = db.execute(
        sa.select(JournalEntry.status, JournalEntry.posting_date).where(
            JournalEntry.tenant_id == tenant, JournalEntry.source_owner == OPENING_OWNER
        )
    ).all()
    if not rows or any(status != JournalStatus.POSTED or posted != cutoff for status, posted in rows):
        refuse("cutoff_differs_from_opening")


def produce(
    legacy: sa.Connection,
    db: Session,
    *,
    tenant: UUID,
    capture: date,
    cutoff: date,
    shas: dict[str, str | None],
    sources: Sources,
) -> dict[str, Any]:
    """Pure over the two read-only connections: ``{"receipts": {code: CodeReceipt}, "manifest": dict}``.

    ``shas`` carries the legacy_snapshot / cutoff_receipt / p_attr_receipt shas for the manifest."""
    from erp_v2.holdings import positions
    from erp_v2.journals.policy import functional_currency

    if shas.get("p_attr_receipt") != rules.P_ATTR_RECEIPT_SHA256:
        refuse("p_attr_receipt_not_pinned")
    sources.require()
    functional = functional_currency(db, tenant)
    # NOTE (F7): inside ONE REPEATABLE READ transaction the ledger cannot move, so ``moved_during_attribution`` below
    # can never fire; it is kept as a tripwire. The real staleness guard is the manifest's ledger sha, which the
    # holdings plan compares with the ledger it reads (``attribution_manifest_stale``).
    v2_sha = positions.v2_posted_sha(db, tenant)

    itemised: list[tuple[str, str, str]] = []
    admission: list[tuple[str, str, str, int | None]] = []
    unclassified: list[tuple[str, str]] = []
    if sources.import_itemised is not None:
        for f in register.from_import_itemised(strict_json(sources.import_itemised, "invalid_import_itemised")):
            itemised.append((f.class_code, f.legacy_key, ",".join(f.detail["reasons"])))
    for name in ("unpaid_plan", "settled_plan"):
        raw = getattr(sources, name)
        if raw is not None:
            classified, unc = register.classify_ap_admission(strict_json(raw, "invalid_ap_admission"))
            admission += classified
            unclassified += unc
    fragment: dict[str, Decimal] | None = None
    held_2040: list[core.HeldRow] = []
    fragment_unkeyed = 0
    if sources.s8_fragment is not None:
        fragment, held_2040, fragment_unkeyed = core.parse_fragment(
            strict_json(sources.s8_fragment, "invalid_claims_fragment")
        )
        _check_s8_receipt(
            sources.s8_receipt or b"",
            sources.sha("s8_fragment"),
            tenant=tenant,
            snapshot_sha=str(shas.get("legacy_snapshot")),
            v2_journals=_s8_journals(db, tenant),
        )

    receipts: dict[str, core.CodeReceipt] = {}
    reasons_unclassified: dict[str, int] = {}
    for code in rules.CODES:
        accounts = _account_ids(db, tenant, code)
        _legacy_account_check(legacy, accounts, code)
        lines = _legacy_lines(legacy, accounts, capture, cutoff)
        journal_ids = sorted({ln.journal for ln in lines})
        v2, v2_gl = _v2_journals(db, tenant, code)
        if code == "2040" and fragment is None and any(j.kind == "claim_reclass" for j in v2):
            refuse("s8_fragment_required")  # the S8 reclass journal cannot be attributed without its fragment
        owners = _owners(legacy, code, journal_ids, capture)
        zero = frozenset()
        k2_pairs = 0
        if code in rules.K2_CODES:
            zero, k2_pairs = _zero_units(legacy, lines, capture)
        held: list[core.HeldRow] = []
        if code == "2000":
            ids = sorted(
                {k.removeprefix("invoice:") for _c, k, *_r in itemised}
                | {k.removeprefix("invoice:") for _c, k, *_r in admission}
                | {k.removeprefix("invoice:") for k, _r in unclassified}
            )
            held, reasons_unclassified = core.held_2000(
                itemised=itemised,
                admission=admission,
                unclassified=unclassified,
                invoices=_invoice_facts(legacy, ids, capture, functional),
            )
        elif code == "2040":
            _party_kinds_2040(db, tenant)
            held = list(held_2040)
        sub_keys = _owner_sub_keys(db, tenant, code, functional)
        receipts[code] = core.build_code_receipt(
            core.CodeInputs(code, functional, tuple(lines), owners, zero, v2_gl, tuple(v2), tuple(held), fragment)
        )
        if code in rules.K2_CODES:
            receipts[code].summary["k2_zero_units"] = k2_pairs
        receipt_parties = {p["key"] for p in receipts[code].body["parties"]}
        receipts[code].summary["owner_sub_parties"] = len(sub_keys)
        receipts[code].summary["party_overlap"] = len(receipt_parties & sub_keys)  # S11: the keys must meet
    _check_opening(db, tenant, cutoff)
    if positions.v2_posted_sha(db, tenant) != v2_sha:
        refuse("moved_during_attribution")  # a tripwire, see the note above
    manifest_sources = {
        **{k: shas.get(k) for k in ("legacy_snapshot", "cutoff_receipt", "p_attr_receipt")},
        **{
            name: sources.sha(name)
            for name in ("import_itemised", "unpaid_plan", "settled_plan", "s8_receipt", "s8_fragment")
        },
    }
    manifest = core.build_manifest(
        tenant_scope=hashlib.sha256(str(tenant).encode()).hexdigest(),
        capture_date=capture.isoformat(),
        sources=manifest_sources,
        v2_posted_sha256=v2_sha,
        receipts=receipts,
        reasons_unclassified=reasons_unclassified,
        extra={"claims_fragment_unkeyed": fragment_unkeyed, "declared_absent": sources.declared_absent},
    )
    return {"receipts": receipts, "manifest": manifest, "currency": functional}


def _write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def write_outputs(out: dict[str, Any], out_dir: Path) -> dict[str, str]:
    shas: dict[str, str] = {}
    for code, r in out["receipts"].items():
        _write(out_dir / f"gl-attribution-{code}-{out['currency']}.json", r.raw)
        shas[code] = r.sha256
    raw = core.dumps(out["manifest"])
    _write(out_dir / "gl-attribution-manifest.json", raw)
    shas["manifest"] = sha256_bytes(raw)
    return shas


def _bound(path: Path | None, sha: str | None, name: str) -> bytes | None:
    if path is None:
        if sha is not None:
            refuse(f"{name}_path_required")
        return None
    if sha is None:
        refuse(f"{name}_sha256_required")
    data, _ = cli.read_bound(path, sha_text(sha, f"{name}_sha256_invalid"))
    return data


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="erp_v2.migration_ledger.attribution_cli")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--capture-date", required=True)
    p.add_argument("--snapshot-manifest", type=Path, required=True)
    p.add_argument("--snapshot-sha256", required=True)
    p.add_argument("--cutoff-receipt", type=Path, required=True)
    p.add_argument("--cutoff-receipt-sha256", required=True)
    p.add_argument("--p-attr-receipt", type=Path, required=True)
    p.add_argument("--p-attr-receipt-sha256", required=True)
    for name in ("import-itemised", "unpaid-plan", "settled-plan", "s8-receipt", "s8-fragment"):
        p.add_argument(f"--{name}", type=Path)
        p.add_argument(f"--{name}-sha256")
    p.add_argument(
        "--absent", action="append", default=[], choices=SOURCE_NAMES,
        help="declare a source absent (repeatable); every source is otherwise required",
    )  # fmt: skip
    p.add_argument("--out-dir", type=Path, required=True)
    return p


def run(args: argparse.Namespace) -> dict[str, str]:
    from erp_v2.importer.snapshot import ManifestError, SnapshotManifest
    from erp_v2.migration_ledger import opening

    tenant = uuid_text(args.tenant_id, "invalid_tenant_id")
    capture = day(args.capture_date, "invalid_capture_date")
    try:
        manifest = SnapshotManifest.load(args.snapshot_manifest)
    except ManifestError:
        return refuse("invalid_snapshot_manifest")
    if manifest.snapshot_sha256 != sha_text(args.snapshot_sha256, "snapshot_sha256_invalid"):
        refuse("snapshot_sha_mismatch")
    if manifest.tenant_extent and str(tenant) not in manifest.tenant_extent:
        refuse("tenant_not_in_snapshot_extent")
    cutoff_raw = _bound(args.cutoff_receipt, args.cutoff_receipt_sha256, "cutoff_receipt")
    p_attr_raw = _bound(args.p_attr_receipt, args.p_attr_receipt_sha256, "p_attr_receipt")
    cutoff_body = strict_json(cutoff_raw or b"", "invalid_import_receipt")
    cutoff = opening.parse_import_receipt(cutoff_body)
    carried = cutoff_body.get("snapshot_sha256") or (cutoff_body.get("bindings") or {}).get("snapshot_sha256")
    if carried is not None and carried != manifest.snapshot_sha256:  # the receipt names a snapshot: it must be ours
        refuse("cutoff_receipt_snapshot_mismatch")
    if cutoff > capture:
        refuse("capture_before_cutoff")
    sources = Sources(
        import_itemised=_bound(args.import_itemised, args.import_itemised_sha256, "import_itemised"),
        unpaid_plan=_bound(args.unpaid_plan, args.unpaid_plan_sha256, "unpaid_plan"),
        settled_plan=_bound(args.settled_plan, args.settled_plan_sha256, "settled_plan"),
        s8_receipt=_bound(args.s8_receipt, args.s8_receipt_sha256, "s8_receipt"),
        s8_fragment=_bound(args.s8_fragment, args.s8_fragment_sha256, "s8_fragment"),
        absent=args.absent,
    )
    if not args.out_dir.is_dir():
        refuse("out_dir_missing")
    shas: dict[str, str | None] = {
        "legacy_snapshot": manifest.snapshot_sha256,
        "cutoff_receipt": sha256_bytes(cutoff_raw or b""),
        "p_attr_receipt": sha256_bytes(p_attr_raw or b""),
    }
    destination = cli.engine()  # MIGRATION_DATABASE_URL, popped; serving DSNs refused
    legacy_engine = cli.legacy_engine()  # LEGACY_DATABASE_URL, popped
    try:
        with legacy_engine.connect() as legacy, legacy.begin():
            legacy.execute(sa.text("SET TRANSACTION READ ONLY"))
            if legacy.scalar(sa.text("SELECT current_database()")) != manifest.source_database:
                refuse("source_database_mismatch")
            with Session(destination) as db:
                db.execute(sa.text(RR_RO))
                if db.scalar(sa.text("SELECT current_database()")) != manifest.destination_database:
                    refuse("destination_database_mismatch")
                if not db.scalar(sa.text("SELECT is_active FROM tenants WHERE id = :t"), {"t": tenant}):
                    refuse("tenant_missing_or_inactive")
                try:
                    out = produce(legacy, db, tenant=tenant, capture=capture, cutoff=cutoff, shas=shas, sources=sources)
                finally:
                    db.rollback()
    finally:
        legacy_engine.dispose()
        destination.dispose()
    return write_outputs(out, args.out_dir)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        shas = run(args)
    except LedgerRefused as exc:
        print(f"gl attribution refused: {exc.code}")
        return 2
    except Exception as exc:
        print(f"gl attribution refused: {type(exc).__name__}")
        return 2
    print(f"manifest {shas.pop('manifest')}")
    for code, sha in sorted(shas.items()):
        print(f"{code} {sha}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
