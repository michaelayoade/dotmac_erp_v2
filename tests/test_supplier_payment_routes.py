"""Mounted supplier payment routes: permission matrix, dormancy, CSRF, actor wiring, no-store file.

Same harness as test_payee_bank_routes: the real facet, cookie auth, tenant resolution and CSRF
middleware run; SQLite and spy services substitute only the database and the financial actions.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel import create_app
from dotmac_kernel.config import settings
from dotmac_kernel.deps import get_db
from dotmac_kernel.middleware import tenant as tenant_middleware
from dotmac_kernel.models import AuthSession, Base, Party, PartyRoleGrant, PartyType, Role, Tenant, TenantDomain
from dotmac_kernel.security import hash_token, issue_access_token
from dotmac_kernel.settings_models import DomainSetting
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from erp_v2 import access
from erp_v2.access import NEW_ROLES
from erp_v2.assembly import assembly
from erp_v2.disbursement import file as bank_file
from erp_v2.supplier_payments import routes as sp_routes
from erp_v2.supplier_payments.service import BankFile, PaymentOutcome, PaymentRefused

PID = uuid4()
PRINCIPALS = {
    "staff": ("staff",),
    "admin": ("admin",),
    "preparer": ("staff", "finance_preparer"),
    "approver": ("staff", "finance_approver"),
    "treasury": ("staff", "treasury"),
    "payroll": ("staff", "payroll_approver", "payroll_finalizer", "bank_reconciler"),
    "preparer_nostaff": ("finance_preparer",),
    "approver_nostaff": ("finance_approver",),
    "treasury_nostaff": ("treasury",),
}
P, A, T = sp_routes.PREPARE_BASE, sp_routes.APPROVE_BASE, sp_routes.TREASURY_BASE
R = sp_routes.REVERSAL_BASE
OBLIGATION = uuid4()
FORM = {
    f"amount_{OBLIGATION}": "10.00",
    "request_id": str(uuid4()),
    "bank_account_id": str(uuid4()),
    "value_date": "2026-01-20",
    "obligation_id": str(OBLIGATION),
    "reason": "r",
    "bank_reference": "REF-1",
    "reversal_date": "2026-01-25",
    "payment_id": str(PID),
}
# action -> (path, principals admitted, spied service function)
ACTIONS = {
    "create": (P, {"preparer"}, "prepare_payment"),
    "approve": (f"{A}/{PID}/approve", {"approver"}, "approve_payment"),
    "cancel": (f"{A}/{PID}/cancel", {"approver"}, "cancel_payment"),
    "record": (f"{T}/{PID}/record", {"treasury"}, "record_payment"),
    "reverse": (f"{R}/{PID}/reverse", {"approver"}, "reverse_payment"),
    "bank_file": (f"{T}/bank-file", {"treasury"}, "generate_bank_file"),
}
GETS = {
    P: {"preparer"},
    f"{P}/new": {"preparer"},
    A: {"approver"},
    R: {"approver"},
    T: {"treasury"},
}


@dataclass
class Browser:
    client: TestClient
    tenant: Tenant
    parties: dict[str, Party]
    tokens: dict[str, str]
    spies: dict[str, Mock]
    db: Session

    def sign_in(self, who: str | None) -> str:
        self.client.cookies.clear()
        if who is not None:
            self.client.cookies.set("access_token", self.tokens[who])
        assert self.client.get("/login").status_code == 200
        token = self.client.cookies.get("csrf_token")
        assert token
        return token


@pytest.fixture()
def browser(monkeypatch):
    monkeypatch.setattr(settings, "csrf_enabled", True)
    monkeypatch.setattr(settings, "platform_root_domain", "erp.test")
    monkeypatch.setattr(settings, "trusted_hosts", "acme.erp.test")
    monkeypatch.setattr(settings, "rate_limit_requests", 10000)
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            model.__table__ for model in (Tenant, TenantDomain, Party, Role, PartyRoleGrant, AuthSession, DomainSetting)
        ],
    )
    with Session(engine, expire_on_commit=False) as db:
        tenant = Tenant(id=uuid4(), slug="acme", name="Acme")
        db.add(tenant)
        db.flush()
        roles = {}
        for slug in ("admin", "staff", *NEW_ROLES):
            roles[slug] = Role(tenant_id=tenant.id, slug=slug, name=slug)
            db.add(roles[slug])
        db.flush()
        parties, tokens = {}, {}
        for name, held in PRINCIPALS.items():
            party = Party(
                id=uuid4(),
                tenant_id=tenant.id,
                party_type=PartyType.person,
                display_name=name,
                email=f"{name}@acme.test",
            )
            db.add(party)
            db.flush()
            for slug in held:
                db.add(PartyRoleGrant(tenant_id=tenant.id, party_id=party.id, role_id=roles[slug].id))
            token, expires_at = issue_access_token(party.id, tenant.id)
            db.add(
                AuthSession(tenant_id=tenant.id, party_id=party.id, token_hash=hash_token(token), expires_at=expires_at)
            )
            parties[name], tokens[name] = party, token
        db.commit()

        @contextmanager
        def resolver_session():
            yield db

        def request_session():
            yield db

        monkeypatch.setattr(tenant_middleware, "resolver_session", resolver_session)
        app = create_app(replace(assembly, web_enabled=True, database_runtime=None, require_database_runtime=False))
        app.dependency_overrides[get_db] = request_session
        file = bank_file.build_csv(
            [
                bank_file.DisbursementRow(
                    "SP-1",
                    date(2026, 1, 20),
                    "NGN",
                    __import__("decimal").Decimal("10.00"),
                    "ACME",
                    "058",
                    "0123456789",
                    "n",
                    "d",
                )
            ]
        )
        spies = {spy_name: Mock(return_value=PaymentOutcome(PID, False)) for _, _, spy_name in ACTIONS.values()}
        spies["generate_bank_file"].return_value = BankFile(file, "supplier-payments-abc.csv", 1)
        for name, spy in spies.items():
            monkeypatch.setattr(sp_routes.service, name, spy)
        for name, value in (
            ("register", []),
            ("bank_accounts", []),
            ("open_obligations", []),
            ("detail", None),
        ):
            spy = Mock(return_value=value)
            spies[f"queries.{name}"] = spy
            monkeypatch.setattr(sp_routes.queries, name, spy)
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tenant, parties, tokens, spies, db)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


def _post(browser, path, token, **data):
    return browser.client.post(path, data={"csrf_token": token, **FORM, **data})


def _enable_file(monkeypatch):
    monkeypatch.setattr(sp_routes.disbursement_policy, "bank_file_generation_enabled", lambda db, tenant_id: True)


@pytest.fixture()
def file_enabled(monkeypatch):
    _enable_file(monkeypatch)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("who", PRINCIPALS)
def test_each_action_is_admitted_only_for_its_named_role(browser, file_enabled, action, who):
    path, allowed, spy_name = ACTIONS[action]
    token = browser.sign_in(who)
    response = _post(browser, path, token)
    service_spies = {k: v for k, v in browser.spies.items() if not k.startswith("queries.")}
    if who in allowed:
        assert response.status_code in (200, 303), (action, who, response.status_code)
        service_spies[spy_name].assert_called_once()
        kwargs = service_spies[spy_name].call_args.kwargs
        assert kwargs["tenant"].id == browser.tenant.id
        assert kwargs["party"].id == browser.parties[who].id
    else:
        assert response.status_code == 403, (action, who)
        for spy in service_spies.values():
            spy.assert_not_called()


@pytest.mark.parametrize("path", GETS)
@pytest.mark.parametrize("who", PRINCIPALS)
def test_get_pages_follow_the_same_matrix_and_are_no_store(browser, path, who):
    browser.sign_in(who)
    response = browser.client.get(path)
    if who in GETS[path]:
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
    else:
        assert response.status_code == 403


@pytest.mark.parametrize("who", [None])
def test_anonymous_is_redirected_to_login(browser, who):
    browser.sign_in(who)
    for path in GETS:
        assert browser.client.get(path).status_code == 302


@pytest.mark.parametrize(
    "who", ["staff", "admin", "payroll", "preparer_nostaff", "approver_nostaff", "treasury_nostaff"]
)
def test_dormant_until_a_named_finance_role_holds_the_power(browser, who):
    token = browser.sign_in(who)
    for path, _, _ in ACTIONS.values():
        assert _post(browser, path, token).status_code in (302, 403)
    service_spies = [v for k, v in browser.spies.items() if not k.startswith("queries.")]
    for spy in service_spies:
        spy.assert_not_called()


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("proof", ["missing", "mismatch", "foreign-origin"])
def test_invalid_csrf_never_reaches_the_service(browser, file_enabled, action, proof):
    path, allowed, _ = ACTIONS[action]
    token = browser.sign_in(sorted(allowed)[0])
    headers = {}
    if proof == "mismatch":
        headers["x-csrf-token"] = token + "x"
    elif proof == "foreign-origin":
        headers.update({"x-csrf-token": token, "origin": "https://other.example"})
    assert browser.client.post(path, headers=headers, data=FORM).status_code == 403
    for name, spy in browser.spies.items():
        if not name.startswith("queries."):
            spy.assert_not_called()


def test_treasury_cannot_approve_cancel_or_reverse_and_approver_cannot_record_or_generate(browser):
    token = browser.sign_in("treasury")
    for path in (f"{A}/{PID}/approve", f"{A}/{PID}/cancel", f"{R}/{PID}/reverse", P):
        assert _post(browser, path, token).status_code == 403
    token = browser.sign_in("approver")
    for path in (f"{T}/{PID}/record", f"{T}/bank-file"):
        assert _post(browser, path, token).status_code == 403
    assert not browser.spies["record_payment"].called and not browser.spies["generate_bank_file"].called


def test_refusal_renders_code_and_message(browser):
    token = browser.sign_in("approver")
    browser.spies["approve_payment"].side_effect = PaymentRefused("sod_approver_is_preparer", "no self approval")
    browser.spies["queries.detail"].return_value = None
    response = _post(browser, f"{A}/{PID}/approve", token)
    assert response.status_code == 404  # unknown payment page; refusal on a real page is covered below


def test_refusal_on_an_existing_payment_renders_400_with_the_code(browser):
    token = browser.sign_in("treasury")
    browser.spies["record_payment"].side_effect = PaymentRefused("sod_recorder_is_approver", "you approved it")
    payment = SimpleNamespace(
        id=PID,
        number="SP-1",
        status=SimpleNamespace(value="APPROVED"),
        supplier_name_snapshot="S",
        supplier_ref="s",
        value_date=date(2026, 1, 20),
        currency_code="NGN",
        gross_amount=1,
        wht_amount=0,
        net_amount=1,
        cash_account_ref="x",
        prepared_by="p",
        prepared_at=None,
        approved_by=None,
        recorded_by=None,
        cancelled_by=None,
        reversed_by=None,
    )
    browser.spies["queries.detail"].return_value = {
        "payment": payment,
        "lines": [],
        "receipts": {},
        "bank": None,
        "file_receipts": [],
        "payee_instruction": None,
        "names": {},
    }
    response = _post(browser, f"{T}/{PID}/record", token)
    assert response.status_code == 400 and "sod_recorder_is_approver" in response.text
    assert "no-store" in response.headers["cache-control"]


def test_bank_file_is_dormant_404_by_default(browser):
    token = browser.sign_in("treasury")
    browser.spies["generate_bank_file"].side_effect = PaymentRefused("bank_file_disabled", "off")
    assert _post(browser, f"{T}/bank-file", token).status_code == 404


def test_bank_file_download_is_no_store_attachment_with_receipt_committed_first(browser, file_enabled):
    token = browser.sign_in("treasury")
    events = []
    browser.spies["generate_bank_file"].side_effect = lambda *a, **kw: (
        events.append("generated")
        or BankFile(
            bank_file.build_csv(
                [
                    bank_file.DisbursementRow(
                        "SP-1",
                        date(2026, 1, 20),
                        "NGN",
                        __import__("decimal").Decimal("10.00"),
                        "ACME",
                        "058",
                        "0123456789",
                        "n",
                        "d",
                    )
                ]
            ),
            "supplier-payments-abc.csv",
            1,
        )
    )
    real_commit = browser.db.commit
    browser.db.commit = lambda: events.append("commit") or real_commit()
    response = _post(browser, f"{T}/bank-file", token)
    assert response.status_code == 200 and events[:2] == ["generated", "commit"]
    assert response.headers["content-type"].startswith("text/csv")
    assert "no-store" in response.headers["cache-control"] and response.headers["pragma"] == "no-cache"
    assert response.headers["content-disposition"] == 'attachment; filename="supplier-payments-abc.csv"'
    assert b"0123456789" in response.content


def test_bank_file_refusal_is_a_page_not_a_download(browser, file_enabled):
    token = browser.sign_in("treasury")
    browser.spies["generate_bank_file"].side_effect = PaymentRefused("not_approved", "Payment SP-1 is not approved")
    response = _post(browser, f"{T}/bank-file", token)
    assert response.status_code == 400 and "not_approved" in response.text
    assert not response.headers["content-type"].startswith("text/csv")


def test_form_parsing_reaches_the_service_with_typed_values(browser):
    token = browser.sign_in("preparer")
    ob = str(uuid4())
    response = browser.client.post(
        P,
        data={
            "csrf_token": token,
            "request_id": FORM["request_id"],
            "bank_account_id": FORM["bank_account_id"],
            "value_date": "2026-01-20",
            "obligation_id": [ob],
            f"amount_{ob}": "12.50",
        },
    )
    assert response.status_code == 303 and response.headers["location"] == f"{P}/{PID}"
    kwargs = browser.spies["prepare_payment"].call_args.kwargs
    assert str(kwargs["allocations"][0].obligation_id) == ob and str(kwargs["allocations"][0].amount) == "12.50"
    assert kwargs["value_date"] == date(2026, 1, 20)


def test_form_never_supplies_cash_or_payer_references(browser):
    token = browser.sign_in("preparer")
    ob = str(uuid4())
    browser.client.post(
        P,
        data={
            "csrf_token": token,
            "request_id": FORM["request_id"],
            "bank_account_id": FORM["bank_account_id"],
            "value_date": "2026-01-20",
            "obligation_id": [ob],
            f"amount_{ob}": "1",
            "cash_account_ref": str(uuid4()),
            "payer_instrument_ref": str(uuid4()),
            "withholding": "5",
        },
    )
    kwargs = browser.spies["prepare_payment"].call_args.kwargs
    assert set(kwargs) == {"tenant", "party", "request_id", "bank_account_id", "value_date", "allocations"}


@pytest.mark.parametrize(
    "extra",
    [
        [("unrelated", "1")],  # selected obligation has no amount
        [("obligation_id", ["not-a-uuid"])],
        [("value_date", "garbage")],
    ],
)
def test_malformed_input_refuses_before_the_service(browser, extra):
    token = browser.sign_in("preparer")
    data = {
        "csrf_token": token,
        "request_id": FORM["request_id"],
        "bank_account_id": FORM["bank_account_id"],
        "value_date": "2026-01-20",
        "obligation_id": [FORM["obligation_id"]],
    }
    data.update(dict(extra))
    response = browser.client.post(P, data=data)
    assert response.status_code == 400
    browser.spies["prepare_payment"].assert_not_called()


def test_route_permission_table_matches_declared_permissions():
    declared = {spec.code: set(spec.default_roles) for spec in access.PERMISSION_SPECS}
    assert set(sp_routes.ROUTE_PERMISSIONS.values()) == {
        "erp.finance.supplier_payments.prepare",
        "erp.finance.supplier_payments.approve",
        "erp.treasury.supplier_payments.record",
        "erp.treasury.disbursement_files.generate",
    }
    for code in set(sp_routes.ROUTE_PERMISSIONS.values()):
        assert not {"admin", "staff"} & declared[code], code
    assert declared[sp_routes.PREPARE] == {"finance_preparer"}
    assert declared[sp_routes.APPROVE] == {"finance_approver"}
    assert declared[sp_routes.RECORD] == declared[sp_routes.GENERATE] == {"treasury"}


def test_role_sod_pairs_keep_prepare_approve_and_record_with_different_people():
    pairs = access.SOD_CONFLICT_PAIRS
    assert frozenset({"finance_preparer", "finance_approver"}) in pairs  # prepare + approve/reverse
    assert frozenset({"finance_approver", "treasury"}) in pairs  # approve/reverse + record/file
    assert frozenset({"finance_approver", "treasury"}) in access.NON_WAIVABLE_SOD_PAIRS


def test_supplier_payment_routes_are_not_facet_entry_routes():
    assert "erp_supplier_payments" not in {r.module for r in assembly.web_facets[0].entry_routes}
