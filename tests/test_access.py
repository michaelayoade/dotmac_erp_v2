"""Role/permission declarations: the new powers exist only for the named roles."""

from erp_v2 import access
from erp_v2.assembly import assembly
from erp_v2.web.payroll_routes import PAYROLL_MANAGE_PERMISSION
from erp_v2.web.revenue_routes import WRITE_PERMISSION
from erp_v2.web.routes import ACCESS_PERMISSION

BEFORE_ADMIN = {ACCESS_PERMISSION, WRITE_PERMISSION, PAYROLL_MANAGE_PERMISSION}


def profile():
    (p,) = assembly.role_grant_profiles
    return p


def grants(role):
    return {g.permission_code for g in profile().grants if g.role_code == role}


def test_profile_is_erp_web_v4():
    assert (profile().code, profile().version) == ("erp.web", 4)


def test_admin_and_staff_get_nothing_new():
    assert grants("admin") == BEFORE_ADMIN
    assert grants("staff") == {ACCESS_PERMISSION}


def test_no_new_permission_defaults_to_admin_or_staff():
    for spec in access.PERMISSION_SPECS:
        assert len(spec.default_roles) >= 1
        assert not {"admin", "staff"} & set(spec.default_roles), spec.code


def test_each_new_permission_is_granted_to_exactly_its_role_and_declared_once():
    holders = {}
    for g in profile().grants:
        holders.setdefault(g.permission_code, set()).add(g.role_code)
    declared = [s.code for m in assembly.modules for s in getattr(m, "permissions", ())]
    for spec in access.PERMISSION_SPECS:
        assert holders[spec.code] == set(spec.default_roles)
        assert declared.count(spec.code) == 1


def test_new_roles_defined_empty_and_hold_only_their_declared_powers():
    codes = {r.code for r in assembly.role_definitions}
    assert codes == {"admin", "staff", *access.NEW_ROLES}
    for role in access.NEW_ROLES:
        # No portal entry grant: runtime authorization would ignore it (default_roles).
        assert ACCESS_PERMISSION not in grants(role)
        assert grants(role) <= access.NEW_PERMISSION_CODES
    for role in ("admin", "staff"):
        assert not next(r for r in assembly.role_definitions if r.code == role).create_if_missing


def test_profile_roles_equal_spec_default_roles_for_every_new_code():
    for spec in access.PERMISSION_SPECS:
        granted = {g.role_code for g in profile().grants if g.permission_code == spec.code}
        assert granted == set(spec.default_roles), spec.code


def test_every_new_code_in_the_profile_is_a_declared_spec_with_matching_roles():
    specs = {s.code: set(s.default_roles) for s in access.PERMISSION_SPECS}
    legacy = {ACCESS_PERMISSION, WRITE_PERMISSION, PAYROLL_MANAGE_PERMISSION}
    for code in {g.permission_code for g in profile().grants} - legacy:
        assert code in specs, code
        assert {g.role_code for g in profile().grants if g.permission_code == code} == specs[code]


def test_sod_pairs_reference_only_new_roles_and_audit_action_is_declared():
    for pair in access.SOD_CONFLICT_PAIRS:
        assert len(pair) == 2 and pair <= set(access.NEW_ROLES)
    assert access.AUDIT_ROLE_GRANTED in access.access_feature.audit_actions


def test_segregation_roles_do_not_overlap_on_the_critical_pairs():
    approve_post, finalize = "erp.payroll.runs.approve", "erp.payroll.runs.finalize"
    for role in access.NEW_ROLES:
        assert not {approve_post, finalize} <= grants(role)
    assert "erp.finance.journals.prepare" not in grants("finance_approver")
    assert "erp.finance.journals.approve" not in grants("finance_preparer")


def test_feature_registry_order_is_canonical():
    from erp_v2.features import ERP_FEATURES

    assert ERP_FEATURES[0].name == "erp_shell.readiness"
    assert [m.name for m in assembly.modules[: len(ERP_FEATURES)]] == [m.name for m in ERP_FEATURES]


PAYEE_PERMISSIONS = set(access.PAYEE_BANK_CODES)


def holders_of(code):
    return {g.role_code for g in profile().grants if g.permission_code == code}


def test_payee_bank_permissions_declared_once_and_admin_staff_gain_nothing():
    assert PAYEE_PERMISSIONS <= access.NEW_PERMISSION_CODES
    declared = [s.code for m in assembly.modules for s in getattr(m, "permissions", ())]
    for code in PAYEE_PERMISSIONS:
        assert declared.count(code) == 1
        assert not {"admin", "staff"} & holders_of(code), code
    assert grants("admin") == BEFORE_ADMIN and grants("staff") == {ACCESS_PERMISSION}


def test_treasury_holds_no_payee_bank_power_beyond_masked_read():
    assert grants("treasury") & PAYEE_PERMISSIONS == {access.PAYEE_BANK_READ}
    for code in PAYEE_PERMISSIONS - {access.PAYEE_BANK_READ}:
        assert "treasury" not in holders_of(code), code


def test_payee_bank_propose_and_approve_are_different_roles_that_cannot_be_combined():
    proposers, approvers = holders_of(access.PAYEE_BANK_PROPOSE), holders_of(access.PAYEE_BANK_APPROVE)
    assert proposers == {"finance_preparer"} and approvers == {"finance_approver"}
    assert not proposers & approvers
    # one person may not hold propose+approve, nor treasury+approve (role SoD pairs)
    assert frozenset({"finance_preparer", "finance_approver"}) in access.SOD_CONFLICT_PAIRS
    assert frozenset({"finance_approver", "treasury"}) in access.SOD_CONFLICT_PAIRS


def test_every_role_holding_approve_conflicts_with_treasury_and_with_propose_roles():
    for approver in holders_of(access.PAYEE_BANK_APPROVE):
        for other in (*holders_of(access.PAYEE_BANK_PROPOSE), "treasury"):
            assert frozenset({approver, other}) in access.SOD_CONFLICT_PAIRS, (approver, other)


def test_only_approver_sees_unmasked_numbers_and_revokes():
    assert holders_of(access.PAYEE_BANK_READ_UNMASKED) == {"finance_approver"}
    assert holders_of(access.PAYEE_BANK_REVOKE) == {"finance_approver"}
    assert holders_of(access.PAYEE_BANK_READ) == {"finance_preparer", "finance_approver", "treasury"}
