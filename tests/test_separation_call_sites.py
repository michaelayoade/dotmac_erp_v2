"""Architecture test (D-9 safeguard 1): every ERP call into the governed Starter functions passes the policy.

dotmac-payroll a7 and dotmac-payables a7 default ``separation_of_duties`` to False. A call site that forgets the
keyword would silently run with separation OFF whatever the tenant policy says, so every call must pass it
explicitly and source it from the tenant governance policy. This scans ``src/erp_v2`` with the AST.

Rules (each has a negative case below):

* Every reference to a governed function name (attribute, bare name, ``import x as y`` / ``from m import name``,
  string constant for ``getattr``/``partial``) must be the ``func`` of a checked call. Aliases, ``getattr``,
  ``functools.partial`` and renamed imports therefore fail.
* The call passes ``separation_of_duties=<expr>``. ``<expr>`` is a call to ``governance.separation_of_duties`` or
  ``governance.dual_control``, or an ``or`` of operands that contains one (never ``and``, ``not`` or a constant);
  or a Name assigned exactly once in the enclosing function from such an expression.
* There is no shim: payables calls pass the keyword explicitly like payroll calls (``**kwargs`` spreads fail).
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


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_policy_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in POLICY_READERS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "governance"
    )


def _policy_expression(node: ast.AST) -> bool:
    """A policy call, or an ``or`` containing one; no inversion, ``and`` or constant anywhere in it."""
    if any(isinstance(n, ast.UnaryOp | ast.IfExp | ast.Compare | ast.Lambda) for n in ast.walk(node)):
        return False
    if _is_policy_call(node):
        return True
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        if any(isinstance(v, ast.Constant) for v in node.values):
            return False
        return any(_policy_expression(v) for v in node.values)
    return False


def _functions(tree: ast.AST) -> list[ast.AST]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _innermost(tree: ast.AST, target: ast.AST) -> ast.AST:
    best = tree
    for function in _functions(tree):
        if any(child is target for child in ast.walk(function)):
            best = function
    return best


def _bindings(function: ast.AST, name: str) -> list[ast.AST]:
    """Every statement in the function that binds ``name`` (assignment, augmented, annotated, loop, with, walrus)."""
    found: list[ast.AST] = []
    for n in ast.walk(function):
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for target in n.targets for t in ast.walk(target)
        ):
            found.append(n)
        elif isinstance(n, ast.AugAssign | ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            found.append(n)
        elif isinstance(n, ast.NamedExpr) and n.target.id == name:
            found.append(n)
        elif isinstance(n, ast.For | ast.AsyncFor) and any(
            isinstance(t, ast.Name) and t.id == name for t in ast.walk(n.target)
        ):
            found.append(n)
        elif isinstance(n, ast.With | ast.AsyncWith) and any(
            isinstance(t, ast.Name) and t.id == name
            for item in n.items
            if item.optional_vars is not None
            for t in ast.walk(item.optional_vars)
        ):
            found.append(n)
    return found


def _violations(source: str, filename: str = "<src>") -> tuple[list[str], int]:
    tree = ast.parse(source, filename)
    problems: list[str] = []
    checked_funcs: set[int] = set()
    sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in GOVERNED:
            continue
        sites += 1
        checked_funcs.add(id(node.func))
        where = f"{filename}:{node.lineno} {name}"
        explicit = [k for k in node.keywords if k.arg == KEYWORD]
        if len(explicit) != 1:
            problems.append(f"{where}: no single separation_of_duties= keyword")
            continue
        value = explicit[0].value
        if isinstance(value, ast.Name):
            function = _innermost(tree, node)
            bound = _bindings(function, value.id)
            if len(bound) != 1 or not isinstance(bound[0], ast.Assign) or not _policy_expression(bound[0].value):
                problems.append(f"{where}: {value.id} is not assigned exactly once from the governance policy")
        elif not _policy_expression(value):
            problems.append(f"{where}: separation_of_duties is not read from the governance policy")
    # Any other reference to a governed function (alias, import as, getattr/partial string) evades the call check.
    for node in ast.walk(tree):
        reference = None
        if isinstance(node, ast.Attribute) and node.attr in GOVERNED:
            reference = node.attr
        elif isinstance(node, ast.Name) and node.id in GOVERNED:
            reference = node.id
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in GOVERNED:
            reference = node.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in GOVERNED:
            reference = node.value
        if reference is None:
            continue
        if isinstance(node, ast.Attribute | ast.Name) and any(
            isinstance(c, ast.Call) and c.func is node and id(c.func) in checked_funcs for c in ast.walk(tree)
        ):
            continue
        problems.append(f"{filename}:{getattr(node, 'lineno', 0)} {reference}: referenced other than as a checked call")
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
        # aliases, getattr, partial, import-as
        "f = payroll.approve_payroll_run\nf(db)",
        "f = getattr(payroll, 'finalize_payroll_run')\nf(db)",
        "import functools\nfunctools.partial(payroll.reverse_liability_settlement, db)()",
        "from dotmac_payroll.service import approve_payroll_run as go",
        "import dotmac_payroll.service.approve_payroll_run as go",
        # the retired shim and kwargs spreads no longer satisfy the rule
        "payables.record_supplier_payment(db, **evil.payables_kwargs(db, t))",
        "from erp_v2.supplier_payments import separation\n"
        "payables.record_supplier_payment(db, **separation.payables_kwargs(db, t))",
        # inversions, constants, reassignment
        "def f(db, t):\n    payroll.approve_payroll_run(db, separation_of_duties=not governance.dual_control(db, t))",
        "def f(db, t):\n    payroll.approve_payroll_run(\n"
        "        db, separation_of_duties=governance.dual_control(db, t) and False)",
        "def f(db, t):\n    payroll.approve_payroll_run(\n"
        "        db, separation_of_duties=governance.dual_control(db, t) or False)",
        "def f(db, t):\n    s = governance.separation_of_duties(db, t)\n    s = False\n"
        "    payroll.approve_payroll_run(db, separation_of_duties=s)",
        "def f(db, t):\n    s = governance.separation_of_duties(db, t)\n    s &= False\n"
        "    payroll.approve_payroll_run(db, separation_of_duties=s)",
        "def f(db, t):\n    s = other.separation_of_duties(db, t)\n"
        "    payroll.approve_payroll_run(db, separation_of_duties=s)",
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
        "def f(db, t):\n    s = governance.separation_of_duties(db, t) or _recorded(db, t)\n"
        "    payroll.approve_payroll_run(db, separation_of_duties=s)",
        "payables.approve_supplier_payment(db, separation_of_duties=governance.separation_of_duties(db, t))",
    ],
)
def test_the_scan_accepts_a_call_sourced_from_the_policy(snippet: str) -> None:
    assert _violations(snippet)[0] == [], snippet
