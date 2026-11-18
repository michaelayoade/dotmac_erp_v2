"""Section 6 import-path exemption proof for the pre-flip validations V1-V3 (PR-1).

V1-V3 live in the native workflows (journals, supplier invoices, payments, payouts). The import and
admission paths must not be able to reach them, so the rehearsal evidence of the step-3 image stays
valid. Four mechanical checks, none of which needs a database:

* import graph: nothing under importer/, legacy_ap/, payees/, posting/, migrations/ imports a
  validated workflow or ``ledger_roles``, except the two named, pinned legacy edges;
* the two imported names of ``revenue_approval_services`` (B1b) are AST-identical to origin/main;
* the audit action values and permission codes are unchanged;
* the delta gate: the changed files are a subset of the allowed set and the protected tests are
  byte-identical (needs ``origin/main``; skipped, loudly, where the checkout has none).
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "erp_v2"

# ast.dump sha256 of the two names B1b imports, taken from origin/main 7f6a194.
PINNED_AST = {
    "ApprovalRefused": "c204b8da3abd012e9f3ec70405043f9126ab0e9786cbeb581613f27e3a4acd45",
    "_submitter_party_id": "175261bd22ca4bbe7feb54d52fdc81647e6224972dac94930c2f5d8dd4695fdf",
}

GUARDED_MODULES = {
    "erp_v2.journals.service",
    "erp_v2.journals.queries",
    "erp_v2.web.revenue_services",
    "erp_v2.web.revenue_approval_services",
    "erp_v2.supplier_payments.service",
    "erp_v2.payroll_payouts.service",
    "erp_v2.ledger_roles",
}
# Directories that must stay unreachable from V1-V3, with the only edges allowed into the guarded
# modules (module -> imported names), each proven by its own protected tests.
SEALED = ("importer", "legacy_ap", "payees", "posting", "migrations")
ALLOWED_EDGES = {
    ("legacy_ap/recognize_cli.py", "erp_v2.web.revenue_approval_services"): {"ApprovalRefused", "_submitter_party_id"},
    # imported as a module (``from erp_v2.web import revenue_services as drafts``); its one use is checked below
    ("legacy_ap/service.py", "erp_v2.web.revenue_services"): set(),
}


def _imports(path: Path) -> list[tuple[str, set[str]]]:
    found: list[tuple[str, set[str]]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, {alias.name for alias in node.names}))
            # ``from erp_v2.journals import service`` names the submodule through the package
            found.extend((f"{node.module}.{alias.name}", set()) for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend((alias.name, set()) for alias in node.names)
    return found


def test_sealed_paths_reach_no_validated_workflow_except_the_two_pinned_edges():
    seen_edges: dict[tuple[str, str], set[str]] = {}
    violations: list[str] = []
    for directory in SEALED:
        for path in sorted((SRC / directory).rglob("*.py")):
            relative = path.relative_to(SRC).as_posix()
            for module, names in _imports(path):
                if module not in GUARDED_MODULES:
                    continue
                key = (relative, module)
                if key in ALLOWED_EDGES and (names or module):
                    if names:
                        seen_edges.setdefault(key, set()).update(names)
                    continue
                violations.append(f"{relative} imports {module}")
    assert violations == []
    assert {key: names for key, names in seen_edges.items()} == {k: v for k, v in ALLOWED_EDGES.items() if v}


def test_legacy_reentry_uses_only_the_draft_command_and_its_error_from_the_draft_module():
    """Re-entry is subject to V1/V2 by design; it must reach nothing else of the validated module."""
    tree = ast.parse((SRC / "legacy_ap" / "service.py").read_text())
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "drafts"
    }
    assert used == {"create_supplier_draft", "RevenueActionError"}


def test_importer_payee_mint_names_are_the_four_pinned_payees_service_names():
    names: set[str] = set()
    for path in (SRC / "importer").rglob("*.py"):
        for module, imported in _imports(path):
            if module == "erp_v2.payees.service":
                names |= imported
    assert names == {"AUDIT_ACTIONS", "AUDIT_MIGRATED", "SOURCE_APPLICATION", "missing_fields"}


def test_the_two_names_b1b_imports_are_ast_identical_to_origin_main():
    tree = ast.parse((SRC / "web" / "revenue_approval_services.py").read_text())
    found = {
        node.name: hashlib.sha256(ast.dump(node).encode()).hexdigest()
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef) and node.name in PINNED_AST
    }
    assert found == PINNED_AST


def test_audit_action_values_and_permission_codes_are_unchanged():
    from erp_v2.journals import service as journals
    from erp_v2.supplier_payments import service as supplier_payments
    from erp_v2.web import revenue_approval_services as b1

    assert journals.AUDIT_ACTIONS == (
        "erp.journal.prepared",
        "erp.journal.post_requested",
        "erp.journal.posted",
        "erp.journal.rejected",
        "erp.journal.reversal_requested",
        "erp.journal.reversed",
        "erp.journal.reversal_rejected",
        "erp.period.opened",
        "erp.period.soft_closed",
        "erp.period.locked",
    )
    assert supplier_payments.AUDIT_ACTIONS == (
        "erp.finance.supplier_payment.prepared",
        "erp.finance.supplier_payment.approved",
        "erp.finance.supplier_payment.cancelled",
        "erp.finance.supplier_payment.recorded_posted",
        "erp.finance.supplier_payment.reversed_posted",
        "erp.finance.supplier_payment.refused",
        "erp.treasury.disbursement_file.generated",
    )
    assert (b1.AUDIT_APPROVED_POSTED, b1.AUDIT_APPROVAL_REFUSED) == (
        "erp.finance.supplier_invoice.approved_posted",
        "erp.finance.supplier_invoice.approval_refused",
    )


# --- delta gate (needs origin/main) -----------------------------------------------------------

ALLOWED_SRC = {
    "src/erp_v2/ledger_roles.py",
    "src/erp_v2/web/revenue_services.py",
    "src/erp_v2/web/revenue_approval_services.py",
    "src/erp_v2/journals/service.py",
    "src/erp_v2/journals/queries.py",
    "src/erp_v2/supplier_payments/service.py",
    "src/erp_v2/payroll_payouts/service.py",
}
ALLOWED_PATTERNS = ("src/erp_v2/templates/*", "tests/*", "docs/*.md")
PROTECTED_TESTS = (
    "tests/test_importer_*.py",
    "tests/test_payables_history*.py",
    "tests/test_settled_admission*.py",
    "tests/test_payables_reconciliation.py",
    "tests/test_payee_bank_pg.py",
    "tests/test_payee_bank_service.py",
    "tests/test_legacy_reentry_pg.py",
    "tests/test_legacy_ap_migration.py",
    "tests/test_b1b_recognize.py",
)
# The one protected file whose shared fixture moves: test_b1b_recognize_pg.py builds its invoices on the
# B1 world, whose tax account moves from code 2100 to 1455 (V1). Only that account-code label may differ.
B1B_FIXTURE_LABEL_ONLY = "tests/test_b1b_recognize_pg.py"


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed git argv, no shell, read-only
            ["git", *args],  # noqa: S607
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return completed.stdout
    except (subprocess.SubprocessError, OSError):
        return None


def _base() -> str | None:
    base = _git("merge-base", "HEAD", "origin/main")
    return base.strip() if base else None


def _changed(base: str) -> list[str]:
    committed = (_git("diff", "--name-only", base, "HEAD") or "").splitlines()
    working = (_git("status", "--porcelain", "--untracked-files=all") or "").splitlines()
    return sorted({*committed, *(line[3:].strip() for line in working if line[3:].strip())})


def test_delta_gate_changed_files_are_inside_the_allowed_set_and_protected_tests_are_untouched():
    base = _base()
    if base is None:
        pytest.skip("origin/main is not available in this checkout; run the gate where it is")
    changed = _changed(base)
    outside = [
        name
        for name in changed
        if name not in ALLOWED_SRC and not any(fnmatch.fnmatch(name, pattern) for pattern in ALLOWED_PATTERNS)
    ]
    assert outside == []
    touched = [name for name in changed if any(fnmatch.fnmatch(name, pattern) for pattern in PROTECTED_TESTS)]
    assert touched == []
    if B1B_FIXTURE_LABEL_ONLY in changed:
        original = _git("show", f"{base}:{B1B_FIXTURE_LABEL_ONLY}")
        assert original is not None
        current = (ROOT / B1B_FIXTURE_LABEL_ONLY).read_text()
        assert current.replace('"1455"', '"2100"') == original
