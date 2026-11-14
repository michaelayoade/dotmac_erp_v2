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
        ({"auto_post_gl": True}, "payroll_gl_handoff_not_qualified"),
        ({"auto_post_gl": None}, "effective_auto_post_gl_unresolved"),
        ({"loan_links": 1, "repayment_refs": 1}, "payroll_loan_repayment_handoff_not_qualified"),
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


@pytest.mark.parametrize("role", [" HR_MANAGER ", "Payroll_Approver", " ADMIN "])
def test_legacy_role_normalization_prevents_false_equivalence(role):
    value = complete()
    identity = replace(value.identities[0], source_roles=(role,), target_roles=("staff",))
    assert preflight.source_access(identity)
    assert evaluate(replace(value, identities=(identity,)))["authority_equivalence"] == "mismatch"


def test_scope_tokens_remain_case_sensitive_like_legacy():
    assert not preflight.source_access(preflight.Identity(PERSON, PERSON, True, True, source_scopes=("HR:ACCESS",)))


def setting(text=None, json_value=None, tenant=TENANT, secret=False, value_type="string"):
    return {
        "organization_id": UUID(tenant) if tenant else None,
        "value_type": value_type,
        "value_text": text,
        "value_json": json_value,
        "is_secret": secret,
    }


class Facts:
    """Typed source/target fixture responses, with no ORM or business actions."""

    def __init__(self, *, target=False, settings=(), matched=True):
        self.target, self.settings, self.matched = target, settings, matched
        self.queries = []

    def rows(self, sql, **parameters):
        assert sql.startswith("SELECT ") and ";" not in sql
        assert parameters.get("tenant") == TENANT
        self.queries.append(sql)
        if self.target:
            if "FROM public.parties " in sql:
                return [{"id": UUID(PERSON), "is_active": True}]
            if "FROM mod_people.employees " in sql:
                return [{"id": UUID(EMPLOYEE).hex, "party_id": UUID(PERSON).hex, "status": "ACTIVE"}]
            if "FROM public.party_role_grants " in sql:
                return [{"party_id": UUID(PERSON).hex, "slug": "staff"}]
            if "FROM mod_payroll.payroll_runs " in sql:
                return (
                    [
                        {
                            "id": UUID(RUN_ID).hex,
                            "period_start": START,
                            "period_end": END,
                            "created_by_id": UUID(PERSON).hex,
                            "status": "DRAFT",
                        }
                    ]
                    if self.matched
                    else []
                )
            if "FROM mod_payroll.payroll_calculations " in sql:
                return [{"employee_ref": UUID(EMPLOYEE).hex}]
        else:
            if "FROM payroll.payroll_entry " in sql:
                return [
                    {
                        "entry_id": UUID(RUN_ID),
                        "start_date": START,
                        "end_date": END,
                        "created_by_id": UUID(PERSON),
                        "department_id": None,
                        "designation_id": None,
                        "employment_type_id": None,
                        "status": "DRAFT",
                        "journal_entry_id": None,
                    }
                ]
            if "FROM hr.employee " in sql:
                return [
                    {
                        "employee_id": UUID(EMPLOYEE),
                        "status": "ACTIVE",
                        "date_of_leaving": None,
                        "eligible_for_final_payroll": False,
                        "final_payroll_processed_at": None,
                        "final_payroll_cutoff_date": None,
                        "department_id": None,
                        "designation_id": None,
                        "employment_type_id": None,
                    }
                ]
            if "FROM payroll.salary_structure_assignment " in sql:
                return [{"employee_id": UUID(EMPLOYEE), "from_date": START, "to_date": None}]
            if "JOIN public.permissions " in sql:
                return [{"person": UUID(PERSON), "key": "hr:access"}]
            if "FROM public.people p JOIN " in sql:
                return [{"person": UUID(PERSON), "name": " HR_MANAGER "}]
            if "FROM public.people " in sql:
                return [{"id": UUID(PERSON), "is_active": True, "status": "active"}]
            if "FROM payroll.salary_slip_loan_deduction " in sql:
                return []
            if "FROM payroll.salary_slip " in sql:
                return [{"slip_id": UUID(OTHER), "created_by_id": UUID(PERSON)}]
            if "FROM public.domain_settings " in sql:
                assert "CASE WHEN is_secret IS FALSE" in sql
                return list(self.settings)
        raise AssertionError("unexpected diagnostic relation: " + sql)


@pytest.mark.parametrize(
    "text,json_value,expected",
    [
        (" off ", None, False),
        (" NO ", None, False),
        ("0", None, False),
        (" on ", None, True),
        (" YES ", None, True),
        ("1", None, True),
        (None, False, False),
        (None, " off ", False),
        (None, True, True),
        ("false", True, False),
        (None, None, True),
        ("invalid", None, True),
        (None, 0, True),
    ],
)
def test_collector_registered_boolean_coercion(text, json_value, expected):
    source, target = Facts(settings=(setting(text, json_value),)), Facts(target=True)
    observed = preflight.collect(source, target, TENANT, RUN_ID)
    assert observed.auto_post_gl is expected
    assert observed.destination_employees == (EMPLOYEE,)
    assert observed.source_creator_party == observed.destination_creator == PERSON
    assert observed.identities[0].party == PERSON
    assert observed.identities[0].target_roles == ("staff",)
    assert preflight.source_access(observed.identities[0])


@pytest.mark.parametrize(
    "rows,expected,origin",
    [
        ((), True, "source_policy_default_true"),
        ((setting("off", tenant=None),), False, "global"),
        ((setting("off", tenant=None), setting("on")), True, "organization"),
        ((setting("on", tenant=None), setting("off")), False, "organization"),
        ((setting("off", tenant=None), setting()), True, "organization_registered_default_true"),
    ],
)
def test_collector_setting_precedence(rows, expected, origin):
    observed = preflight.collect(Facts(settings=rows), Facts(target=True), TENANT, RUN_ID)
    assert (observed.auto_post_gl, observed.gl_provenance) == (expected, origin)


def test_secret_setting_is_not_read_or_defaulted():
    class Protected(dict):
        def __getitem__(self, key):
            if key in {"value_text", "value_json"}:
                raise AssertionError("secret value read")
            return super().__getitem__(key)

    row = Protected(setting(secret=True))
    observed = preflight.collect(Facts(settings=(row,)), Facts(target=True), TENANT, RUN_ID)
    assert observed.auto_post_gl is None
    assert observed.gl_provenance == "unreadable_secret_setting"


def test_ambiguous_settings_and_foreign_tenant_refuse():
    for rows, reason in (
        ((setting("on"), setting("off")), "ambiguous_auto_post_gl_setting"),
        ((setting("off", tenant=OTHER),), "setting_tenant_mismatch"),
    ):
        with pytest.raises(preflight.PreflightRefusal, match=reason):
            preflight.resolve_auto_post_gl(rows, TENANT)


def test_collector_unmatched_destination_is_not_a_new_run():
    observed = preflight.collect(Facts(), Facts(target=True, matched=False), TENANT, RUN_ID)
    assert observed.imported_run is None
    assert observed.destination_employees is None


class Connection:
    def __init__(self, *, scope=TENANT, readonly="on", isolation="serializable"):
        self.scope, self.readonly, self.isolation = scope, readonly, isolation
        self.calls = []

    @property
    def autoflush(self):
        raise AssertionError("ORM/autoflush must not be used")

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append((sql, parameters))
        value = self.scope if "current_setting" in sql else None
        if sql == "SHOW transaction_read_only":
            value = self.readonly
        elif sql == "SHOW transaction_isolation":
            value = self.isolation
        return SimpleNamespace(scalar_one=lambda: value)


def test_transaction_uses_kernel_tenant_key_and_checks_read_only():
    connection = Connection()
    assert preflight._transaction(connection, tenant=TENANT).connection is connection
    sql = [call[0] for call in connection.calls]
    assert "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY" in sql
    assert "SELECT set_config('app.current_tenant', :tenant, true)" in sql
    assert "SELECT current_setting('app.current_tenant', true)" in sql
    assert all(text.startswith(("SET ", "SELECT ", "SHOW ")) for text in sql)


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"scope": OTHER}, "destination_tenant_binding_mismatch"),
        ({"readonly": "off"}, "read_only_required"),
        ({"isolation": "read committed"}, "serializable_required"),
    ],
)
def test_transaction_rejects_missing_guarantees(options, reason):
    with pytest.raises(preflight.PreflightRefusal, match=reason):
        preflight._transaction(Connection(**options), tenant=TENANT)
