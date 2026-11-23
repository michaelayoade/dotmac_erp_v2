"""Architecture test (D-9 safeguard 1): every ERP call into the governed Starter functions passes the policy.

dotmac-payroll a7 and dotmac-payables a7 default ``separation_of_duties`` to False. A call site that forgets the
keyword would silently run with separation OFF whatever the tenant policy says, so every call must pass it
explicitly and source it from the tenant governance policy (``governance.separation_of_duties`` or
``governance.dual_control``). This scans ``src/erp_v2`` with the AST and fails on any call without it.

Payroll calls must carry the explicit ``separation_of_duties=<name or call>`` keyword. Payables calls go through
``**separation.payables_kwargs(db, tenant_id)``, which supplies exactly that keyword once payables a7 is the
pinned version (and nothing before, so a6 keeps working); the keyword form is accepted too.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "erp_v2"

PAYROLL = {"approve_payroll_run", "finalize_payroll_run", "reverse_liability_settlement"}
PAYABLES = {
    "create_supplier_payment",
    "approve_supplier_payment",
    "record_supplier_payment",
    "reverse_supplier_payment",
}
GOVERNED = PAYROLL | PAYABLES
KEYWORD = "separation_of_duties"
POLICY_READERS = {"separation_of_duties", "dual_control"}
SHIM = "payables_kwargs"


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _reads_policy(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and _called_name(n) in POLICY_READERS and not isinstance(n.func, ast.Name)
        for n in ast.walk(node)
    )


def _enclosing_function(tree: ast.AST, target: ast.AST) -> ast.AST:
    best = tree
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if any(child is target for child in ast.walk(node)):
                best = node  # ast.walk is breadth first: the last match is the innermost
    return best


def _violations(source: str, filename: str = "<src>") -> tuple[list[str], int]:
    tree = ast.parse(source, filename)
    problems: list[str] = []
    sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in GOVERNED:
            continue
        sites += 1
        where = f"{filename}:{node.lineno} {name}"
        explicit = [k for k in node.keywords if k.arg == KEYWORD]
        shim = [
            k
            for k in node.keywords
            if k.arg is None and isinstance(k.value, ast.Call) and _called_name(k.value) == SHIM and name in PAYABLES
        ]
        if shim:
            continue
        if not explicit:
            problems.append(f"{where}: no separation_of_duties= keyword")
            continue
        value = explicit[0].value
        if isinstance(value, ast.Constant):
            problems.append(f"{where}: separation_of_duties is a literal, not the tenant policy")
        elif isinstance(value, ast.Name):
            function = _enclosing_function(tree, node)
            assigned = [
                n.value
                for n in ast.walk(function)
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == value.id for t in n.targets)
            ]
            if not any(_reads_policy(v) for v in assigned):
                problems.append(f"{where}: {value.id} is not assigned from the governance policy")
        elif not _reads_policy(value):
            problems.append(f"{where}: separation_of_duties is not read from the governance policy")
    return problems, sites


def _scan() -> tuple[list[str], dict[str, int]]:
    problems: list[str] = []
    per_file: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        found, sites = _violations(path.read_text(), str(path.relative_to(SRC.parent)))
        problems += found
        if sites:
            per_file[str(path.relative_to(SRC))] = sites
    return problems, per_file


def test_every_governed_call_passes_the_tenant_policy_explicitly() -> None:
    problems, _ = _scan()
    assert not problems, "\n".join(problems)


def test_the_scan_sees_every_known_call_site() -> None:
    """Guards against a vacuous pass (renamed function, moved module): the three payroll and four payables sites."""
    _, per_file = _scan()
    assert per_file == {
        "payroll_payouts/service.py": 1,
        "supplier_payments/service.py": 4,
        "web/payroll_run_services.py": 2,
    }


@pytest.mark.parametrize(
    "snippet",
    [
        "payroll.approve_payroll_run(db, tenant_id=t, run_id=r, approved_by_id=p, approved_at=n)",
        "payroll.finalize_payroll_run(db, separation_of_duties=True)",
        "payroll.reverse_liability_settlement(db, separation_of_duties=False)",
        "def f(db):\n    payroll.approve_payroll_run(db, separation_of_duties=flag)",
        "def f(db):\n    flag = True\n    payroll.approve_payroll_run(db, separation_of_duties=flag)",
        "payables.approve_supplier_payment(db, scope=s)",
        "payables.create_supplier_payment(db, **{'separation_of_duties': True})",
        "payables.record_supplier_payment(db, **other.kwargs(db, t))",
        "from x import approve_payroll_run\napprove_payroll_run(db)",
    ],
)
def test_the_scan_fails_a_call_without_the_policy(snippet: str) -> None:
    assert _violations(snippet)[0], snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(db, t):\n    s = governance.separation_of_duties(db, t)\n"
        "    payroll.approve_payroll_run(db, separation_of_duties=s)",
        "def f(db, t):\n    payroll.finalize_payroll_run(db, separation_of_duties=governance.dual_control(db, t))",
        "def f(db, t):\n    s = governance.separation_of_duties(db, t) or line.flag\n"
        "    payroll.reverse_liability_settlement(db, separation_of_duties=s)",
        "payables.record_supplier_payment(db, **separation.payables_kwargs(db, t))",
        "payables.approve_supplier_payment(db, separation_of_duties=governance.separation_of_duties(db, t))",
    ],
)
def test_the_scan_accepts_a_call_sourced_from_the_policy(snippet: str) -> None:
    assert _violations(snippet)[0] == [], snippet


def test_a_payroll_call_cannot_hide_behind_the_payables_shim() -> None:
    assert _violations("payroll.approve_payroll_run(db, **separation.payables_kwargs(db, t))")[0]
