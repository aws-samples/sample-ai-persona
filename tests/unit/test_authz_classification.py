"""Mechanical check for retrieval paths that bypass the access decision.

Lists the public methods of Managers (``src/managers/*_manager.py`` and
``src/managers/<package>/manager.py`` for Manager packages such as
``advanced_generation/manager.py``) with ``ast`` and compares them against the
classification in ``tests/unit/authz_classification.py``.

- A method missing from the list, or listed but gone, fails (ratchet)
- A ``checks`` method calls a decision in its body (and in same-class private
  methods, one level deep): an ``AccessPolicy`` decision or a method of a
  component registered as a decision point. A statement that discards the
  return value of ``can_*`` fails
- ``exempt`` reasons are one of three enum values. ``pending_unit`` names the Unit that will resolve it
- The ``pending_unit`` count must match the per-Unit baseline (each Unit reduces
  it). Units up to ``CURRENT_UNIT`` have zero ``pending_unit``, and at
  ``FINAL_UNIT`` the total is zero

This check only catches whether a decision is called, not whether it is applied
to the right target; the decision-table tests and each Unit's A / B tests cover that.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest

from src.managers.components.access_policy import DECISION_FUNCTIONS
from tests.unit.authz_classification import (
    CLASSIFICATION,
    CURRENT_UNIT,
    DECISION_PORTS,
    FINAL_UNIT,
    PENDING_BASELINE,
    UNIT_ORDER,
    Entry,
    Kind,
    Reason,
)

pytestmark = pytest.mark.unit

_MANAGERS_DIR = Path(__file__).parent.parent.parent / "src" / "managers"

#: Decision calls whose result is the decision itself (discarding it is a bug).
_RESULT_DECISIONS = frozenset(
    {"can_read", "can_manage", "can_use_asset", "shareable_groups", "is_owner"}
)
#: Receiver names that mean "the AccessPolicy".
_POLICY_RECEIVERS = ("accesspolicy", "access_policy", "policy")


# =============================================================================
# The check itself
# =============================================================================


def _manager_files(directory: Path) -> list[Path]:
    """``*_manager.py`` and each Manager package's ``<package>/manager.py``."""
    return sorted([*directory.glob("*_manager.py"), *directory.glob("*/manager.py")])


def _manager_classes(directory: Path) -> list[ast.ClassDef]:
    classes: list[ast.ClassDef] = []
    for path in _manager_files(directory):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes += [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name.endswith("Manager")
        ]
    return classes


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def public_methods(
    directory: Path,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """``"Class.method"`` -> function node, for every public Manager method."""
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for cls in _manager_classes(directory):
        for name, node in _methods(cls).items():
            if not name.startswith("_"):
                found[f"{cls.name}.{name}"] = node
    return found


def _receiver_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


def _is_decision_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    receiver = _receiver_name(func.value).lower()
    if func.attr in DECISION_FUNCTIONS and any(
        r in receiver for r in _POLICY_RECEIVERS
    ):
        return True
    return any(
        func.attr in methods and _snake(port) in receiver
        for port, methods in DECISION_PORTS.items()
    )


def _calls(node: ast.AST) -> list[ast.Call]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)]


def _discarded_results(node: ast.AST) -> list[int]:
    """Lines of statements that call a ``can_*`` decision and drop its result."""
    return [
        stmt.lineno
        for stmt in ast.walk(node)
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and stmt.value.func.attr in _RESULT_DECISIONS
    ]


def _private_helpers(
    method: ast.AST, siblings: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
) -> list[ast.AST]:
    """Same-class private methods that ``method`` calls as ``self._x(...)``."""
    helpers: list[ast.AST] = []
    for call in _calls(method):
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in {"self", "cls"}
            and func.attr.startswith("_")
            and func.attr in siblings
        ):
            helpers.append(siblings[func.attr])
    return helpers


def check_problems(directory: Path, classification: dict[str, Entry]) -> list[str]:
    """Every rule violation as a readable line (empty when clean)."""
    problems: list[str] = []
    methods = public_methods(directory)
    for key in sorted(set(methods) - set(classification)):
        problems.append(f"{key}: 分類の一覧にない")
    for key in sorted(set(classification) - set(methods)):
        problems.append(f"{key}: 一覧にあるがもう存在しない")
    for cls in _manager_classes(directory):
        siblings = _methods(cls)
        for name, node in siblings.items():
            key = f"{cls.name}.{name}"
            entry = classification.get(key)
            if entry is None or entry.kind is not Kind.CHECKS:
                continue
            for line in _discarded_results(node):
                problems.append(f"{key}: {line} 行目で判定の結果を捨てている")
            bodies: list[ast.AST] = [node, *_private_helpers(node, siblings)]
            if not any(_is_decision_call(c) for body in bodies for c in _calls(body)):
                problems.append(f"{key}: checks なのに判定を呼んでいない")
    return problems


# =============================================================================
# Checks against the real code
# =============================================================================


def test_every_manager_method_is_classified_and_checked():
    problems = check_problems(_MANAGERS_DIR, CLASSIFICATION)
    assert not problems, "\n".join(problems)


def test_exempt_reasons_are_the_three_kinds():
    for key, entry in CLASSIFICATION.items():
        if entry.kind is Kind.CHECKS:
            assert entry.reason is None and entry.pending_unit is None, key
            continue
        assert isinstance(entry.reason, Reason), key
        if entry.reason is Reason.PENDING_UNIT:
            assert entry.pending_unit in UNIT_ORDER, key
        else:
            assert entry.pending_unit is None, key


def test_pending_counts_match_the_baseline():
    counts = Counter(
        entry.pending_unit
        for entry in CLASSIFICATION.values()
        if entry.reason is Reason.PENDING_UNIT
    )
    assert dict(counts) == PENDING_BASELINE, (
        "pending_unit の件数が基準値と違います。判定を入れた Unit は "
        "tests/unit/authz_classification.py の PENDING_BASELINE を下げてください。"
    )


def test_no_pending_is_left_for_units_already_built():
    current = UNIT_ORDER.index(CURRENT_UNIT)
    overdue = [
        key
        for key, entry in CLASSIFICATION.items()
        if entry.pending_unit is not None
        and UNIT_ORDER.index(entry.pending_unit) <= current
    ]
    assert not overdue, f"{CURRENT_UNIT} までに解消すべき pending_unit: {overdue}"


def test_nothing_is_pending_after_the_final_unit():
    if UNIT_ORDER.index(CURRENT_UNIT) < UNIT_ORDER.index(FINAL_UNIT):
        return  # enforced once CURRENT_UNIT is set to FINAL_UNIT
    assert sum(PENDING_BASELINE.values()) == 0


def test_the_final_unit_mark():
    assert FINAL_UNIT == "U7"
    assert CURRENT_UNIT == "U7"


# =============================================================================
# The check itself does not pass vacuously (violations are introduced on purpose)
# =============================================================================


class TestCheckerItself:
    def _write(self, tmp_path: Path, source: str) -> Path:
        directory = tmp_path / "managers"
        directory.mkdir()
        (directory / "fake_manager.py").write_text(source, encoding="utf-8")
        return directory

    def test_a_checks_method_without_a_decision_fails(self, tmp_path):
        directory = self._write(
            tmp_path,
            "class FakeManager:\n"
            "    def get(self, principal, item_id):\n"
            "        return self._db.get(item_id)\n",
        )
        problems = check_problems(directory, {"FakeManager.get": Entry(Kind.CHECKS)})
        assert problems == ["FakeManager.get: checks なのに判定を呼んでいない"]

    def test_a_decision_call_passes(self, tmp_path):
        directory = self._write(
            tmp_path,
            "class FakeManager:\n"
            "    def get(self, principal, item_id):\n"
            "        target = self._lookup.get(item_id)\n"
            "        AccessPolicy.ensure_readable(principal, target, CODE)\n",
        )
        assert not check_problems(directory, {"FakeManager.get": Entry(Kind.CHECKS)})

    def test_one_level_of_private_helper_is_followed(self, tmp_path):
        directory = self._write(
            tmp_path,
            "class FakeManager:\n"
            "    def get(self, principal, item_id):\n"
            "        return self._checked(principal, item_id)\n"
            "    def _checked(self, principal, item_id):\n"
            "        AccessPolicy.require(principal, PERMISSION)\n",
        )
        assert not check_problems(directory, {"FakeManager.get": Entry(Kind.CHECKS)})

    def test_two_levels_of_private_helper_are_not_followed(self, tmp_path):
        directory = self._write(
            tmp_path,
            "class FakeManager:\n"
            "    def get(self, principal, item_id):\n"
            "        return self._outer(principal)\n"
            "    def _outer(self, principal):\n"
            "        return self._inner(principal)\n"
            "    def _inner(self, principal):\n"
            "        AccessPolicy.require(principal, PERMISSION)\n",
        )
        assert check_problems(directory, {"FakeManager.get": Entry(Kind.CHECKS)})

    def test_a_discarded_decision_fails(self, tmp_path):
        directory = self._write(
            tmp_path,
            "class FakeManager:\n"
            "    def get(self, principal, target):\n"
            "        AccessPolicy.can_read(principal, target)\n"
            "        return target\n",
        )
        problems = check_problems(directory, {"FakeManager.get": Entry(Kind.CHECKS)})
        assert any("結果を捨てている" in p for p in problems)

    def test_a_registered_port_counts_as_a_decision(self, tmp_path):
        directory = self._write(
            tmp_path,
            "class FakeManager:\n"
            "    def result(self, principal, job_id):\n"
            "        return self._job_manager.get(principal, job_id)\n",
        )
        assert not check_problems(directory, {"FakeManager.result": Entry(Kind.CHECKS)})

    def test_a_manager_package_is_scanned(self, tmp_path):
        directory = self._write(tmp_path, "")
        package = directory / "fake_package"
        package.mkdir()
        (package / "manager.py").write_text(
            "class PackagedManager:\n"
            "    def get(self, principal, item_id):\n"
            "        return self._db.get(item_id)\n",
            encoding="utf-8",
        )
        (package / "helpers.py").write_text(
            "class HelperManager:\n    def ignored(self):\n        return 1\n",
            encoding="utf-8",
        )
        problems = check_problems(directory, {})
        assert problems == ["PackagedManager.get: 分類の一覧にない"]

    def test_unclassified_and_stale_entries_fail(self, tmp_path):
        directory = self._write(
            tmp_path,
            "class FakeManager:\n    def new_method(self):\n        return 1\n",
        )
        problems = check_problems(
            directory, {"FakeManager.gone": Entry(Kind.EXEMPT, Reason.NO_USER_DATA)}
        )
        assert "FakeManager.new_method: 分類の一覧にない" in problems
        assert "FakeManager.gone: 一覧にあるがもう存在しない" in problems
