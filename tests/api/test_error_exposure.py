"""Router層が例外の内容をレスポンスに書いていないことの静的検査（Issue #112）。

#103 で「Routerに固定文言を直書きする」慣習を確立したが、規律を人間が守る形
だったため #111 で再発した。本テストは web/routers/ の全モジュールをASTで走査し、
`except ... as e:` で捕捉した例外変数がログ以外の文脈で参照されていないことを
機械的に検査する。これにより CodeQL の `py/stack-trace-exposure` を待たずに
リポジトリ内で再発を検知できる。

許可される参照先:
- `logger.*(...)` の引数（診断情報はログへ）
- `raise ... from e`（例外チェーンの維持）
- `user_message_for(e)`（エラーコード→文言カタログの参照）
- `isinstance(e, ...)` / `type(e)`（型による分岐）

ユーザー向け文言は `web/error_messages.user_message_for()` から取得すること。
詳細は `docs/note/exception-message-design.md` を参照。

移行は段階的に進めるため、未移行の箇所は `_BASELINE` に明示的に列挙する
ラチェット方式をとる。検出結果が baseline と完全一致することを検査するので、
新規の漏出は即座に失敗し、移行済みの箇所を baseline に残したままにもできない。
移行完了時に `_BASELINE` は空になる。
"""

import ast
from pathlib import Path

import pytest

_ROUTERS_DIR = Path(__file__).parent.parent.parent / "web" / "routers"

# 例外変数を渡してよい関数。ログ出力と文言カタログの参照のみ。
_ALLOWED_CALLS = frozenset({"user_message_for", "isinstance", "type"})

# 未移行の漏出箇所（module -> 関数名の集合）。Issue #112 の移行で空にする。
# 行番号ではなく関数名で管理するのは、無関係な編集で baseline がずれないため。
_BASELINE: dict[str, set[str]] = {
    "discussion.py": {
        "delete_discussion",
        "delete_report",
        "regenerate_insights",
        "save_report",
        "start_discussion",
        "stream_discussion",
    },
    "interview.py": {
        "save_interview_session",
    },
    "persona.py": {
        "add_persona_memory",
        "create_dataset_binding",
        "delete_all_persona_memories",
        "delete_persona_memory",
        "get_persona_memories",
        "update_persona",
    },
    "settings.py": {
        "create_dataset",
        "create_knowledge_base",
        "update_dataset",
    },
}

# traceback を文字列化している未移行モジュール。Issue #112 の移行で空にする。
_TRACEBACK_BASELINE: set[str] = {"interview.py", "persona.py"}


def _router_modules() -> list[Path]:
    return sorted(p for p in _ROUTERS_DIR.glob("*.py") if p.name != "__init__.py")


class _ExposureVisitor(ast.NodeVisitor):
    """`except ... as e` の例外変数が許可外の文脈で参照される箇所を集める。"""

    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []
        self._handler_names: list[str] = []
        self._scope: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    @property
    def offending_scopes(self) -> set[str]:
        """違反を含む最外側の関数名（ネストした内部関数は外側の名前に丸める）。"""
        return {scope for _, scope in self.violations}

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is None:
            self.generic_visit(node)
            return
        self._handler_names.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self._handler_names.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_logger_call(node.func) or self._is_allowed_call(node.func):
            # 引数内の例外参照は許可するが、ネストした except は追う必要がある。
            for child in ast.walk(node):
                if isinstance(child, ast.ExceptHandler):
                    self.visit_ExceptHandler(child)
            return
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        # `raise X from e` の cause 位置は例外チェーンの維持なので許可する。
        if node.exc is not None:
            self.visit(node.exc)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self._handler_names:
            self.violations.append(
                (node.lineno, self._scope[0] if self._scope else "<module>")
            )

    @staticmethod
    def _is_logger_call(func: ast.expr) -> bool:
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in {"logger", "logging", "self"}
        )

    @staticmethod
    def _is_allowed_call(func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            return func.id in _ALLOWED_CALLS
        return isinstance(func, ast.Attribute) and func.attr in _ALLOWED_CALLS


@pytest.mark.parametrize("path", _router_modules(), ids=lambda p: p.name)
def test_router_does_not_reference_exception_outside_logging(path: Path) -> None:
    """例外変数がログ・文言カタログ以外から参照されていないこと。

    検出結果が `_BASELINE` と完全一致することを検査する（ラチェット方式）。
    """
    visitor = _ExposureVisitor()
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))

    found = visitor.offending_scopes
    expected = _BASELINE.get(path.name, set())

    new_leaks = found - expected
    assert not new_leaks, (
        f"{path.name}: 例外変数をログ以外で参照している新規箇所があります: {sorted(new_leaks)}。"
        " ユーザー向け文言は web.error_messages.user_message_for() を使ってください。"
    )

    resolved = expected - found
    assert not resolved, (
        f"{path.name}: 移行済みの箇所が _BASELINE に残っています: {sorted(resolved)}。"
        " tests/api/test_error_exposure.py の _BASELINE から削除してください。"
    )


@pytest.mark.parametrize("path", _router_modules(), ids=lambda p: p.name)
def test_router_does_not_import_traceback(path: Path) -> None:
    """Router がスタックトレースを文字列化する経路を持たないこと。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    uses_traceback = "traceback" in imported

    if path.name in _TRACEBACK_BASELINE:
        assert uses_traceback, (
            f"{path.name}: traceback の使用が解消されています。"
            " _TRACEBACK_BASELINE から削除してください。"
        )
    else:
        assert not uses_traceback, (
            f"{path.name}: traceback をインポートしています。"
            " スタックトレースは logger(..., exc_info=True) でログに出してください。"
        )


def test_baseline_has_no_stale_modules() -> None:
    """_BASELINE に存在しないモジュール名が残っていないこと。"""
    known = {p.name for p in _router_modules()}
    assert not set(_BASELINE) - known
    assert not _TRACEBACK_BASELINE - known


class TestVisitorItself:
    """検査ロジック自体の妥当性。"""

    @staticmethod
    def _scan(source: str) -> _ExposureVisitor:
        visitor = _ExposureVisitor()
        visitor.visit(ast.parse(source))
        return visitor

    def test_allowed_patterns_are_not_flagged(self):
        visitor = self._scan("""
def handler():
    try:
        pass
    except ValueError as e:
        logger.warning("失敗: %s", e)
        logger.error("失敗", exc_info=True)
        msg = user_message_for(e, default="失敗しました")
        if isinstance(e, KeyError):
            raise RuntimeError("wrapped") from e
        return msg
""")
        assert visitor.violations == []

    def test_str_interpolation_is_flagged(self):
        visitor = self._scan("""
def handler():
    try:
        pass
    except ValueError as e:
        return f"エラー: {str(e)}"
""")
        assert visitor.offending_scopes == {"handler"}

    def test_attribute_access_is_flagged(self):
        """FileUploadError.user_message のようなプロパティ経由の漏出。"""
        visitor = self._scan("""
def handler():
    try:
        pass
    except ValueError as e:
        return {"error": e.user_message}
""")
        assert visitor.offending_scopes == {"handler"}

    def test_args_access_is_flagged(self):
        visitor = self._scan("""
def handler():
    try:
        pass
    except ValueError as e:
        return e.args[0] if e.args else "エラー"
""")
        assert visitor.offending_scopes == {"handler"}

    def test_helper_function_call_is_flagged(self):
        """Router内ヘルパーへの委譲も漏出経路として検出する。"""
        visitor = self._scan("""
def handler():
    try:
        pass
    except ValueError as e:
        return _get_user_friendly_error_message(e)
""")
        assert visitor.offending_scopes == {"handler"}

    def test_nested_function_is_attributed_to_outer_scope(self):
        visitor = self._scan("""
def outer():
    def inner():
        try:
            pass
        except ValueError as e:
            return str(e)
    return inner
""")
        assert visitor.offending_scopes == {"outer"}
