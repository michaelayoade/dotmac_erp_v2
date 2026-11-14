"""Read-only, snapshot-bound payroll workflow observations; never authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import date
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from erp_v2.importer.identity_cli import _manifest, _safe_engine, _sha

LIMIT = 10000
SOURCE_POLICY = "legacy-hr-access-and-payroll-eligibility-v1"
HR_ROLES = frozenset({"hr_manager", "hr_director", "payroll_admin", "payroll_approver"})


class PreflightRefusal(ValueError):
    """A stable refusal code, never a driver or row diagnostic."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise PreflightRefusal(reason)


@dataclass(frozen=True)
class Run:
    tenant: str
    source_id: str
    start: date
    end: date
    creator: str | None
    department: str | None = None
    designation: str | None = None
    employment_type: str | None = None


@dataclass(frozen=True)
class Employee:
    id: str
    status: str
    leaving: date | None = None
    final_eligible: bool = False
    final_processed: bool = False
    final_cutoff: date | None = None
    department: str | None = None
    designation: str | None = None
    employment_type: str | None = None


@dataclass(frozen=True)
class Assignment:
    employee: str
    start: date
    end: date | None


@dataclass(frozen=True)
class Identity:
    """One observed source-person to destination-party mapping, no credentials."""

    person: str
    party: str | None
    active_member: bool
    target_active: bool
    source_roles: tuple[str, ...] = ()
    source_scopes: tuple[str, ...] = ()
    target_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorityPolicy:
    # None means no independently observed candidate-bound evaluator catalogue.
    payroll_default_roles: tuple[str, ...] | None = None
    external_claims_complete: bool = False
    module_enabled: bool | None = None
    candidate_revision: str | None = None


@dataclass(frozen=True)
class Observations:
    run: Run
    employees: tuple[Employee, ...]
    assignments: tuple[Assignment, ...]
    identities: tuple[Identity, ...]
    policy: AuthorityPolicy
    # None is an unmatched/unimported run, never a newly empty payroll roster.
    destination_employees: tuple[str, ...] | None
    source_slip_creators: tuple[str | None, ...] = ()
    destination_creator: str | None = None
    source_creator_party: str | None = None
    loan_links: int = 0
    repayment_refs: int = 0
    auto_post_gl: bool | None = None
    gl_provenance: str = "unobserved"
    source_posted: bool = False
    imported_run: bool | None = None


def eligible(employee: Employee, run: Run) -> bool:
    if any(
        expected is not None and expected != actual
        for expected, actual in (
            (run.department, employee.department),
            (run.designation, employee.designation),
            (run.employment_type, employee.employment_type),
        )
    ):
        return False
    if employee.status in {"ACTIVE", "ON_LEAVE"}:
        return True
    effective = employee.final_cutoff or employee.leaving
    return bool(
        employee.status in {"RESIGNED", "TERMINATED", "RETIRED"}
        and employee.final_eligible
        and not employee.final_processed
        and employee.leaving is not None
        and employee.leaving <= run.end
        and effective is not None
        and run.start <= effective <= run.end
    )


def source_access(identity: Identity) -> bool:
    return identity.active_member and bool(
        "admin" in identity.source_roles
        or HR_ROLES.intersection(identity.source_roles)
        or "hr:access" in identity.source_scopes
        or any(scope.startswith("hr:schedule:") for scope in identity.source_scopes)
    )


def evaluate(observed: Observations, *, candidate_revision: str) -> dict:
    """Independent verdicts; the receipt cannot enable any workflow."""
    run = observed.run
    require(run.start <= run.end, "invalid_run_period")
    employees = {employee.id: employee for employee in observed.employees}
    require(len(employees) == len(observed.employees), "ambiguous_source_employee")
    expected = Counter(
        assignment.employee
        for assignment in observed.assignments
        if assignment.start <= run.end
        and (assignment.end is None or assignment.end >= run.start)
        and assignment.employee in employees
        and eligible(employees[assignment.employee], run)
    )
    roster_reasons = []
    if not expected:
        roster_reasons.append("empty_expected_roster")
    if any(
        a.start <= run.end and (a.end is None or a.end >= run.start) and a.employee not in employees
        for a in observed.assignments
    ):
        roster_reasons.append("assignment_employee_unobserved")
    if any(count > 1 for count in expected.values()):
        roster_reasons.append("multiple_qualifying_assignments")
    destination = observed.destination_employees
    roster = "indeterminate"
    missing = extra = 0
    if destination is None:
        roster_reasons.append("destination_run_unmatched")
    else:
        missing, extra = len(set(expected) - set(destination)), len(set(destination) - set(expected))
        if len(destination) != len(set(destination)):
            roster_reasons.append("duplicate_destination_calculation")
        roster = "mismatch" if missing or extra or roster_reasons else "proven"
        if {"empty_expected_roster", "assignment_employee_unobserved"}.intersection(roster_reasons):
            roster = "indeterminate"
    policy = observed.policy
    authority_reasons = []
    if not observed.identities:
        authority_reasons.append("identity_cohort_empty")
    if not policy.external_claims_complete:
        authority_reasons.append("external_claims_projection_unobserved")
    if policy.module_enabled is None:
        authority_reasons.append("source_module_enablement_unobserved")
    if policy.payroll_default_roles is None or policy.candidate_revision != candidate_revision:
        authority_reasons.append("candidate_permission_spec_unobserved")
    persons = [row.person for row in observed.identities]
    parties = [row.party for row in observed.identities if row.party]
    if len(set(persons)) != len(persons) or len(set(parties)) != len(parties):
        authority_reasons.append("ambiguous_identity_mapping")
    if any(row.party is None or not row.active_member or not row.target_active for row in observed.identities):
        authority_reasons.append("inactive_or_unmapped_identity")
    authority = "indeterminate" if authority_reasons else "proven"
    if authority == "proven" and any(
        (source_access(row) and policy.module_enabled)
        != bool(set(row.target_roles).intersection(policy.payroll_default_roles or ()))
        for row in observed.identities
    ):
        authority = "mismatch"
        authority_reasons.append("source_target_access_difference")
    effects = []
    if observed.auto_post_gl is True:
        effects.append("released_payroll_gl_handoff_hook_absent")
    if observed.loan_links:
        effects.append("released_payroll_loan_repayment_hook_absent")
    if observed.auto_post_gl is None:
        effects.append("effective_auto_post_gl_unresolved")
    sod = "indeterminate"
    if (
        run.creator
        and observed.source_creator_party
        and observed.destination_creator
        and observed.source_slip_creators
        and all(observed.source_slip_creators)
    ):
        sod = "proven"
        if observed.source_creator_party != observed.destination_creator:
            sod = "mismatch"
        elif any(creator != run.creator for creator in observed.source_slip_creators):
            # Source checks every slip's creator, whereas the target checks its
            # run creator. A different slip creator changes the denied actors.
            sod = "mismatch"
    return {
        "version": 1,
        "kind": "payroll-workflow-preflight",
        "authority_equivalence": authority,
        "authority_reasons": sorted(authority_reasons),
        "roster_completeness": roster,
        "roster_scope": "current_policy_as_of_snapshot_not_original_frozen_cohort",
        "roster_reasons": sorted(roster_reasons),
        "roster_counts": {"expected": len(expected), "missing": missing, "extra": extra},
        "segregation_of_duties_equivalence": sod,
        "approval_effect_dependencies": "blocked" if effects else "resolved",
        "approval_effect_reasons": sorted(effects),
        "effects": {
            "loan_link_count": observed.loan_links,
            "repayment_reference_count": observed.repayment_refs,
            "auto_post_gl_on_approval": observed.auto_post_gl,
            "auto_post_gl_provenance": observed.gl_provenance,
            "source_posted": observed.source_posted,
            "posting_equivalence": "indeterminate",
            "settlement_equivalence": "indeterminate",
        },
        "historical_admission": "BLOCKED",
        "imported_run": observed.imported_run,
        "workflow_enabled": False,
        "production_ready": False,
        "evidence_is_authorization": False,
    }


class Reader:
    """Connection-only collector: no ORM session, autoflush or lifecycle calls."""

    def __init__(self, connection):
        self.connection = connection

    def rows(self, sql: str, **parameters) -> list[dict]:
        require(sql.startswith("SELECT ") and ";" not in sql, "select_only_required")
        # SQL is fixed in this module; operator strings only enter bind values.
        result = self.connection.execute(sa.text(sql + f" LIMIT {LIMIT + 1}"), parameters).mappings()
        rows = [dict(row) for row in result]
        require(len(rows) <= LIMIT, "observation_limit_exceeded")
        return rows


def _id(value) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise PreflightRefusal("invalid_observed_uuid") from None


def collect(source: Reader, destination: Reader, tenant: str, run_id: str) -> Observations:
    """Exact legacy schemas and explicit safe columns; missing relations refuse."""
    headers = source.rows(
        "SELECT entry_id,start_date,end_date,created_by_id,department_id,designation_id,"
        "employment_type_id,status,journal_entry_id FROM payroll.payroll_entry "
        "WHERE organization_id=:tenant AND entry_id=:run",
        tenant=tenant,
        run=run_id,
    )
    require(len(headers) == 1, "source_run_missing_or_ambiguous")
    header = headers[0]
    run = Run(
        tenant,
        run_id,
        header["start_date"],
        header["end_date"],
        _id(header["created_by_id"]),
        _id(header["department_id"]),
        _id(header["designation_id"]),
        _id(header["employment_type_id"]),
    )
    staff = source.rows(
        "SELECT employee_id,status,date_of_leaving,eligible_for_final_payroll,final_payroll_processed_at,"
        "final_payroll_cutoff_date,department_id,designation_id,employment_type_id FROM hr.employee "
        "WHERE organization_id=:tenant",
        tenant=tenant,
    )
    employees = tuple(
        Employee(
            _id(row["employee_id"]),
            str(row["status"]),
            row["date_of_leaving"],
            row["eligible_for_final_payroll"] is True,
            row["final_payroll_processed_at"] is not None,
            row["final_payroll_cutoff_date"],
            _id(row["department_id"]),
            _id(row["designation_id"]),
            _id(row["employment_type_id"]),
        )
        for row in staff
    )
    assignments = tuple(
        Assignment(_id(row["employee_id"]), row["from_date"], row["to_date"])
        for row in source.rows(
            "SELECT employee_id,from_date,to_date FROM payroll.salary_structure_assignment "
            "WHERE organization_id=:tenant AND from_date<=:end AND (to_date IS NULL OR to_date>=:start)",
            tenant=tenant,
            start=run.start,
            end=run.end,
        )
    )
    # Public people are the tenant membership anchor; credentials are never read.
    people = source.rows("SELECT id,is_active,status FROM public.people WHERE organization_id=:tenant", tenant=tenant)
    roles = source.rows(
        "SELECT p.id AS person,r.name FROM public.people p JOIN public.person_roles pr ON pr.person_id=p.id "
        "JOIN public.roles r ON r.id=pr.role_id WHERE p.organization_id=:tenant AND r.is_active=true",
        tenant=tenant,
    )
    scopes = source.rows(
        "SELECT p.id AS person,q.key FROM public.people p JOIN public.person_roles pr ON pr.person_id=p.id "
        "JOIN public.roles r ON r.id=pr.role_id JOIN public.role_permissions rp ON rp.role_id=r.id "
        "JOIN public.permissions q ON q.id=rp.permission_id WHERE p.organization_id=:tenant "
        "AND r.is_active=true AND q.is_active=true AND (q.key='hr:access' OR q.key LIKE 'hr:schedule:%')",
        tenant=tenant,
    )
    parties = destination.rows(
        "SELECT id,is_active FROM public.parties WHERE replace(btrim(tenant_id::text),'-','')=replace(:tenant,'-','')",
        tenant=tenant,
    )
    target_employees = destination.rows(
        "SELECT id,party_id,status FROM mod_people.employees WHERE "
        "replace(btrim(tenant_id::text),'-','')=replace(:tenant,'-','')",
        tenant=tenant,
    )
    grants = destination.rows(
        "SELECT g.party_id,r.slug FROM public.party_role_grants g JOIN public.roles r "
        "ON r.tenant_id=g.tenant_id AND r.id=g.role_id WHERE "
        "replace(btrim(g.tenant_id::text),'-','')=replace(:tenant,'-','')",
        tenant=tenant,
    )
    # Import identity preserves person UUID as Party UUID. Observe actual rows;
    # never derive a role grant or an active identity from that convention alone.
    active = {_id(row["id"]): row["is_active"] is True for row in parties}
    identities = tuple(
        Identity(
            _id(row["id"]),
            _id(row["id"]) if _id(row["id"]) in active else None,
            row["is_active"] is True and str(row["status"]).lower() == "active",
            active.get(_id(row["id"]), False),
            tuple(sorted({r["name"] for r in roles if _id(r["person"]) == _id(row["id"])})),
            tuple(sorted({r["key"] for r in scopes if _id(r["person"]) == _id(row["id"])})),
            tuple(sorted({r["slug"] for r in grants if _id(r["party_id"]) == _id(row["id"])})),
        )
        for row in people
    )
    runs = destination.rows(
        "SELECT id,period_start,period_end,created_by_id,status FROM mod_payroll.payroll_runs "
        "WHERE replace(btrim(tenant_id::text),'-','')=replace(:tenant,'-','') AND "
        "replace(btrim(id::text),'-','')=replace(:run,'-','')",
        tenant=tenant,
        run=run_id,
    )
    require(len(runs) <= 1, "ambiguous_destination_run")
    target = runs[0] if runs else None
    if target:
        require((target["period_start"], target["period_end"]) == (run.start, run.end), "destination_period_mismatch")
    calculations = (
        destination.rows(
            "SELECT employee_ref FROM mod_payroll.payroll_calculations WHERE "
            "replace(btrim(tenant_id::text),'-','')=replace(:tenant,'-','') AND "
            "replace(btrim(run_id::text),'-','')=replace(:run,'-','')",
            tenant=tenant,
            run=run_id,
        )
        if target
        else []
    )
    known_employees = {_id(row["id"]) for row in target_employees}
    require(all(_id(row["employee_ref"]) in known_employees for row in calculations), "destination_employee_unmapped")
    slips = source.rows(
        "SELECT slip_id,created_by_id FROM payroll.salary_slip WHERE organization_id=:tenant AND payroll_entry_id=:run",
        tenant=tenant,
        run=run_id,
    )
    loans = source.rows(
        "SELECT l.repayment_id FROM payroll.salary_slip_loan_deduction l JOIN payroll.salary_slip s "
        "ON s.slip_id=l.slip_id WHERE s.organization_id=:tenant AND s.payroll_entry_id=:run",
        tenant=tenant,
        run=run_id,
    )
    settings = source.rows(
        "SELECT organization_id,value_type,is_secret,"
        "CASE WHEN is_secret IS FALSE THEN value_text ELSE NULL END AS value_text,"
        "CASE WHEN is_secret IS FALSE THEN value_json ELSE NULL END AS value_json FROM public.domain_settings "
        "WHERE domain='payroll' AND key='auto_post_gl_on_approval' AND is_active=true "
        "AND (organization_id=:tenant OR organization_id IS NULL)",
        tenant=tenant,
    )
    local = [row for row in settings if _id(row["organization_id"]) == tenant]
    selected = local or settings
    require(len(selected) <= 1, "ambiguous_auto_post_gl_setting")
    gl, provenance = True, "source_policy_default_true"
    if selected:
        row = selected[0]
        provenance = "organization" if local else "global"
        raw = row["value_text"] if row["value_text"] is not None else row["value_json"]
        gl = raw if type(raw) is bool else {"true": True, "false": False}.get(str(raw).lower())
        if row["is_secret"] is not False:
            gl, provenance = None, "unreadable_secret_setting"
        elif str(row["value_type"]).lower() != "boolean":
            gl = None
    return Observations(
        run,
        employees,
        assignments,
        identities,
        AuthorityPolicy(),
        tuple(_id(row["employee_ref"]) for row in calculations) if target else None,
        tuple(_id(row["created_by_id"]) for row in slips),
        _id(target["created_by_id"]) if target else None,
        run.creator if active.get(run.creator) else None,
        len(loans),
        sum(row["repayment_id"] is not None for row in loans),
        gl,
        provenance,
        header["journal_entry_id"] is not None,
        True if target else None,
    )


def _transaction(connection, *, tenant: str | None = None) -> Reader:
    connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY"))
    if tenant is None:
        connection.execute(sa.text("SET LOCAL row_security = off"))
    else:
        # SET LOCAL cannot bind a parameter. set_config is a SELECT and scopes
        # the tenant to this rolled-back transaction, never a persistent setting.
        connection.execute(sa.text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": tenant})
        scope = connection.execute(sa.text("SELECT current_setting('app.current_tenant', true)")).scalar_one()
        require(scope == tenant, "destination_tenant_binding_mismatch")
    require(connection.execute(sa.text("SHOW transaction_read_only")).scalar_one() == "on", "read_only_required")
    require(
        connection.execute(sa.text("SHOW transaction_isolation")).scalar_one() == "serializable",
        "serializable_required",
    )
    return Reader(connection)


def run_preflight(args) -> dict:
    from erp_v2.importer.cli import _take_env

    # Reject serving materials before consuming operator DSNs or importing assembly.
    require(
        not any(name.lower() in {"database_url", "platform_database_url"} for name in os.environ),
        "serving_database_materials_forbidden",
    )
    source_url, target_url = _take_env(args.source_url_env), _take_env(args.dest_url_env)
    require(bool(source_url and target_url), "database_environment_required")
    manifest_bytes = Path(args.manifest).read_bytes()
    manifest = _manifest(manifest_bytes)
    require(_sha(Path(args.snapshot_file)) == manifest.snapshot_sha256, "snapshot_digest_mismatch")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", manifest.candidate_revision)), "invalid_candidate_revision")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", args.source_policy_revision)), "invalid_source_policy_revision")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", args.operator_revision)), "invalid_operator_revision")
    require(len(manifest.tenant_extent) == 1, "single_tenant_extent_required")
    from erp_v2.web.payroll_routes import PAYROLL_MANAGE_PERMISSION, payroll_web_feature

    specs = [spec for spec in payroll_web_feature.permissions if spec.code == PAYROLL_MANAGE_PERMISSION]
    require(len(specs) == 1, "payroll_permission_spec_missing_or_ambiguous")
    policy_roles = tuple(sorted(specs[0].default_roles))
    tenant = str(UUID(manifest.tenant_extent[0]))
    require(tenant == manifest.tenant_extent[0], "canonical_tenant_required")
    run_id = str(UUID(args.run_id))
    require(run_id == args.run_id, "canonical_run_id_required")
    engines = []
    try:
        engines.append(_safe_engine(source_url))
        engines.append(_safe_engine(target_url))
        with engines[0].connect() as source_connection, engines[1].connect() as target_connection:
            source = _transaction(source_connection)
            target = _transaction(target_connection, tenant=tenant)
            bindings = manifest.as_dict() | {
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "actual_dump_sha256": manifest.snapshot_sha256,
                "restore_origin": "requires_independent_restore_receipt",
                "source_policy": SOURCE_POLICY,
                "source_policy_revision": args.source_policy_revision,
                "dependencies": {name: version(name) for name in ("dotmac-kernel", "dotmac-payroll", "dotmac-people")},
            }
            for reader, name in ((source, "source_database"), (target, "destination_database")):
                identity = reader.rows("SELECT current_database() AS name,txid_current_snapshot()::text AS revision")[0]
                require(identity["name"] == getattr(manifest, name), "database_binding_mismatch")
                bindings[name + "_observation_revision"] = identity["revision"]
            require(
                len(
                    target.rows(
                        "SELECT id FROM public.tenants WHERE replace(btrim(id::text),'-','')=replace(:tenant,'-','')",
                        tenant=tenant,
                    )
                )
                == 1,
                "destination_tenant_not_visible",
            )
            observed = collect(source, target, tenant, run_id)
            observed = replace(
                observed,
                policy=AuthorityPolicy(
                    payroll_default_roles=policy_roles,
                    candidate_revision=args.operator_revision,
                ),
            )
            receipt = evaluate(observed, candidate_revision=args.operator_revision)
            bindings["installed_permission_spec"] = {
                "code": PAYROLL_MANAGE_PERMISSION,
                "default_roles": list(policy_roles),
            }
            bindings["operator_revision"] = args.operator_revision
            bindings["revision_provenance"] = "operator_declared_not_runtime_git_verified"
            bindings["identity_observation_count"] = len(observed.identities)
            bindings["target_employee_observations_sha256"] = hashlib.sha256(
                json.dumps(
                    sorted(
                        (_id(row["id"]), _id(row["party_id"]), str(row["status"]))
                        for row in target.rows(
                            "SELECT id,party_id,status FROM mod_people.employees "
                            "WHERE replace(btrim(tenant_id::text),'-','')=replace(:tenant,'-','')",
                            tenant=tenant,
                        )
                    )
                ).encode()
            ).hexdigest()
            bindings["run"] = asdict(observed.run)
            # Actual role/identity evidence remains private, with no names/email.
            bindings["identity_observations_sha256"] = hashlib.sha256(
                json.dumps([asdict(row) for row in observed.identities], sort_keys=True).encode()
            ).hexdigest()
            receipt["bindings"] = bindings
            return receipt
    finally:
        for engine in engines:
            engine.dispose()


def command(args) -> int:
    """Fresh owner-only receipt; scrub all unexpected diagnostics."""
    try:
        receipt_path = Path(args.receipt)
        require(receipt_path.parent.resolve(strict=True) == receipt_path.parent.absolute(), "receipt_parent_symlink")
        require(receipt_path.parent.is_dir(), "receipt_parent_missing")
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except (OSError, PreflightRefusal):
        return 2
    with os.fdopen(fd, "w") as stream:
        try:
            receipt = run_preflight(args)
            code = 3  # Diagnostic is deliberately never workflow-ready.
        except PreflightRefusal as exc:
            receipt, code = {"refusal": str(exc), "workflow_enabled": False, "production_ready": False}, 2
        except Exception:
            receipt, code = (
                {"refusal": "payroll_preflight_refused", "workflow_enabled": False, "production_ready": False},
                2,
            )
        json.dump(receipt, stream, sort_keys=True, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return code
