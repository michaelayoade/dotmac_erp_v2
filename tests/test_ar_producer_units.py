"""B-16 AR producer: units (no database), route matrix, source greps.

The real facet, cookie auth, tenant resolution and CSRF middleware run (same harness as the supplier payment
routes); SQLite and spy services substitute only the database and the financial actions.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_billing.contracts import DueDateBasisStatus, SettlementFundingLane
from dotmac_kernel import create_app
from dotmac_kernel.config import settings as kernel_settings
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
from erp_v2.ar import constants, routes, service, settings, terms
from erp_v2.ar.service import ArRefused, LineInput, Outcome
from erp_v2.assembly import assembly
from erp_v2.posting.rules import MAX_KEY_LENGTH

SRC = Path(__file__).resolve().parents[1] / "src" / "erp_v2"
BIG = UUID(int=2**128 - 1)


# --- keys ---------------------------------------------------------------------------------------------------------


def test_every_billing_key_is_stable_distinct_and_within_the_kernel_limit():
    keys = [
        service.key_draft(BIG),
        service.key_invoice(BIG),
        service.key_credit_note(BIG),
        service.key_line(BIG, 100),
        service.key_autoapply(BIG),
        service.key_apply(BIG),
        service.key_alloc(BIG, 100),
        service.key_dealloc(BIG, BIG),
        service.key_draw_reverse(BIG),
        service.key_reverse_draw(BIG, BIG),
        service.key_reverse(BIG),
        service.key_reverse_dealloc(BIG, BIG),
        service.key_void(BIG),
        service.key_receipt_number(BIG),
    ]
    assert len(set(keys)) == len(keys)
    assert max(map(len, keys)) <= MAX_KEY_LENGTH == 200
    assert service.key_invoice(BIG) == service.key_invoice(UUID(str(BIG)))  # derived only from the id
    assert service.key_line(BIG, 3) == f"{BIG}:3"  # the spec's source_fact_id


def test_constants_are_code_not_settings():
    keys = {spec.key for spec in settings.SPECS}
    assert not any(k in keys for k in ("ar.confirmation_evidence", "ar.source_kind", "ar.funding_lane"))
    assert constants.CONFIRMATION_EVIDENCE == "erp_v2.ar.receipt_recorded"
    assert constants.SOURCE_KIND == "erp_v2.ar_invoice_line"
    assert constants.ACCEPTED_CONFIRMATION_EVIDENCE == frozenset({"erp_v2.ar.receipt_recorded"})
    assert constants.ACCEPTED_SOURCE_KINDS == frozenset({"erp_v2.ar_invoice_line"})


def test_funding_lane_is_always_available_credit():
    source = (SRC / "ar" / "service.py").read_text()
    assert source.count("funding_lane=") == 1
    assert "SettlementFundingLane.AVAILABLE_CREDIT" in source
    assert "PREPAID_FUNDING" not in source and "prepaid_funding" not in source.lower().replace("prepaid_funding_", "")
    assert SettlementFundingLane.AVAILABLE_CREDIT.value == "available_credit"


# --- source greps -------------------------------------------------------------------------------------------------

R9_COMMANDS = (
    "accept_withholding_discharge",
    "freeze_issued_invoice_withholding",
    "grant_non_cash_credit",
    "apply_non_cash_credit",
    "refund_settlement",
)
BILLING_WRITES = (
    "create_billing_account",
    "accept_rated_obligation",
    "create_draft_document",
    "create_multi_line_draft_document",
    "issue_document",
    "issue_credit_note",
    "void_document",
    "accept_settlement",
    "allocate_settlement",
    "deallocate_settlement",
    "reallocate_settlement",
    "apply_available_credit",
    "reverse_posting_group",
    "reverse_cash_credit_draw",
    "issue_rated_invoice",
    "record_document_artifact",
    "repair_document_artifact",
    "reverse_non_cash_credit_application",
    "reduce_non_cash_credit_grant",
    "reverse_withholding_discharge",
    *R9_COMMANDS,
)


def _sources(*, exclude: tuple[str, ...] = ()):
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if "__pycache__" in path.parts or any(relative.startswith(prefix) for prefix in exclude):
            continue
        yield relative, path.read_text()


def test_before_r9_no_wht_or_non_cash_command_is_called_anywhere():
    """R9 (public ``get_accounting_fact_v2/v3``) is pending: WHT and non-cash stay blocked (3.9)."""
    called = []
    for relative, text in _sources():
        for name in R9_COMMANDS:
            if re.search(rf"\b{name}\s*\(", text) or re.search(rf"import[^\n]*\b{name}\b", text):
                called.append((relative, name))
    assert not called, called


OPENING_IMPORTER_MODULES = ("importer/billing_opening.py", "importer/ar_")  # the opening commands (#72), when merged


def test_only_the_ar_package_and_the_opening_importer_call_or_import_billing_write_commands():
    """Any call, attribute use or import of a billing write command outside ``ar/`` and the opening importer."""
    offenders = []
    for relative, text in _sources(exclude=("ar/", "migrations/", *OPENING_IMPORTER_MODULES)):
        for name in BILLING_WRITES:
            if re.search(rf"\b{name}\b", text):
                offenders.append((relative, name))
    assert not offenders, offenders


# --- validation (database stubbed) --------------------------------------------------------------------------------


def _account(code="4000", *, active=True, postable=True, kind=None):
    from dotmac_accounting.contracts import AccountKind

    return SimpleNamespace(
        id=uuid4(), code=code, is_active=active, posting_allowed=postable, kind=kind or AccountKind.POSTING
    )


def _db_returning(account, *, tax_ids=(), holding_ids=(), control_ids=None, monkeypatch=None):
    db = Mock()
    db.scalar.return_value = account
    if control_ids is None:  # the real resolver finds the fixed codes by code; the stub does the same for the account
        fixed = account is not None and account.code in service.ledger_roles.CONTROL_ACCOUNT_CODES
        control_ids = (account.id,) if fixed else ()
    monkeypatch.setattr(service.ledger_roles, "control_account_ids", lambda db_, tenant: frozenset(control_ids))
    monkeypatch.setattr(service.ledger_roles, "tax_role_account_ids", lambda db_, tenant: {str(i) for i in tax_ids})
    monkeypatch.setattr(service.queries, "holding_account_ids", lambda db_, tenant: set(holding_ids))
    return db


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("2000", "revenue_account_is_control"),
        ("2040", "revenue_account_is_control"),
        ("1410", "revenue_account_is_control"),
        ("1400", "revenue_account_is_control"),
        ("2045", "revenue_account_is_control"),
    ],
)
def test_control_accounts_are_refused_as_revenue(monkeypatch, code, expected):
    db = _db_returning(_account(code), monkeypatch=monkeypatch)
    with pytest.raises(ArRefused) as info:
        service._revenue_account(db, uuid4(), uuid4())
    assert info.value.code == expected


def test_the_resolved_prepayment_account_is_refused_as_revenue_whatever_its_code(monkeypatch):
    prepay = _account("2300")
    db = _db_returning(prepay, control_ids=(prepay.id,), monkeypatch=monkeypatch)
    with pytest.raises(ArRefused) as info:
        service._revenue_account(db, uuid4(), prepay.id)
    assert info.value.code == "revenue_account_is_control"


def test_tax_role_holding_inactive_and_unpostable_accounts_are_refused(monkeypatch):
    tax = _account("1455")
    with pytest.raises(ArRefused) as info:
        service._revenue_account(_db_returning(tax, tax_ids=(tax.id,), monkeypatch=monkeypatch), uuid4(), tax.id)
    assert info.value.code == "revenue_account_is_tax"
    held = _account("4999")
    with pytest.raises(ArRefused) as info:
        service._revenue_account(_db_returning(held, holding_ids=(held.id,), monkeypatch=monkeypatch), uuid4(), held.id)
    assert info.value.code == "revenue_account_is_holding"
    for account, code in (
        (_account(active=False), "revenue_account_not_postable"),
        (_account(postable=False), "revenue_account_not_postable"),
    ):
        with pytest.raises(ArRefused) as info:
            service._revenue_account(_db_returning(account, monkeypatch=monkeypatch), uuid4(), account.id)
        assert info.value.code == code
    with pytest.raises(ArRefused) as info:
        service._revenue_account(_db_returning(None, monkeypatch=monkeypatch), uuid4(), uuid4())
    assert info.value.code == "revenue_account_unknown"
    with pytest.raises(ArRefused) as info:
        service._revenue_account(Mock(), uuid4(), None)
    assert info.value.code == "revenue_account_required"
    ok = _account("4000")
    assert service._revenue_account(_db_returning(ok, monkeypatch=monkeypatch), uuid4(), ok.id) is ok


def _price(monkeypatch, lines, **kw):
    monkeypatch.setattr(service, "_revenue_account", lambda db, tenant, account_id: SimpleNamespace(id=account_id))
    return service._price_lines(
        Mock(), tenant_id=uuid4(), lines=lines, code="NGN", on=date(2026, 1, 12), source_prefix="t", **kw
    )


def _line(**kw):
    values = {"description": "x", "quantity": Decimal(1), "unit_amount": Decimal(10), "revenue_account_id": uuid4()}
    values.update(kw)
    return LineInput(**values)


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"unit_amount": Decimal(-1)}, "negative_line_unsupported"),
        ({"quantity": Decimal(-1)}, "negative_line_unsupported"),
        ({"unit_amount": Decimal(0)}, "invalid_amount"),
        ({"quantity": Decimal("1.0000001")}, "invalid_amount"),
        ({"unit_amount": Decimal("10.005")}, "line_amount_not_exact"),
        ({"description": "  "}, "description_required"),
        ({"unit_amount": Decimal("NaN")}, "invalid_amount"),
    ],
)
def test_line_validation_refusals(monkeypatch, kw, code):
    with pytest.raises(ArRefused) as info:
        _price(monkeypatch, [_line(**kw)])
    assert info.value.code == code


def test_empty_and_oversized_documents_are_refused_and_exact_lines_price(monkeypatch):
    with pytest.raises(ArRefused) as info:
        _price(monkeypatch, [])
    assert info.value.code == "no_lines"
    with pytest.raises(ArRefused) as info:
        _price(monkeypatch, [_line()] * (constants.MAX_LINES + 1))
    assert info.value.code == "too_many_lines"
    priced = _price(monkeypatch, [_line(quantity=Decimal("2.5"), unit_amount=Decimal("4.20"))])
    assert (priced[0].pre_tax, priced[0].tax, priced[0].snapshot) == (Decimal("10.500"), 0, None)


def _periods(*periods):
    db = Mock()
    db.scalars.return_value = list(periods)
    return db


def _period(status="OPEN", adjustment=False):
    from dotmac_accounting.contracts import PeriodStatus

    return SimpleNamespace(status=PeriodStatus(status), is_adjustment=adjustment)


def test_future_date_closed_missing_and_adjustment_periods_are_refused():
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    day = date(2026, 1, 12)
    service._require_postable_date(_periods(_period()), uuid4(), day, now)  # open: fine
    for db, when, code in (
        (_periods(_period()), date(2026, 1, 17), "date_future"),
        (_periods(), day, "period_missing"),
        (_periods(_period(), _period()), day, "period_missing"),
        (_periods(_period("SOFT_CLOSED")), day, "period_closed"),
        (_periods(_period("LOCKED")), day, "period_closed"),
        (_periods(_period("REOPENED")), day, "period_closed"),
        (_periods(_period(adjustment=True)), day, "period_adjustment_only"),
    ):
        with pytest.raises(ArRefused) as info:
            service._require_postable_date(db, uuid4(), when, now)
        assert info.value.code == code, code


def test_the_lagos_date_decides_future():
    late_utc = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)  # already the 16th in Lagos
    service._require_postable_date(_periods(_period()), uuid4(), date(2026, 1, 16), late_utc)


def test_fx_is_refused_at_issue(monkeypatch):
    monkeypatch.setattr(service.settings, "load", lambda db, tenant: _complete_settings())
    monkeypatch.setattr(service, "_functional", lambda db, tenant: "NGN")
    draft = SimpleNamespace(currency="USD", id=uuid4())
    with pytest.raises(ArRefused) as info:
        service._issue_invoice(
            Mock(), tenant=SimpleNamespace(id=uuid4()), party=Mock(), draft=draft, now=datetime.now(UTC)
        )
    assert info.value.code == "fx_unsupported"


def _complete_settings(**over):
    values = dict(
        series_invoice="INV",
        series_credit_note="CN",
        series_receipt="RCT",
        terms={"net_30": 30},
        default_terms_code="net_30",
        default_revenue_account=None,
        seller={"legal_name": "S", "address_line": "A"},
        payment_instructions={"method_code": "bank_transfer"},
        profile_code="erp_v2.invoice",
        profile_version="1",
        auto_apply=True,
    )
    values.update(over)
    return settings.ArSettings(**values)


# --- terms: the VERIFIED due-date basis ----------------------------------------------------------------------------


def test_the_due_date_basis_is_verified_and_complete():
    basis = terms.net_days_basis(
        invoice_date=date(2026, 1, 12), terms_code="net_30", terms_version="v7", evidence_ref="erp_v2.ar.draft:x"
    )
    assert basis.status is DueDateBasisStatus.VERIFIED and basis.automated_collection_allowed
    for field in (
        "payment_terms_code",
        "payment_terms_version",
        "issued_at",
        "effective_at",
        "timezone",
        "derivation_policy",
        "derivation_version",
    ):
        assert getattr(basis, field) is not None, field
    assert (basis.derivation_policy, basis.derivation_version) == ("erp_v2.ar.net_days", "1")
    assert basis.issued_at == basis.effective_at == datetime(2026, 1, 11, 23, tzinfo=UTC)  # 00:00 Africa/Lagos
    assert terms.due_at(date(2026, 1, 12), 30) == datetime(2026, 2, 10, 23, tzinfo=UTC)
    assert basis.issued_at.utcoffset().total_seconds() == 0  # UTC: billing seals over the ISO rendering


# --- settings ------------------------------------------------------------------------------------------------------


def test_setup_gaps_name_the_missing_key():
    assert _complete_settings().missing() == []
    gaps = settings.load.__annotations__  # the dataclass is the contract
    assert gaps
    incomplete = _complete_settings(
        series_invoice=None, terms={}, default_terms_code="nope", seller={}, payment_instructions={}
    )
    assert incomplete.missing() == [
        settings.KEY_SERIES_INVOICE,
        settings.KEY_TERMS,
        settings.KEY_DEFAULT_TERMS,
        settings.KEY_SELLER,
        settings.KEY_PAYMENT_INSTRUCTIONS,
    ]
    assert _complete_settings(default_terms_code="net_90").missing() == [settings.KEY_DEFAULT_TERMS]


def test_producer_is_off_by_default_and_the_specs_are_the_spec_keys():
    by_key = {spec.key: spec for spec in settings.SPECS}
    assert by_key["ar.producer.enabled"].default is False
    assert by_key["ar.auto_apply_credit_on_issue"].default is True
    assert by_key["ar.document_profile_code"].default == "erp_v2.invoice"
    assert by_key["ar.default_revenue_account"].default is None  # pre-fill only, never a mapper fallback
    assert set(by_key) == {
        "ar.producer.enabled",
        "ar.series.invoice",
        "ar.series.credit_note",
        "ar.series.receipt",
        "ar.payment_terms",
        "ar.default_payment_terms_code",
        "ar.default_revenue_account",
        "ar.seller_snapshot",
        "ar.payment_instructions",
        "ar.document_profile_code",
        "ar.document_profile_version",
        "ar.auto_apply_credit_on_issue",
    }
    assert all(spec.inherits is False for spec in settings.SPECS)


def test_a_disabled_producer_refuses_without_touching_the_database(monkeypatch):
    monkeypatch.setattr(service.settings, "producer_enabled", lambda db, tenant_id: False)
    db = Mock()
    tenant, party = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4(), tenant_id=None)
    party.tenant_id = tenant.id
    work = Mock()
    with pytest.raises(ArRefused) as info:
        service._guard(db, tenant=tenant, party=party, entity_id="x", action="issue", work=work)
    assert info.value.code == "ar_producer_disabled" and not info.value.audit
    work.assert_not_called()
    assert db.method_calls == []  # no savepoint, no audit row, nothing written


def test_a_party_from_another_tenant_is_refused_before_anything_else(monkeypatch):
    monkeypatch.setattr(service.settings, "producer_enabled", lambda db, tenant_id: True)
    tenant, party = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    with pytest.raises(ArRefused) as info:
        service._guard(Mock(), tenant=tenant, party=party, entity_id="x", action="issue", work=Mock())
    assert info.value.code == "actor_tenant"


# --- D-9 -----------------------------------------------------------------------------------------------------------


def test_dual_control_stamp_on_refuses_the_preparer_off_allows_them():
    person = SimpleNamespace(id=uuid4())
    other = SimpleNamespace(id=uuid4())
    on = SimpleNamespace(dual_control=True, prepared_by=person.id)
    off = SimpleNamespace(dual_control=False, prepared_by=person.id)
    with pytest.raises(ArRefused) as info:
        service._require_dual_stamp(on, person)
    assert info.value.code == "same_person"
    service._require_dual_stamp(on, other)
    service._require_dual_stamp(off, person)


def test_live_policy_governs_void_apply_deallocate_and_reverse(monkeypatch):
    person, other = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    tenant = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(service, "_dual_control_live", lambda db, tenant_id: True)
    with pytest.raises(ArRefused) as info:
        service._require_other_person(Mock(), tenant, person, person.id)
    assert info.value.code == "same_person"
    service._require_other_person(Mock(), tenant, other, person.id)
    service._require_other_person(Mock(), tenant, person, None)  # a migrated document has no ERP issuer
    monkeypatch.setattr(service, "_dual_control_live", lambda db, tenant_id: False)
    service._require_other_person(Mock(), tenant, person, person.id)


# --- access --------------------------------------------------------------------------------------------------------


def test_receivables_permissions_are_declared_for_the_finance_roles_only():
    declared = {spec.code: set(spec.default_roles) for spec in access.PERMISSION_SPECS}
    assert declared[routes.READ] == {"finance_preparer", "finance_approver"}  # a poster can review a draft
    assert not {"admin", "staff"} & declared[routes.READ]
    assert access.PROFILE_VERSION == 6
    assert declared[routes.PREPARE] == {"finance_preparer"}
    assert declared[routes.POST] == {"finance_approver"}
    assert not {"admin", "staff"} & (declared[routes.PREPARE] | declared[routes.POST])
    names = list(access._NEW_PERMISSIONS)
    assert names.index("erp.finance.receivables.prepare") == names.index("erp.finance.supplier_payments.approve") + 1
    assert names.index("erp.finance.receivables.post") == names.index("erp.finance.receivables.prepare") + 1
    grants = {(g.role_code, g.permission_code) for p in access.ROLE_GRANT_PROFILES for g in p.grants}
    assert ("finance_preparer", routes.PREPARE) in grants and ("finance_approver", routes.POST) in grants


def test_every_route_is_in_the_permission_table_and_guarded_by_it():
    found = {}
    for route in routes.router.routes:
        perms = set()
        for dep in route.dependant.dependencies:
            perms.update(c.cell_contents for c in (dep.call.__closure__ or []) if isinstance(c.cell_contents, str))
        found[route.endpoint.__name__] = perms & {routes.ACCESS, routes.PREPARE, routes.POST, routes.READ}
    assert set(found) == set(routes.ROUTE_PERMISSIONS)
    for name, permission in routes.ROUTE_PERMISSIONS.items():
        assert found[name] == {permission}, name
    posts = {r.endpoint.__name__ for r in routes.router.routes if "POST" in r.methods}
    assert not {n for n in posts if routes.ROUTE_PERMISSIONS[n] == routes.ACCESS}  # reading never writes


def test_the_routes_are_under_finance_receivables_and_the_spec_surface_exists():
    paths = {(sorted(r.methods)[0], r.path) for r in routes.router.routes}
    base = routes.BASE
    for method, path in (
        ("GET", f"{base}/invoices/new"),
        ("POST", f"{base}/invoices/new"),
        ("GET", f"{base}/drafts/{{draft_id}}"),
        ("POST", f"{base}/drafts/{{draft_id}}"),
        ("POST", f"{base}/drafts/{{draft_id}}/discard"),
        ("POST", f"{base}/drafts/{{draft_id}}/issue"),
        ("GET", f"{base}/documents/{{document_id}}"),
        ("POST", f"{base}/documents/{{document_id}}/void"),
        ("GET", f"{base}/credit-notes/new"),
        ("POST", f"{base}/credit-notes/new"),
        ("GET", f"{base}/receipts/new"),
        ("POST", f"{base}/receipts/new"),
        ("GET", f"{base}/receipts/{{draft_id}}"),
        ("POST", f"{base}/receipts/{{draft_id}}/post"),
        ("POST", f"{base}/receipts/{{draft_id}}/reverse"),
        ("POST", f"{base}/accounts/{{billing_account_id}}/apply-credit"),
        ("POST", f"{base}/allocations/{{allocation_id}}/deallocate"),
    ):
        assert (method, path) in paths, (method, path)
    assert all(path.startswith("/finance/receivables") for _, path in paths)


def test_the_feature_is_registered_after_the_revenue_feature_and_declares_its_source():
    from erp_v2.features import ERP_FEATURES

    names = [f.name for f in ERP_FEATURES]
    assert names.index("erp_receivables_producer") == names.index("erp_revenue") + 1
    feature = routes.receivables_producer_feature
    assert tuple(feature.obligation_sources) == (constants.SOURCE_KIND,)
    assert str(settings.DOMAIN) in tuple(feature.setting_domains)
    assert set(service.AUDIT_ACTIONS) <= set(feature.audit_actions)


def test_templates_exist_and_the_register_links_to_them():
    folder = SRC / "templates" / "erp_v2" / "receivables"
    assert {p.name for p in folder.glob("*.html")} >= {
        "invoice_form.html",
        "credit_note_form.html",
        "receipt_form.html",
        "receipt.html",
        "document.html",
        "drafts.html",
    }
    register = (SRC / "templates" / "erp_v2" / "revenue" / "receivables.html").read_text()
    assert "unavailable" not in register and "/finance/receivables/invoices/new" in register
    assert "Legacy credit notes are cleared by R8; do not re-issue" in (folder / "credit_note_form.html").read_text()


# --- routes: matrix, CSRF, actor wiring, refusal rendering ----------------------------------------------------------

PRINCIPALS = {
    "staff": ("staff",),
    "admin": ("admin",),
    "preparer": ("staff", "finance_preparer"),
    "approver": ("staff", "finance_approver"),
    "treasury": ("staff", "treasury"),
    "preparer_nostaff": ("finance_preparer",),
    "approver_nostaff": ("finance_approver",),
}
ID = uuid4()
FORM = {
    "request_id": str(uuid4()),
    "customer_account_id": str(uuid4()),
    "document_date": "2026-01-12",
    "line_description": "Install",
    "line_quantity": "1",
    "line_unit_amount": "100",
    "line_revenue_account_id": str(uuid4()),
    "line_tax_code_id": "",
    "original_document_id": str(uuid4()),
    "reason": "r",
    "document_id": str(uuid4()),
    "amount": "10",
    "occurred_on": "2026-01-12",
    "bank_account_id": str(uuid4()),
    "bank_reference": "REF-1",
}
B = routes.BASE
# action -> (path, principals admitted, spied service function)
ACTIONS = {
    "invoice_create": (f"{B}/invoices/new", {"preparer"}, "create_draft"),
    "draft_save": (f"{B}/drafts/{ID}", {"preparer"}, "update_draft"),
    "draft_discard": (f"{B}/drafts/{ID}/discard", {"preparer"}, "discard_draft"),
    "draft_issue": (f"{B}/drafts/{ID}/issue", {"approver"}, "issue_draft"),
    "document_void": (f"{B}/documents/{ID}/void", {"approver"}, "void_document"),
    "credit_note_create": (f"{B}/credit-notes/new", {"preparer"}, "create_draft"),
    "receipt_create": (f"{B}/receipts/new", {"preparer"}, "create_draft"),
    "receipt_post": (f"{B}/receipts/{ID}/post", {"approver"}, "post_receipt"),
    "receipt_reverse": (f"{B}/receipts/{ID}/reverse", {"approver"}, "reverse_receipt"),
    "apply_credit": (f"{B}/accounts/{ID}/apply-credit", {"approver"}, "apply_credit"),
    "deallocate": (f"{B}/allocations/{ID}/deallocate", {"approver"}, "deallocate"),
}
SPIED = sorted({spy for _, _, spy in ACTIONS.values()})


class Browser:
    def __init__(self, client, tenant, parties, tokens, spies):
        self.client, self.tenant, self.parties, self.tokens, self.spies = client, tenant, parties, tokens, spies

    def sign_in(self, who):
        self.client.cookies.clear()
        if who is not None:
            self.client.cookies.set("access_token", self.tokens[who])
        assert self.client.get("/login").status_code == 200
        token = self.client.cookies.get("csrf_token")
        assert token
        return token


@pytest.fixture()
def browser(monkeypatch):
    monkeypatch.setattr(kernel_settings, "csrf_enabled", True)
    monkeypatch.setattr(kernel_settings, "platform_root_domain", "erp.test")
    monkeypatch.setattr(kernel_settings, "trusted_hosts", "acme.erp.test")
    monkeypatch.setattr(kernel_settings, "rate_limit_requests", 10000)
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[m.__table__ for m in (Tenant, TenantDomain, Party, Role, PartyRoleGrant, AuthSession, DomainSetting)],
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
        spies = {name: Mock(return_value=Outcome(ID, False, document_id=ID, settlement_id=ID)) for name in SPIED}
        for name, spy in spies.items():
            monkeypatch.setattr(routes.service, name, spy)
        client = TestClient(app, base_url="http://acme.erp.test", follow_redirects=False)
        try:
            yield Browser(client, tenant, parties, tokens, spies)
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture(autouse=True)
def _known_draft(monkeypatch):
    draft = SimpleNamespace(
        id=ID,
        kind="invoice",
        original_document_id=None,
        bank_account_id=None,
        bank_reference=None,
        amount=None,
        reason=None,
    )
    monkeypatch.setattr(routes.queries, "draft", lambda db, tenant_id, draft_id: draft)


def _post(browser, path, token, **data):
    return browser.client.post(path, data={"csrf_token": token, **FORM, **data})


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("who", PRINCIPALS)
def test_each_action_is_admitted_only_for_its_named_role_with_the_authenticated_actor(browser, action, who):
    path, allowed, spy_name = ACTIONS[action]
    token = browser.sign_in(who)
    response = _post(browser, path, token)
    if who in allowed:
        assert response.status_code in (200, 303, 404, 400), (action, who, response.status_code)
        spy = browser.spies[spy_name]
        if response.status_code in (303, 200):
            spy.assert_called_once()
            kwargs = spy.call_args.kwargs
            assert kwargs["tenant"].id == browser.tenant.id
            assert kwargs["party"].id == browser.parties[who].id  # the guard's party, never request data
    else:
        assert response.status_code == 403, (action, who)
        for spy in browser.spies.values():
            spy.assert_not_called()


@pytest.mark.parametrize("who", ["staff", "admin", "treasury", "preparer_nostaff", "approver_nostaff"])
def test_dormant_until_a_named_finance_role_holds_the_power(browser, who):
    token = browser.sign_in(who)
    for path, _, _ in ACTIONS.values():
        assert _post(browser, path, token).status_code in (302, 403)
    for spy in browser.spies.values():
        spy.assert_not_called()


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("proof", ["missing", "mismatch", "foreign-origin"])
def test_invalid_csrf_never_reaches_the_service(browser, action, proof):
    path, allowed, _ = ACTIONS[action]
    token = browser.sign_in(sorted(allowed)[0])
    headers = {}
    if proof == "mismatch":
        headers["x-csrf-token"] = token + "x"
    elif proof == "foreign-origin":
        headers.update({"x-csrf-token": token, "origin": "https://other.example"})
    assert browser.client.post(path, headers=headers, data=FORM).status_code == 403
    for spy in browser.spies.values():
        spy.assert_not_called()


def test_a_disabled_producer_refusal_is_rendered_and_nothing_else_is_called(browser, monkeypatch):
    token = browser.sign_in("approver")
    browser.spies["issue_draft"].side_effect = ArRefused("ar_producer_disabled", "not enabled", audit=False)
    monkeypatch.setattr(
        routes, "_draft_page", lambda request, db, tenant, party, draft_id, error=None: _refusal_page(error)
    )
    response = _post(browser, f"{B}/drafts/{ID}/issue", token)
    assert response.status_code == 200 and "ar_producer_disabled" in response.text
    browser.spies["issue_draft"].assert_called_once()


def _refusal_page(error):
    from fastapi.responses import HTMLResponse

    return HTMLResponse(f"<p role='alert'>{error}</p>")


# --- migration module ----------------------------------------------------------------------------------------------


def test_migration_revision_chain_and_head():
    from erp_v2.migrate import EXPECTED_HEADS
    from erp_v2.migrations.versions import e2_0032_ar_producer as migration

    assert migration.revision == "e2_0032_ar_producer"
    assert migration.down_revision == "e2_0031_billing_fact_postings"
    assert "e2_0032_ar_producer" not in EXPECTED_HEADS  # e2_0033 (B-15 rev 2 VAT) chains after it
    assert "e2_0033_billing_gl_vat" in EXPECTED_HEADS
    assert not any(h.startswith("e2_003") and h != "e2_0033_billing_gl_vat" for h in EXPECTED_HEADS)  # one e2 head


def test_the_gate_check_module_is_read_only():
    text = (SRC / "ar" / "checks.py").read_text()
    assert not re.search(r"\b(add|delete|execute|commit|flush)\(", text)


# --- review fixes ------------------------------------------------------------------------------------------------


def test_a_multi_line_credit_note_is_refused_until_the_mapper_compares_sums():
    service._require_single_credit_line([1])
    with pytest.raises(ArRefused) as info:
        service._require_single_credit_line([1, 2])
    assert info.value.code == "credit_note_single_line_only"


def test_d9_on_locks_a_draft_to_its_preparer_off_does_not():
    mine, other = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    on = SimpleNamespace(dual_control=True, prepared_by=mine.id)
    off = SimpleNamespace(dual_control=False, prepared_by=mine.id)
    service._require_preparer_when_dual(on, mine)
    service._require_preparer_when_dual(off, other)
    with pytest.raises(ArRefused) as info:
        service._require_preparer_when_dual(on, other)
    assert info.value.code == "not_preparer"


def test_every_dated_action_requires_an_open_period(monkeypatch):
    seen = []
    monkeypatch.setattr(service, "_require_postable_date", lambda db, tenant, day, now: seen.append(day))
    at = service._action_time(Mock(), uuid4(), date(2026, 1, 12), datetime(2026, 1, 15, tzinfo=UTC))
    assert seen == [date(2026, 1, 12)] and at == terms.midnight(date(2026, 1, 12))
    monkeypatch.undo()
    with pytest.raises(ArRefused) as info:
        service._action_time(
            _periods(_period("SOFT_CLOSED")), uuid4(), date(2026, 1, 12), datetime(2026, 1, 15, tzinfo=UTC)
        )
    assert info.value.code == "period_closed"


def test_a_row_of_an_opening_bearing_draw_cannot_be_deallocated_alone(monkeypatch):
    monkeypatch.setattr(service, "_functional", lambda db, tenant: "NGN")
    monkeypatch.setattr(service, "_action_time", lambda *a, **k: terms.midnight(date(2026, 1, 12)))
    monkeypatch.setattr(service.queries, "posting_group_for_source", lambda *a: None)
    monkeypatch.setattr(service, "_is_opening_draw", lambda db, group_id: True)
    row = SimpleNamespace(document_id=uuid4(), amount_delta=Decimal(5), posting_group_id=uuid4())
    db = Mock()
    db.scalar.return_value = row
    with pytest.raises(ArRefused) as info:
        service._deallocate(
            db,
            tenant=SimpleNamespace(id=uuid4()),
            party=Mock(),
            allocation_id=uuid4(),
            occurred_on=date(2026, 1, 12),
            request_id=uuid4(),
            now=datetime.now(UTC),
        )
    assert info.value.code == "opening_draw_unit"


def test_the_f_d_notice_is_a_structural_sql_filter_with_no_truncation():
    text = (SRC / "ar" / "queries.py").read_text()
    body = text[text.index("def credit_note_notice") :]
    assert "LIMIT" not in body.upper() and "limit(" not in body
    assert "ar" in body and "detail::jsonb -> 'party' ->> 0" in body and "class_code = 'E35'" in body


def test_the_deallocate_key_carries_the_allocation_and_the_request():
    a, b, r = uuid4(), uuid4(), uuid4()
    assert service.key_dealloc(a, r) != service.key_dealloc(b, r) != service.key_dealloc(a, uuid4())


def test_manual_basis_is_verified_and_names_the_manual_policy():
    basis = terms.manual_basis(
        invoice_date=date(2026, 1, 12), terms_code="net_30", terms_version="v1", evidence_ref="e", actor_ref="p"
    )
    assert basis.status is DueDateBasisStatus.VERIFIED and basis.derivation_policy == "erp_v2.ar.manual_due_date"
    assert basis.override_actor == "p" and basis.override_reason


def test_the_customer_screen_seams_exist_and_default_to_the_tenant_terms(monkeypatch):
    cfg = _complete_settings()
    assert service.default_terms_for_customer(Mock(), uuid4(), uuid4(), cfg) == "net_30"
    # resolved at create/update and stored; issue reads only the stored code
    monkeypatch.setattr(service.settings, "load", lambda db_, tenant: cfg)
    assert service.resolve_terms_code(Mock(), uuid4(), service.KIND_INVOICE, uuid4(), None) == "net_30"
    assert service.resolve_terms_code(Mock(), uuid4(), service.KIND_INVOICE, uuid4(), "net_60") == "net_60"
    assert service.resolve_terms_code(Mock(), uuid4(), "credit_note", uuid4(), None) is None
    assert service._stored_terms_code(SimpleNamespace(payment_terms_code="net_30"), cfg) == "net_30"
    for bad, refusal in (
        (None, "payment_terms_missing"),
        ("", "payment_terms_missing"),
        ("x", "payment_terms_unknown"),
    ):
        with pytest.raises(service.ArRefused) as e:
            service._stored_terms_code(SimpleNamespace(payment_terms_code=bad), cfg)
        assert e.value.code == refusal
    snap = service._customer_document_snapshot(Mock(), uuid4(), SimpleNamespace(display_name="Acme"))
    assert snap.legal_name == "Acme"
