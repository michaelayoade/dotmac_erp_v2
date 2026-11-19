"""Operator plan/apply of a sha-bound Finance payee file: bulk bank-instruction proposals (B-11).

About 551 employees and the suppliers need payee bank instructions before the first payroll or payment run.
Each file row becomes ONE B8 ``propose`` (``erp_v2.payees.service``); approval stays in the UI except
onboarding. There is no migration and no new write path: this command only calls the existing governed service.

Finance file (JSON, at most 1500 rows, mode 0600, duplicate keys refused, bound by ``--file-sha256``)::

    {"version": 1, "tenant_id": "<uuid>", "proposer_party_id": "<uuid of the named Finance person>",
     "file_ref": "<text, <=120>",
     "rows": [{"kind": "supplier|employee|statutory", "ref": "<payee ref>", "bank_code": "<CBN code>",
               "account_number": "<NUBAN>", "account_name": "<name on the account>",
               "evidence_ref": "<signed letter / document ref>",
               "callback_ref": "<call-back verification ref>"  <- REQUIRED for supplier rows}]}

Rules (all refuse the whole file; apply writes nothing unless every row is eligible):
* the bank reference (code, name, NUBAN check applicability; see ``bank_codes``) is REQUIRED, sha-bound, and its
  sha256 is recorded in the receipt; no reference, no run;
* unknown payee (employee id, supplier ref, or a statutory ref that does not resolve), unknown bank code,
  invalid NUBAN (format, plus the check digit where the reference marks it applicable) are refused per row;
* ``callback_ref`` (call-back verification) is REQUIRED for supplier rows, statutory rows and any row that changes
  an existing APPROVED instruction. It is stored in the instruction evidence
  (``<evidence_ref> | callback:<callback_ref>``, at most 240 characters): there is no column and no migration;
* the same payee twice in one file is refused (ambiguous);
* ``proposer_party_id`` must be an active party of the tenant holding the B8 propose permission (and the approve
  permission when ``--approve``), else ``proposer_not_authorised``. ``operator_ref`` goes in every audit event;
* outcomes: ``new`` (no live instruction), ``replace`` (an open proposal is rejected and a new one proposed),
  ``change`` (would change an APPROVED instruction), ``name_differs`` (same bank and account as the approved
  one, different account name; counted as a change), ``unchanged`` (identical to the approved one: no-op);
* ``--approve`` approves ONLY onboarding rows (``new`` / ``replace``: payees with no approved instruction yet),
  and only when the tenant dual-control policy is OFF and ``--operator-ref`` is given; with the policy ON it is
  refused. Rows that change an approved instruction are proposed and left for per-row approval in the B8 UI with
  a call-back reference (counted as ``changes_to_approved`` / ``left_for_ui_approval``);
* ``--expect new=N,replace=N,change=N`` must equal the plan counts exactly or apply is refused (exit 3).
  It is REQUIRED with ``--approve`` (``expect_required``, plan and apply); optional for propose-only runs.

    python -m erp_v2.payees.bulk_cli --file F.json --file-sha256 S --bank-reference B.json --bank-reference-sha256 T \
        --receipt R.json \
        [--apply --operator-ref <who>] [--approve] [--expect new=N,replace=N,change=N]

Apply needs ``--apply``, ERP_IMPORT_APPROVED=1 and ERP_IMPORT_APPLY_OK=1. The DSN is environment-only
(MIGRATION_DATABASE_URL). The receipt (0600, created exclusively before any work) holds counts, refusal reasons
and, per row, only the row number, kind, outcome and the LAST FOUR digits of the account. A full account number
is never written to stdout, the receipt, audit details or error text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from erp_v2.payees import service
from erp_v2.payees.bank_codes import BankReference, BankReferenceError, parse_bank_reference
from erp_v2.payees.models import InstructionStatus, PayeeBankInstruction, PayeeKind
from erp_v2.payees.service import PayeeBankError

MAX_ROWS = 1500
EVIDENCE_MAX = 240
_SHA = re.compile(r"[0-9a-f]{64}")
_ROW_KEYS = {"kind", "ref", "bank_code", "account_number", "account_name", "evidence_ref"}
_PAYEE_KINDS = {k.value for k in PayeeKind}


class BulkRefusal(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise BulkRefusal(reason)


def _uuid(value, reason: str) -> UUID:
    try:
        ident = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise BulkRefusal(reason) from exc
    _require(str(ident) == value, reason)
    return ident


def _text(value, reason: str, maximum: int) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()) and value == value.strip() and len(value) <= maximum, reason
    )
    return value


@dataclass(frozen=True, slots=True)
class PayeeRow:
    kind: str
    ref: str
    bank_code: str
    account_number: str = field(repr=False)
    account_name: str
    evidence_ref: str
    callback_ref: str | None

    def __repr__(self) -> str:  # never carry the account number
        return f"PayeeRow({self.kind}, ****{self.account_number[-4:]})"

    @property
    def last4(self) -> str:
        return self.account_number[-4:] if len(self.account_number) >= 4 else ""

    @property
    def evidence(self) -> str:
        return self.evidence_ref if self.callback_ref is None else f"{self.evidence_ref} | callback:{self.callback_ref}"


@dataclass(frozen=True, slots=True)
class PayeeFile:
    tenant_id: UUID
    proposer_party_id: UUID
    file_ref: str
    sha256: str
    rows: tuple[PayeeRow, ...]


def read_private_file(path: Path) -> bytes:
    """Read the Finance file only when it is a regular file of this user with mode 0600 (it holds PII)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BulkRefusal("file_unreadable") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode), "file_not_regular")
        _require(stat.S_IMODE(info.st_mode) == 0o600, "file_mode_not_0600")
        _require(info.st_uid == os.geteuid(), "file_owner_mismatch")
        return stream.read()


def load_bank_reference(path: Path | None, sha256: str | None) -> BankReference:
    try:
        raw = None if path is None else path.read_bytes()
    except OSError as exc:
        raise BulkRefusal("bank_reference_required") from exc
    try:
        return parse_bank_reference(raw, sha256)
    except BankReferenceError as exc:
        raise BulkRefusal(exc.reason) from exc


def _json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except BulkRefusal:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise BulkRefusal("invalid_json_document") from exc
    _require(isinstance(value, dict), "json_object_required")
    return value


def parse_file(raw: bytes, expected_sha256: str) -> PayeeFile:
    """Validate the sha256-bound file STRUCTURE. Row semantics (payee, bank, NUBAN) are classified later."""
    _require(bool(_SHA.fullmatch(expected_sha256 or "")), "file_sha256_required")
    _require(hashlib.sha256(raw).hexdigest() == expected_sha256, "file_digest_mismatch")
    data = _json(raw)
    _require(set(data) == {"version", "tenant_id", "proposer_party_id", "file_ref", "rows"}, "invalid_file_schema")
    _require(type(data["version"]) is int and data["version"] == 1, "invalid_file_version")
    tenant = _uuid(data["tenant_id"], "invalid_tenant_id")
    proposer = _uuid(data["proposer_party_id"], "invalid_proposer_party_id")
    ref = _text(data["file_ref"], "file_ref_required", 120)
    rows = data["rows"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= MAX_ROWS, "explicit_bounded_rows_required")
    parsed = []
    for row in rows:
        _require(isinstance(row, dict), "invalid_row")
        _require(_ROW_KEYS <= set(row) <= _ROW_KEYS | {"callback_ref"}, "invalid_row_schema")
        _require(all(isinstance(row[k], str) for k in set(row)), "invalid_row_schema")
        kind = row["kind"]
        _require(kind in _PAYEE_KINDS, "invalid_row_kind")
        callback = row.get("callback_ref")
        parsed.append(
            PayeeRow(
                kind=kind,
                ref=_text(row["ref"], "invalid_row_ref", 255),
                bank_code=row["bank_code"],
                account_number=row["account_number"],
                account_name=_text(row["account_name"], "invalid_row_account_name", 200),
                evidence_ref=_text(row["evidence_ref"], "invalid_row_evidence_ref", EVIDENCE_MAX),
                callback_ref=None if callback is None else _text(callback, "invalid_row_callback_ref", 120),
            )
        )
    keys = [(r.kind, r.ref) for r in parsed]
    _require(len(set(keys)) == len(keys), "duplicate_payee_in_file")
    return PayeeFile(tenant, proposer, ref, expected_sha256, tuple(parsed))


ONBOARDING_APPROVABLE = ("new", "replace")  # payees with NO approved instruction yet
_OK = ("new", "replace", "change", "name_differs", "unchanged")
_CHANGES = ("change", "name_differs")
_EXPECT_KEYS = ("new", "replace", "change")


def classify_row(db, tenant_id: UUID, row: PayeeRow, banks: BankReference) -> str:
    """One outcome per row, read-only.

    'new' (no live instruction) | 'replace' (an open proposal, no approved one: onboarding) |
    'change' (would change an APPROVED instruction: never auto-approved) | 'name_differs' (same bank and account
    as the approved one, different account name: a change too) | 'unchanged' | a refusal reason.
    """
    import sqlalchemy as sa

    if banks.name(row.bank_code) is None:
        return "unknown_bank_code"
    if not banks.valid_nuban(row.bank_code, row.account_number):
        return "invalid_nuban"
    if len(row.evidence) > EVIDENCE_MAX:
        return "evidence_too_long"
    if service.resolve_payee(db, tenant_id, row.kind, row.ref) is None:
        return "statutory_ref_unresolved" if row.kind == PayeeKind.STATUTORY.value else "unknown_payee"
    live = db.scalars(
        sa.select(PayeeBankInstruction).where(
            PayeeBankInstruction.tenant_id == tenant_id,
            PayeeBankInstruction.payee_kind == row.kind,
            PayeeBankInstruction.payee_ref == row.ref,
            PayeeBankInstruction.status.in_((InstructionStatus.PROPOSED.value, InstructionStatus.APPROVED.value)),
        )
    ).all()
    approved = next((i for i in live if i.status == InstructionStatus.APPROVED.value), None)
    open_proposal = next((i for i in live if i.status == InstructionStatus.PROPOSED.value), None)
    if approved is None:
        outcome = "replace" if open_proposal is not None else "new"
    elif (approved.bank_code, approved.account_number) == (row.bank_code, row.account_number):
        if (approved.account_name or "").strip() == row.account_name.strip():
            return "unchanged"
        outcome = "name_differs"
    else:
        outcome = "change"
    # Call-back verification: suppliers, statutory bodies, and any change to an approved instruction.
    if row.callback_ref is None and (
        row.kind in (PayeeKind.SUPPLIER.value, PayeeKind.STATUTORY.value) or outcome in _CHANGES
    ):
        return "callback_ref_required"
    return outcome


def classify(db, file: PayeeFile, banks: BankReference) -> list[tuple[PayeeRow, str]]:
    return [(row, classify_row(db, file.tenant_id, row, banks)) for row in file.rows]


def parse_expect(text: str | None) -> dict[str, int] | None:
    """``new=N,replace=N,change=N`` (all three, non-negative integers) or a refusal."""
    if text is None:
        return None
    parts = [p.split("=", 1) for p in text.split(",")]
    _require(all(len(p) == 2 for p in parts), "invalid_expect")
    keys = [k for k, _ in parts]
    _require(sorted(keys) == sorted(_EXPECT_KEYS), "invalid_expect")
    _require(all(v.isascii() and v.isdigit() and len(v) <= 6 for _, v in parts), "invalid_expect")
    return {k: int(v) for k, v in parts}


def plan_counts(outcomes) -> dict[str, int]:
    tally = Counter(reason for _row, reason in outcomes)
    return {
        "new": tally["new"],
        "replace": tally["replace"],
        "change": tally["change"] + tally["name_differs"],
    }


def apply_rows(
    db, file: PayeeFile, outcomes, *, banks: BankReference, operator_ref: str | None, approve: bool
) -> Counter:
    """Propose (and, with --approve and the policy off, approve ONBOARDING rows only) in the caller's transaction."""
    import sqlalchemy as sa

    _require(all(reason in _OK for _row, reason in outcomes), "file_not_fully_eligible")
    written: Counter = Counter()
    for row, reason in outcomes:
        if reason == "unchanged":
            continue
        if reason in ("replace", "change", "name_differs"):
            open_proposal = db.scalar(
                sa.select(PayeeBankInstruction).where(
                    PayeeBankInstruction.tenant_id == file.tenant_id,
                    PayeeBankInstruction.payee_kind == row.kind,
                    PayeeBankInstruction.payee_ref == row.ref,
                    PayeeBankInstruction.status == InstructionStatus.PROPOSED.value,
                )
            )
            if open_proposal is not None and open_proposal.origin == "manual":
                service.reject(
                    db,
                    tenant_id=file.tenant_id,
                    instruction_id=open_proposal.id,
                    decider_party_id=file.proposer_party_id,
                    reason=f"replaced by bulk file {file.file_ref}"[:500],
                    operator_ref=operator_ref,
                )
                written["replaced"] += 1
            # a legacy-import open proposal is replaced by service.propose itself
        proposed = service.propose(
            db,
            tenant_id=file.tenant_id,
            proposer_party_id=file.proposer_party_id,
            payee_kind=row.kind,
            payee_ref=row.ref,
            bank_code=row.bank_code,
            bank_name=banks.banks[row.bank_code][0],
            account_number=row.account_number,
            account_name=row.account_name,
            evidence_ref=row.evidence,
            operator_ref=operator_ref,
        )
        written["proposed"] += 1
        if approve and reason in ONBOARDING_APPROVABLE:
            service.approve(
                db,
                tenant_id=file.tenant_id,
                instruction_id=proposed.id,
                approver_party_id=file.proposer_party_id,
                note=f"bulk approve by {operator_ref}; file {file.file_ref}"[:500],
                operator_ref=operator_ref,
            )
            written["approved"] += 1
        elif approve:
            written["left_for_ui_approval"] += 1
    return written


def receipt_of(
    file: PayeeFile,
    outcomes,
    *,
    banks: BankReference,
    mode: str,
    dual: bool,
    approve: bool,
    expect: dict[str, int] | None = None,
    written: Counter | None = None,
):
    """Counts, digests and per-row (row number, kind, outcome, last four digits). Nothing else about a payee."""
    counts = Counter(reason for _row, reason in outcomes)
    refused = {k: v for k, v in counts.items() if k not in _OK}
    written = written or Counter()
    planned = plan_counts(outcomes)
    return dict(
        mode=mode,
        production_ready=False,
        file_sha256=file.sha256,
        bank_reference_sha256=banks.sha256,
        dual_control=dual,
        approve_requested=approve,
        counts=dict(
            requested=len(file.rows),
            new=planned["new"],
            replace=planned["replace"],
            changes_to_approved=planned["change"],
            name_differs=counts["name_differs"],
            unchanged=counts["unchanged"],
            refused=sum(refused.values()),
        ),
        expect=expect,
        expect_matches=None if expect is None else expect == planned,
        by_kind=dict(sorted(Counter(row.kind for row in file.rows).items())),
        refusals_by_reason=dict(sorted(refused.items())),
        written=dict(
            proposed=written["proposed"],
            replaced=written["replaced"],
            approved=written["approved"],
            left_for_ui_approval=written["left_for_ui_approval"],
        ),
        rows=[
            dict(row=i, kind=row.kind, outcome=reason, account_last4=row.last4)
            for i, (row, reason) in enumerate(outcomes, start=1)
        ],
    )


def check_proposer(db, file: PayeeFile, *, approve: bool) -> None:
    """The proposer must be an active person of THIS tenant holding the B8 propose permission (and the approve
    permission when --approve makes them the approver). Roles come from the declared permission specs."""
    import sqlalchemy as sa
    from dotmac_kernel.models import Party, PartyRoleGrant, Role

    from erp_v2.access import PAYEE_BANK_APPROVE, PAYEE_BANK_PROPOSE, PERMISSION_SPECS

    party = db.scalar(sa.select(Party).where(Party.tenant_id == file.tenant_id, Party.id == file.proposer_party_id))
    _require(party is not None and party.is_active, "proposer_not_authorised")
    held = set(
        db.scalars(
            sa.select(Role.slug)
            .join(PartyRoleGrant, (PartyRoleGrant.role_id == Role.id) & (PartyRoleGrant.tenant_id == Role.tenant_id))
            .where(PartyRoleGrant.tenant_id == file.tenant_id, PartyRoleGrant.party_id == file.proposer_party_id)
        )
    )
    roles_for = {spec.code: set(spec.default_roles) for spec in PERMISSION_SPECS}
    needed = [PAYEE_BANK_PROPOSE] + ([PAYEE_BANK_APPROVE] if approve else [])
    _require(all(held & roles_for[code] for code in needed), "proposer_not_authorised")


def run(
    db,
    file: PayeeFile,
    banks: BankReference,
    *,
    apply: bool,
    operator_ref: str | None,
    approve: bool,
    expect: dict[str, int] | None = None,
) -> dict:
    """Core of both modes; the caller owns the transaction (plan is rolled back by the caller)."""
    import sqlalchemy as sa

    from erp_v2.governance.policy import dual_control

    if db.get_bind().dialect.name == "postgresql":
        db.execute(sa.text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(file.tenant_id)})
        _require(
            db.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM public.tenants WHERE id = :t AND is_active)"),
                {"t": file.tenant_id},
            )
            is True,
            "tenant_unknown",
        )
    dual = dual_control(db, file.tenant_id)
    if approve:
        _require(bool(operator_ref), "operator_ref_required")
        _require(expect is not None, "expect_required")
        _require(not dual, "approve_refused_dual_control_on")
    check_proposer(db, file, approve=approve)
    outcomes = classify(db, file, banks)
    common = dict(banks=banks, dual=dual, approve=approve, expect=expect)
    if not apply:
        return receipt_of(file, outcomes, mode="plan", **common)
    if any(reason not in _OK for _row, reason in outcomes):
        # Fail closed and whole-file: nothing is written, and the receipt says why (counts, never values).
        return receipt_of(file, outcomes, mode="apply_refused", **common)
    if expect is not None and expect != plan_counts(outcomes):
        return receipt_of(file, outcomes, mode="apply_refused_expect_mismatch", **common)
    written = apply_rows(db, file, outcomes, banks=banks, operator_ref=operator_ref, approve=approve)
    return receipt_of(file, outcomes, mode="apply", written=written, **common)


def _authorized(args) -> bool:
    return not args.apply or (
        os.environ.get("ERP_IMPORT_APPROVED") == "1" and os.environ.get("ERP_IMPORT_APPLY_OK") == "1"
    )


def _run(args) -> dict:
    url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    _require(_authorized(args), "explicit_apply_authorization_required")
    _require(bool(url), "operator_database_materials_required")
    _require(
        "DATABASE_URL" not in os.environ and "PLATFORM_DATABASE_URL" not in os.environ,
        "serving_database_materials_forbidden",
    )
    operator_ref = (args.operator_ref or "").strip() or None
    _require(operator_ref is None or len(operator_ref) <= 120, "operator_ref_required")
    _require(not (args.apply or args.approve) or operator_ref is not None, "operator_ref_required")
    file = parse_file(read_private_file(args.file), args.file_sha256)
    banks = load_bank_reference(args.bank_reference, args.bank_reference_sha256)
    expect = parse_expect(args.expect)
    _require(not args.approve or expect is not None, "expect_required")
    import sqlalchemy as sa
    from dotmac_kernel.audit_actions import (
        AuditActionRegistry,
        AuditActionsNotInstalledError,
        active_audit_actions,
        install_audit_actions,
    )
    from sqlalchemy.orm import Session

    try:
        active_audit_actions()
    except AuditActionsNotInstalledError:
        from erp_v2.assembly import assembly

        install_audit_actions(AuditActionRegistry.from_manifests(assembly.modules))
    engine = sa.create_engine(url, echo=False, hide_parameters=True)
    try:
        _require(engine.dialect.name == "postgresql", "postgresql_required")
        with Session(engine) as db, db.begin():
            if not args.apply:
                db.execute(sa.text("SET TRANSACTION READ ONLY"))
            return run(
                db, file, banks, apply=args.apply, operator_ref=operator_ref, approve=args.approve, expect=expect
            )
    finally:
        engine.dispose()


def _receipt(path: Path) -> int:
    _require(path.parent.resolve(strict=True) == Path(os.path.abspath(path.parent)), "receipt_symlink_forbidden")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.payees.bulk_cli")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--file-sha256", required=True)
    parser.add_argument("--bank-reference", type=Path, help="Finance-confirmed bank reference file")
    parser.add_argument("--bank-reference-sha256")
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--operator-ref")
    parser.add_argument("--expect", help="new=N,replace=N,change=N: must equal the plan counts exactly at apply")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args(argv)
    try:
        _require(_authorized(args), "explicit_apply_authorization_required")
        fd = _receipt(args.receipt)
    except Exception:
        print("bulk payees refused: authorization or private receipt path")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload = _run(args)
            code = 0 if payload["counts"]["refused"] == 0 and payload.get("expect_matches") is not False else 3
        except BulkRefusal as exc:
            payload, code = dict(production_ready=False, refusal=exc.reason), 2
        except PayeeBankError as exc:
            payload, code = dict(production_ready=False, refusal="payee_service_refused", code=exc.code), 2
        except Exception:
            payload, code = dict(production_ready=False, refusal="bulk_payees_command_refused"), 2
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    counts = payload.get("counts", {})
    print(
        "bulk payees: "
        + (
            f"requested={counts.get('requested', 0)} new={counts.get('new', 0)} replace={counts.get('replace', 0)} "
            f"changes_to_approved={counts.get('changes_to_approved', 0)} "
            f"unchanged={counts.get('unchanged', 0)} refused={counts.get('refused', 0)}"
            if counts
            else "command refused"
        )
        + "; production_ready=false"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
