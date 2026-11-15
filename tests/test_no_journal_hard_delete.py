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
from dataclasses import replace
from pathlib import Path

import pytest
from dotmac_kernel import create_app
from dotmac_kernel.config import settings

from erp_v2.assembly import assembly

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


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setattr(settings, "platform_root_domain", "erp.test")
    monkeypatch.setattr(settings, "trusted_hosts", "acme.erp.test")
    return create_app(replace(assembly, web_enabled=True, database_runtime=None, require_database_runtime=False))


def flatten(routes, prefix=""):
    for route in routes:
        path = prefix + getattr(route, "path", "")
        children = getattr(route, "routes", None)
        if children is not None:
            yield from flatten(children, path)
        elif getattr(route, "methods", None):
            yield path, set(route.methods)


def test_no_mounted_route_deletes_or_purges_and_journals_have_no_draft_removal_path(app):
    routes = list(flatten(app.routes))
    paths = sorted({path for path, _ in routes})
    assert "/finance/journals" in paths, paths[:40]  # the real journal facet is mounted
    for path, methods in routes:
        assert "DELETE" not in methods, path
        assert "PUT" not in methods or "journal" not in path, path
        if "journal" in path or "/finance/" in path:
            assert not re.search(r"delete|purge|remove|truncate|destroy", path, re.I), path
    # every mutation of a journal is a POST to a named governed command, never a bare resource verb
    journal_posts = sorted(path for path, methods in routes if "journal" in path and "POST" in methods)
    assert journal_posts and all(
        p == "/finance/journals" or re.search(r"/(request-post|request-reversal|approve|approve-reversal|reject)$", p)
        for p in journal_posts
    )
