"""The restricted AR opening writer role (billing R5): the closed grant set, the creation / adoption guard and the
exact-privilege assertion. Shared by migration e2_0029 and the operator CLI's apply preflight.

Rules (replan section 2.7, architect review af472276 findings 1, 2, 10):
- PostgreSQL 16 or newer (membership options `SET` / `INHERIT` and `REVOKE ... OPTION FOR`).
- The role is NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION, a member of nothing, not linked
  to app_user / platform_api / app_admin in either direction, and owns nothing, with no default privileges.
- NO member of the role may hold the SET or INHERIT option, from any role. Only an ADMIN-only row is allowed: PG16+
  adds one automatically for a non-superuser CREATEROLE role that creates it. If `createrole_self_grant` also gave the
  creator SET or INHERIT on a role this migration just created, those two options are revoked at once.
- An adopted (pre-existing) role is not trusted: every privilege it holds in this database is revoked, then exactly
  the closed R5 set is granted, then the exact set is asserted by enumerating the ACLs.
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from erp_v2.importer.ar_opening_models import WRITER_ROLE

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SERVING_ROLES = ("app_user", "platform_api", "app_admin")

_SCHEMA = "mod_billing"
# bi_0009 docstring: INSERT closure.
INSERT_TABLES = (
    "documents",
    "posting_groups",
    "posting_effects",
    "accounting_facts",
    "receivable_position_facts",
    "receivable_position_facts_v2",
    "receivable_exposure_facts_v2",
    "opening_import_receipts",
    "opening_cash_lots",
    "opening_cash_effects",
    "opening_receivable_bases",
    "opening_historical_discharge_claims",
    "cash_source_claims",
    "receivable_source_claims",
    "opening_window_admissions",
    "opening_window_seals",
    # bi_0014: variance ledger written by the import and the seal.
    "opening_variances",
    "opening_variance_events",
    "opening_control_gaps",
    "opening_control_gap_events",
)
# SELECT-only tables the closure reads (REVIEW-R5: derived, not confirmed). The first two are named by bi_0009;
# the rest were found by running the real a12 lane as the restricted role (each a "permission denied" read).
SELECT_ONLY_TABLES = (
    "billing_accounts",
    "opening_windows",
    "confirmed_settlements",
    "rated_obligations",
    "withholding_discharges",
    "non_cash_credit_grants",
    "non_cash_credit_effects",
    "document_events",
    "allocation_effects",
)
KERNEL_TABLES = ("idempotency_records", "outbox_events")  # public schema, SELECT + INSERT


class WriterRoleRefused(RuntimeError):
    """The writer role is not in a state the opening may use (fail closed; the message never carries secrets)."""


def _ident(role: str) -> str:
    if not _IDENT.fullmatch(role):
        raise WriterRoleRefused("writer_role_name_invalid")
    return role


def grant_lines(role: str = WRITER_ROLE) -> list[str]:
    _ident(role)
    lines = [f"GRANT USAGE ON SCHEMA {_SCHEMA} TO {role}", f"GRANT USAGE ON SCHEMA public TO {role}"]
    for table in (*INSERT_TABLES, *SELECT_ONLY_TABLES):
        lines.append(f"GRANT SELECT ON {_SCHEMA}.{table} TO {role}")
    for table in INSERT_TABLES:
        lines.append(f"GRANT INSERT ON {_SCHEMA}.{table} TO {role}")
    lines.append(f"GRANT UPDATE (generation) ON {_SCHEMA}.opening_windows TO {role}")
    lines.append(f"GRANT UPDATE (id) ON {_SCHEMA}.billing_accounts TO {role}")
    for table in KERNEL_TABLES:
        lines.append(f"GRANT SELECT, INSERT ON public.{table} TO {role}")
    return lines


def expected_privileges() -> set[tuple[str, str, str]]:
    """The exact closed set: (kind, object, privilege) with kind in schema / table / column."""

    out: set[tuple[str, str, str]] = {("schema", _SCHEMA, "USAGE"), ("schema", "public", "USAGE")}
    for table in (*INSERT_TABLES, *SELECT_ONLY_TABLES):
        out.add(("table", f"{_SCHEMA}.{table}", "SELECT"))
    for table in INSERT_TABLES:
        out.add(("table", f"{_SCHEMA}.{table}", "INSERT"))
    out.add(("column", f"{_SCHEMA}.opening_windows.generation", "UPDATE"))
    out.add(("column", f"{_SCHEMA}.billing_accounts.id", "UPDATE"))
    for table in KERNEL_TABLES:
        out.add(("table", f"public.{table}", "SELECT"))
        out.add(("table", f"public.{table}", "INSERT"))
    return out


_ACTUAL_SQL = """
SELECT 'table', n.nspname || '.' || c.relname, a.privilege_type
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a
 WHERE a.grantee = :oid
UNION ALL
SELECT 'column', n.nspname || '.' || c.relname || '.' || t.attname, a.privilege_type
  FROM pg_attribute t JOIN pg_class c ON c.oid = t.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace,
       aclexplode(t.attacl) a
 WHERE a.grantee = :oid AND t.attacl IS NOT NULL
UNION ALL
SELECT 'schema', n.nspname, a.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) a WHERE a.grantee = :oid
UNION ALL
SELECT 'function', p.oid::regprocedure::text, a.privilege_type
  FROM pg_proc p, aclexplode(p.proacl) a WHERE a.grantee = :oid
UNION ALL
SELECT 'database', d.datname, a.privilege_type FROM pg_database d, aclexplode(d.datacl) a WHERE a.grantee = :oid
UNION ALL
SELECT 'type', t.oid::regtype::text, a.privilege_type FROM pg_type t, aclexplode(t.typacl) a WHERE a.grantee = :oid
UNION ALL
SELECT 'language', l.lanname, a.privilege_type FROM pg_language l, aclexplode(l.lanacl) a WHERE a.grantee = :oid
UNION ALL
SELECT 'tablespace', s.spcname, a.privilege_type FROM pg_tablespace s, aclexplode(s.spcacl) a WHERE a.grantee = :oid
"""


def _oid(conn, role: str) -> int | None:
    return conn.scalar(sa.text("SELECT oid::bigint FROM pg_catalog.pg_roles WHERE rolname = :n"), {"n": role})


def actual_privileges(conn, role: str = WRITER_ROLE) -> set[tuple[str, str, str]]:
    oid = _oid(conn, role)
    if oid is None:
        raise WriterRoleRefused("writer_role_missing")
    return {tuple(r) for r in conn.execute(sa.text(_ACTUAL_SQL), {"oid": oid})}


def membership_problems(conn, role: str = WRITER_ROLE, *, only_set_member: str | None = None) -> list[str]:
    """Reasons the role's memberships are not acceptable (empty = fine).

    Any row ON the role with SET or INHERIT is refused, except (apply preflight) the single operator `only_set_member`
    holding SET without INHERIT. ADMIN-only rows are allowed. A row FOR the role (it is a member of something) is
    refused, as is any link to the serving roles."""

    oid = _oid(conn, role)
    if oid is None:
        return ["writer_role_missing"]
    problems: list[str] = []
    rows = conn.execute(
        sa.text(
            "SELECT m.member::regrole::text, m.set_option, m.inherit_option FROM pg_auth_members m "
            "WHERE m.roleid = :oid AND (m.set_option OR m.inherit_option)"
        ),
        {"oid": oid},
    ).all()
    for member, set_option, inherit_option in rows:
        if inherit_option:
            problems.append("member_inherits_writer")
        elif only_set_member is None or member.strip('"') != only_set_member or not set_option:
            problems.append("member_can_set_writer")
    if conn.scalar(sa.text("SELECT count(*) FROM pg_auth_members WHERE member = :oid"), {"oid": oid}):
        problems.append("writer_is_member_of_another_role")
    linked = conn.scalar(
        sa.text(
            "SELECT count(*) FROM pg_catalog.pg_roles g WHERE g.rolname = ANY(:names) AND "
            "(pg_has_role(:oid, g.oid, 'MEMBER') OR pg_has_role(g.oid, :oid, 'MEMBER'))"
        ),
        {"names": list(SERVING_ROLES), "oid": oid},
    )
    if linked:
        problems.append("writer_linked_to_serving_role")
    return problems


def posture_problems(conn, role: str = WRITER_ROLE) -> list[str]:
    """Attribute, ownership and default-privilege problems of the role itself."""

    row = conn.execute(
        sa.text(
            "SELECT oid::bigint, rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
            "FROM pg_catalog.pg_roles WHERE rolname = :n"
        ),
        {"n": role},
    ).one_or_none()
    if row is None:
        return ["writer_role_missing"]
    oid, *flags = row
    problems = ["writer_role_attribute_forbidden"] if any(flags) else []
    owned = conn.scalar(
        sa.text(
            "SELECT count(*) FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass AND refobjid = :oid "
            "AND deptype = 'o' AND dbid IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))"
        ),
        {"oid": oid},
    )
    if owned:
        problems.append("writer_role_owns_objects")
    default_acl = conn.scalar(
        sa.text(
            "SELECT count(*) FROM pg_default_acl d WHERE d.defaclrole = :oid OR EXISTS "
            "(SELECT 1 FROM aclexplode(d.defaclacl) a WHERE a.grantee = :oid)"
        ),
        {"oid": oid},
    )
    if default_acl:
        problems.append("writer_role_has_default_privileges")
    return problems


def assert_exact_privileges(conn, role: str = WRITER_ROLE, expected: set | None = None) -> None:
    """Every ACL the role holds in this database equals the closed R5 set, no more, no less."""

    want = expected_privileges() if expected is None else expected
    have = actual_privileges(conn, role)
    if have != want:
        raise WriterRoleRefused(f"writer_privileges_not_exact:extra={len(have - want)}:missing={len(want - have)}")


def preflight(
    conn, role: str = WRITER_ROLE, *, operator: str | None = None, forbid_session_admin: bool = False
) -> None:
    """Apply-time check (also the final check of the migration): membership, posture, exact privileges."""

    problems = [*membership_problems(conn, role, only_set_member=operator), *posture_problems(conn, role)]
    # The ADMIN-only creator row lets its holder GRANT the writer to itself. If the operator session still holds ADMIN,
    # "membership only by Michael" would be procedure, not a control: refuse (apply only; the migration's own creator
    # row is expected and legitimate at that point). (Runbook: after the migration Michael
    # revokes the migrator's ADMIN row, then grants the operator SET without INHERIT or ADMIN.)
    if forbid_session_admin and conn.scalar(
        sa.text(
            "SELECT count(*) FROM pg_auth_members m WHERE m.roleid = (SELECT oid FROM pg_roles WHERE rolname = :r) "
            "AND m.admin_option AND m.member = (SELECT oid FROM pg_roles WHERE rolname = session_user)"
        ),
        {"r": role},
    ):
        problems.append("session_user_holds_admin_on_writer")
    if problems:
        raise WriterRoleRefused("writer_role_refused:" + ",".join(sorted(set(problems))))
    assert_exact_privileges(conn, role)


def prepare_role(conn, role: str = WRITER_ROLE) -> bool:
    """Create the role, or verify and narrow an existing one. Returns True when it was created here."""

    _ident(role)
    if int(conn.scalar(sa.text("SELECT current_setting('server_version_num')::integer / 10000"))) < 16:
        raise WriterRoleRefused("postgresql_16_required")
    created = False
    if _oid(conn, role) is None:
        conn.execute(
            sa.text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
        )
        created = True
        # PG16+ gives a non-superuser CREATEROLE creator an ADMIN membership; `createrole_self_grant` may add SET and
        # INHERIT. Keep ADMIN only: revoke the two options at once. (A superuser creator gets no row.)
        rows = conn.execute(
            sa.text(
                "SELECT m.member::regrole::text, m.grantor::regrole::text FROM pg_auth_members m "
                "WHERE m.roleid = (SELECT oid FROM pg_roles WHERE rolname = :n) AND (m.set_option OR m.inherit_option)"
            ),
            {"n": role},
        ).all()
        for member, grantor in rows:
            for option in ("SET", "INHERIT"):
                conn.execute(sa.text(f"REVOKE {option} OPTION FOR {role} FROM {member} GRANTED BY {grantor}"))
    problems = [*membership_problems(conn, role), *posture_problems(conn, role)]
    if problems:
        raise WriterRoleRefused("writer_role_refused:" + ",".join(sorted(set(problems))))
    if not created:
        # An adopted role is not trusted: strip every privilege it holds in this database, then re-grant the closure.
        present = [
            n
            for n in (_SCHEMA, "public")
            if conn.scalar(sa.text("SELECT 1 FROM pg_namespace WHERE nspname = :n"), {"n": n})
        ]
        if present:
            schemas = ", ".join(present)
            for kind in ("TABLES", "SEQUENCES", "FUNCTIONS"):
                conn.execute(sa.text(f"REVOKE ALL ON ALL {kind} IN SCHEMA {schemas} FROM {role}"))
            conn.execute(sa.text(f"REVOKE ALL ON SCHEMA {schemas} FROM {role}"))
    return created
