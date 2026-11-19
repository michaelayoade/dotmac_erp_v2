"""The reconciliation bridge (MIGRATION-REVIEWED-CONVERSION s3.3): per account x period, itemised, versioned, signed.

Sign convention: net = debit - credit. Per account x period:

    legacy     = carry + transform(legacy side) + queue            (the partition proof; gap must be 0)
    v2 planned = carry + transform(v2 side) + ias8 + rounding
    difference = v2 planned - legacy = sum(transform delta) + ias8 + rounding - queue - gap

There is NO materiality threshold: any non-zero gap, at any magnitude, makes the version not ``ready`` and so
unsignable. Every legacy journal or document ref appears in exactly one bucket (carry, transform or queue). The
opening period (``OPENING``) is carried by the posted opening journal and its IAS 8 journals, read from the ledger.

A version is the canonical body below, bound by its sha256. Michael signs a version; a signed version is immutable
(also in the database). The T-ROUND-2DP header deltas (all on 2000) are summed per period and become ONE planned
correction per period against the Finance-named rounding account (a required declared input).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.migration_ledger import models as m
from erp_v2.migration_ledger.common import (
    OPENING_PERIOD,
    PERIOD_RE,
    ZERO,
    canonical_sha,
    fmt,
    keys_exactly,
    money,
    refuse,
    sha_text,
    text,
)

BUCKETS: Final = ("carry", "transform", "queue")
ROUND_RULE: Final = "T-ROUND-2DP"
AP_CONTROL: Final = "2000"
SCOPES: Final = ("header", "line")
MAX_ITEMS: Final = 500_000


@dataclass(frozen=True)
class Item:
    item_id: str
    bucket: str
    ref: str
    account: str
    period: str
    rule: str | None
    scope: str | None
    legacy: Decimal
    v2: Decimal
    evidence_sha256: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "item_id": self.item_id,
            "bucket": self.bucket,
            "ref": self.ref,
            "account": self.account,
            "period": self.period,
            "rule": self.rule,
            "scope": self.scope,
            "legacy_amount": fmt(self.legacy),
            "v2_amount": fmt(self.v2),
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True)
class Inputs:
    snapshot_sha256: str
    rounding_account_code: str | None
    universe: frozenset[str]
    legacy: dict[tuple[str, str], Decimal]
    items: tuple[Item, ...]
    inputs_sha256: str


def _period(value: object, code: str) -> str:
    if not isinstance(value, str) or not (value == OPENING_PERIOD or PERIOD_RE.match(value)):
        refuse(code)
    return value


def parse_inputs(raw: object, raw_sha: str) -> Inputs:
    """The bridge input file (version 1), every amount a string in net (debit - credit) terms."""
    body = keys_exactly(
        raw,
        {"version", "snapshot_sha256", "rounding_account_code", "universe_refs", "legacy", "items"},
        "invalid_bridge_inputs_schema",
    )
    if body["version"] != 1 or not isinstance(body["items"], list) or len(body["items"]) > MAX_ITEMS:
        refuse("invalid_bridge_inputs_schema")
    rounding = body["rounding_account_code"]
    if rounding is not None:
        rounding = text(rounding, "invalid_rounding_account", 30)
    if not isinstance(body["universe_refs"], list) or not isinstance(body["legacy"], list):
        refuse("invalid_bridge_inputs_schema")
    refs = [text(r, "invalid_universe_ref", 255) for r in body["universe_refs"]]
    if len(set(refs)) != len(refs):
        refuse("universe_duplicate_ref")
    legacy: dict[tuple[str, str], Decimal] = {}
    for row in body["legacy"]:
        entry = keys_exactly(row, {"account", "period", "net"}, "invalid_legacy_row")
        key = (text(entry["account"], "invalid_legacy_row", 30), _period(entry["period"], "invalid_legacy_row"))
        if key in legacy:
            refuse("legacy_duplicate_row")
        legacy[key] = money(entry["net"], "invalid_legacy_row")
    seen: set[str] = set()
    items: list[Item] = []
    keys = {
        "item_id",
        "bucket",
        "ref",
        "account",
        "period",
        "rule",
        "scope",
        "legacy_amount",
        "v2_amount",
        "evidence_sha256",
    }
    for row in body["items"]:
        e = keys_exactly(row, keys, "invalid_bridge_item")
        item_id = text(e["item_id"], "invalid_bridge_item", 255)
        if item_id in seen:
            refuse("bridge_duplicate_item")
        seen.add(item_id)
        bucket = e["bucket"]
        if bucket not in BUCKETS:
            refuse("invalid_bridge_item")
        period = _period(e["period"], "invalid_bridge_item")
        if period == OPENING_PERIOD:
            refuse("opening_period_is_ledger_carried", "OPENING items come from the posted journals, not the file")
        legacy_amount, v2_amount = (
            money(e["legacy_amount"], "invalid_bridge_item"),
            money(e["v2_amount"], "invalid_bridge_item"),
        )
        rule = None if e["rule"] is None else text(e["rule"], "invalid_bridge_item", 60)
        scope = e["scope"]
        if bucket == "carry" and (legacy_amount != v2_amount or rule is not None):
            refuse("carry_item_differs")
        if bucket == "queue" and v2_amount != 0:
            refuse("queue_item_posted")
        if bucket == "transform" and (rule is None or scope not in SCOPES):
            refuse("transform_item_needs_rule_and_scope")
        if bucket != "transform" and scope is not None:
            refuse("invalid_bridge_item")
        account = text(e["account"], "invalid_bridge_item", 30)
        if rule == ROUND_RULE and scope == "header" and account != AP_CONTROL:
            refuse("rounding_header_not_ap_control")
        items.append(
            Item(
                item_id,
                bucket,
                text(e["ref"], "invalid_bridge_item", 255),
                account,
                period,
                rule,
                scope,
                legacy_amount,
                v2_amount,
                sha_text(e["evidence_sha256"], "invalid_bridge_item"),
            )
        )
    return Inputs(
        sha_text(body["snapshot_sha256"], "invalid_snapshot_sha"),
        rounding,
        frozenset(refs),
        legacy,
        tuple(sorted(items, key=lambda i: i.item_id)),
        raw_sha,
    )


def partition_proof(inputs: Inputs) -> dict[str, Any]:
    """Every legacy ref in exactly one bucket, and the universe equals the items' refs (no ref missing or extra)."""
    buckets_of: dict[str, set[str]] = defaultdict(set)
    counts = {b: 0 for b in BUCKETS}
    for item in inputs.items:
        buckets_of[item.ref].add(item.bucket)
        counts[item.bucket] += 1
    refs = set(buckets_of)
    multi = sorted(r for r, b in buckets_of.items() if len(b) > 1)
    ref_counts = {b: sum(1 for r, bs in buckets_of.items() if bs == {b}) for b in BUCKETS}
    return {
        "universe_refs": len(inputs.universe),
        "bridged_refs": len(refs),
        "refs_missing_from_bridge": len(inputs.universe - refs),
        "refs_not_in_universe": len(refs - inputs.universe),
        "refs_in_multiple_buckets": len(multi),
        "refs_by_bucket": ref_counts,
        "items_by_bucket": counts,
        "ok": refs == set(inputs.universe) and not multi,
    }


def rounding_residue(inputs: Inputs) -> dict[str, Decimal]:
    """Per period: sum of T-ROUND-2DP header deltas (v2 - legacy, net) on 2000. Non-zero entries only."""
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for i in inputs.items:
        if i.bucket == "transform" and i.rule == ROUND_RULE and i.scope == "header":
            out[i.period] += i.v2 - i.legacy
    return {p: v for p, v in sorted(out.items()) if v != 0}


def compute(
    inputs: Inputs,
    *,
    opening_net: dict[str, Decimal] | None,
    ias8: list[tuple[str, Decimal]],
    opening_tb_sha: str | None,
    ias8_sha: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The versioned body and the itemised (private) file. Pure.

    ``opening_net`` is the posted opening journal's ledger net per account (the OPENING carry), ``ias8`` the posted
    IAS 8 journals' (account, movement) pairs; both are read from the ledger by the caller.
    """
    residue = rounding_residue(inputs)
    if residue and not inputs.rounding_account_code:
        refuse("rounding_account_required", "Finance must name the rounding account")
    if inputs.rounding_account_code in {AP_CONTROL, "3100"}:
        refuse("rounding_account_invalid")
    keys = set(inputs.legacy)
    cols: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(
        lambda: {k: ZERO for k in ("carry", "t_legacy", "t_v2", "queue", "ias8", "rounding")}
    )
    for item in inputs.items:
        c = cols[(item.account, item.period)]
        if item.bucket == "carry":
            c["carry"] += item.legacy
        elif item.bucket == "transform":
            c["t_legacy"] += item.legacy
            c["t_v2"] += item.v2
        else:
            c["queue"] += item.legacy
    for code, net in (opening_net or {}).items():
        cols[(code, OPENING_PERIOD)]["carry"] += net
    for code, move in ias8:
        cols[(code, OPENING_PERIOD)]["ias8"] += move
    for period, value in residue.items():
        cols[(AP_CONTROL, period)]["rounding"] += -value
        cols[(inputs.rounding_account_code, period)]["rounding"] += value  # type: ignore[index]
    keys |= set(cols)
    rows = []
    gap_total = queue_total = ZERO
    for account, period in sorted(keys):
        c = cols.get((account, period)) or {k: ZERO for k in ("carry", "t_legacy", "t_v2", "queue", "ias8", "rounding")}
        legacy = inputs.legacy.get((account, period), ZERO)
        gap = legacy - (c["carry"] + c["t_legacy"] + c["queue"])
        planned = c["carry"] + c["t_v2"] + c["ias8"] + c["rounding"]
        gap_total += abs(gap)
        queue_total += abs(c["queue"])
        rows.append(
            {
                "account": account,
                "period": period,
                "legacy": fmt(legacy),
                "carry": fmt(c["carry"]),
                "transform_legacy": fmt(c["t_legacy"]),
                "transform_v2": fmt(c["t_v2"]),
                "transform_delta": fmt(c["t_v2"] - c["t_legacy"]),
                "queue": fmt(c["queue"]),
                "ias8": fmt(c["ias8"]),
                "rounding": fmt(c["rounding"]),
                "v2_planned": fmt(planned),
                "difference": fmt(planned - legacy),
                "partition_gap": fmt(gap),
            }
        )
    proof = partition_proof(inputs)
    itemised = {
        "items": [i.as_dict() for i in inputs.items],
        "opening_carry": {k: fmt(v) for k, v in sorted((opening_net or {}).items())},
        "ias8": [[a, fmt(v)] for a, v in sorted(ias8)],
    }
    itemised_sha = canonical_sha(itemised)
    ready = proof["ok"] and gap_total == 0 and queue_total == 0
    body = {
        "version": 1,
        "binding": {
            "snapshot_sha256": inputs.snapshot_sha256,
            "inputs_sha256": inputs.inputs_sha256,
            "opening_tb_sha256": opening_tb_sha,
            "ias8_sha256": ias8_sha,
        },
        "rounding_account_code": inputs.rounding_account_code,
        "rounding_residue_by_period": {p: fmt(v) for p, v in residue.items()},
        "rows": rows,
        "partition": proof,
        "partition_gap_abs_total": fmt(gap_total),
        "queue_abs_total": fmt(queue_total),
        "itemised_sha256": itemised_sha,
        "ready": ready,
    }
    return body, itemised


def body_sha256(body: dict[str, Any]) -> str:
    return canonical_sha(body)


# --- database --------------------------------------------------------------------------------------------------


def latest(db: Session, tenant: UUID) -> m.BridgeVersion | None:
    return db.scalar(
        sa.select(m.BridgeVersion)
        .where(m.BridgeVersion.tenant_id == tenant)
        .order_by(m.BridgeVersion.version.desc())
        .limit(1)
    )


def by_sha(db: Session, tenant: UUID, sha: str) -> m.BridgeVersion | None:
    return db.scalar(
        sa.select(m.BridgeVersion).where(m.BridgeVersion.tenant_id == tenant, m.BridgeVersion.sha256 == sha)
    )


def verified_body(row: m.BridgeVersion) -> dict[str, Any]:
    """The stored body, re-hashed: a tampered body refuses."""
    if body_sha256(row.body) != row.sha256:
        refuse("bridge_body_tampered")
    return row.body


def ledger_inputs(
    db: Session, tenant: UUID
) -> tuple[dict[str, Decimal] | None, list[tuple[str, Decimal]], str | None, str | None]:
    """The posted opening journal's net per account, the posted IAS 8 movements and their source shas."""
    from erp_v2.migration_ledger import opening

    rows = list(db.scalars(sa.select(m.OpeningJournal).where(m.OpeningJournal.tenant_id == tenant)))
    op = next((r for r in rows if r.kind == m.KIND_OPENING), None)
    opening_net = opening.ledger_net_by_account(db, tenant, op.journal_id) if op else None
    ias8 = [(r.account_code, r.amount) for r in rows if r.kind == m.KIND_IAS8 and r.account_code]
    ias8_shas = sorted({r.source_sha256 for r in rows if r.kind == m.KIND_IAS8})
    return (
        opening_net,
        ias8,
        (op.source_sha256 if op else None),
        (ias8_shas[0] if len(ias8_shas) == 1 else canonical_sha(ias8_shas) if ias8_shas else None),
    )


def store_version(db: Session, tenant: UUID, body: dict[str, Any], *, created_by: str) -> tuple[m.BridgeVersion, bool]:
    """Insert the next version (or replay the latest when its sha is identical). Returns (row, created)."""
    sha = body_sha256(body)
    current = latest(db, tenant)
    if current is not None and current.sha256 == sha:
        return current, False
    if by_sha(db, tenant, sha) is not None:
        refuse("bridge_sha_already_recorded", "an older version has this sha; compute a new one")
    row = m.BridgeVersion(
        tenant_id=tenant,
        version=(current.version + 1) if current else 1,
        sha256=sha,
        body=body,
        itemised_sha256=body["itemised_sha256"],
        ready=bool(body["ready"]),
        created_by=created_by,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row, True


def sign_version(
    db: Session, tenant: UUID, *, version: int, expect_sha: str, signed_by: str
) -> tuple[m.BridgeVersion, bool]:
    """Michael signs one version: it must be the latest, bound by sha, ready, and every planned rounding journal
    posted. A repeat by the same signer replays; a signed version never changes."""
    from erp_v2.migration_ledger import rounding_verify

    row = latest(db, tenant)
    if row is None or row.version != version:
        refuse("bridge_version_not_latest")
    if row.sha256 != expect_sha:
        refuse("bridge_sha_mismatch")
    body = verified_body(row)
    if row.signed_at is not None:
        if row.signed_by == signed_by:
            return row, False
        refuse("bridge_already_signed")
    if not row.ready or not body["ready"]:
        refuse("bridge_not_ready")
    pending = rounding_verify.pending_periods(db, tenant, body)
    if pending:
        refuse("rounding_journal_pending", f"{len(pending)} period(s)")
    row.signed_by = signed_by
    row.signed_at = datetime.now(UTC)
    db.flush()
    return row, True
