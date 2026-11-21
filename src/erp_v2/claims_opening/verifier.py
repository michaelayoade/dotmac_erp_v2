"""Trusted, SELECT-only verifier for expenses a2 ``admit_historical_claim_liability`` (S8 section 2).

Modelled on ``legacy_ap.settled_verifier``. From the restored legacy snapshot, read-only, it proves for one claim:

* it is a bucket A claim (APPROVED, net payable known, balance due > 0, claim journal POSTED), functional currency,
  with an employee, no advance adjustment, and an imported destination claim of the same id;
* its reimbursement journal (if any) is POSTED and sums to ``amount_paid``;
* its net over the legacy claim family {2040, 2030, 2110} (claim journal, reimbursement journal and the RECLASS
  ledger lines that cite the claim) sits on exactly ONE family account (the carried account) and equals the residual;
* every journal passes the history cutoff proof: after the cutoff it exists in v2 by its legacy id (legacy owner,
  POSTED); on or before it, it is proven in the opening trial balance and absent from v2.

Amounts are never recomputed. Any doubt refuses with a named reason. The verifier never writes or commits.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import partial
from uuid import UUID

import sqlalchemy as sa
from dotmac_accounting.models import Account
from dotmac_expenses import (
    HistoricalClaimLiabilityCommand,
    HistoricalClaimLiabilityEvidence,
    HistoricalClaimLiabilityVerifier,
    HistoricalClaimSettlement,
)
from sqlalchemy.orm import Session

from erp_v2.claims_opening import extract
from erp_v2.claims_opening.reasons import FAMILY, FUNCTIONAL
from erp_v2.importer.cutoff_evidence import journal_proof
from erp_v2.importer.payables_history import _day, _equal, _fingerprint, _id, _instant, _read
from erp_v2.importer.snapshot import SnapshotManifest

SOURCE_OWNER = "erp.legacy"
IMPORT_OWNER = "legacy-erp"  # the importer's journal owner in v2
RECLASS_TYPE = "RECLASS"
JOURNAL_DOMAIN = "erp.claims-opening.accounting.v1"
FACTS_DOMAIN = "erp.claims-opening.facts.v1"
SETTLEMENT_DOMAIN = "erp.claims-opening.reimbursement.v1"
TWO_PLACES = Decimal("0.01")


class ClaimEvidenceError(ValueError):
    """A named fail-closed evidence failure. ``carried_code`` / ``amount`` describe the legacy GL for the E37 row."""

    def __init__(self, reason: str, carried_code: str | None = None, amount: Decimal | None = None):
        self.reason = reason
        self.carried_code = carried_code
        self.amount = amount
        super().__init__(reason)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ClaimEvidenceError(reason)


def _cutoff_require(ok: bool, reason: str) -> None:
    if not ok:
        raise ClaimEvidenceError("journal_undated" if reason == "journal_ledger_line_undated" else reason)


def _two_places(value: Decimal) -> bool:
    return value == value.quantize(TWO_PLACES)


def _net(lines: list[dict]) -> dict[str, Decimal]:
    """Credit-positive net per family account over ledger lines."""
    out: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for r in lines:
        code = str(r["account_code"])
        if code in FAMILY:
            out[code] += Decimal(r["credit_amount"]) - Decimal(r["debit_amount"])
    return dict(out)


@dataclass(frozen=True)
class ClaimOpeningVerifier(HistoricalClaimLiabilityVerifier):
    source: sa.Connection
    manifest: SnapshotManifest
    history_cutoff: date | None
    operator_ref: str
    source_owner: str = SOURCE_OWNER

    # --- command / owner contract ----------------------------------------------------------------------------
    @property
    def snapshot_sha256(self) -> str:
        return self.manifest.snapshot_sha256

    def evidence_reference(self, claim_id: str) -> str:
        return f"erp.claims-opening:{self.snapshot_sha256[:16]}:{claim_id}"

    def command(self, evidence: HistoricalClaimLiabilityEvidence) -> HistoricalClaimLiabilityCommand:
        return HistoricalClaimLiabilityCommand(
            source_owner=evidence.source_owner,
            source_claim_id=evidence.source_claim_id,
            claim_id=evidence.claim_id,
            source_snapshot_sha256=evidence.source_snapshot_sha256,
            evidence_fingerprint=evidence.fingerprint(),
            evidence_reference=self.evidence_reference(evidence.source_claim_id),
            admitted_by=self.operator_ref,
        )

    def verify(
        self, *, scope, command: HistoricalClaimLiabilityCommand, destination: Session
    ) -> HistoricalClaimLiabilityEvidence:
        _require(command.source_owner == self.source_owner, "command_source_owner")
        _require(command.source_snapshot_sha256 == self.snapshot_sha256, "command_snapshot")
        _require(command.source_claim_id == str(command.claim_id), "command_source_claim")
        evidence = self.evidence(scope=scope, claim_id=command.claim_id, destination=destination)
        _require(command.evidence_fingerprint == evidence.fingerprint(), "command_fingerprint")
        return evidence

    def evidence(self, *, scope, claim_id: UUID, destination: Session) -> HistoricalClaimLiabilityEvidence:
        _require(isinstance(self.source, sa.Connection), "source_connection_required")
        _require(isinstance(destination, Session), "destination_session_required")
        _require(not destination.new and not destination.dirty and not destination.deleted, "destination_dirty_session")
        tenant = getattr(scope, "tenant_id", None)
        _require(isinstance(tenant, UUID) and isinstance(claim_id, UUID), "scope_tenant_required")
        _require(str(tenant) in {_id(v) for v in self.manifest.tenant_extent}, "tenant_extent")
        with destination.no_autoflush:
            return self._evidence(tenant, claim_id, destination)

    # --- the proof ---------------------------------------------------------------------------------------------
    def _account_id(self, db: Session, tenant: UUID, code: str) -> str:
        ids = list(db.scalars(sa.select(Account.id).where(Account.tenant_id == tenant, Account.code == code)))
        _require(len(ids) == 1, f"account_missing:{code}")
        return str(ids[0])

    def _opening_posted(self, dest: sa.Connection, tenant: UUID) -> bool:
        cutoff = self.history_cutoff
        if cutoff is None:
            return False
        for row in _read(dest, "journal_entries", schema="mod_accounting", key="source_owner", value="erp.opening"):
            if (
                _equal(row["tenant_id"], tenant)
                and row["status"] == "POSTED"
                and _day(row["posting_date"], "opening_posting_date") == cutoff
            ):
                return True
        return False

    def _journal(self, tenant: UUID, journal_id: str, dest: sa.Connection) -> tuple[dict, list[dict], dict | None]:
        """(legacy journal, its posted ledger lines, cutoff proof) with the destination presence/absence rule."""
        src = self.source
        rows = _read(src, "journal_entry", key="journal_entry_id", value=journal_id)
        _require(len(rows) == 1, "destination_journal_missing")
        journal = rows[0]
        _require(_equal(journal["organization_id"], tenant), "claim_journal_tenant")
        ledger = _read(src, "posted_ledger_line", key="journal_entry_id", value=journal_id)
        _require(all(_equal(r["organization_id"], tenant) for r in ledger), "claim_journal_tenant")
        proof = journal_proof(
            journal, ledger, self.history_cutoff, _cutoff_require, partial(self._opening_posted, dest, tenant)
        )
        found = _read(dest, "journal_entries", schema="mod_accounting", key="id", value=journal_id)
        if proof is not None:
            _require(not found, "cutoff_journal_present_in_destination")
        else:
            _require(len(found) == 1, "destination_journal_missing")
            _require(
                _equal(found[0]["tenant_id"], tenant)
                and found[0]["status"] == "POSTED"
                and found[0]["source_owner"] == IMPORT_OWNER,
                "destination_journal_missing",
            )
        return journal, ledger, proof

    def _evidence(self, tenant: UUID, claim_id: UUID, db: Session) -> HistoricalClaimLiabilityEvidence:
        src, dest = self.source, db.connection()
        rows = extract.read_claims(src, tenant, str(claim_id))
        _require(len(rows) <= 1, "source_claim_duplicate")
        _require(len(rows) == 1, "source_claim_missing")
        claim = rows[0]
        _require(claim.status == "APPROVED", "owner_refused:historical_claim_not_open")
        if claim.net is None:
            raise ClaimEvidenceError("claim_net_payable_missing")
        _require(claim.employee_id is not None, "claim_without_employee")
        _require(claim.currency == FUNCTIONAL, "claim_currency_unsupported")
        _require(claim.advance == 0, "claim_advance_unproven")
        for value in (claim.net, claim.paid, extract.owed(claim)):
            _require(_two_places(value), "legacy_claim_gl_unattributable")
        residual = claim.net - claim.paid
        _require(residual > 0 and extract.owed(claim) == residual, "owner_refused:historical_claim_not_open")
        _require(extract.journal_posted(src, claim.journal_id), "owner_refused:historical_claim_not_open")

        target = db.execute(
            sa.text(
                "SELECT claimant_party_id, currency_code FROM mod_expenses.expense_claims "
                "WHERE tenant_id = :t AND id = :c"
            ),
            {"t": tenant, "c": claim_id},
        ).one_or_none()
        _require(target is not None, "destination_claim_missing")
        _require(target.currency_code == claim.currency, "destination_claim_missing")
        claimant: UUID = target.claimant_party_id

        # legacy GL for the claim
        journals: dict[str, tuple[dict, list[dict], dict | None]] = {}
        settlements: list[HistoricalClaimSettlement] = []
        nets: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        facts_lines: list[dict] = []

        def add(net: dict[str, Decimal]) -> None:
            for code, value in net.items():
                nets[code] += value

        journals[claim.journal_id] = self._journal(tenant, claim.journal_id, dest)
        add(_net(journals[claim.journal_id][1]))
        posted_at = journals[claim.journal_id][0]["posted_at"]
        _require(posted_at is not None, "journal_undated")

        if claim.reimbursement_journal_id is not None:
            if extract.journal_posted(src, claim.reimbursement_journal_id):
                rid = claim.reimbursement_journal_id
                journals[rid] = self._journal(tenant, rid, dest)
                reimb_net = _net(journals[rid][1])
                add(reimb_net)
                paid_gl = -sum(reimb_net.values(), Decimal(0))
                _require(paid_gl == claim.paid, "settlements_sum_mismatch")
                if claim.paid > 0:
                    settlements.append(
                        HistoricalClaimSettlement(
                            evidence_ref=f"journal:{rid}",
                            amount=claim.paid,
                            evidence_fingerprint=_fingerprint(
                                SETTLEMENT_DOMAIN, dict(journal=rid, amount=format(claim.paid, "f"))
                            ),
                        )
                    )
            else:
                _require(claim.paid == 0, "reimbursement_not_posted")
        else:
            _require(claim.paid == 0, "reimbursement_not_posted")

        cited = [
            r
            for r in _read(src, "posted_ledger_line", key="source_document_id", value=str(claim_id))
            if str(r["source_document_type"]) == RECLASS_TYPE and _equal(r["organization_id"], tenant)
        ]
        by_journal: dict[str, list[dict]] = defaultdict(list)
        for r in cited:
            if _id(r["journal_entry_id"]) not in journals:
                by_journal[_id(r["journal_entry_id"])].append(r)
        for jid, lines in sorted(by_journal.items()):
            journals[jid] = self._journal(tenant, jid, dest)
            add(_net(lines))
        for jid, (_j, ledger, _p) in sorted(journals.items()):
            facts_lines.append(
                dict(
                    journal=jid,
                    lines=sorted(
                        [str(r["account_code"]), format(r["debit_amount"], "f"), format(r["credit_amount"], "f")]
                        for r in ledger
                        if str(r["account_code"]) in FAMILY
                    ),
                )
            )

        nonzero = {c: v for c, v in nets.items() if v != 0}
        if len(nonzero) > 1:
            raise ClaimEvidenceError("claim_carried_split", None, residual)
        if not nonzero:
            raise ClaimEvidenceError("claim_carried_outside_family", None, residual)
        carried, carried_net = next(iter(nonzero.items()))
        if carried_net != residual:
            raise ClaimEvidenceError("legacy_claim_gl_unattributable", carried, carried_net)

        def refuse_with(reason: str):
            raise ClaimEvidenceError(reason, carried, residual)

        liability_ref = self._account_id(db, tenant, "2045")
        carried_ref = self._account_id(db, tenant, carried)
        snapshot16 = self.snapshot_sha256[:16]
        try:
            original = _instant(posted_at, "journal_undated")
        except ValueError:
            refuse_with("journal_undated")
        return HistoricalClaimLiabilityEvidence(
            tenant_id=tenant,
            source_owner=self.source_owner,
            source_claim_id=str(claim_id),
            source_snapshot_sha256=self.snapshot_sha256,
            claim_id=claim_id,
            claimant_party_id=claimant,
            currency_code=claim.currency,
            net_payable_amount=claim.net,
            settled_before_admission=claim.paid,
            residual=residual,
            settlements=tuple(settlements),
            liability_account_ref=liability_ref,
            carried_control_amount=carried_net,
            carried_account_ref=carried_ref,
            carried_evidence_ref=f"legacy-gl:{carried}:{snapshot16}",
            source_journal_ref=f"journal:{claim.journal_id}",
            original_posted_at=original,
            accounting_evidence_fingerprint=_fingerprint(JOURNAL_DOMAIN, dict(journals=facts_lines)),
            facts_fingerprint=_fingerprint(
                FACTS_DOMAIN,
                dict(
                    claim_id=str(claim_id),
                    employee_id=claim.employee_id,
                    status=claim.status,
                    currency=claim.currency,
                    net=format(claim.net, "f"),
                    paid=format(claim.paid, "f"),
                    balance=format(extract.owed(claim), "f"),
                    journal=claim.journal_id,
                    reimbursement_journal=claim.reimbursement_journal_id,
                    cutoff=None if self.history_cutoff is None else self.history_cutoff.isoformat(),
                ),
            ),
        )


def carried_code_of(evidence: HistoricalClaimLiabilityEvidence) -> str:
    """The family code a verified evidence is carried on (from its evidence ref ``legacy-gl:<code>:<sha16>``)."""
    return evidence.carried_evidence_ref.split(":")[1]


__all__ = ["ClaimEvidenceError", "ClaimOpeningVerifier", "carried_code_of"]
