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


def test_profile_is_erp_web_v3():
    assert (profile().code, profile().version) == ("erp.web", 3)


def test_admin_and_staff_get_nothing_new():
    assert grants("admin") == BEFORE_ADMIN
    assert grants("staff") == {ACCESS_PERMISSION}


def test_no_new_permission_defaults_to_admin_or_staff():
    for spec in access.PERMISSION_SPECS:
        assert len(spec.default_roles) == 1
        assert not {"admin", "staff"} & set(spec.default_roles), spec.code


def test_each_new_permission_is_granted_to_exactly_its_role_and_declared_once():
    holders = {}
    for g in profile().grants:
        holders.setdefault(g.permission_code, set()).add(g.role_code)
    declared = [s.code for m in assembly.modules for s in getattr(m, "permissions", ())]
    for spec in access.PERMISSION_SPECS:
        assert holders[spec.code] == set(spec.default_roles)
        assert declared.count(spec.code) == 1


def test_new_roles_defined_empty_and_sign_in_only_plus_their_powers():
    codes = {r.code for r in assembly.role_definitions}
    assert codes == {"admin", "staff", *access.NEW_ROLES}
    for role in access.NEW_ROLES:
        assert ACCESS_PERMISSION in grants(role)
        assert grants(role) - {ACCESS_PERMISSION} <= access.NEW_PERMISSION_CODES
    for role in ("admin", "staff"):
        assert not next(r for r in assembly.role_definitions if r.code == role).create_if_missing


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
