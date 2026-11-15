"""No ERP v2 code path hard-deletes accounting journals or lines.

``app_user`` still holds DELETE on draft journal rows at the database layer, so the only
defence in the application is that nothing issues one: draft removal must go through the governed
void/reject command (audited, tenant-scoped, period-checked). This guard fails if any source file
under ``src`` gains a SQL DELETE/TRUNCATE touching ledger tables, an ORM ``delete`` call that is
not on the reviewed allowlist, or an HTTP route that deletes or purges.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "erp_v2"
LEDGER = (
    r"(journal_entries|journal_lines|journal_line_dimensions|posted_ledger_lines"
    r"|posted_ledger_dimensions|mod_accounting)"
)
SQL_DELETE = re.compile(
    rf"(?is)\b(delete\s+from|truncate(\s+table)?)\b[^;]*?\b{LEDGER}\b|\b{LEDGER}\b[^;]*?\bcascade\b"
)
# Reviewed ORM/Core delete call sites: path relative to src/erp_v2 -> permitted call count.
# auth_service deletes an AuthSession row (sign-out); it never touches accounting models.
ALLOWED_DELETE_CALLS = {"web/auth_service.py": 1}
JOURNAL_MODELS = {
    "JournalEntry",
    "JournalLine",
    "JournalLineDimension",
    "PostedLedgerLine",
    "PostedLedgerDimension",
    "ManualJournal",
}


def source_files():
    files = [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(files) > 50  # the scan is not vacuous
    return files


def test_no_sql_delete_or_truncate_against_ledger_tables_in_src():
    offenders = []
    for path in source_files():
        text = path.read_text()
        if SQL_DELETE.search(text):
            offenders.append(str(path.relative_to(SRC)))
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL_DELETE.search(node.value):
                offenders.append(str(path.relative_to(SRC)))
    assert not offenders


def delete_calls(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr in {"delete", "bulk_delete_mappings"}) or (
                isinstance(func, ast.Name) and func.id == "delete"
            ):
                yield node


def test_orm_and_core_delete_calls_are_exactly_the_reviewed_allowlist():
    found = {}
    for path in source_files():
        count = sum(1 for _ in delete_calls(path))
        if count:
            found[str(path.relative_to(SRC))] = count
    assert found == ALLOWED_DELETE_CALLS
    # the one allowed site must not be fed a ledger model
    allowed = SRC / "web" / "auth_service.py"
    names = {n.id for n in ast.walk(ast.parse(allowed.read_text())) if isinstance(n, ast.Name)}
    assert not names & JOURNAL_MODELS


def test_no_cascading_foreign_key_or_relationship_deletes_ledger_rows():
    # ERP v2 declarations (journals/models.py etc.) must not cascade-delete into accounting.
    for path in source_files():
        if path.is_relative_to(SRC / "migrations"):
            continue
        text = path.read_text()
        for match in re.finditer(r"ondelete\s*=\s*['\"](CASCADE|SET NULL)['\"]", text):
            window = text[max(0, match.start() - 400) : match.end()]
            assert not re.search(r"(journal_entries|journal_lines|posted_ledger_lines)", window), (
                path.name,
                match.group(0),
            )
        assert not re.search(r"cascade\s*=\s*['\"][^'\"]*delete", text), path.name


LEDGER_PATH = re.compile(r"journal|ledger|accounting|posting", re.I)
PURGE = re.compile(r"delete|purge|remove|truncate|destroy", re.I)


def composed_routes():
    """Every route the assembly would mount: assembly.modules (ERP_FEATURES + Starter modules)."""
    from erp_v2.assembly import assembly
    from erp_v2.features import ERP_FEATURES

    erp = {id(m) for m in ERP_FEATURES}
    for manifest in assembly.modules:
        for group in ("routers", "web_routers"):
            for router in getattr(manifest, group, ()) or ():
                for route in router.routes:
                    yield id(manifest) in erp, route.path, set(getattr(route, "methods", None) or ())


def test_no_composed_route_deletes_or_purges_ledger_paths_and_journals_have_no_removal_path():
    routes = list(composed_routes())
    assert "/finance/journals" in {path for _, path, _ in routes}  # the real journal facet is composed
    assert sum(1 for erp, *_ in routes if erp) > 40
    for is_erp, path, methods in routes:
        if is_erp:
            assert "DELETE" not in methods, path  # ERP v2 routes never delete anything over HTTP
        if LEDGER_PATH.search(path) or path.startswith("/finance/"):
            assert "DELETE" not in methods and "PUT" not in methods, path
            assert not PURGE.search(path), path
    # every mutation of a journal is a POST to a named governed command, never a bare resource verb
    journal_posts = sorted(
        path for is_erp, path, methods in routes if is_erp and "journal" in path and "POST" in methods
    )
    assert journal_posts and all(
        p == "/finance/journals" or re.search(r"/(request-post|request-reversal|approve|approve-reversal|reject)$", p)
        for p in journal_posts
    )
