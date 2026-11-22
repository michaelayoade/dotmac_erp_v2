"""Pure core of the GL attribution receipts for 2000, 2040 and 1410 (GL-ATTRIBUTION-RECEIPTS-SPEC).

No database and no file access: ``attribution_cli`` reads the legacy snapshot and the destination ledger and feeds the
rows in. Signs are GL-signed (debit - credit: a liability is negative), amounts are exact 2-dp strings, and an inexact
amount refuses (never rounds). The receipt is the EXACT ``holdings.attribution.parse_gl_attribution`` v1 shape; its
identity is its sha256 (the manifest lists it).

The rule (spec section 0): a legacy journal is the unit. One owner: its net on the code goes to that party. None: an
``unattributed`` row. Two or more: an ``unattributed`` row with ``shared: true`` (never split). K2 (2040): a backfill
reclass and the journal it reclassifies that cancel EXACTLY on the code form a zero unit and stay out of the receipt.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from erp_v2.holdings.attribution import Explained, GlAttribution, Party, UnattributedJournal
from erp_v2.migration_ledger.common import ZERO, LedgerRefused, canonical_sha, money, refuse, sha256_bytes

CENT: Final = Decimal("0.01")


def exact2(value: Decimal, ref: str) -> Decimal:
    """The amount at 2 dp; an amount with more precision refuses (``gl_amount_not_exact``), it is never rounded."""
    if value != value.quantize(CENT):
        refuse(f"gl_amount_not_exact:{ref}")
    return value.quantize(CENT)


def fmt2(value: Decimal) -> str:
    return format(value.quantize(CENT), "f")


# --- K2: backfill reclass zero units (2040) ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class K2Row:
    """One journal R whose correlation id starts with the backfill prefix. ``source`` is the journal S whose id the
    correlation id names (exact equality), or None when no such journal exists."""

    reclass: str
    source: str | None
    reclass_posted: bool
    source_posted: bool


def zero_units(rows: Iterable[K2Row], nets: Mapping[str, Decimal]) -> tuple[frozenset[str], int]:
    """The journal ids in zero units and the number of units that touch the code.

    A pair applies when S is posted, R is posted, R references only S (one correlation id), S is referenced only by R
    and ``net_code(S) + net_code(R) == 0`` exactly. Anything else is NOT paired: both journals take the ordinary
    rule."""
    rows = list(rows)
    referenced = Counter(r.source for r in rows if r.source is not None)
    paired: set[str] = set()
    on_code = 0
    for r in rows:
        if r.source is None or not (r.reclass_posted and r.source_posted):
            continue
        if referenced[r.source] != 1 or r.reclass == r.source:
            continue
        a, b = nets.get(r.reclass, ZERO), nets.get(r.source, ZERO)
        if a + b != 0:
            continue
        if a != 0 or b != 0:
            on_code += 1
        paired.update((r.reclass, r.source))
    return frozenset(paired), on_code


# --- held (explained) rows ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeldRow:
    party: Party
    class_code: str
    reason: str
    legacy_key: str
    amount: Decimal  # GL-signed


# --- the per-code receipt ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegacyLine:
    """One (journal, source_document_type) group of legacy lines on the code up to capture (debit - credit)."""

    journal: str
    source_type: str
    net: Decimal
    net_cut: Decimal  # the part dated on or before the history cutoff (the opening)
    lines: int


@dataclass(frozen=True, slots=True)
class V2Journal:
    journal: str
    kind: str  # opening | legacy | claim_reclass | rounding (the classes a receipt may carry)
    net: Decimal


@dataclass(frozen=True, slots=True)
class CodeInputs:
    code: str
    currency: str
    legacy: tuple[LegacyLine, ...]
    owners: Mapping[str, Mapping[Party, frozenset[str]]]  # journal -> party -> the rules that resolved it
    zero_unit: frozenset[str]
    v2_gl: Decimal
    v2: tuple[V2Journal, ...]
    held: tuple[HeldRow, ...] = ()
    fragment_reclass: Mapping[str, Decimal] | None = None  # S8 reclass_2040 (employee key -> Dr 2040, positive)


@dataclass(frozen=True, slots=True)
class CodeReceipt:
    code: str
    body: dict[str, Any]
    raw: bytes
    sha256: str
    summary: dict[str, Any]
    by_source_type: list[dict[str, Any]]
    flags: list[dict[str, str]] = field(default_factory=list)


def dumps(body: Mapping[str, Any]) -> bytes:
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()


def build_code_receipt(inp: CodeInputs) -> CodeReceipt:
    code = inp.code
    flags: list[dict[str, str]] = []
    # integrity: Σ legacy lines after the cutoff must be what v2 carries as legacy-owner (and sub-kobo) journals.
    journal_net: dict[str, Decimal] = defaultdict(lambda: ZERO)
    legacy_le_cut = ZERO
    legacy_gt_cut = ZERO
    for ln in inp.legacy:
        journal_net[ln.journal] += ln.net
        legacy_le_cut += ln.net_cut
        legacy_gt_cut += ln.net - ln.net_cut
    v2_opening = sum((j.net for j in inp.v2 if j.kind == "opening"), ZERO)
    v2_legacy = sum((j.net for j in inp.v2 if j.kind == "legacy"), ZERO)
    if v2_legacy != legacy_gt_cut:
        refuse(f"legacy_import_differs:{code}")
    opening_residue = exact2(v2_opening - legacy_le_cut, f"opening:{code}")

    parties: dict[Party, Decimal] = defaultdict(lambda: ZERO)
    unattributed: list[UnattributedJournal] = []
    shared_ids: list[str] = []
    outcome: dict[str, str] = {}
    for jid in sorted(journal_net):
        net = journal_net[jid]
        if jid in inp.zero_unit:
            outcome[jid] = "zero_unit"
            continue
        if net == 0:
            outcome[jid] = "zero"
            continue
        amount = exact2(net, jid)
        owners = sorted(inp.owners.get(jid, {}))
        if len(owners) == 1:
            parties[owners[0]] += amount
            outcome[jid] = "one"
        elif not owners:
            unattributed.append(UnattributedJournal(jid, amount, False))
            outcome[jid] = "none"
        else:
            unattributed.append(UnattributedJournal(jid, amount, True))
            shared_ids.append(jid)
            outcome[jid] = "shared"

    for j in sorted((j for j in inp.v2 if j.kind == "rounding"), key=lambda j: j.journal):
        if j.net != 0:
            unattributed.append(UnattributedJournal(j.journal, exact2(j.net, j.journal), False))
    reclass = sorted((j for j in inp.v2 if j.kind == "claim_reclass"), key=lambda j: j.journal)
    if reclass:
        total = sum((j.net for j in reclass), ZERO)
        fragment = inp.fragment_reclass
        if code != "2040":
            refuse(f"native_posting_before_flip:{code}")
        if fragment is None:
            flags.append({"journal_id": "+".join(j.journal for j in reclass), "reason": "fragment_missing"})
        elif len(reclass) != 1:
            flags.append({"journal_id": "+".join(j.journal for j in reclass), "reason": "fragment_ambiguous"})
        elif sum(fragment.values(), ZERO) != total:
            flags.append({"journal_id": reclass[0].journal, "reason": "fragment_mismatch"})
        else:
            for key, amount in fragment.items():
                parties[Party("employee", key)] += exact2(amount, f"fragment:{key}")
            reclass = []
        for j in reclass:
            unattributed.append(UnattributedJournal(j.journal, exact2(j.net, j.journal), False))

    total_attributed = sum(parties.values(), ZERO) + sum((u.amount for u in unattributed), ZERO) + opening_residue
    if total_attributed != inp.v2_gl:
        refuse(f"attribution_not_exact:{code}")

    held_rows = sorted(inp.held, key=lambda h: (h.party, h.class_code, h.legacy_key))
    keys = [(h.class_code, h.legacy_key) for h in held_rows]
    if len(set(keys)) != len(keys):
        refuse("held_source_overlap")
    kind = next(iter(sorted({p.kind for p in parties})), None)
    body = {
        "version": 1,
        "code": code,
        "currency": inp.currency,
        "parties": [{"kind": p.kind, "key": p.key, "gl": fmt2(v)} for p, v in sorted(parties.items())],
        "held": [
            {
                "kind": h.party.kind,
                "key": h.party.key,
                "class_code": h.class_code,
                "reason": h.reason,
                "legacy_key": h.legacy_key,
                "amount": fmt2(exact2(h.amount, h.legacy_key)),
            }
            for h in held_rows
        ],
        "unattributed": [
            {"journal_id": u.journal_id, "amount": fmt2(u.amount), "shared": u.shared}
            for u in sorted(unattributed, key=lambda u: u.journal_id)
        ],
        "opening_residue": fmt2(opening_residue),
    }
    raw = dumps(body)
    summary = {
        "gl": fmt2(inp.v2_gl),
        "parties": len(parties),
        "party_kind": kind,
        "held": len(held_rows),
        "unattributed": len(unattributed),
        "shared": len(shared_ids),
        "opening_residue": fmt2(opening_residue),
        "zero_unit_journals": sum(1 for o in outcome.values() if o == "zero_unit"),
    }
    return CodeReceipt(code, body, raw, sha256_bytes(raw), summary, _by_source_type(inp, outcome), flags)


def _by_source_type(inp: CodeInputs, outcome: Mapping[str, str]) -> list[dict[str, Any]]:
    """The P-ATTR grouping per source_document_type (credit-positive nets), for the S11 reconciliation."""
    groups: dict[str, dict[str, Any]] = {}
    for ln in inp.legacy:
        g = groups.setdefault(
            ln.source_type, {"lines": 0, "journals": set(), "net": ZERO, "by_outcome": Counter(), "by_rule": Counter()}
        )
        g["lines"] += ln.lines
        g["journals"].add(ln.journal)
        g["net"] += ln.net
        g["by_outcome"][outcome.get(ln.journal, "zero")] += 1
        rules = sorted({r for rs in inp.owners.get(ln.journal, {}).values() for r in rs})
        g["by_rule"]["+".join(rules) or "-"] += 1
    return [
        {
            "code": inp.code,
            "source_document_type": t,
            "lines": g["lines"],
            "journals": len(g["journals"]),
            "net_credit_positive": fmt2(-g["net"]),
            "journals_by_outcome": dict(sorted(g["by_outcome"].items())),
            "journals_by_rule": dict(sorted(g["by_rule"].items())),
        }
        for t, g in sorted(groups.items())
    ]


# --- held derivation ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvoiceFact:
    supplier: str
    residual: Decimal  # functional, at capture
    posted: bool


def held_2000(
    *,
    itemised: Iterable[
        tuple[str, str, str]
    ],  # (class, legacy_key 'invoice:<id>', reason) from register.from_import_itemised
    admission: Iterable[tuple[str, str, str, int | None]],  # (class, 'invoice:<id>', reason, plan gl_signed_minor)
    unclassified: Iterable[tuple[str, str]],  # (legacy_key, reason) refused with a reason outside the closed map
    invoices: Mapping[str, InvoiceFact],
) -> tuple[list[HeldRow], dict[str, int]]:
    """Held rows for 2000 and the unclassified reason counts. The class and key are the register adapter's own."""
    itemised, admission, unclassified = list(itemised), list(admission), list(unclassified)
    seen: Counter[str] = Counter()
    for _c, key, *_rest in itemised:
        seen[key] += 1
    for _c, key, *_rest in admission:
        seen[key] += 1
    for key, _r in unclassified:
        seen[key] += 1
    if any(n > 1 for n in seen.values()):
        refuse("held_source_overlap")
    rows: list[HeldRow] = []
    for klass, key, reason in itemised:
        fact = _invoice(invoices, key)
        if not fact.posted:
            continue  # an unposted invoice carries no GL
        rows.append(HeldRow(Party("supplier", fact.supplier), klass, reason, key, -fact.residual))
    for klass, key, reason, plan_minor in admission:
        fact = _invoice(invoices, key)
        if plan_minor is not None and plan_minor != int(-fact.residual * 100):
            refuse("held_residual_differs_from_plan", key)
        rows.append(HeldRow(Party("supplier", fact.supplier), klass, reason, key, -fact.residual))
    counts: Counter[str] = Counter(reason for _key, reason in unclassified)
    return [r for r in rows if r.amount != 0], dict(sorted(counts.items()))


def _invoice(invoices: Mapping[str, InvoiceFact], key: str) -> InvoiceFact:
    fact = invoices.get(key.removeprefix("invoice:"))
    if fact is None:
        refuse("held_invoice_not_in_legacy", key)
    return fact


FRAGMENT_KIND: Final = "claims-2040-fragment"
_HELD_KEYS: Final = {"kind", "key", "class_code", "reason", "legacy_key", "amount"}


def parse_fragment(body: object) -> tuple[dict[str, Decimal], list[HeldRow], int]:
    """The S8 ``claims-2040-fragment`` v1: (reclass_2040 per legacy employee id, held_2040 rows, unkeyed count)."""
    if (
        not isinstance(body, dict)
        or body.get("kind") != FRAGMENT_KIND
        or body.get("version") != 1
        or not isinstance(body.get("reclass_2040"), list)
        or not isinstance(body.get("held_2040"), list)
    ):
        refuse("invalid_claims_fragment")
    per: dict[str, Decimal] = {}
    for row in body["reclass_2040"]:
        if not isinstance(row, dict) or set(row) != {"key", "amount"} or not isinstance(row["key"], str):
            refuse("invalid_claims_fragment")
        if row["key"] in per:
            refuse("invalid_claims_fragment", "duplicate employee")
        per[row["key"]] = money(row["amount"], "invalid_claims_fragment")
    held: list[HeldRow] = []
    for row in body["held_2040"]:
        if not isinstance(row, dict) or set(row) != _HELD_KEYS:
            refuse("invalid_claims_fragment")
        if row["kind"] != "employee" or not isinstance(row["key"], str) or not isinstance(row["legacy_key"], str):
            refuse("invalid_claims_fragment")
        if (
            row["class_code"] != "E37"
            or not row["legacy_key"].startswith("claim:")
            or not isinstance(row["reason"], str)
        ):
            refuse("invalid_claims_fragment")
        held.append(
            HeldRow(
                Party("employee", row["key"]),
                row["class_code"],
                row["reason"],
                row["legacy_key"],
                money(row["amount"], "invalid_claims_fragment"),
            )
        )
    unkeyed = body.get("held_2040_unkeyed", 0)
    if type(unkeyed) is not int or unkeyed < 0:
        refuse("invalid_claims_fragment")
    return per, held, unkeyed


# --- the manifest ------------------------------------------------------------------------------------------------

MANIFEST_KIND: Final = "gl-attribution-manifest"


def build_manifest(
    *,
    tenant_scope: str,
    capture_date: str,
    sources: Mapping[str, str | None],
    v2_posted_sha256: str,
    receipts: Mapping[str, CodeReceipt],
    reasons_unclassified: Mapping[str, int],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``sources`` carries legacy_snapshot / cutoff_receipt / p_attr_receipt / import_itemised / unpaid_plan /
    settled_plan / s8_receipt / s8_fragment shas (None when the source was not supplied)."""
    body: dict[str, Any] = {
        "kind": MANIFEST_KIND,
        "version": 1,
        "tenant_scope": tenant_scope,
        "capture_date": capture_date,
        "legacy_snapshot_sha256": sources["legacy_snapshot"],
        "cutoff_receipt_sha256": sources["cutoff_receipt"],
        "p_attr_receipt_sha256": sources["p_attr_receipt"],
        "v2_posted_sha256": v2_posted_sha256,
        "sources": {
            k: v for k, v in sorted(sources.items()) if k not in ("legacy_snapshot", "cutoff_receipt", "p_attr_receipt")
        },
        "codes": {
            code: {"receipt_sha256": r.sha256, **r.summary, "flags": r.flags} for code, r in sorted(receipts.items())
        },
        "reasons_unclassified": dict(sorted(reasons_unclassified.items())),
        "by_source_type": [row for _c, r in sorted(receipts.items()) for row in r.by_source_type],
    }
    if extra:
        body.update(extra)
    return body


def manifest_digest(body: Mapping[str, Any]) -> str:
    return canonical_sha(dict(body))


def parse_manifest(raw: bytes) -> tuple[dict[str, Any], str]:
    """The manifest as holdings reads it: (body, file sha256). Only the fields the plan binds are checked."""
    from erp_v2.migration_ledger.common import sha_text, strict_json

    body = strict_json(raw, "invalid_attribution_manifest")
    if not isinstance(body, dict) or body.get("kind") != MANIFEST_KIND or body.get("version") != 1:
        refuse("invalid_attribution_manifest")
    sha_text(body.get("v2_posted_sha256"), "invalid_attribution_manifest")
    sha_text(body.get("tenant_scope"), "invalid_attribution_manifest")
    if not isinstance(body.get("capture_date"), str):
        refuse("invalid_attribution_manifest")
    codes = body.get("codes")
    if not isinstance(codes, dict):
        refuse("invalid_attribution_manifest")
    for entry in codes.values():
        if not isinstance(entry, dict):
            refuse("invalid_attribution_manifest")
        sha_text(entry.get("receipt_sha256"), "invalid_attribution_manifest")
    return body, sha256_bytes(raw)


def attribution_view(raw: bytes) -> GlAttribution:  # pragma: no cover - convenience for tests
    from erp_v2.holdings.attribution import parse_gl_attribution

    return parse_gl_attribution(raw)[0]


__all__ = [
    "CodeInputs",
    "CodeReceipt",
    "Explained",
    "HeldRow",
    "InvoiceFact",
    "K2Row",
    "LedgerRefused",
    "LegacyLine",
    "V2Journal",
    "build_code_receipt",
    "build_manifest",
    "held_2000",
    "parse_fragment",
    "parse_manifest",
    "zero_units",
]
