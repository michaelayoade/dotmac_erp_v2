"""Destination-only reviewed payroll account configuration.

No journals, opening balances, historical rows, or Payroll rules are changed.
Future B4 assembly should call ``resolve_future_deduction_accounts(db, tenant_id)``
and snapshot its UUID refs into a NEW pay-structure revision: NHIS is remittable;
LOAN_REPAYMENT credits a staff-loan receivable and is not a supplier liability.
This command does not perform that runtime adoption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from erp_v2.importer.payables_history_cli import OperatorRefusal, _json, _require, _uuid
from erp_v2.tax_seed import _safe_engine

LOAN_ACCOUNT_ID = UUID("67bea8e4-f372-4c2c-a677-2223fe7ceff6")
NHIS_CATEGORY_ID = UUID("0b97d749-8ca1-4ba7-bcf4-e8737826f9bf")


@dataclass(frozen=True)
class AccountSpec:
    purpose: str
    action: str
    code: str
    name: str
    category_id: UUID
    account_id: UUID | None
    account_class: str
    normal_balance: str
    currency_code: str | None


@dataclass(frozen=True)
class SetupSpec:
    tenant_id: UUID
    destination_database: str
    accounts: tuple[AccountSpec, ...]


def parse_account_spec(raw: bytes) -> SetupSpec:
    data = _json(raw)
    _require(set(data) == {"version", "tenant_id", "destination_database", "accounts"}, "invalid_account_spec")
    _require(type(data["version"]) is int and data["version"] == 1, "invalid_account_spec_version")
    tenant = _uuid(data["tenant_id"], "invalid_tenant_id")
    database = data["destination_database"]
    _require(isinstance(database, str) and bool(database.strip()) and len(database) <= 63, "invalid_database_name")
    rows = data["accounts"]
    _require(isinstance(rows, list) and len(rows) == 2, "two_explicit_payroll_accounts_required")
    accounts = []
    for row in rows:
        _require(
            isinstance(row, dict)
            and set(row)
            == {
                "purpose",
                "action",
                "code",
                "name",
                "category_id",
                "account_id",
                "account_class",
                "normal_balance",
                "currency_code",
            },
            "invalid_account_definition",
        )
        purpose = row["purpose"]
        _require(purpose in ("NHIS", "LOAN_REPAYMENT"), "unsupported_payroll_account")
        _require(row["action"] in ("create", "reuse"), "invalid_account_action")
        _require(
            isinstance(row["name"], str) and 0 < len(row["name"]) <= 200 and row["name"] == row["name"].strip(),
            "invalid_account_name",
        )
        category = _uuid(row["category_id"], "invalid_category_id")
        account = None if row["account_id"] is None else _uuid(row["account_id"], "invalid_account_id")
        currency = row["currency_code"]
        _require(
            currency is None
            or (
                isinstance(currency, str)
                and len(currency) == 3
                and currency.isascii()
                and currency.isalpha()
                and currency.isupper()
            ),
            "invalid_currency",
        )
        if purpose == "NHIS":
            _require(
                row["code"] == "2133"
                and row["name"] == "NHIS Payable"
                and category == NHIS_CATEGORY_ID
                and row["account_class"] == "LIABILITY"
                and row["normal_balance"] == "CREDIT",
                "nhis_account_policy_mismatch",
            )
            _require(row["action"] != "reuse" or account is not None, "reuse_account_id_required")
            _require(row["action"] != "create" or account is None, "create_account_id_forbidden")
        else:
            _require(
                row["action"] == "reuse"
                and row["code"] == "1410"
                and account == LOAN_ACCOUNT_ID
                and row["account_class"] == "ASSET"
                and row["normal_balance"] == "DEBIT",
                "staff_loan_receivable_required",
            )
        accounts.append(
            AccountSpec(
                purpose,
                row["action"],
                row["code"],
                row["name"],
                category,
                account,
                row["account_class"],
                row["normal_balance"],
                currency,
            )
        )
    _require({a.purpose for a in accounts} == {"NHIS", "LOAN_REPAYMENT"}, "duplicate_payroll_account_purpose")
    return SetupSpec(tenant, database, tuple(sorted(accounts, key=lambda a: a.purpose)))


def _category(db, tenant_id: UUID, category_id: UUID, account_class: str):
    from dotmac_accounting.models import AccountCategory

    row = db.scalar(
        sa.select(AccountCategory).where(AccountCategory.tenant_id == tenant_id, AccountCategory.id == category_id)
    )
    _require(row is not None and row.is_active and row.account_class == account_class, "account_category_conflict")
    return row


def _existing(db, tenant_id: UUID, spec: AccountSpec):
    from dotmac_accounting.models import Account

    _category(db, tenant_id, spec.category_id, spec.account_class)
    if spec.purpose == "NHIS":
        _nhf_category(db, tenant_id)
    row = db.scalar(sa.select(Account).where(Account.tenant_id == tenant_id, Account.code == spec.code))
    if row is None:
        _require(spec.action == "create", "required_account_missing")
        return None
    _require(
        (spec.account_id is None or row.id == spec.account_id)
        and row.category_id == spec.category_id
        and row.name == spec.name
        and row.kind == "POSTING"
        and row.normal_balance == spec.normal_balance
        and row.is_active
        and row.posting_allowed
        and row.currency_code == spec.currency_code
        and (spec.action != "create" or row.parent_id is None),
        "existing_account_conflict",
    )
    return row


def plan_accounts(db: Session, spec: SetupSpec) -> list[dict]:
    """SELECT-only plan: does not invoke the owner creation API, flush, or persist."""
    with db.no_autoflush:
        return [
            dict(
                purpose=a.purpose,
                code=a.code,
                action="create" if row is None else "reuse",
                account_id=None if row is None else str(row.id),
            )
            for a in spec.accounts
            for row in [_existing(db, spec.tenant_id, a)]
        ]


def _creation_owner():
    # a1 reaches the kernel reference runtime through its savepoint import.
    # a2 replaces that import with the public transaction helper. Never shim it.
    installed = version("dotmac-accounting")
    alpha = re.fullmatch(r"0\.1\.0a([0-9]+)", installed)
    stable = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", installed)
    qualified = bool(alpha and int(alpha[1]) >= 2) or bool(
        stable and tuple(int(part) for part in stable.groups()) >= (0, 1, 0)
    )
    _require(qualified, "accounting_a2_creation_release_required")
    from dotmac_accounting.service import create_account

    return create_account


def apply_accounts(db: Session, spec: SetupSpec) -> list[dict]:
    """Apply in the caller's SERIALIZABLE transaction, serializing tenant setup."""
    from dotmac_accounting.contracts import AccountKind, CreateAccount, NormalBalance
    from dotmac_kernel.cache import TenantScope

    db.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"erp-payroll-accounts:{spec.tenant_id}"},
    )
    plan = plan_accounts(db, spec)
    create = _creation_owner() if any(row["action"] == "create" for row in plan) else None
    for account, result in zip(spec.accounts, plan, strict=True):
        if result["action"] == "create":
            row = create(
                db,
                scope=TenantScope(spec.tenant_id),
                command=CreateAccount(
                    code=account.code,
                    name=account.name,
                    category_id=account.category_id,
                    kind=AccountKind.POSTING,
                    normal_balance=NormalBalance(account.normal_balance),
                    currency_code=account.currency_code,
                    posting_allowed=True,
                ),
            )
            result["account_id"] = str(row.id)
            _existing(db, spec.tenant_id, account)
    return plan


def _nhf_category(db, tenant_id: UUID):
    from dotmac_accounting.models import Account

    nhf = db.scalar(sa.select(Account).where(Account.tenant_id == tenant_id, Account.code == "2132"))
    _require(nhf is not None and nhf.category_id == NHIS_CATEGORY_ID, "nhf_category_evidence_conflict")


def resolve_future_deduction_accounts(db: Session, tenant_id: UUID) -> dict[str, UUID]:
    """Read-only validated refs for NEW structure revisions; loan is not remittable."""
    from dotmac_accounting.models import Account

    refs = {}
    with db.no_autoflush:
        for purpose, code, klass, normal in (
            ("NHIS", "2133", "LIABILITY", "CREDIT"),
            ("LOAN_REPAYMENT", "1410", "ASSET", "DEBIT"),
        ):
            row = db.scalar(sa.select(Account).where(Account.tenant_id == tenant_id, Account.code == code))
            _require(
                row is not None
                and row.is_active
                and row.posting_allowed
                and row.kind == "POSTING"
                and row.normal_balance == normal,
                "future_payroll_account_invalid",
            )
            _category(db, tenant_id, row.category_id, klass)
            _require(purpose != "LOAN_REPAYMENT" or row.id == LOAN_ACCOUNT_ID, "staff_loan_identity_conflict")
            _require(purpose != "NHIS" or row.category_id == NHIS_CATEGORY_ID, "nhis_category_conflict")
            if purpose == "NHIS":
                _nhf_category(db, tenant_id)
            refs[purpose] = row.id
    return refs


def _run(args) -> dict:
    raw = args.spec.read_bytes()
    spec = parse_account_spec(raw)
    digest = hashlib.sha256(raw).hexdigest()
    _require(
        not args.apply
        or (os.environ.get("ERP_PAYROLL_ACCOUNT_SETUP_APPROVED") == "1" and args.approved_spec_sha256 == digest),
        "reviewed_account_apply_required",
    )
    _require(
        not any(key in os.environ for key in ("DATABASE_URL", "PLATFORM_DATABASE_URL", "LEGACY_DATABASE_URL")),
        "serving_or_source_database_materials_forbidden",
    )
    url = os.environ.pop("MIGRATION_DATABASE_URL", "")
    _require(bool(url), "migration_database_material_required")
    engine = _safe_engine(url)
    try:
        with Session(engine) as db, db.begin():
            if not args.apply:
                db.execute(sa.text("SET TRANSACTION READ ONLY"))
            _require(
                db.scalar(sa.text("SELECT current_database()")) == spec.destination_database,
                "destination_database_mismatch",
            )
            db.execute(
                sa.text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(spec.tenant_id)}
            )
            observed = db.scalar(sa.text("SELECT public.app_current_tenant_id()"))
            _require(str(observed) == str(spec.tenant_id), "tenant_context_mismatch")
            results = apply_accounts(db, spec) if args.apply else plan_accounts(db, spec)
            refs = resolve_future_deduction_accounts(db, spec.tenant_id) if args.apply else None
            return dict(
                mode="apply" if args.apply else "plan",
                spec_sha256=digest,
                tenant_id=str(spec.tenant_id),
                destination_database=spec.destination_database,
                accounts=results,
                future_deduction_account_refs=None
                if refs is None
                else {key: str(value) for key, value in refs.items()},
                payroll_runtime_wired=False,
            )
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.payroll_accounts")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-spec-sha256")
    args = parser.parse_args(argv)
    try:
        parent = args.receipt.parent.absolute()
        _require(parent.resolve() == parent, "receipt_parent_symlink_forbidden")
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent_fd)
            _require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, "private_receipt_directory_required")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(args.receipt.name, flags, 0o600, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        print("payroll account setup refused: private exclusive receipt required")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload, code = _run(args), 0
        except OperatorRefusal as exc:
            payload, code = {"ready": False, "refusal": exc.reason}, 2
        except Exception:
            payload, code = {"ready": False, "refusal": "payroll_account_setup_refused"}, 2
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("payroll account setup receipt written" if code == 0 else "payroll account setup refused; see receipt")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
