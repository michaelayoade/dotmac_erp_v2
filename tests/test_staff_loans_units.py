"""Staff loan entries: pure checks (no database): policy, consequence shape, manifest, migration identity."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from erp_v2.features import ERP_FEATURES
from erp_v2.migrations.versions import e2_0023_staff_loan_entries as migration
from erp_v2.posting import rules
from erp_v2.staff_loans import models, service

FP = "b" * 64


def _consequence(*, debit_loan=True, amount="100"):
    a, b = str(uuid4()), str(uuid4())
    d, c = Decimal(amount), Decimal(0)
    lines = (
        service._Line(a, d if debit_loan else c, c if debit_loan else d, "x"),
        service._Line(b, c if debit_loan else d, d if debit_loan else c, "x"),
    )
    source = service._Source("erp", "staff_loan_entry", str(uuid4()), "1", FP)
    return service._Consequence(source, date(2026, 1, 12), "Staff loan advance E [ref]", "NGN", Decimal(1), lines, FP)


def test_the_erp_staff_loan_kind_has_one_declared_policy_with_uuid_refs_only():
    policy = rules.policy_for("erp", "staff_loan_entry")
    assert policy.code_refs == frozenset()
    with pytest.raises(rules.PostingRefused) as exc:
        rules.policy_for("erp", "something_else")
    assert exc.value.code == "unknown_consequence_kind"


@pytest.mark.parametrize("debit_loan", [True, False])
def test_the_staff_loan_consequence_shape_passes_the_bridge_validation(debit_loan):
    assert rules.validate(_consequence(debit_loan=debit_loan)) == rules.RefPolicy()


def test_a_code_typed_account_ref_is_refused_for_the_staff_loan_kind():
    c = _consequence()
    bad = service._Line("1410", Decimal(100), Decimal(0), "x")
    forged = service._Consequence(c.source, c.posting_date, c.description, "NGN", Decimal(1), (bad, c.entries[1]), FP)
    with pytest.raises(rules.PostingRefused) as exc:
        rules.validate(forged)
    assert exc.value.code == "account_ref_form_refused"


def test_the_idempotency_key_stays_within_the_kernel_limit():
    c = _consequence()
    assert len(rules.idempotency_key(c.source, "create")) <= rules.MAX_KEY_LENGTH


def test_the_feature_manifest_registers_the_audit_actions_and_no_routes():
    feature = next(f for f in ERP_FEATURES if f.name == "erp_staff_loans")
    assert tuple(feature.audit_actions) == service.AUDIT_ACTIONS
    assert not feature.web_routers
    assert len(set(service.AUDIT_ACTIONS)) == 4


def test_the_migration_takes_a_distinct_revision_id_after_e2_0021():
    assert migration.revision == "e2_0023_staff_loan_entries"
    assert migration.down_revision == "e2_0021_payroll_payout_returns"
    assert migration.revision != "e2_0022_dual_control_policy"  # PR-4 owns e2_0022


def test_the_runtime_role_updates_lifecycle_columns_only():
    assert set(models.UPDATE_COLUMNS).isdisjoint(migration._IMMUTABLE)
    assert {"amount", "kind", "employee_ref", "tenant_id"} <= set(migration._IMMUTABLE)
