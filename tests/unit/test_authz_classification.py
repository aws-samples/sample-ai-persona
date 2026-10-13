"""判定を通らない取得経路の機械検査（BR2.15、NFR1.2、NFR1.8、NFR Design R-05）。

``src/managers/*_manager.py`` の Manager の公開メソッドを ``ast`` で列挙し、
``tests/unit/authz_classification.py`` の分類の一覧と突き合わせる。

- 一覧にないメソッド、一覧にあるがもうないメソッドがあれば失敗（ラチェット）
- ``checks`` のメソッドは、本体（と同じクラスの非公開メソッドを 1 段だけ）で判定
  （``AccessPolicy`` の判定、または判定する口として登録した部品のメソッド）を呼ぶ。
  ``can_*`` の戻り値を捨てる文は失敗
- ``exempt`` の理由は 3 種類の列挙値。``pending_unit`` には解消する Unit を書く
- ``pending_unit`` の件数は Unit ごとの基準値と一致させる（減らすのは各 Unit）。
  ``CURRENT_UNIT`` までの Unit の ``pending_unit`` は 0 件、``FINAL_UNIT``（U7）の
  Build and Test では全体 0 件

この検査が捕まえるのは「判定を呼んでいるか」まで。「正しい対象に当てているか」は
判定表テストと各 Unit の A / B のテストが確かめる（NFR1.8）。
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
# 検査の本体
# =============================================================================


def _manager_classes(directory: Path) -> list[ast.ClassDef]:
    classes: list[ast.ClassDef] = []
    for path in sorted(directory.glob("*_manager.py")):
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
    """Every rule violation of BR2.15 as a readable line (empty when clean)."""
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
# 本物のコードに対する検査
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
        return  # U7 の Build and Test で CURRENT_UNIT を U7 にすると強制される
    assert sum(PENDING_BASELINE.values()) == 0


def test_the_final_unit_mark():
    assert FINAL_UNIT == "U7"
    assert CURRENT_UNIT == "U4"


# =============================================================================
# 検査そのものが空振りしないこと（わざと違反させる）
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
