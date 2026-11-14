"""Independent payroll diagnostics never confer action authority."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest

from erp_v2.importer import payroll_workflow_preflight as preflight

REVISION = "a" * 40
TENANT, PERSON, EMPLOYEE, RUN_ID, OTHER = (str(UUID(int=n)) for n in range(1, 6))
START, END = date(2026, 11, 1), date(2026, 11, 30)


def complete():
    return preflight.Observations(
        run=preflight.Run(TENANT, RUN_ID, START, END, PERSON),
        employees=(preflight.Employee(EMPLOYEE, "ACTIVE"),),
        assignments=(preflight.Assignment(EMPLOYEE, START, None),),
        identities=(preflight.Identity(PERSON, PERSON, True, True, source_roles=("admin",), target_roles=("admin",)),),
        policy=preflight.AuthorityPolicy(("admin",), True, True, REVISION),
        destination_employees=(EMPLOYEE,),
        source_slip_creators=(PERSON,),
        destination_creator=PERSON,
        source_creator_party=PERSON,
        auto_post_gl=False,
        gl_provenance="organization",
        imported_run=True,
    )


def evaluate(value):
    return preflight.evaluate(value, candidate_revision=REVISION)


def test_complete_observations_still_cannot_enable_workflow():
    receipt = evaluate(complete())
    assert receipt["authority_equivalence"] == "proven"
    assert receipt["roster_completeness"] == "proven"
    assert receipt["approval_effect_dependencies"] == "resolved"
    assert receipt["segregation_of_duties_equivalence"] == "proven"
    assert receipt["historical_admission"] == "BLOCKED"
    assert receipt["workflow_enabled"] is False
    assert receipt["production_ready"] is False
    assert receipt["evidence_is_authorization"] is False
    assert "not_original_frozen_cohort" in receipt["roster_scope"]


@pytest.mark.parametrize(
    "roles,scopes",
    [
        (("admin",), ()),
        (("hr_manager",), ()),
        (("hr_director",), ()),
        (("payroll_admin",), ()),
        (("payroll_approver",), ()),
        ((), ("hr:access",)),
        ((), ("hr:schedule:read",)),
    ],
)
def test_source_access_alternatives(roles, scopes):
    identity = preflight.Identity(PERSON, PERSON, True, True, roles, scopes)
    assert preflight.source_access(identity)
    assert not preflight.source_access(replace(identity, active_member=False))


def test_catalogue_required_and_stale_catalogue_refused():
    value = complete()
    for policy in (
        replace(value.policy, payroll_default_roles=None),
        replace(value.policy, candidate_revision="b" * 40),
    ):
        receipt = evaluate(replace(value, policy=policy))
        assert receipt["authority_equivalence"] == "indeterminate"
        assert "candidate_permission_spec_unobserved" in receipt["authority_reasons"]


def test_known_access_difference():
    value = complete()
    identity = replace(value.identities[0], source_roles=("hr_manager",), target_roles=("staff",))
    assert evaluate(replace(value, identities=(identity,)))["authority_equivalence"] == "mismatch"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"external_claims_complete": False}, "external_claims_projection_unobserved"),
        ({"module_enabled": None}, "source_module_enablement_unobserved"),
    ],
)
def test_missing_policy_facts_named(change, reason):
    value = complete()
    receipt = evaluate(replace(value, policy=replace(value.policy, **change)))
    assert receipt["authority_equivalence"] == "indeterminate"
    assert reason in receipt["authority_reasons"]


def test_inactive_ambiguous_and_empty_identity_do_not_pass():
    value = complete()
    for identities in ((), (replace(value.identities[0], target_active=False),), value.identities * 2):
        assert evaluate(replace(value, identities=identities))["authority_equivalence"] == "indeterminate"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"employees": (), "assignments": (), "destination_employees": ()}, "empty_expected_roster"),
        ({"employees": ()}, "assignment_employee_unobserved"),
        ({"destination_employees": None}, "destination_run_unmatched"),
    ],
)
def test_incomplete_rosters_indeterminate(change, reason):
    receipt = evaluate(replace(complete(), **change))
    assert receipt["roster_completeness"] == "indeterminate"
    assert reason in receipt["roster_reasons"]


def test_duplicate_assignments_and_calculations():
    value = complete()
    receipt = evaluate(replace(value, assignments=value.assignments * 2))
    assert receipt["roster_completeness"] == "mismatch"
    assert "multiple_qualifying_assignments" in receipt["roster_reasons"]
    assert evaluate(replace(value, destination_employees=(EMPLOYEE, EMPLOYEE)))["roster_completeness"] == "mismatch"


def test_missing_extra_employees():
    assert evaluate(replace(complete(), destination_employees=(OTHER,)))["roster_counts"] == {
        "expected": 1,
        "missing": 1,
        "extra": 1,
    }


@pytest.mark.parametrize(
    "leaving,cutoff,processed,expected",
    [
        (START, None, False, True),
        (END, None, False, True),
        (date(2026, 10, 31), START, False, True),
        (date(2026, 10, 31), None, False, False),
        (END, None, True, False),
        (date(2026, 12, 1), END, False, False),
    ],
)
def test_final_payroll_boundaries(leaving, cutoff, processed, expected):
    employee = preflight.Employee(EMPLOYEE, "RESIGNED", leaving, True, processed, cutoff)
    assert preflight.eligible(employee, complete().run) is expected


def test_assignment_overlap_filters_and_nonoverlapping_orphan():
    value = complete()
    assert (
        evaluate(replace(value, assignments=(preflight.Assignment(EMPLOYEE, END, END),)))["roster_completeness"]
        == "proven"
    )
    for field in ("department", "designation", "employment_type"):
        assert not preflight.eligible(value.employees[0], replace(value.run, **{field: OTHER}))
    past = preflight.Assignment(OTHER, date(2025, 1, 1), date(2025, 1, 31))
    assert evaluate(replace(value, assignments=(*value.assignments, past)))["roster_completeness"] == "proven"


@pytest.mark.parametrize(
    "creators,expected", [((OTHER,), "mismatch"), ((None,), "indeterminate"), ((), "indeterminate")]
)
def test_per_slip_separation(creators, expected):
    assert evaluate(replace(complete(), source_slip_creators=creators))["segregation_of_duties_equivalence"] == expected


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"auto_post_gl": True}, "released_payroll_gl_handoff_hook_absent"),
        ({"auto_post_gl": None}, "effective_auto_post_gl_unresolved"),
        ({"loan_links": 1, "repayment_refs": 1}, "released_payroll_loan_repayment_hook_absent"),
    ],
)
def test_conditional_approval_effects(changes, reason):
    receipt = evaluate(replace(complete(), **changes))
    assert receipt["approval_effect_dependencies"] == "blocked"
    assert reason in receipt["approval_effect_reasons"]


def test_uuid_storage_normalization():
    assert preflight._id(UUID(EMPLOYEE).hex) == EMPLOYEE
    assert preflight._id(UUID(EMPLOYEE)) == EMPLOYEE
    assert preflight._id(None) is None
    with pytest.raises(preflight.PreflightRefusal, match="invalid_observed_uuid"):
        preflight._id("not-an-id")


@pytest.mark.parametrize("sql", ["UPDATE payroll SET status='approved'", "DELETE FROM payroll", "SELECT 1; SELECT 2"])
def test_reader_rejects_mutation_and_multiple_statements(sql):
    with pytest.raises(preflight.PreflightRefusal, match="select_only_required"):
        preflight.Reader(None).rows(sql)


def test_receipt_cannot_overwrite_existing_evidence(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("existing evidence")
    assert preflight.command(SimpleNamespace(receipt=receipt)) == 2
    assert receipt.read_text() == "existing evidence"


def test_receipt_rejects_final_and_parent_symlinks(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("existing evidence")
    link = tmp_path / "receipt.json"
    link.symlink_to(target)
    assert preflight.command(SimpleNamespace(receipt=link)) == 2
    folder = tmp_path / "real"
    folder.mkdir()
    parent = tmp_path / "alias"
    parent.symlink_to(folder, target_is_directory=True)
    assert preflight.command(SimpleNamespace(receipt=parent / "new.json")) == 2
    assert target.read_text() == "existing evidence"
    assert not (folder / "new.json").exists()


def test_cli_diagnostic_has_no_apply_option():
    from erp_v2.importer.cli import build_parser

    args = build_parser().parse_args(
        [
            "payroll-workflow-preflight",
            "--manifest",
            "manifest.json",
            "--snapshot-file",
            "dump",
            "--run-id",
            RUN_ID,
            "--source-policy-revision",
            REVISION,
            "--operator-revision",
            "b" * 40,
            "--receipt",
            "receipt.json",
        ]
    )
    assert args.func is preflight.command
    assert not hasattr(args, "apply")
