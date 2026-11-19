# ruff: noqa: E501
"""Control attribution (B14.4): itemise X = GL(code) - subledger(code) exactly by party. Pure; no database.

Signs are net = debit - credit throughout. ``D_P = GL_P - Sub_P`` is each party's difference; X is the sum of all
parties' D_P plus the lines that cannot be attributed. Items:

1. the party's EXPLAINED components (its refused items, at their GL-signed amounts; class and reason come from the
   source receipt);
2. the remainder ``r_P = D_P - sum(explained)`` as ``partygap:<code>:<ccy>:<party_key>`` (class E36a for 1400 and
   prepayment, otherwise E37);
3. lines that cannot be attributed, per journal: ``gljournal:<code>:<ccy>:<journal_id>``;
4. a journal touching two or more parties on one control code is NOT split: ``sharedjournal:<code>:<ccy>:<id>`` (its
   lines are already outside every party's GL);
5. the opening residue ``opening:<code>:<ccy>`` (only when the opening line cannot be fully attributed).

``itemise`` refuses ``holding_unitemised`` unless the items sum to X exactly (X is computed independently from the
ledger and the subledger totals, so a provider that misses a journal cannot pass).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from erp_v2.holdings.errors import refuse
from erp_v2.holdings.models import PREPAYMENT
from erp_v2.migration_ledger.common import canonical_sha, fmt, money, sha_text, strict_json, text

ZERO: Final = Decimal(0)
CLASS_GAP_AR: Final = "E36a"
CLASS_GAP_OTHER: Final = "E37"
LEGACY_KEY_MAX: Final = 255


def gap_class(code: str) -> str:
    return CLASS_GAP_AR if code in ("1400", PREPAYMENT) else CLASS_GAP_OTHER


@dataclass(frozen=True, slots=True, order=True)
class Party:
    kind: str
    key: str


@dataclass(frozen=True, slots=True)
class Explained:
    """A party's refused item, at its GL-signed amount, with the class and reason from the source receipt."""

    class_code: str
    reason: str
    legacy_key: str
    amount: Decimal
    origin_key: str | None = None  # the source register exception's legacy_key (defaults to ``legacy_key``)

    @property
    def source_key(self) -> str:
        return self.origin_key or self.legacy_key


@dataclass(frozen=True, slots=True)
class UnattributedJournal:
    journal_id: str
    amount: Decimal  # GL-signed
    shared: bool = False


@dataclass(frozen=True, slots=True)
class GlAttribution:
    """The per-party GL side for one control code and currency (from a sealed receipt, never typed)."""

    code: str
    currency: str
    gl: Mapping[Party, Decimal]
    explained: Mapping[Party, tuple[Explained, ...]]
    unattributed: tuple[UnattributedJournal, ...]
    opening_residue: Decimal
    source_receipt_sha256: str
    evidence_refs: tuple[str, ...] = ()

    def gl_total(self) -> Decimal:
        return sum(self.gl.values(), ZERO) + sum((u.amount for u in self.unattributed), ZERO) + self.opening_residue


@dataclass(frozen=True, slots=True)
class HoldingItem:
    class_code: str
    legacy_key: str  # the register key (unique per tenant with the class)
    control_code: str
    currency: str
    party: Party | None
    amount: Decimal  # GL-signed difference to move: > 0 = the GL holds more debit than the subledger
    reason: str
    holding_account_code: str
    evidence_refs: tuple[str, ...] = ()
    source_receipt_sha256: str = ""
    origin: tuple[str, str] | None = None  # (class, legacy_key) of the source register exception (L6)

    def detail(self) -> dict[str, Any]:
        return {
            "control_code": self.control_code,
            "currency": self.currency,
            "party": None if self.party is None else {"kind": self.party.kind, "key": self.party.key},
            "amount": fmt(self.amount),
            "reason": self.reason,
            "holding_account_code": self.holding_account_code,
            "legacy_evidence_refs": list(self.evidence_refs),
            "source_receipt_sha256": self.source_receipt_sha256,
            "origin": None if self.origin is None else {"class": self.origin[0], "legacy_key": self.origin[1]},
        }

    def as_plan(self) -> dict[str, Any]:
        return {"class": self.class_code, "key": self.legacy_key, **self.detail()}


def _key(prefix: str, *parts: str) -> str:
    out = ":".join((prefix, *parts))
    if len(out) > LEGACY_KEY_MAX:
        refuse("holding_item_key_too_long", prefix)
    return out


def itemise(
    *,
    attribution: GlAttribution,
    sub: Mapping[Party, Decimal],
    gl_total: Decimal,
    sub_total: Decimal,
    holding_account_code: str,
) -> list[HoldingItem]:
    """Items for one control code x currency. ``gl_total`` / ``sub_total`` come from the ledger and the owner
    subledger directly; the items must sum to ``gl_total - sub_total`` exactly."""
    code, ccy = attribution.code, attribution.currency
    common = dict(
        control_code=code,
        currency=ccy,
        holding_account_code=holding_account_code,
        source_receipt_sha256=attribution.source_receipt_sha256,
        evidence_refs=attribution.evidence_refs,
    )
    items: list[HoldingItem] = []
    for party in sorted(set(attribution.gl) | set(sub) | set(attribution.explained)):
        difference = attribution.gl.get(party, ZERO) - sub.get(party, ZERO)
        explained = tuple(e for e in attribution.explained.get(party, ()) if e.amount != 0)
        remainder = difference - sum((e.amount for e in explained), ZERO)
        for e in explained:
            items.append(
                HoldingItem(
                    class_code=e.class_code,
                    legacy_key=_key("held", code, ccy, e.legacy_key),
                    party=party,
                    amount=e.amount,
                    reason=e.reason,
                    origin=(e.class_code, e.source_key),
                    **common,
                )
            )
        if remainder != 0:
            items.append(
                HoldingItem(
                    class_code=gap_class(code),
                    legacy_key=_key("partygap", code, ccy, party.key),
                    party=party,
                    amount=remainder,
                    reason="party_gap",
                    **common,
                )
            )
    for u in sorted(attribution.unattributed, key=lambda u: u.journal_id):
        if u.amount == 0:
            continue
        items.append(
            HoldingItem(
                class_code=gap_class(code),
                legacy_key=_key("sharedjournal" if u.shared else "gljournal", code, ccy, u.journal_id),
                party=None,
                amount=u.amount,
                reason="shared_journal" if u.shared else "unattributed_journal",
                **common,
            )
        )
    if attribution.opening_residue != 0:
        items.append(
            HoldingItem(
                class_code=CLASS_GAP_OTHER,
                legacy_key=_key("opening", code, ccy),
                party=None,
                amount=attribution.opening_residue,
                reason="opening_residue",
                **common,
            )
        )
    keys = [(i.class_code, i.legacy_key) for i in items]
    if len(set(keys)) != len(keys):
        refuse("holding_item_duplicate", f"{code}:{ccy}")
    if sum((i.amount for i in items), ZERO) != gl_total - sub_total:
        refuse("holding_unitemised", f"{code}:{ccy}")
    return items


# --- receipts -> GlAttribution -----------------------------------------------------------------------------------


def parse_gl_attribution(raw: bytes) -> tuple[GlAttribution, str]:
    """The generic per-code receipt (AP, payroll, claims, loans). Returns (attribution, receipt sha256).

    ``{"version": 1, "code": "2000", "currency": "NGN",
       "parties": [{"kind": "supplier", "key": "...", "gl": "-10.00"}],
       "held": [{"kind": ..., "key": ..., "class_code": "E02", "reason": "...", "legacy_key": "...", "amount": "..."}],
       "unattributed": [{"journal_id": "...", "amount": "...", "shared": false}],
       "opening_residue": "0.00"}``
    """
    import hashlib

    sha = hashlib.sha256(raw).hexdigest()
    body = strict_json(raw, "invalid_gl_attribution")
    if not isinstance(body, dict) or set(body) != {
        "version", "code", "currency", "parties", "held", "unattributed", "opening_residue"
    } or body["version"] != 1:  # fmt: skip
        refuse("invalid_gl_attribution")
    code = text(body["code"], "invalid_gl_attribution", 30)
    currency = text(body["currency"], "invalid_gl_attribution", 3)
    gl: dict[Party, Decimal] = {}
    for row in _rows(body["parties"], {"kind", "key", "gl"}):
        party = Party(text(row["kind"], "invalid_gl_attribution"), text(row["key"], "invalid_gl_attribution", 240))
        if party in gl:
            refuse("invalid_gl_attribution", "duplicate party")
        gl[party] = money(row["gl"], "invalid_gl_attribution")
    explained: dict[Party, list[Explained]] = defaultdict(list)
    for row in _rows(body["held"], {"kind", "key", "class_code", "reason", "legacy_key", "amount"}):
        party = Party(text(row["kind"], "invalid_gl_attribution"), text(row["key"], "invalid_gl_attribution", 240))
        explained[party].append(
            Explained(
                text(row["class_code"], "invalid_gl_attribution", 20),
                text(row["reason"], "invalid_gl_attribution"),
                text(row["legacy_key"], "invalid_gl_attribution", 200),
                money(row["amount"], "invalid_gl_attribution"),
            )
        )
    unattributed = tuple(
        UnattributedJournal(
            text(row["journal_id"], "invalid_gl_attribution", 120),
            money(row["amount"], "invalid_gl_attribution"),
            _flag(row["shared"]),
        )
        for row in _rows(body["unattributed"], {"journal_id", "amount", "shared"})
    )
    return (
        GlAttribution(
            code,
            currency,
            gl,
            {p: tuple(v) for p, v in explained.items()},
            unattributed,
            money(body["opening_residue"], "invalid_gl_attribution"),
            sha,
        ),
        sha,
    )


def _flag(value: object) -> bool:
    if type(value) is not bool:
        refuse("invalid_gl_attribution")
    return value


def _rows(value: object, keys: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(r, dict) or set(r) != keys for r in value):
        refuse("invalid_gl_attribution")
    return value


# --- the #72 census receipt (B14.4 fields only) -------------------------------------------------------------------

CENSUS_HELD_CLASS: Final = (
    ("partition_held:", "E35"),
    ("ar_unapplied_credit_note", "E35"),
    ("row_after_cutoff", "E35"),
    ("variance", "E36b"),
)
UNAPPLIED_CN: Final = "ar_unapplied_credit_note"


def census_register_key(reason: str, legacy_key: str) -> str:
    """The register legacy_key of the #72 ``ar_census`` exception a held[] row came from."""
    if reason.startswith("partition_held:"):
        return legacy_key  # already ``partition:<cust>:<ccy>``
    if reason == UNAPPLIED_CN:
        return f"{UNAPPLIED_CN}:{legacy_key}"
    if reason == "row_after_cutoff":
        return f"row:{legacy_key}"
    return legacy_key


CENSUS_PARTY_KIND: Final = "customer"
MINOR_EXPONENT: Final = 2  # NGN is the only supported currency (holding_fx_unsupported otherwise)


def _minor(value: object, code: str = "invalid_census") -> Decimal:
    if type(value) is not int:
        refuse(code)
    return Decimal(value).scaleb(-MINOR_EXPONENT)


def census_class(reason: str) -> str:
    for prefix, klass in CENSUS_HELD_CLASS:
        if reason == prefix or reason.startswith(prefix):
            return klass
    return refuse("census_reason_unknown", "add the reason to the class table; never guess")


@dataclass(frozen=True, slots=True)
class CensusParty:
    customer: str
    currency: str
    gl_minor: int
    carried_minor: int
    cash_lot_minor: int


@dataclass(frozen=True, slots=True)
class Census:
    """Exactly the #72 census receipt fields B14.4 names. ``held[].customer`` / ``.currency`` attribute a held item to its
    party (the receipt groups held items per party; the flat list must carry the party)."""

    sha256: str
    parties: tuple[CensusParty, ...]
    held: tuple[dict[str, Any], ...]
    unattributed: tuple[dict[str, Any], ...]
    shared_ar_journal: tuple[dict[str, Any], ...]
    digest: str | None = None  # the receipt's own ``census_sha256`` field (what e2_0029 rows carry), NOT the file sha

    def currencies(self) -> set[str]:
        return {p.currency for p in self.parties}

    def cash_lot_total(self, currency: str) -> int:
        return sum(p.cash_lot_minor for p in self.parties if p.currency == currency)

    def gl_total_minor(self, currency: str, functional: str) -> int:
        """Σ gl_minor + Σ unattributed (unattributed is in the functional currency): the legacy 1400 net."""
        total = sum(p.gl_minor for p in self.parties if p.currency == currency)
        if currency == functional:
            total += sum(int(u["amount_minor"]) for u in self.unattributed)
        return total


def parse_census(body: Mapping[str, Any], sha256: str, digest: str | None = None) -> Census:
    """``body`` is the census receipt's ``gl_attribution`` object."""
    sha_text(sha256, "invalid_census")
    if not isinstance(body, Mapping):
        refuse("invalid_census")
    for key in ("parties", "held", "unattributed", "shared_ar_journal"):
        if not isinstance(body.get(key), list):
            refuse("invalid_census")
    parties = []
    for p in body["parties"]:
        if not isinstance(p, Mapping) or not {
            "customer",
            "currency",
            "gl_minor",
            "carried_minor",
            "cash_lot_minor",
        } <= set(p):
            refuse("invalid_census")
        for k in ("gl_minor", "carried_minor", "cash_lot_minor"):
            if type(p[k]) is not int:
                refuse("invalid_census")
        parties.append(
            CensusParty(str(p["customer"]), str(p["currency"]), p["gl_minor"], p["carried_minor"], p["cash_lot_minor"])
        )
    keys = [(p.customer, p.currency) for p in parties]
    if len(set(keys)) != len(keys):
        refuse("invalid_census", "duplicate party")
    for row in (*body["held"], *body["unattributed"]):
        if not isinstance(row, Mapping) or type(row.get("amount_minor")) is not int:
            refuse("invalid_census")
    for row in body["held"]:
        if not isinstance(row.get("reason"), str) or not isinstance(row.get("legacy_key"), str):
            refuse("invalid_census")
    for row in body["unattributed"]:
        if not isinstance(row.get("gljournal_id"), str):
            refuse("invalid_census")
    return Census(
        sha256, tuple(parties), tuple(dict(h) for h in body["held"]), tuple(dict(u) for u in body["unattributed"]),
        tuple(dict(s) for s in body["shared_ar_journal"]),
        None if digest is None else sha_text(digest, "invalid_census"),
    )  # fmt: skip


def census_attribution(census: Census, *, code: str, currency: str, functional: str) -> GlAttribution:
    """The 1400 or prepayment attribution from the census, AFTER T1 (T1 moves each party's cash-lot residual from
    prepayment to 1400, so 1400 GL = gl_minor + cash_lot_minor and prepayment GL = - cash_lot_minor)."""
    if code not in ("1400", PREPAYMENT):
        refuse("invalid_census", "census attributes 1400 and prepayment only")
    gl: dict[Party, Decimal] = {}
    for p in census.parties:
        if p.currency != currency:
            continue
        party = Party(CENSUS_PARTY_KIND, p.customer)
        gl[party] = _minor(p.gl_minor + p.cash_lot_minor if code == "1400" else -p.cash_lot_minor)
    explained: dict[Party, list[Explained]] = defaultdict(list)
    if code == "1400":
        for h in census.held:
            if "customer" not in h or "currency" not in h:
                refuse("census_held_party_unknown", "held[] must name its customer and currency")
            if str(h["currency"]) != currency:
                continue
            amount = _minor(h["amount_minor"])
            reason = str(h["reason"])
            key = str(h["legacy_key"])
            # Credit notes are keyed on the distinct reason; a legacy_key prefix that disagrees refuses (it never
            # overrides the reason).
            if key.startswith(f"{UNAPPLIED_CN}:") and reason != UNAPPLIED_CN:
                refuse("census_reason_mismatch", "legacy_key prefix disagrees with the reason")
            if reason == "doc_unposted":
                if amount != 0:
                    refuse("invalid_census", "an unposted document carries no GL")
                continue  # H-DOC: a work-queue item only (no GL, no holding)
            explained[Party(CENSUS_PARTY_KIND, str(h["customer"]))].append(
                Explained(census_class(reason), reason, key, amount, census_register_key(reason, key))
            )
    unattributed: tuple[UnattributedJournal, ...] = ()
    if code == "1400" and currency == functional:
        unattributed = tuple(
            UnattributedJournal(str(u["gljournal_id"]), _minor(u["amount_minor"]), bool(u.get("shared", False)))
            for u in census.unattributed
        )
    return GlAttribution(
        code, currency, gl, {p: tuple(v) for p, v in explained.items()}, unattributed, ZERO, census.sha256,
        evidence_refs=(f"census:{census.sha256}",),
    )  # fmt: skip


def plan_digest(body: Any) -> str:
    return canonical_sha(body)


def sum_items(items: Iterable[HoldingItem]) -> Decimal:
    return sum((i.amount for i in items), ZERO)
