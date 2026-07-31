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
- `toast_response(e)` / `is_transient(e)` / `error_kind_of(e)`
  （`web/error_messages.py` の公開API。いずれも `code` / `context` のみを読み、
  例外メッセージをレスポンスに載せない）
- `isinstance(e, ...)` / `type(e)`（型による分岐）

ユーザー向け文言は `web/error_messages.py` の公開API経由で取得すること。
詳細は `docs/note/exception-message-design.md` を参照。

検出結果が `_BASELINE` と完全一致することを検査するラチェット方式をとる。
移行が完了したため baseline は空であり、漏出が1箇所でも入れば失敗する。
段階的な移行が再び必要になった場合のみ baseline に列挙する。
"""

import ast
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.managers.persona_manager import PersonaManagerError
from src.models.errors import ErrorCode

_ROUTERS_DIR = Path(__file__).parent.parent.parent / "web" / "routers"

# 例外変数を渡してよい関数。ログ出力・型分岐と、web/error_messages.py の
# 公開API（例外メッセージをレスポンスに載せないことが保証されているもの）のみ。
# ここに関数を追加する場合は、その関数が str(exc) をレスポンスへ流さないことを
# 確認すること。
_ALLOWED_CALLS = frozenset(
    {
        "user_message_for",
        "toast_response",
        "is_transient",
        "is_correctable",
        "error_kind_of",
        # context["field"] の安定キーのみを返す。例外メッセージには触らない
        # （TestFieldOfDoesNotLeak で直接検証している）。
        "field_of",
        "isinstance",
        "type",
    }
)

# 既知の漏出箇所（module -> 関数名の集合）。Issue #112 の移行で空になった。
# 関数名で管理するのは、無関係な編集で baseline がずれないため。新たに漏出を
# 追加する場合はここに列挙するのではなく user_message_for() を使うこと。
_BASELINE: dict[str, set[str]] = {}

# traceback を文字列化しているモジュール。同様に空を維持する。
_TRACEBACK_BASELINE: set[str] = set()


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


class TestRequestValidationErrorHandling:
    """422（RequestValidationError）がカタログ相当の文言を返すことの検査。

    Form(...) の必須パラメータ欠落などは FastAPI/Pydantic が Router に入る前に
    検出するため、上のAST検査（`except ... as e` を対象とする）では捕捉できない
    経路である。グローバルハンドラで塞いでいることを実リクエストで検証する。
    """

    def test_global_handler_is_registered(self):
        """ハンドラが将来削除されても気づけるようにする。"""
        from fastapi.exceptions import RequestValidationError

        from web.main import app

        assert RequestValidationError in app.exception_handlers

    def test_htmx_request_gets_partial_html(self, client):
        """htmx 経路では汎用文言のパーシャルHTMLを返すこと。"""
        response = client.put(
            "/persona/some-id", data={"age": "30"}, headers={"HX-Request": "true"}
        )

        assert response.status_code == 422
        assert "text/html" in response.headers["content-type"]
        assert "入力内容を確認してください" in response.text

    def test_htmx_422_is_marked_renderable(self, client):
        """422 の本文も印が無ければ htmx に破棄される（#117 ステップ3）。

        文言を返していても X-Render-Response が無いと画面には届かず、
        app.js の汎用フォールバックだけが出る。
        """
        response = client.put(
            "/persona/some-id", data={"age": "30"}, headers={"HX-Request": "true"}
        )

        assert response.headers.get("X-Render-Response") == "true"

    def test_json_client_response_is_not_marked(self, test_app):
        """JSON APIクライアント向けの標準422応答には印を付けない。

        印は htmx にDOM反映を許可するためのものなので、JSON経路には不要。
        `client` フィクスチャは HX-Request を常に付けるため、ここでは付けない
        クライアントを用意する（CSRFは X-Requested-With で通す）。
        """
        from fastapi.testclient import TestClient

        with TestClient(
            test_app, headers={"X-Requested-With": "XMLHttpRequest"}
        ) as json_client:
            response = json_client.put("/persona/some-id", data={"age": "30"})

        assert response.status_code == 422
        assert "application/json" in response.headers["content-type"]
        assert "X-Render-Response" not in response.headers

    def test_htmx_response_does_not_echo_user_input(self, client):
        """`exc.errors()` の `input` に載る利用者入力を転写しないこと。

        htmx は4xx本文をDOMへ挿入しないが、転写自体を避けるのが本Issueの原則。
        """
        payload = "<img src=x onerror=alert(1)>"
        response = client.put(
            "/persona/some-id",
            data={"age": payload},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 422
        assert payload not in response.text
        assert "onerror" not in response.text

    def test_htmx_response_does_not_expose_validation_internals(self, client):
        """Pydanticの内部表現（type/loc/msg）をレスポンスに出さないこと。"""
        response = client.put(
            "/persona/some-id", data={}, headers={"HX-Request": "true"}
        )

        assert response.status_code == 422
        for internal in ("int_parsing", '"loc"', "Field required", "body"):
            assert internal not in response.text

    def test_json_api_keeps_standard_422(self, test_app):
        """JSON APIクライアント（web/routers/api.py）の挙動を変えないこと。

        共有の `client` フィクスチャはCSRF対策で HX-Request を常時付与するため、
        htmx以外のクライアントを再現するには専用のクライアントが必要。
        `/api/*` はCSRF免除パスなのでヘッダーなしでも到達する。
        """
        from fastapi.testclient import TestClient

        with TestClient(test_app) as api_client:
            response = api_client.post("/api/discussions", json={})

        assert response.status_code == 422
        assert "application/json" in response.headers["content-type"]
        assert "detail" in response.json()

    @pytest.mark.parametrize(
        ("method", "path", "data"),
        [
            ("post", "/persona/save-selected", {}),
            ("post", "/settings/datasets", {}),
            ("post", "/discussion/start", {}),
            ("post", "/interview/create-session", {}),
        ],
    )
    def test_form_endpoints_across_routers(self, client, method, path, data):
        """Form(...) を使う各Routerで一貫して文言が返ること。"""
        response = getattr(client, method)(
            path, data=data, headers={"HX-Request": "true"}
        )

        # 422 以外（例: 先にCSRFや別の検証で弾かれる）なら本テストの対象外
        if response.status_code == 422:
            assert "入力内容を確認してください" in response.text
            assert "Field required" not in response.text


class TestTransientErrorsUseToast:
    """TRANSIENT は画面を書き換えずトーストで通知すること（#117 ステップ2）。

    VALIDATION と TRANSIENT は同じ Manager 例外型で送出されるため、Router の
    ``except`` 節は型で区別できない。`ErrorKind` による分岐が実際に効いている
    ことを実リクエストで検証する。
    """

    _FORM = {
        "name": "太郎",
        "age": "30",
        "occupation": "エンジニア",
        "background": "背景テキスト",
        "values": "価値観",
        "pain_points": "課題",
        "goals": "目標",
    }

    @staticmethod
    def _manager_raising(exc):
        """update_persona が `exc` を送出する Manager のモックを返す。

        Manager クラスではなく Router の `get_*_manager()` を差し替えるのは、
        実インスタンスの生成（AWS認証を要する）を避けるため。CI には認証情報が
        ないので、クラスメソッドを patch すると生成時に DatabaseError になり
        意図した経路に到達しない。
        """
        manager = Mock()
        manager.update_persona.side_effect = exc
        return manager

    @patch("web.routers.persona.get_persona_manager")
    def test_transient_returns_empty_body_with_toast(self, mock_get_manager, client):
        """入力フォームを消さないため本文を返さない。"""
        mock_get_manager.return_value = self._manager_raising(
            PersonaManagerError(
                "persona update failed (DatabaseError)",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            )
        )

        response = client.put("/persona/p1", data=self._FORM)

        assert response.text == ""
        payload = json.loads(response.headers["HX-Trigger"])
        assert "ペルソナの処理中にエラー" in payload["showToast"]["message"]

    @patch("web.routers.persona.get_persona_manager")
    def test_validation_still_returns_partial_html(self, mock_get_manager, client):
        """入力を直せば解決するものは本文で返す（トーストにしない）。"""
        mock_get_manager.return_value = self._manager_raising(
            PersonaManagerError(
                "name is blank",
                code=ErrorCode.PERSONA_FIELD_REQUIRED,
                context={"field": "name"},
            )
        )

        response = client.put("/persona/p1", data=self._FORM)

        assert "HX-Trigger" not in response.headers
        assert "ペルソナ名が設定されていません" in response.text
        # 本文を返す以上、htmx がスワップできる印が必要
        assert response.headers.get("X-Render-Response") == "true"

    @patch("web.routers.persona.get_persona_manager")
    def test_toast_does_not_leak_exception_message(self, mock_get_manager, client):
        secret = "arn:aws:dynamodb:ap-northeast-1:123456789012:table/internal"
        mock_get_manager.return_value = self._manager_raising(
            PersonaManagerError(secret, code=ErrorCode.PERSONA_OPERATION_FAILED)
        )

        response = client.put("/persona/p1", data=self._FORM)

        assert secret not in response.headers["HX-Trigger"]
        assert secret not in response.text


class TestGenericExceptionsDoNotReplaceContent:
    """変更系の `except Exception` がエラーパーシャルを返さないこと（#117 ステップ2）。

    コードを持たない未知の例外は `ErrorKind.TRANSIENT` に落ちる。これを本文で
    返すと `hx-target`（本体コンテンツや一覧）がエラー表示に置換され、利用者の
    入力・選択がすべて失われる。加えて htmx 1.9.10 は非2xx本文をスワップしない
    ため、置換されないかわりに**文言そのものが画面に届かない**。どちらの結末も
    不適切なので、変更系（POST/PUT/DELETE）の総称ハンドラは `toast_response()`
    を使う。

    AST で機械的に検査するのは、Router を追加した際に同じ失敗が再発するのを
    防ぐため（`_ExposureVisitor` と同じ「構造で保証する」方針）。
    """

    #: 2xx で返しているものは htmx がスワップするので文言は届く。置換の是非は
    #: 別の設計判断なので、この検査の対象は「届かない」非2xx に限る。
    _MUTATING = frozenset({"post", "put", "delete", "patch"})

    def _violations(self) -> list[str]:
        found = []
        for path in _router_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                methods = {
                    m
                    for d in fn.decorator_list
                    for m in self._MUTATING
                    if ast.unparse(d).startswith(f"router.{m}")
                }
                if not methods:
                    continue
                for handler in ast.walk(fn):
                    if not isinstance(handler, ast.ExceptHandler):
                        continue
                    if handler.type is None:
                        caught = "bare"
                    else:
                        caught = ast.unparse(handler.type)
                    if caught not in ("Exception", "bare"):
                        continue
                    for node in ast.walk(handler):
                        if not isinstance(node, ast.Call):
                            continue
                        template = next(
                            (
                                a.value
                                for a in node.args
                                if isinstance(a, ast.Constant)
                                and isinstance(a.value, str)
                                and a.value.endswith(".html")
                            ),
                            None,
                        )
                        if not template or "error" not in template:
                            continue
                        status = next(
                            (
                                kw.value.value
                                for kw in node.keywords
                                if kw.arg == "status_code"
                                and isinstance(kw.value, ast.Constant)
                                and isinstance(kw.value.value, int)
                            ),
                            200,
                        )
                        if 200 <= status < 300:
                            continue
                        found.append(
                            f"{path.name}:{node.lineno} {fn.name} -> {template}"
                        )
        return sorted(found)

    def test_no_generic_handler_returns_error_partial(self):
        violations = self._violations()
        assert violations == [], (
            "変更系の except Exception がエラーパーシャルを非2xxで返している。"
            "toast_response() を使うこと（文言が画面に届かず、入力も失われる）:\n"
            + "\n".join(violations)
        )

    def test_visitor_detects_a_reintroduced_partial(self, tmp_path):
        """検査が実際に機能することを確認する（常に空を返していないこと）。"""
        module = tmp_path / "leaky.py"
        module.write_text(
            "@router.post('/x')\n"
            "async def handler(request):\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        return templates.TemplateResponse(\n"
            "            request, 'partials/error.html',\n"
            "            {'error': 'x'}, status_code=500)\n",
            encoding="utf-8",
        )
        with patch(
            "tests.api.test_error_exposure._router_modules", return_value=[module]
        ):
            assert self._violations() != []


class TestErrorPartialsReachTheScreen:
    """非2xxで返すエラーパーシャルが画面に届くこと（#117 ステップ3）。

    htmx 1.9.10 は ``status>=200 && status<400 && status!==204`` 以外の本文を
    スワップしない。したがって 4xx/5xx でエラーパーシャルを返しても、文言は
    生成されているのにDOMへ反映されず、`app.js` の汎用フォールバック
    （「エラーが発生しました。再度お試しください。」）だけが表示される。
    #112 で84件の文言カタログを整備したにもかかわらず、その半数が画面に
    到達していなかった原因がこれである。

    サーバーが ``X-Render-Response: true``（``mark_renderable()``）を付けた
    応答のみクライアントがスワップする。この検査は「非2xxでエラーパーシャルを
    返すなら印が付いている」ことをASTで保証し、Routerを追加した際に同じ失敗が
    再発するのを防ぐ。#112 の `str(e)` 露出検査と同じ「構造で保証する」方針。

    ステータスコードで一律にスワップを許可しない理由は Issue #117 の原因B
    （汎用パーシャルが本体コンテンツを置換してフォームごと消える）を参照。
    """

    def _unreachable(self, modules: list[Path] | None = None) -> list[str]:
        """非2xxで返すエラーパーシャルのうち、印が付いていない箇所を集める。"""
        found = []
        for path in modules if modules is not None else _router_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))

            # mark_renderable(...) の引数として渡されている呼び出しの位置
            marked = {
                (arg.lineno, arg.col_offset)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "mark_renderable"
                for arg in node.args
                if isinstance(arg, ast.Call)
            }

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if "TemplateResponse" not in ast.unparse(node.func):
                    continue
                template = next(
                    (
                        a.value
                        for a in node.args
                        if isinstance(a, ast.Constant)
                        and isinstance(a.value, str)
                        and a.value.endswith(".html")
                    ),
                    None,
                )
                if not template or "error" not in template:
                    continue
                status = next(
                    (
                        kw.value.value
                        for kw in node.keywords
                        if kw.arg == "status_code"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, int)
                    ),
                    200,
                )
                # 2xx は htmx がそのままスワップするので印は不要
                if 200 <= status < 300:
                    continue
                if (node.lineno, node.col_offset) in marked:
                    continue
                found.append(f"{path.name}:{node.lineno} -> {template} ({status})")
        return sorted(found)

    def test_all_error_partials_are_marked_renderable(self):
        unreachable = self._unreachable()
        assert unreachable == [], (
            "非2xxでエラーパーシャルを返しているが mark_renderable() が無い。"
            "htmx が本文を破棄するため文言が画面に届かない:\n" + "\n".join(unreachable)
        )

    def test_check_detects_an_unmarked_partial(self, tmp_path):
        """検査が実際に機能することを確認する（常に空を返していないこと）。"""
        module = tmp_path / "unmarked.py"
        module.write_text(
            "async def handler(request):\n"
            "    return templates.TemplateResponse(\n"
            "        request, 'partials/error.html',\n"
            "        {'error': 'x'}, status_code=404)\n",
            encoding="utf-8",
        )
        assert self._unreachable([module]) != []

    def test_marked_partial_passes_the_check(self, tmp_path):
        module = tmp_path / "marked.py"
        module.write_text(
            "async def handler(request):\n"
            "    return mark_renderable(templates.TemplateResponse(\n"
            "        request, 'partials/error.html',\n"
            "        {'error': 'x'}, status_code=404))\n",
            encoding="utf-8",
        )
        assert self._unreachable([module]) == []

    def test_2xx_partial_needs_no_mark(self, tmp_path):
        """2xx は htmx がスワップするので印を強制しない。"""
        module = tmp_path / "ok.py"
        module.write_text(
            "async def handler(request):\n"
            "    return templates.TemplateResponse(\n"
            "        request, 'partials/error.html', {'error': 'x'})\n",
            encoding="utf-8",
        )
        assert self._unreachable([module]) == []


class TestErrorTemplatesAreConsolidated:
    """エラー表示テンプレートが統合された状態を保つこと（#117 ステップ5）。

    以前は5種に分裂し、変数名も `error` と `message` が混在していた。
    Router が「どれを返すか」を個別に判断する根拠がなく、`message` を
    渡しているのに `{{ error }}` を描画するテンプレート（settings.py の
    7箇所）では**エラー枠が空で表示される**バグも生じていた。
    """

    _TEMPLATES_DIR = Path(__file__).parent.parent.parent / "web" / "templates"

    #: 統合で廃止したテンプレート。復活したら気づけるようにする
    _REMOVED = (
        "partials/error.html",
        "survey/partials/error_message.html",
        "persona/partials/memory_add_error.html",
    )

    def test_removed_templates_are_not_reintroduced(self):
        existing = [t for t in self._REMOVED if (self._TEMPLATES_DIR / t).exists()]
        assert existing == [], (
            "統合で廃止したテンプレートが復活している。"
            "partials/error_inline.html（フォーム近傍）または "
            f"partials/error_banner.html（領域置換）を使うこと: {existing}"
        )

    def test_consolidated_templates_exist(self):
        for name in ("partials/error_inline.html", "partials/error_banner.html"):
            assert (self._TEMPLATES_DIR / name).exists(), f"{name} が無い"

    def test_routers_do_not_reference_removed_templates(self):
        offenders = []
        for path in _router_modules():
            src = path.read_text(encoding="utf-8")
            for removed in self._REMOVED:
                if f'"{removed}"' in src or f"'{removed}'" in src:
                    offenders.append(f"{path.name} -> {removed}")
        assert offenders == [], offenders

    def test_error_partials_use_the_error_variable(self):
        """変数名が `error` に統一されていること（`message` 混在の再発防止）。"""
        for name in ("partials/error_inline.html", "partials/error_banner.html"):
            body = (self._TEMPLATES_DIR / name).read_text(encoding="utf-8")
            assert "{{ error }}" in body, f"{name} が error を描画していない"
            assert "{{ message }}" not in body, f"{name} に message が残っている"

    @staticmethod
    def _render(name: str, **ctx: object) -> str:
        """テンプレートを実際に描画する（注釈ではなく出力を検証するため）。"""
        from jinja2 import Environment, FileSystemLoader

        env = Environment(
            loader=FileSystemLoader(
                Path(__file__).parent.parent.parent / "web" / "templates"
            )
        )
        return env.get_template(name).render(**ctx)

    def test_inline_template_has_no_reload_prompt(self):
        """入力を直せば解決するエラーで「更新」を促してはならない。

        更新すると直そうとしている入力が破棄されるため。
        """
        out = self._render("partials/error_inline.html", error="入力が長すぎます")

        assert "入力が長すぎます" in out
        assert "location.reload" not in out
        assert "ページを更新" not in out

    def test_banner_template_offers_recovery(self):
        """領域置換型は再試行の導線を持つこと。"""
        out = self._render("partials/error_banner.html", error="見つかりません")

        assert "見つかりません" in out
        assert "location.reload" in out

    def test_banner_template_can_show_a_back_link(self):
        """NOT_FOUND では復帰リンクを出せること。"""
        out = self._render(
            "partials/error_banner.html",
            error="ペルソナが見つかりません",
            back_url="/persona/management",
            back_label="ペルソナ一覧へ",
        )

        assert "/persona/management" in out
        assert "ペルソナ一覧へ" in out

    def test_dom_coupled_templates_keep_their_contracts(self):
        """DOM契約を持つ2種は統合せず、契約を維持していること。

        htmx のスワップ先IDや領域クリアの契約を共通テンプレートへ寄せると
        差し替えが壊れるため、外側は各テンプレートに残す設計にしている。
        """
        deleted = self._render(
            "persona/partials/memory_delete_error.html",
            error="削除できません",
            memory_id="m1",
        )
        assert 'id="memory-item-m1"' in deleted
        assert "削除できません" in deleted

        upload = self._render(
            "persona/partials/knowledge_file_error.html", error="形式が不正です"
        )
        assert "#file-upload-preview-area" in upload
        assert "形式が不正です" in upload


class TestHtmxHandlersAreNotDuplicated:
    """htmx ハンドラが二重登録されていないこと（#117 ステップ5）。

    base.html と app.js の両方に afterSwap / beforeRequest / afterRequest を
    登録していたため、fade-in が二重に適用されていた。
    """

    def test_base_html_does_not_register_htmx_handlers(self):
        base = (
            Path(__file__).parent.parent.parent / "web" / "templates" / "base.html"
        ).read_text(encoding="utf-8")
        assert "addEventListener('htmx:" not in base, (
            "base.html で htmx ハンドラを登録している。app.js に集約すること"
        )

    def test_app_js_registers_the_handlers(self):
        app_js = (
            Path(__file__).parent.parent.parent / "web" / "static" / "js" / "app.js"
        ).read_text(encoding="utf-8")
        for event in ("htmx:beforeSwap", "htmx:responseError", "showToast"):
            assert f"addEventListener('{event}'" in app_js, f"{event} が無い"
