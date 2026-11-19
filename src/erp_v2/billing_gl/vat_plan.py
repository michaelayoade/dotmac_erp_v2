"""Database-bound VAT planning for the mapper: build the journal lines and VAT movements of one billing fact.

The pure arithmetic is ``vat.py``. Everything read here is read-only: the billing document rows and the document
fact (public getter), the ERP-owned structural rows ``erp_ar_line_accounts`` (revenue account per document line,
B-16) and the predecessor's posted journal lines. Nothing is recomputed from metadata: a void is the exact negation
of the predecessor's posted journal and stored movements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.models import PostedLedgerLine
from dotmac_billing import GetInvoiceDocumentFactV1, get_invoice_document_fact
from dotmac_billing.models import BillingDocument
from dotmac_kernel.cache import TenantScope
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp_v2.billing_gl import vat
from erp_v2.billing_gl.models import ORIGIN_NATIVE, VatMovement, VatPosition
from erp_v2.billing_gl.rules import Line, Plan

DOCUMENT_KINDS = frozenset({"invoice_issued", "credit_note_issued"})
_INVOICE_REF = re.compile(r"^document:([0-9a-f-]{36}):issue$")
_CREDIT_REF = re.compile(r"^credit_note:([0-9a-f-]{36})$")
ZERO = Decimal(0)


@dataclass(slots=True)
class Persist:
    """What to write once the journal exists: new positions (by document/treatment) and the movements."""

    new_positions: list[dict]
    moves: list[tuple[UUID, vat.Move]]  # (document id, move)


def _blocked(reason: str) -> Plan:
    return Plan(lines=(), persist=None, block=reason)


def document_id_of(kind: str, source_ref: str) -> UUID | None:
    pattern = _INVOICE_REF if kind == "invoice_issued" else _CREDIT_REF
    found = pattern.match(source_ref)
    try:
        return UUID(found.group(1)) if found else None
    except ValueError:
        return None


# ---------------------------------------------------------------- state load


def load_doc(db: Session, tenant_id: UUID, document_id: UUID) -> vat.DocVat | None:
    positions = list(
        db.scalars(
            select(VatPosition).where(VatPosition.tenant_id == tenant_id, VatPosition.document_id == document_id)
        )
    )
    if not positions:
        return None
    total = Decimal(positions[0].total)
    book = vat.DocVat(
        document_id,
        total,
        Decimal(positions[0].open_initial),
        {
            p.treatment_code: vat.Position(
                p.id,
                p.treatment_code,
                None if p.deferral_account_id is None else str(p.deferral_account_id),
                None if p.current_account_id is None else str(p.current_account_id),
                Decimal(p.deferred),
                ZERO,
            )
            for p in positions
        },
    )
    by_id = {p.id: p.treatment_code for p in positions}
    rows = list(
        db.scalars(
            select(VatMovement)
            .where(VatMovement.tenant_id == tenant_id, VatMovement.position_id.in_(list(by_id)))
            .order_by(VatMovement.created_at, VatMovement.id)
        )
    )
    # Replay the stored movements through the same fold the planner uses; the result is the current state.
    for row in rows:
        move = vat.Move(
            by_id[row.position_id],
            row.kind,
            Decimal(row.deferral_delta),
            Decimal(row.current_delta),
            Decimal(row.open_delta),
            row.carrier,
            row.allocation_ref,
        )
        _fold(book, move)
    return book


def _fold(book: vat.DocVat, move: vat.Move) -> None:
    """Fold one stored movement without re-checking the invariants (they held when it was written)."""
    pos = book.positions[move.treatment]
    pos.remaining += move.deferral_delta
    if move.carrier:
        book.open += move.open_delta
    ref = move.allocation_ref
    if ref and move.kind in ("release", "unrelease", "reverse"):
        book.net_release[ref][move.treatment] = book.net_release[ref].get(move.treatment, ZERO) - move.deferral_delta
        if move.carrier:
            book.settled[ref] -= move.open_delta
    if ref.startswith(vat.CLOSEOUT_PREFIX):
        book.closeouts[ref][move.treatment] = book.closeouts[ref].get(move.treatment, ZERO) - move.deferral_delta


def stored_moves(db: Session, tenant_id: UUID, fact_id: UUID) -> list[tuple[UUID, vat.Move]]:
    """The movements a fact wrote, with the document each belongs to."""
    out = []
    rows = db.execute(
        select(VatMovement, VatPosition)
        .join(
            VatPosition, (VatPosition.tenant_id == VatMovement.tenant_id) & (VatPosition.id == VatMovement.position_id)
        )
        .where(VatMovement.tenant_id == tenant_id, VatMovement.fact_id == fact_id)
        .order_by(VatMovement.created_at, VatMovement.id)
    )
    for movement, position in rows:
        out.append(
            (
                position.document_id,
                vat.Move(
                    position.treatment_code,
                    movement.kind,
                    Decimal(movement.deferral_delta),
                    Decimal(movement.current_delta),
                    Decimal(movement.open_delta),
                    movement.carrier,
                    movement.allocation_ref,
                ),
            )
        )
    return out


# ------------------------------------------------------------ line accounts


def line_accounts(db: Session, tenant_id: UUID, document_id: UUID) -> list[dict]:
    """ERP's structural revenue account per document line (B-16 ``erp_ar_line_accounts``), read-only."""
    if not db.scalar(sa.text("SELECT to_regclass('public.erp_ar_line_accounts') IS NOT NULL")):
        return []
    rows = db.execute(
        sa.text(
            "SELECT line_number, revenue_account_id, pre_tax, tax, treatment_code FROM erp_ar_line_accounts "
            "WHERE tenant_id = :t AND billing_document_id = :d ORDER BY line_number"
        ),
        {"t": tenant_id, "d": document_id},
    )
    return [
        {
            "line_number": r.line_number,
            "account": str(r.revenue_account_id),
            "pre_tax": Decimal(r.pre_tax),
            "tax": Decimal(r.tax),
            "treatment": r.treatment_code or "",
        }
        for r in rows
    ]


def _check_lines(db, scope, tenant_id, document: BillingDocument, rows: list[dict]) -> str | None:
    """Σ line_accounts must equal the document fact exactly, line by line. No rounding absorption."""
    if not rows:
        return "revenue_account_unmapped"
    try:
        fact = get_invoice_document_fact(db, scope=scope, request=GetInvoiceDocumentFactV1(scope, document.id, 1)).fact
    except Exception:  # a billing read refusal blocks and retries
        return "invoice_document_unresolved"
    if len(fact.lines) != len(rows):
        return "line_accounts_mismatch"
    for line, row in zip(sorted(fact.lines, key=lambda x: x.line_number), rows, strict=True):
        if (
            line.line_number != row["line_number"]
            or line.pre_tax_amount.amount != row["pre_tax"]
            or line.tax_amount.amount != row["tax"]
        ):
            return "line_accounts_mismatch"
    if sum((r["pre_tax"] for r in rows), ZERO) != fact.subtotal.amount:
        return "line_accounts_mismatch"
    if sum((r["tax"] for r in rows), ZERO) != fact.tax_total.amount:
        return "line_accounts_mismatch"
    return None


def _taxes(rows: list[dict], tax_accounts: dict[str, dict[str, str]]):
    """treatment -> (tax, deferral account, current account); or a block reason."""
    out: dict[str, tuple[Decimal, str, str]] = {}
    for row in rows:
        if row["tax"] == 0:
            continue
        mapped = tax_accounts.get(row["treatment"])
        if not mapped or not mapped.get("current") or not mapped.get("deferral"):
            return "tax_account_unmapped"
        tax, _, _ = out.get(row["treatment"], (ZERO, "", ""))
        out[row["treatment"]] = (tax + row["tax"], mapped["deferral"], mapped["current"])
    return out


# ------------------------------------------------------------------- planners


def _document(db: Session, tenant_id: UUID, document_id: UUID) -> BillingDocument | None:
    return db.scalar(
        select(BillingDocument).where(BillingDocument.tenant_id == tenant_id, BillingDocument.id == document_id)
    )


def issue_plan(db, scope: TenantScope, view, receivable: str, tax_accounts) -> Plan:
    document_id = document_id_of("invoice_issued", view.source_ref)
    document = None if document_id is None else _document(db, scope.tenant_id, document_id)
    if document is None or document.billing_account_id != view.billing_account_id:
        return _blocked("invoice_document_unresolved")
    total = sum((e.delta for e in view.effects if e.lane == "receivable"), ZERO)
    if document.grand_total != total:
        return _blocked("line_accounts_mismatch")
    rows = line_accounts(db, scope.tenant_id, document_id)
    reason = _check_lines(db, scope, scope.tenant_id, document, rows)
    if reason:
        return _blocked(reason)
    taxes = _taxes(rows, tax_accounts)
    if isinstance(taxes, str):
        return _blocked(taxes)
    doc, moves = vat.issue(document_id, total, taxes)
    lines = [Line(receivable, total, ZERO, "invoice receivable")]
    lines += [Line(r["account"], ZERO, r["pre_tax"], f"revenue line {r['line_number']}") for r in rows if r["pre_tax"]]
    for account, amount in vat.lines(moves, doc):
        lines.append(
            Line(account, ZERO, amount, "output VAT deferred") if amount > 0 else Line(account, -amount, ZERO, "VAT")
        )
    persist = Persist(
        [
            {
                "document_id": document_id,
                "treatment": t,
                "deferral": taxes[t][1],
                "current": taxes[t][2],
                "total": total,
                "deferred": taxes[t][0],
            }
            for t in sorted(taxes)
        ],
        [(document_id, m) for m in moves],
    )
    return Plan(tuple(lines), persist)


def credit_plan(db, scope: TenantScope, view, receivable: str, tax_accounts) -> Plan:
    document_id = document_id_of("credit_note_issued", view.source_ref)
    document = None if document_id is None else _document(db, scope.tenant_id, document_id)
    if (
        document is None
        or document.billing_account_id != view.billing_account_id
        or document.credits_document_id is None
    ):
        return _blocked("invoice_document_unresolved")
    total = -sum((e.delta for e in view.effects if e.lane == "receivable"), ZERO)
    if document.grand_total != total:
        return _blocked("line_accounts_mismatch")
    rows = line_accounts(db, scope.tenant_id, document_id)
    reason = _check_lines(db, scope, scope.tenant_id, document, rows)
    if reason:
        return _blocked(reason)
    taxes = {}
    for row in rows:
        if row["tax"]:
            taxes[row["treatment"]] = taxes.get(row["treatment"], ZERO) + row["tax"]
    original = load_doc(db, scope.tenant_id, document.credits_document_id)
    lines = [
        Line(r["account"], r["pre_tax"], ZERO, f"revenue reversed line {r['line_number']}")
        for r in rows
        if r["pre_tax"]
    ]
    persist = Persist([], [])
    if original is None:
        origin = _document(db, scope.tenant_id, document.credits_document_id)
        if taxes or origin is None or origin.authoring_mode == "imported_open_item":
            return _blocked("vat_position_missing")
    else:
        try:
            moves = vat.credit(original, taxes, total, f"{vat.CLOSEOUT_PREFIX}{view.fact_id}", view.minor_units)
            vat.apply(original, moves)
            nets = vat.lines(moves, original)
        except vat.VatRefused as exc:
            return _blocked(exc.code)
        for account, amount in nets:
            lines.append(
                Line(account, ZERO, amount, "VAT") if amount > 0 else Line(account, -amount, ZERO, "VAT credited")
            )
        persist.moves = [(original.document_id, m) for m in moves]
    lines.append(Line(receivable, ZERO, total, "credit note receivable"))
    return Plan(tuple(lines), persist)


def allocation_plan(db, scope: TenantScope, view) -> Plan:
    """VAT movements for every document the fact settles (allocation, deallocation, reallocation, advance draw)."""
    docs: dict[UUID, vat.DocVat | None] = {}
    all_moves: list[tuple[UUID, vat.Move]] = []
    rows = [r for r in view.alloc_rows if r.document_id is not None]
    # deallocations first: they reopen documents that a paired allocation in the same fact may then settle.
    for row in sorted(rows, key=lambda r: (r.delta > 0, str(r.id))):
        doc_id = row.document_id
        if doc_id not in docs:
            docs[doc_id] = load_doc(db, scope.tenant_id, doc_id)
        doc = docs[doc_id]
        if doc is None:
            document = _document(db, scope.tenant_id, doc_id)
            if document is None:
                return _blocked("invoice_document_unresolved")
            if document.authoring_mode == "imported_open_item" or document.tax_total != 0:
                return _blocked("vat_position_missing")
            continue
        try:
            if row.delta > 0:
                if row.id is None:
                    return _blocked("vat_allocation_unresolved")
                moves = vat.release(doc, row.delta, str(row.id), view.minor_units)
            else:
                if row.offsets_id is None:
                    return _blocked("vat_allocation_unresolved")
                moves = vat.unrelease(doc, -row.delta, str(row.offsets_id), view.minor_units)
            vat.apply(doc, moves)
        except vat.VatRefused as exc:
            return _blocked(exc.code)
        all_moves += [(doc_id, m) for m in moves]
    return Plan(_vat_lines(all_moves, docs), Persist([], all_moves))


def _vat_lines(all_moves: list[tuple[UUID, vat.Move]], docs: dict[UUID, vat.DocVat | None]) -> tuple[Line, ...]:
    net: dict[str, Decimal] = {}
    for doc_id, move in all_moves:
        doc = docs[doc_id]
        for account, amount in vat.lines((move,), doc):
            net[account] = net.get(account, ZERO) + amount
    out = []
    for account, amount in sorted(net.items()):
        if amount > 0:
            out.append(Line(account, ZERO, amount, "output VAT released"))
        elif amount < 0:
            out.append(Line(account, -amount, ZERO, "output VAT"))
    return tuple(out)


def reversal_plan(db, scope: TenantScope, view, predecessor_kind: str | None, predecessor_journal: UUID | None) -> Plan:
    """A void or reversal: the exact negation of the predecessor's movements and, for a document, of its journal."""
    pred_moves = stored_moves(db, scope.tenant_id, view.reverses_fact_id)
    mirrored = [
        (doc, m) for doc, m in zip((d for d, _ in pred_moves), vat.negate(tuple(m for _, m in pred_moves)), strict=True)
    ]
    docs: dict[UUID, vat.DocVat | None] = {}
    try:
        for doc_id, _ in mirrored:
            if doc_id not in docs:
                docs[doc_id] = load_doc(db, scope.tenant_id, doc_id)
        for doc_id, move in mirrored:
            vat.apply(docs[doc_id], (move,))
    except vat.VatRefused as exc:
        return _blocked(exc.code)
    persist = Persist([], mirrored)
    if predecessor_kind in DOCUMENT_KINDS:
        if predecessor_journal is None:
            return _blocked("waiting_predecessor")
        ledger = db.execute(
            select(PostedLedgerLine.account_id, PostedLedgerLine.debit, PostedLedgerLine.credit)
            .where(PostedLedgerLine.tenant_id == scope.tenant_id, PostedLedgerLine.journal_id == predecessor_journal)
            .order_by(PostedLedgerLine.journal_line_id)
        ).all()
        # Exact negation of what was posted: swap each line's side. Never recomputed.
        return Plan(
            tuple(Line(str(a), Decimal(c), Decimal(d), "void of the posted line") for a, d, c in ledger),
            persist,
        )
    return Plan(_vat_lines(mirrored, docs), persist) if mirrored else Plan((), persist)


# ----------------------------------------------------------------- persistence


def persist(db: Session, tenant_id: UUID, fact_id: UUID, fact_version: int, journal_id: UUID, spec: Persist) -> None:
    """Write the positions and movements of a posted fact. A replay hits the unique key and is a no-op."""
    for new in spec.new_positions:
        existing = db.scalar(
            select(VatPosition.id).where(
                VatPosition.tenant_id == tenant_id,
                VatPosition.document_id == new["document_id"],
                VatPosition.treatment_code == new["treatment"],
            )
        )
        if existing is None:
            db.add(
                VatPosition(
                    tenant_id=tenant_id,
                    document_id=new["document_id"],
                    treatment_code=new["treatment"],
                    deferral_account_id=UUID(new["deferral"]),
                    current_account_id=UUID(new["current"]),
                    origin=ORIGIN_NATIVE,
                    total=new["total"],
                    deferred=new["deferred"],
                    open_initial=new["total"],
                    source=str(fact_id),
                )
            )
    db.flush()
    for doc_id, move in spec.moves:
        position = db.scalar(
            select(VatPosition.id).where(
                VatPosition.tenant_id == tenant_id,
                VatPosition.document_id == doc_id,
                VatPosition.treatment_code == move.treatment,
            )
        )
        found = db.scalar(
            select(VatMovement.id).where(
                VatMovement.tenant_id == tenant_id,
                VatMovement.fact_id == fact_id,
                VatMovement.fact_version == fact_version,
                VatMovement.position_id == position,
                VatMovement.kind == move.kind,
                VatMovement.allocation_ref == move.allocation_ref,
            )
        )
        if found is not None:
            continue
        db.add(
            VatMovement(
                tenant_id=tenant_id,
                position_id=position,
                fact_id=fact_id,
                fact_version=fact_version,
                kind=move.kind,
                deferral_delta=move.deferral_delta,
                current_delta=move.current_delta,
                open_delta=move.open_delta,
                carrier=move.carrier,
                allocation_ref=move.allocation_ref,
                journal_id=journal_id,
            )
        )
        db.flush()  # one row at a time: the database invariant trigger sees every earlier row
