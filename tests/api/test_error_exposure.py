"""Static check that routers never write exception content into responses (Issue #112).

The convention of hardcoding fixed text in routers (#103) relied on human
discipline, and the problem recurred (#111). This test scans every module under
web/routers/ with the AST and mechanically checks that an exception variable
captured by `except ... as e:` is not referenced outside logging. That detects a
recurrence inside the repository without waiting for CodeQL's
`py/stack-trace-exposure`.

Allowed references:
- arguments of `logger.*(...)` (diagnostics go to the log)
- `raise ... from e` (keeps the exception chain)
- `user_message_for(e)` (looks up the catalog by error code)
- `toast_response(e)` / `is_transient(e)` / `error_kind_of(e)`
  (public API of `web/error_messages.py`; they read only `code` / `context`
  and never put the exception message in the response)
- `isinstance(e, ...)` / `type(e)` (branching by type)

Get user-facing text through the public API of `web/error_messages.py`. See
`docs/note/exception-message-design.md` for details.

The detected result must match `_BASELINE` exactly (a ratchet). The migration is
done, so the baseline is empty and any single leak fails the test. List entries
in the baseline only if a gradual migration is needed again.
"""

import ast
import dataclasses
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.managers.persona_manager import PersonaManagerError
from src.models.errors import CodedError, ErrorCode

_ROUTERS_DIR = Path(__file__).parent.parent.parent / "web" / "routers"

# Functions that may be passed an exception variable: logging, type branching, and the
# public API of web/error_messages.py (guaranteed not to put exception text in the response).
# When adding a function here, confirm that it never passes str(exc) into the response.
_ALLOWED_CALLS = frozenset(
    {
        "user_message_for",
        "toast_response",
        "is_transient",
        "is_correctable",
        "error_kind_of",
        # Returns only the stable key context["field"]; never touches the exception message
        # (verified directly in TestFieldOfDoesNotLeak).
        "field_of",
        # Returns only the known rule keys of context["rules"] for PASSWORD_POLICY_NOT_MET
        # (verified in tests/unit/test_error_messages.py TestPasswordRuleLabels)
        "missed_password_rules",
        # Turns only the message, code and kind into JSON (no exception text;
        # verified directly in tests/unit/test_error_messages.py TestJsonErrorResponse)
        "json_error_response",
        "error_payload",
        "isinstance",
        "type",
        # Local helper limited to web/routers/interview.py. It derives the error_type string
        # using only isinstance and error_kind_of/is_correctable (both allowed above),
        # and never touches the exception message.
        "_error_type_for",
        # Unauthenticated response from web/auth_middleware.py. It reads only exc.code, and the body is
        # limited to catalog text and the code (verified directly in TestAuthErrorBodies).
        "unauthorized_response",
    }
)

# Known leak sites (module -> set of function names). Emptied by the Issue #112 migration.
# Keyed by function name so unrelated edits do not shift the baseline. To add a new
# leak, use user_message_for() instead of listing it here.
_BASELINE: dict[str, set[str]] = {}

# Modules that stringify a traceback. Kept empty likewise.
_TRACEBACK_BASELINE: set[str] = set()


def _router_modules() -> list[Path]:
    return sorted(p for p in _ROUTERS_DIR.glob("*.py") if p.name != "__init__.py")


#: Components that build responses ahead of the routers (login entry, CSRF, access log).
#: Exception-variable references and traceback use are checked with the same rules as routers.
_ENTRY_MODULES = [
    _ROUTERS_DIR.parent / "auth_middleware.py",
    _ROUTERS_DIR.parent / "middleware.py",
    _ROUTERS_DIR.parent / "access_log.py",
]


def _exposure_modules() -> list[Path]:
    return _router_modules() + _ENTRY_MODULES


class _ExposureVisitor(ast.NodeVisitor):
    """Collect places where the exception variable of `except ... as e` is referenced in a disallowed context."""

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
        """Name of the outermost function containing a violation (nested inner functions roll up to the outer name)."""
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
            # References inside arguments are allowed, but a nested except must still be followed.
            for child in ast.walk(node):
                if isinstance(child, ast.ExceptHandler):
                    self.visit_ExceptHandler(child)
            return
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        # The cause position of `raise X from e` keeps the exception chain, so it is allowed.
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


@pytest.mark.parametrize("path", _exposure_modules(), ids=lambda p: p.name)
def test_router_does_not_reference_exception_outside_logging(path: Path) -> None:
    """The exception variable is referenced only by logging and the message catalog.

    The detected result must match `_BASELINE` exactly (a ratchet).
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


@pytest.mark.parametrize("path", _exposure_modules(), ids=lambda p: p.name)
def test_router_does_not_import_traceback(path: Path) -> None:
    """Routers have no path that stringifies a stack trace."""
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
    """No module name that is absent from _BASELINE remains."""
    known = {p.name for p in _exposure_modules()}
    assert not set(_BASELINE) - known
    assert not _TRACEBACK_BASELINE - known


class TestVisitorItself:
    """The checking logic itself is sound."""

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
        """A leak through a property such as FileUploadError.user_message."""
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
        """Delegation to a helper inside a router is also detected as a leak path."""
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
    """A 422 (RequestValidationError) returns catalog-equivalent text.

    A missing required Form(...) parameter and the like is detected by
    FastAPI/Pydantic before reaching a router, so the AST check above (which
    targets `except ... as e`) cannot catch it. Verify with real requests that
    the global handler covers this path.
    """

    def test_htmx_request_gets_toast_not_partial(self, client):
        """On the htmx path, notify with a toast and return no body.

        This handler is shared by all forms and does not know the originating
        hx-target. Returning partial HTML would be swapped into the main container
        (e.g. persona edit) and wipe the form and its input. Use a toast, which
        keeps the input and leaves the DOM alone.
        """
        response = client.put(
            "/persona/some-id", data={"age": "30"}, headers={"HX-Request": "true"}
        )

        assert response.status_code == 422
        # No body is returned (there is no body to swap in)
        assert response.text == ""
        # The message arrives as an HX-Trigger toast
        payload = json.loads(response.headers["HX-Trigger"])
        assert "入力内容を確認してください" in payload["showToast"]["message"]

    def test_json_client_response_is_not_a_toast(self, test_app, login_as):
        """JSON API clients keep the standard 422 response (JSON body).

        The `client` fixture always sends HX-Request, so prepare a logged-in client
        that does not (CSRF passes via X-Requested-With).
        """
        from fastapi.testclient import TestClient

        with TestClient(
            test_app, headers={"X-Requested-With": "XMLHttpRequest"}
        ) as json_client:
            login_as(json_client)
            response = json_client.put("/persona/some-id", data={"age": "30"})

        assert response.status_code == 422
        assert "application/json" in response.headers["content-type"]
        assert "HX-Trigger" not in response.headers

    def test_htmx_response_does_not_echo_user_input(self, client):
        """User input carried in `input` of `exc.errors()` is not echoed."""
        payload = "<img src=x onerror=alert(1)>"
        response = client.put(
            "/persona/some-id",
            data={"age": payload},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 422
        # No user input appears in either the body or the header (toast text)
        assert payload not in response.text
        assert payload not in response.headers.get("HX-Trigger", "")
        assert "onerror" not in response.headers.get("HX-Trigger", "")

    def test_htmx_response_does_not_expose_validation_internals(self, client):
        """Pydantic's internal representation (type/loc/msg) appears in neither the body nor the header."""
        response = client.put(
            "/persona/some-id", data={}, headers={"HX-Request": "true"}
        )

        assert response.status_code == 422
        trigger = response.headers.get("HX-Trigger", "")
        for internal in ("int_parsing", '"loc"', "Field required", "body"):
            assert internal not in response.text
            assert internal not in trigger

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
        """Every router that uses Form(...) returns toast text consistently."""
        response = getattr(client, method)(
            path, data=data, headers={"HX-Request": "true"}
        )

        # Anything other than 422 (e.g. rejected earlier by CSRF or other validation) is out of scope here
        if response.status_code == 422:
            # HX-Trigger is escaped as \uXXXX because of the latin-1 limit, so parse it
            trigger = response.headers.get("HX-Trigger", "")
            message = json.loads(trigger)["showToast"]["message"] if trigger else ""
            assert "入力内容を確認してください" in message
            # Internal representations appear in neither the body nor the header
            assert "Field required" not in response.text
            assert "Field required" not in trigger


class TestTransientErrorsUseToast:
    """TRANSIENT notifies with a toast without rewriting the screen.

    VALIDATION and TRANSIENT are raised as the same Manager exception type, so a
    router's ``except`` clause cannot tell them apart by type. Verify with real
    requests that branching on `ErrorKind` actually works.
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
        """Return a Manager mock whose update_persona raises `exc`.

        Replace the router's `get_*_manager()` rather than the Manager class to
        avoid building a real instance (which needs AWS credentials). CI has no
        credentials, so patching the class method would raise PersonaStoreError at
        construction and never reach the intended path.
        """
        manager = Mock()
        manager.update_persona.side_effect = exc
        return manager

    @patch("web.routers.persona.get_persona_manager")
    def test_transient_returns_empty_body_with_toast(self, mock_get_manager, client):
        """No body is returned, so the input form is not wiped."""
        mock_get_manager.return_value = self._manager_raising(
            PersonaManagerError(
                "persona update failed (PersonaStoreError)",
                code=ErrorCode.PERSONA_OPERATION_FAILED,
            )
        )

        response = client.put("/persona/p1", data=self._FORM)

        assert response.text == ""
        payload = json.loads(response.headers["HX-Trigger"])
        assert "ペルソナの処理中にエラー" in payload["showToast"]["message"]

    @patch("web.routers.persona.get_persona_manager")
    def test_validation_still_returns_partial_html(self, mock_get_manager, client):
        """Errors fixed by correcting the input return a body (not a toast)."""
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
        # Since a body is returned, a mark that lets htmx swap it is required
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

    _SURVEY_FORM = {
        "template_id": "t1",
        "persona_count": "1000",
        "filters_json": "{}",
        "datasource": "nemotron",
    }

    @patch("web.routers.survey.get_execution_manager")
    def test_survey_create_transient_returns_toast(self, mock_get_manager, client):
        """An S3/DuckDB failure while counting (TRANSIENT) notifies with a toast and keeps the input.

        Replacing the screen with an inline error when there is no filter to loosen
        would make the user redo the settings even though a retry would work
        (Issue #118).
        """
        from src.managers.survey_execution_manager import SurveyExecutionError

        manager = Mock()
        manager.create_survey.side_effect = SurveyExecutionError(
            "available persona count failed (RuntimeError)",
            code=ErrorCode.SURVEY_OPERATION_FAILED,
        )
        mock_get_manager.return_value = manager

        response = client.post(
            "/survey/execute", data=self._SURVEY_FORM, headers={"HX-Request": "true"}
        )

        # No body; notify with an HX-Trigger toast (the screen is not rewritten)
        assert response.text == ""
        payload = json.loads(response.headers["HX-Trigger"])
        assert "showToast" in payload

    @patch("web.routers.survey.get_execution_manager")
    def test_survey_create_config_error_stays_inline(self, mock_get_manager, client):
        """CONFIG (e.g. dataset not downloaded) is explained by replacing the region (no toast)."""
        from src.managers.survey_execution_manager import (
            SurveyExecutionManagerError,
        )

        manager = Mock()
        manager.create_survey.side_effect = SurveyExecutionManagerError(
            "nemotron parquet uri is not configured",
            code=ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED,
        )
        mock_get_manager.return_value = manager

        response = client.post(
            "/survey/execute", data=self._SURVEY_FORM, headers={"HX-Request": "true"}
        )

        assert "HX-Trigger" not in response.headers
        assert response.headers.get("X-Render-Response") == "true"

    @patch("web.routers.survey.get_execution_manager")
    def test_survey_create_transient_does_not_leak(self, mock_get_manager, client):
        """The toast path also does not leak exception text (such as S3 paths)."""
        from src.managers.survey_execution_manager import SurveyExecutionError

        secret = "s3://prod-bucket/secret.parquet"
        manager = Mock()
        manager.create_survey.side_effect = SurveyExecutionError(
            f"IO Error: {secret}", code=ErrorCode.SURVEY_OPERATION_FAILED
        )
        mock_get_manager.return_value = manager

        response = client.post(
            "/survey/execute", data=self._SURVEY_FORM, headers={"HX-Request": "true"}
        )

        assert secret not in response.headers.get("HX-Trigger", "")
        assert secret not in response.text


class TestGenericExceptionsDoNotReplaceContent:
    """A mutating `except Exception` does not return an error partial.

    An unknown exception without a code falls to `ErrorKind.TRANSIENT`. Returning
    it as a body would replace the `hx-target` (main content or list) with the
    error display and lose all of the user's input and selections. Also, htmx
    1.9.10 does not swap non-2xx bodies, so instead of being replaced **the text
    itself never reaches the screen**. Both outcomes are bad, so the generic
    handler of mutating requests (POST/PUT/DELETE) uses `toast_response()`.

    The AST check exists to prevent the same failure from recurring when a
    router is added (the same "guarantee by structure" approach as
    `_ExposureVisitor`).
    """

    #: Responses returned as 2xx are swapped by htmx, so the message arrives. Whether to replace them
    #: is a separate design decision, so this check covers only non-2xx responses, which do not arrive.
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
        """Confirm the check actually works (it does not always return empty)."""
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
    """Error partials returned with a non-2xx status reach the screen.

    htmx 1.9.10 does not swap bodies unless ``status>=200 && status<400 &&
    status!==204``. So even if a 4xx/5xx error partial is returned, the text is
    generated but never applied to the DOM, and only the generic fallback of
    `app.js` is shown. This is why
    half of the 84-entry catalog built in #112 never reached the screen.

    Only responses that the server marks with ``X-Render-Response: true``
    (``mark_renderable()``) are swapped by the client. This check guarantees via
    the AST that an error partial returned with non-2xx carries the mark, so the
    same failure does not recur when a router is added (the same "guarantee by
    structure" approach as the `str(e)` exposure check of #112).

    For why swapping is not allowed by status code alone, see the cause in Issue
    #117 (a generic partial replaces the main content and wipes the whole form).
    """

    def _unreachable(self, modules: list[Path] | None = None) -> list[str]:
        """Collect the non-2xx error partials that lack the mark."""
        found = []
        for path in modules if modules is not None else _router_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))

            # Position of calls passed as the argument of mark_renderable(...)
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
                # 2xx is swapped by htmx as is, so no mark is needed
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
        """Confirm the check actually works (it does not always return empty)."""
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
        """2xx is swapped by htmx, so the mark is not enforced."""
        module = tmp_path / "ok.py"
        module.write_text(
            "async def handler(request):\n"
            "    return templates.TemplateResponse(\n"
            "        request, 'partials/error.html', {'error': 'x'})\n",
            encoding="utf-8",
        )
        assert self._unreachable([module]) == []


class TestErrorTemplatesAreConsolidated:
    """Keep the error display templates consolidated.

    They used to be split into five kinds, with `error` and `message` variable
    names mixed. Routers had no basis for choosing one individually, and
    templates that render `{{ error }}` while receiving `message` (seven places
    in settings.py) **showed an empty error box**.
    """

    _TEMPLATES_DIR = Path(__file__).parent.parent.parent / "web" / "templates"

    #: Templates removed in the consolidation. Notice if one comes back.
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
        """The variable name is unified to `error` (prevents `message` creeping back)."""
        for name in ("partials/error_inline.html", "partials/error_banner.html"):
            body = (self._TEMPLATES_DIR / name).read_text(encoding="utf-8")
            assert "{{ error }}" in body, f"{name} が error を描画していない"
            assert "{{ message }}" not in body, f"{name} に message が残っている"

    @staticmethod
    def _render(name: str, **ctx: object) -> str:
        """Render the template for real (to verify the output, not annotations)."""
        from jinja2 import Environment, FileSystemLoader

        env = Environment(
            loader=FileSystemLoader(
                Path(__file__).parent.parent.parent / "web" / "templates"
            )
        )
        return env.get_template(name).render(**ctx)

    def test_inline_template_has_no_reload_prompt(self):
        """An error fixed by correcting the input must not prompt "refresh".

        Refreshing discards the input the user is trying to fix.
        """
        out = self._render("partials/error_inline.html", error="入力が長すぎます")

        assert "入力が長すぎます" in out
        assert "location.reload" not in out
        assert "ページを更新" not in out

    def test_banner_template_offers_recovery(self):
        """A region-replacing error offers a way to retry."""
        out = self._render("partials/error_banner.html", error="見つかりません")

        assert "見つかりません" in out
        assert "location.reload" in out

    def test_banner_template_can_show_a_back_link(self):
        """NOT_FOUND can show a return link."""
        out = self._render(
            "partials/error_banner.html",
            error="ペルソナが見つかりません",
            back_url="/persona/management",
            back_label="ペルソナ一覧へ",
        )

        assert "/persona/management" in out
        assert "ペルソナ一覧へ" in out

    def test_dom_coupled_templates_keep_their_contracts(self):
        """The two kinds with a DOM contract are not merged, and the contract is kept.

        Moving the htmx swap-target ID and region-clearing contract into a shared
        template would break replacement, so the outer part stays in each template.
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
    """htmx handlers are not registered twice.

    afterSwap / beforeRequest / afterRequest were registered in both base.html
    and app.js, so the fade-in was applied twice.
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


class TestHttpExceptionRendersHtmlPage:
    """`HTTPException` is not returned to the browser as raw JSON.

    When a full-page endpoint (`GET /persona/{id}` etc.) does
    `raise HTTPException(404, ...)`, FastAPI's default handler returns
    `{"detail": "..."}` and **raw JSON is shown in the browser**. Only this path
    missed the NOT_FOUND display (text plus a return link).
    """

    _BROWSER = {"accept": "text/html,application/xhtml+xml"}

    @patch("web.routers.persona.get_persona_manager")
    def test_persona_not_found_renders_html(self, mock_get_mgr, test_app, login_as):
        from fastapi.testclient import TestClient

        mgr = Mock()
        # A persona that cannot be read or does not exist (detail goes through the access decision)
        mgr.get_persona_detail.side_effect = CodedError(
            "not readable", code=ErrorCode.PERSONA_NOT_FOUND
        )
        mock_get_mgr.return_value = mgr

        with TestClient(test_app, follow_redirects=False) as browser:
            login_as(browser)
            response = browser.get("/persona/nope", headers=self._BROWSER)

        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        # Raw JSON is not returned
        assert not response.text.strip().startswith("{")
        assert '"detail"' not in response.text
        # A single screen that does not distinguish the reason, plus a return link. No per-resource wording.
        assert "<h1" in response.text and "ページが見つかりません" in response.text
        assert "ペルソナが見つかりません" not in response.text
        assert 'href="/persona/management"' in response.text

    @patch("web.routers.discussion.get_discussion_manager")
    def test_discussion_not_found_renders_html(self, mock_get_mgr, test_app, login_as):
        from fastapi.testclient import TestClient

        from src.models.errors import CodedError, ErrorCode

        # The Manager raises DISCUSSION_NOT_FOUND for a discussion that cannot be read or does not exist
        mgr = Mock()
        mgr.get_discussion.side_effect = CodedError(
            "discussion not found", code=ErrorCode.DISCUSSION_NOT_FOUND
        )
        mock_get_mgr.return_value = mgr

        with TestClient(test_app, follow_redirects=False) as browser:
            login_as(browser)
            response = browser.get("/discussion/nope", headers=self._BROWSER)

        assert response.status_code == 404
        assert not response.text.strip().startswith("{")
        assert "ページが見つかりません" in response.text
        assert "議論が見つかりません" not in response.text
        assert 'href="/discussion/results"' in response.text

    def test_json_api_keeps_json_response(self, test_app, login_as):
        """`/api/*` is for JSON clients, so the default behavior is kept."""
        from fastapi.testclient import TestClient

        with TestClient(test_app, follow_redirects=False) as browser:
            login_as(browser)
            # JSON is returned even when Accept includes text/html
            response = browser.get("/api/personas/nope", headers=self._BROWSER)

        assert "application/json" in response.headers["content-type"]

    def test_htmx_request_is_not_given_a_full_page(self, test_app, login_as):
        """The htmx path returns a partial by design, so it is not replaced with a full page."""
        from fastapi.testclient import TestClient

        with TestClient(test_app, follow_redirects=False) as hx:
            login_as(hx)
            response = hx.get(
                "/persona/nope",
                headers={"HX-Request": "true", **self._BROWSER},
            )

        # A full page extending base.html (nav, footer) is not returned
        assert "<footer" not in response.text

    def test_error_page_does_not_leak_exception_details(self, test_app, login_as):
        """An internal error (500) does not show exception content on screen."""
        from fastapi.testclient import TestClient

        secret = "arn:aws:dynamodb:ap-northeast-1:999999999999:table/prod"
        mgr = Mock()
        mgr.get_persona.side_effect = RuntimeError(secret)

        with patch("web.routers.persona.get_persona_manager", return_value=mgr):
            with TestClient(
                test_app, raise_server_exceptions=False, follow_redirects=False
            ) as browser:
                login_as(browser)
                response = browser.get("/persona/p1", headers=self._BROWSER)

        assert secret not in response.text


class TestToastIsVisibleRegardlessOfScroll:
    """The toast is pinned to the top right of the screen.

    For TRANSIENT and 422 the toast is the only notification, so it must be
    visible regardless of scroll position. It used to be inserted inline at the
    top of `main`, so it was not visible when a long form was scrolled down.
    """

    _ROOT = Path(__file__).parent.parent.parent

    def test_container_is_fixed_positioned(self):
        base = (self._ROOT / "web" / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        # Extract the class of the element that contains id="flash-messages"
        import re

        m = re.search(r'id="flash-messages"[^>]*', base, re.DOTALL)
        assert m, "flash-messages コンテナが無い"
        tag = m.group(0)
        for cls in ("fixed", "z-toast"):
            assert cls in tag, f"{cls} が無い（スクロールで見えなくなる）"
        # The live region is two containers, one per kind. Do not nest them.
        assert "aria-live" not in tag, "外側の aria-live は二重の読み上げになる"
        assert re.search(r'id="toast-status"[^>]*role="status"', base)
        assert re.search(r'id="toast-alert"[^>]*role="alert"', base)

    def test_required_classes_exist_in_built_css(self):
        """The built CSS contains the needed utilities.

        `web/static/css/tailwind.css` is a committed build artifact, so adding a
        class in a template has no effect if the rebuild is forgotten.
        """
        import re

        css = (self._ROOT / "web" / "static" / "css" / "tailwind.css").read_text(
            encoding="utf-8"
        )
        missing = [
            cls
            for cls in (
                "fixed",
                "top-20",
                "right-4",
                "max-w-sm",
                "pointer-events-none",
                "pointer-events-auto",
                "shadow-lg",
            )
            if not re.search(r"\." + re.escape(cls) + r"(?=[,{:\s])", css)
        ]
        assert missing == [], (
            f"ビルド済みCSSにクラスが無い。./scripts/build-css.sh --minify "
            f"を実行すること: {missing}"
        )

    def test_toast_element_accepts_clicks(self):
        """The container is pointer-events-none, so restore it on the toast itself.

        Otherwise the close button cannot be clicked.
        """
        app_js = (self._ROOT / "web" / "static" / "js" / "app.js").read_text(
            encoding="utf-8"
        )
        assert "pointer-events-auto" in app_js


@pytest.mark.api
class TestPersistedFailuresDoNotLeak:
    """The persisted failure reason is not exception text (Issue #118).

    A survey run is driven by the runner (a web background task), so a failure
    is not returned to the originating request. The reason is saved in the DB
    and rendered by a later GET as a separate request. A router `except` check
    such as `test_router_does_not_reference_exception_outside_logging` cannot
    catch it, and saving `str(e)` would put S3 paths, role ARNs and botocore
    exception text straight on screen.

    Check both the model side and the template side mechanically.
    """

    _ROOT = Path(__file__).parent.parent.parent

    #: Field names that used to store exception text (now removed).
    _LEAKY_FIELD = "error_message"

    def test_survey_model_has_no_message_field(self):
        """`Survey` has no place to store exception text."""
        from src.models.survey import Survey

        fields = {f.name for f in dataclasses.fields(Survey)}
        assert self._LEAKY_FIELD not in fields, (
            f"Survey.{self._LEAKY_FIELD} が復活している。失敗理由は "
            "error_code / error_context で保存し、文言は表示層で解決すること"
        )
        assert {"error_code", "error_context"} <= fields

    def test_job_model_keeps_a_code_not_the_text(self):
        """`Job` keeps a failure as `error_code` and has no place to store exception text."""
        from src.models.job import Job

        fields = {f.name for f in dataclasses.fields(Job)}
        assert "error_code" in fields
        assert "error" not in fields and self._LEAKY_FIELD not in fields

    def test_job_manager_saves_the_code(self):
        """`JobManager` does not pass exception text (str(e)) when saving a failure."""
        path = self._ROOT / "src" / "managers" / "job_manager.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update_failed"
        ]
        assert calls, "update_failed の呼び出しが見つからない"
        for call in calls:
            assert not any(_stringifies_something(arg) for arg in call.args)

    def test_old_job_records_drop_the_text(self):
        """The `error` (exception text) of an old record is dropped on read, leaving only the code."""
        from src.models.job import Job

        job = Job.from_dict(
            {
                "id": "j",
                "status": "failed",
                "created_at": "2026-10-15T00:00:00",
                "updated_at": "2026-10-15T00:00:00",
                "error": "arn:aws:s3:::secret-bucket failed",
            }
        )
        assert job.error_code == "unknown"
        assert "secret" not in repr(job)

    def test_managers_do_not_store_exception_text(self):
        """The Manager layer does not assign exception text to a model field."""
        offenders: list[str] = []
        managers = self._ROOT / "src" / "managers"
        for path in sorted(managers.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                # Whether the right-hand side stringifies an exception, e.g. str(...) or f"{e}"
                if not _stringifies_something(node.value):
                    continue
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and "error" in target.attr
                        and "code" not in target.attr
                    ):
                        offenders.append(f"{path.name}:{node.lineno} -> {target.attr}")
        assert offenders == [], (
            "例外を文字列化してモデルのエラーフィールドへ保存している。"
            f"エラーコードを保存すること（Issue #118）: {offenders}"
        )

    def test_templates_do_not_render_stored_exception_text(self):
        """Templates do not render `*.error_message`."""
        offenders: list[str] = []
        for path in sorted((self._ROOT / "web" / "templates").rglob("*.html")):
            src = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(src.splitlines(), start=1):
                if f".{self._LEAKY_FIELD}" in line and "{{" in line:
                    offenders.append(f"{path.name}:{lineno}")
        assert offenders == [], (
            "保存された例外文を描画している。Router が "
            f"user_message_for_code() で解決した文言を渡すこと: {offenders}"
        )

    def test_stored_code_resolves_to_catalog_wording(self):
        """Text can be resolved from the stored form (a string)."""
        from web.error_messages import FALLBACK_MESSAGE, user_message_for_code

        message = user_message_for_code(
            ErrorCode.SURVEY_AVAILABLE_PERSONAS_TOO_FEW.value,
            {"available_count": 30, "min_count": 100},
        )
        assert message != FALLBACK_MESSAGE
        assert "30" in message and "100" in message

    def test_unknown_stored_code_falls_back(self):
        """No raw text appears even for a code written by another revision."""
        from web.error_messages import FALLBACK_MESSAGE, user_message_for_code

        assert user_message_for_code("code_from_the_future") == FALLBACK_MESSAGE
        assert user_message_for_code(None) == FALLBACK_MESSAGE

    def test_legacy_records_discard_stored_message(self):
        """Reading records from before #118 does not bring exception text back."""
        from src.models.survey import Survey

        survey = Survey.from_dict(
            {
                "id": "s1",
                "name": "n",
                "description": "",
                "template_id": "t1",
                "persona_count": 100,
                "status": "error",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "error_message": "Invalid S3 URI format: s3://secret-bucket/key",
            }
        )
        assert survey.error_code == ErrorCode.SURVEY_EXECUTION_FAILED.value
        assert "secret-bucket" not in str(survey.to_dict())


class TestAuthErrorBodies:
    """The body of login errors (401, 403, 503) shows no internal values.

    The body is only {detail, error_code}, and detail is catalog text.
    """

    _ALLOWED_KEYS = {"detail", "error_code"}

    def _assert_catalog_body(self, response, code: ErrorCode) -> None:
        from src.models.errors import CodedError
        from web.error_messages import user_message_for

        body = response.json()
        assert set(body) == self._ALLOWED_KEYS
        assert body["error_code"] == code.value
        assert body["detail"] == user_message_for(CodedError(code=code))

    def test_unauthenticated_api_body(self, test_app):
        from fastapi.testclient import TestClient

        with TestClient(test_app) as anonymous:
            anonymous.cookies.set("session_id", "secret-session-value")
            response = anonymous.get("/api/personas")

        assert response.status_code == 401
        self._assert_catalog_body(response, ErrorCode.AUTH_REQUIRED)
        assert "secret-session-value" not in response.text

    def test_csrf_rejection_body(self, test_app, login_as):
        from fastapi.testclient import TestClient

        with TestClient(test_app) as no_header:
            login_as(no_header)
            response = no_header.post("/api/discussions", json={})

        assert response.status_code == 403
        self._assert_catalog_body(response, ErrorCode.CSRF_FAILED)

    def test_lookup_unavailable_body(self, test_app, monkeypatch):
        from fastapi.testclient import TestClient

        from src.models.errors import LookupUnavailableError
        from web.routers import auth

        manager = Mock()
        manager.check_request.side_effect = LookupUnavailableError(
            "arn:aws:dynamodb:ap-northeast-1:999999999999:table/Sessions"
        )
        monkeypatch.setattr(auth, "_auth_manager", manager)

        with TestClient(test_app) as browser:
            browser.cookies.set("session_id", "sid")
            response = browser.get("/api/personas")

        assert response.status_code == 503
        self._assert_catalog_body(response, ErrorCode.LOOKUP_UNAVAILABLE)
        assert "arn:aws" not in response.text


class TestAuthzErrorBodies:
    """The body of the code API shows no internal values, exception text or IDs.

    The body is only {detail, error_code}, and detail is catalog text.
    The ACCESS_DENIED status code and body are checked per kind by
    ``tests/api/test_exception_handlers.py``.
    """

    _ALLOWED_KEYS = {"detail", "error_code"}
    _SECRET = "arn:aws:dynamodb:ap-northeast-1:999999999999:table/Jobs-secret"

    def _assert_catalog_body(self, response, code: ErrorCode) -> None:
        from src.models.errors import CodedError
        from web.error_messages import FALLBACK_MESSAGE, user_message_for

        body = response.json()
        assert set(body) == self._ALLOWED_KEYS
        assert body["error_code"] == code.value
        expected = (
            FALLBACK_MESSAGE
            if code is ErrorCode.UNKNOWN
            else user_message_for(CodedError(code=code))
        )
        assert body["detail"] == expected
        assert self._SECRET not in response.text

    def _jobs(self, monkeypatch, **behaviour: object) -> Mock:
        from src.managers.job_manager import JobManager
        from web.routers import api

        lookup = Mock()
        for name, value in behaviour.items():
            setattr(lookup.get_job, name, value)
        monkeypatch.setattr(
            api,
            "_job_manager",
            JobManager(job_store=Mock(), lookup_service=lookup),
        )
        return lookup

    def test_role_required_body(self, test_app, no_role_user):
        from fastapi.testclient import TestClient

        with TestClient(test_app) as browser:
            no_role_user.login(browser)
            response = browser.get("/api/personas")

        assert response.status_code == 403
        self._assert_catalog_body(response, ErrorCode.ROLE_REQUIRED)

    def test_job_not_found_body(self, test_app, member_b, monkeypatch):
        from fastapi.testclient import TestClient

        self._jobs(monkeypatch, return_value=None)
        with TestClient(test_app) as browser:
            member_b.login(browser)
            response = browser.get("/api/jobs/job-secret-1")

        assert response.status_code == 404
        self._assert_catalog_body(response, ErrorCode.JOB_NOT_FOUND)
        assert "job-secret-1" not in response.text

    def test_lookup_unavailable_body(self, test_app, member_a, monkeypatch):
        from fastapi.testclient import TestClient

        from src.services.authz_lookup_service import AuthzLookupError

        self._jobs(
            monkeypatch,
            side_effect=AuthzLookupError(self._SECRET, reason="unreachable"),
        )
        with TestClient(test_app) as browser:
            member_a.login(browser)
            response = browser.get("/api/jobs/job-1")

        assert response.status_code == 503
        self._assert_catalog_body(response, ErrorCode.LOOKUP_UNAVAILABLE)

    def test_request_invalid_body(self, test_app, member_a):
        from fastapi.testclient import TestClient

        with TestClient(
            test_app, headers={"X-Requested-With": "XMLHttpRequest"}
        ) as api_client:
            member_a.login(api_client)
            response = api_client.post(
                "/api/discussions", json={"persona_ids": ["<script>x</script>"]}
            )

        assert response.status_code == 422
        self._assert_catalog_body(response, ErrorCode.REQUEST_INVALID)
        assert "<script>" not in response.text

    def test_http_error_body(self, test_app, member_a):
        from fastapi.testclient import TestClient

        with TestClient(test_app) as browser:
            member_a.login(browser)
            response = browser.get("/api/no-such-endpoint")

        assert response.status_code == 404
        self._assert_catalog_body(response, ErrorCode.HTTP_ERROR)

    def test_unknown_body(self, test_app, member_a, monkeypatch):
        from fastapi.testclient import TestClient

        self._jobs(monkeypatch, side_effect=RuntimeError(self._SECRET))
        with TestClient(test_app, raise_server_exceptions=False) as browser:
            member_a.login(browser)
            response = browser.get("/api/jobs/job-1")

        assert response.status_code == 500
        self._assert_catalog_body(response, ErrorCode.UNKNOWN)


def _stringifies_something(node: ast.AST) -> bool:
    """Whether this is stringification via `str(...)` or an f-string."""
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
    )
