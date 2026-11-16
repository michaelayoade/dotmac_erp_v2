"""Operator command: create one cash GL account per bank account from a reviewed spec.

Plan by default (SELECT-only, READ ONLY transaction, writes nothing). ``--apply`` additionally
needs ``ERP_CASH_GL_SETUP_APPROVED=1`` and ``--approved-spec-sha256`` equal to the sha256 of the
exact spec file reviewed. Idempotent: an account that already exists with exactly the reviewed
attributes is reused; any difference (name, category, parent, class, side, currency, postable,
active) is a conflict and refuses. It creates accounts only: it never re-points a bank account
(that is the governed two-person step in ``erp_v2.cash_gl.service``) and never posts a journal.

Spec (version 1): ``tenant_id``, ``destination_database`` and 1..13 ``accounts`` of
``bank_account_id``, ``code``, ``name``, ``category_id`` (an active ASSET category), ``parent_id``
(null, or a non-posting account) and ``currency_code`` (null or 3 upper-case letters). Accounts are
always POSTING, DEBIT-normal and postable. Creation goes through accounting a2's composed-safe
``create_account``; a1 is refused (its seam imports the kernel reference runtime).
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

MAX_ACCOUNTS = 13
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_ROW_KEYS = {"bank_account_id", "code", "name", "category_id", "parent_id", "currency_code"}


@dataclass(frozen=True)
class CashAccountSpec:
    bank_account_id: UUID
    code: str
    name: str
    category_id: UUID
    parent_id: UUID | None
    currency_code: str | None


@dataclass(frozen=True)
class SetupSpec:
    tenant_id: UUID
    destination_database: str
    accounts: tuple[CashAccountSpec, ...]


def parse_spec(raw: bytes) -> SetupSpec:
    data = _json(raw)
    _require(set(data) == {"version", "tenant_id", "destination_database", "accounts"}, "invalid_account_spec")
    _require(type(data["version"]) is int and data["version"] == 1, "invalid_account_spec_version")
    tenant = _uuid(data["tenant_id"], "invalid_tenant_id")
    database = data["destination_database"]
    _require(isinstance(database, str) and bool(database.strip()) and len(database) <= 63, "invalid_database_name")
    rows = data["accounts"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= MAX_ACCOUNTS, "account_count_out_of_range")
    accounts = []
    for row in rows:
        _require(isinstance(row, dict) and set(row) == _ROW_KEYS, "invalid_account_definition")
        _require(isinstance(row["code"], str) and bool(_CODE.match(row["code"])), "invalid_account_code")
        name = row["name"]
        _require(isinstance(name, str) and 0 < len(name) <= 200 and name == name.strip(), "invalid_account_name")
        currency = row["currency_code"]
        _require(
            currency is None
            or (isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isupper()),
            "invalid_currency",
        )
        accounts.append(
            CashAccountSpec(
                _uuid(row["bank_account_id"], "invalid_bank_account_id"),
                row["code"],
                name,
                _uuid(row["category_id"], "invalid_category_id"),
                None if row["parent_id"] is None else _uuid(row["parent_id"], "invalid_parent_id"),
                currency,
            )
        )
    _require(len({a.code for a in accounts}) == len(accounts), "duplicate_account_code")
    _require(len({a.bank_account_id for a in accounts}) == len(accounts), "duplicate_bank_account")
    return SetupSpec(tenant, database, tuple(sorted(accounts, key=lambda a: a.code)))


def _check_spec_against_db(db: Session, tenant_id: UUID, spec: CashAccountSpec):
    """Validate the references and return the existing account for the code, or None."""
    from dotmac_accounting.contracts import AccountClass, AccountKind, NormalBalance
    from dotmac_accounting.models import Account, AccountCategory
    from dotmac_banking.models import BankAccount

    bank = db.scalar(
        sa.select(BankAccount).where(BankAccount.tenant_id == tenant_id, BankAccount.id == spec.bank_account_id)
    )
    _require(bank is not None and bank.status != "closed", "bank_account_unknown_or_closed")
    _require(spec.currency_code is None or spec.currency_code == bank.currency_code, "currency_conflict")
    category = db.scalar(
        sa.select(AccountCategory).where(AccountCategory.tenant_id == tenant_id, AccountCategory.id == spec.category_id)
    )
    _require(
        category is not None and category.is_active and category.account_class == AccountClass.ASSET,
        "category_conflict",
    )
    if spec.parent_id is not None:
        parent = db.scalar(sa.select(Account).where(Account.tenant_id == tenant_id, Account.id == spec.parent_id))
        _require(parent is not None and parent.kind != AccountKind.POSTING and parent.is_active, "parent_conflict")
        parent_class = db.scalar(
            sa.select(AccountCategory.account_class).where(AccountCategory.id == parent.category_id)
        )
        _require(parent_class == AccountClass.ASSET, "parent_conflict")
    row = db.scalar(sa.select(Account).where(Account.tenant_id == tenant_id, Account.code == spec.code))
    if row is None:
        return None
    _require(
        row.name == spec.name
        and row.category_id == spec.category_id
        and row.parent_id == spec.parent_id
        and row.kind == AccountKind.POSTING
        and row.normal_balance == NormalBalance.DEBIT
        and row.is_active
        and row.posting_allowed
        and row.currency_code == spec.currency_code,
        "existing_account_conflict",
    )
    return row


def plan_accounts(db: Session, spec: SetupSpec) -> list[dict]:
    """SELECT-only plan: never invokes the creation owner, flushes or persists."""
    from dotmac_banking.models import BankAccount

    with db.no_autoflush:
        plan = []
        for account in spec.accounts:
            row = _check_spec_against_db(db, spec.tenant_id, account)
            if row is not None:
                # An existing account already serving a DIFFERENT bank account is a shared GL, not a split.
                others = db.scalars(
                    sa.select(BankAccount.account_code).where(
                        BankAccount.tenant_id == spec.tenant_id,
                        BankAccount.id != account.bank_account_id,
                        BankAccount.cash_account_ref.in_((str(row.id), row.code)),
                    )
                ).all()
                _require(not others, "account_used_by_another_bank_account")
            plan.append(
                dict(
                    bank_account_id=str(account.bank_account_id),
                    code=account.code,
                    action="create" if row is None else "reuse",
                    account_id=None if row is None else str(row.id),
                )
            )
        return plan


def _creation_owner():
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
    """Apply inside the caller's transaction; the advisory lock serializes tenant setup."""
    from dotmac_accounting.contracts import AccountKind, CreateAccount, NormalBalance
    from dotmac_kernel.cache import TenantScope

    db.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"erp-cash-gl-setup:{spec.tenant_id}"},
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
                    normal_balance=NormalBalance.DEBIT,
                    parent_id=account.parent_id,
                    currency_code=account.currency_code,
                    posting_allowed=True,
                ),
            )
            result["account_id"] = str(row.id)
            _check_spec_against_db(db, spec.tenant_id, account)
    return plan


def _run(args) -> dict:
    raw = args.spec.read_bytes()
    spec = parse_spec(raw)
    digest = hashlib.sha256(raw).hexdigest()
    _require(
        not args.apply or (os.environ.get("ERP_CASH_GL_SETUP_APPROVED") == "1" and args.approved_spec_sha256 == digest),
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
            return dict(
                mode="apply" if args.apply else "plan",
                spec_sha256=digest,
                tenant_id=str(spec.tenant_id),
                destination_database=spec.destination_database,
                accounts=results,
                bank_accounts_repointed=False,
            )
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m erp_v2.cash_gl.setup_cli")
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
            fd = os.open(
                args.receipt.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
            )
        finally:
            os.close(parent_fd)
    except Exception:
        print("cash GL setup refused: private exclusive receipt required")
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            payload, code = _run(args), 0
        except OperatorRefusal as exc:
            payload, code = {"ready": False, "refusal": exc.reason}, 2
        except Exception:
            payload, code = {"ready": False, "refusal": "cash_gl_setup_refused"}, 2
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("cash GL setup receipt written" if code == 0 else "cash GL setup refused; see receipt")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
