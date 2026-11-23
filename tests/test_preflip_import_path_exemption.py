"""Section 6 import-path exemption proof for the pre-flip validations V1-V3 (PR-1).

V1-V3 live in the native workflows (journals, supplier invoices, payments, payouts). The import and
admission paths must not be able to reach them. Three mechanical checks, none of which needs a database:

* direct imports: nothing under importer/, legacy_ap/, payees/, posting/, migrations/ imports a
  validated workflow or ``ledger_roles``, except the two named, pinned legacy edges. This is a check
  of DIRECT import statements only. ``importer.cli`` reaches ``erp_v2.assembly``, which loads these
  modules, but loading is not calling: the importer never calls their commands (it is a SQL executor);
* the two imported names of ``revenue_approval_services`` (B1b) are AST-identical to origin/main;
* the audit action values and permission codes are unchanged.

The step-3 rehearsal is re-run on the final image, so the evidence for the import and admission paths comes
from that re-run; this file keeps only the behavioural checks above.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

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
        "erp.finance.supplier_payment.sod_stamp_flagged",  # D-9 stamp read-back (additive; payables a7)
    )
    assert (b1.AUDIT_APPROVED_POSTED, b1.AUDIT_APPROVAL_REFUSED) == (
        "erp.finance.supplier_invoice.approved_posted",
        "erp.finance.supplier_invoice.approval_refused",
    )
