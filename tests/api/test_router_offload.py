"""Static check that async router entry points do not call a Manager directly on the event loop.

A Manager call may include an authorization read (a DynamoDB read via ``ResourceLookup``)
or S3/DuckDB I/O. Calling it directly in an async entry point blocks the event loop, so
other requests wait too.

Within each ``async def`` in ``web/routers/*.py``, the AST is used to count places that
call a method of the return value of ``get_*_manager()`` (or a variable holding it)
directly. The following are not counted, as they are handed to a thread:

- A reference passed without being called (``await _run(manager.method, ...)``, or an
  argument of ``run_in_threadpool``, ``anyio.to_thread.run_sync`` or ``loop.run_in_executor``)
- Inside a nested ``def`` or ``lambda`` (the body of a function handed to a thread). A
  sync function called directly from the async body (a module-level function or a nested
  ``def``) runs on the event loop, so its inside is counted

Calls that may be direct are pinned in ``ALLOWED_DIRECT_CALLS`` with a reason. The test
fails if an entry no longer exists (stale exceptions are not kept).
"""

import ast
import re
from collections import defaultdict
from pathlib import Path

_ROUTERS_DIR = Path(__file__).parent.parent.parent / "web" / "routers"

_GETTER = re.compile(r"^get_\w*manager$")
_SCOPE_BREAK = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_NO_IO = "I/O をしない"
_ROLE_ONLY = "ロールだけを見る（I/O をしない）"
_OUT_OF_SCOPE = "Loop-back 1 の対象外（報告済み）"

#: ``(file, function, method)`` -> why a direct call is acceptable.
#: Calls that include an authorization read do not belong here (hand them to a thread).
ALLOWED_DIRECT_CALLS: dict[tuple[str, str, str], str] = {
    # persona.py
    (
        "persona.py",
        "generate_persona",
        "ensure_data_agent_allowed",
    ): f"ロールと起動時の設定値（DATA_AGENT_ALLOWED）を見るだけで{_NO_IO}",
    (
        "persona.py",
        "generate_persona",
        "validate_persona_source_file",
    ): f"受け取ったバイト列の拡張子・大きさ・空を見るだけで{_NO_IO}",
    (
        "persona.py",
        "_detail_context",
        "avatar_upload_enabled",
    ): f"S3 の設定があるかを属性で見るだけで{_NO_IO}",
    (
        "persona.py",
        "_render_avatar_editor",
        "avatar_upload_enabled",
    ): f"S3 の設定があるかを属性で見るだけで{_NO_IO}",
    (
        "persona.py",
        "upload_knowledge_file_preview",
        "check_knowledge_file_content",
    ): f"変換した文字列の長さを見るだけで{_NO_IO}",
    # auth.py
    (
        "auth.py",
        "login",
        "sanitize_next",
    ): f"戻り先の文字列を検査するだけで{_NO_IO}",
    (
        "auth.py",
        "login_submit",
        "sanitize_next",
    ): f"戻り先の文字列を検査するだけで{_NO_IO}",
    # discussion.py
    (
        "discussion.py",
        "get_discussion_detail",
        "get_default_categories",
    ): f"固定のカテゴリーを返すだけで{_NO_IO}",
    (
        "discussion.py",
        "regenerate_insights",
        "get_default_categories",
    ): f"固定のカテゴリーを返すだけで{_NO_IO}",
    # settings.py
    (
        "settings.py",
        "settings_page",
        "get_data_agent_settings",
    ): f"ロールを見て、メモリ上の設定値を返すだけで{_NO_IO}",
    (
        "settings.py",
        "settings_page",
        "is_advanced_generation_section_offered",
    ): f"環境変数の ON/OFF（メモリ上の設定値）を返すだけで{_NO_IO}",
    (
        "settings.py",
        "data_agent_status",
        "get_data_agent_settings",
    ): f"ロールを見て、メモリ上の設定値を返すだけで{_NO_IO}",
    (
        "settings.py",
        "save_data_agent_settings",
        "get_data_agent_settings",
    ): f"ロールを見て、メモリ上の設定値を返すだけで{_NO_IO}",
    (
        "settings.py",
        "save_data_agent_settings",
        "save_data_agent_settings",
    ): f"ロールを見て、メモリ上の設定値を書き換えるだけで{_NO_IO}（保存先はない）",
    # survey.py
    (
        "survey.py",
        "download_nemotron",
        "ensure_nemotron_downloadable",
    ): f"ロールと起動時の設定値（NEMOTRON_SOURCE）を見るだけで{_NO_IO}",
    (
        "survey.py",
        "dwh_extract",
        "ensure_data_agent_allowed",
    ): f"ロールと起動時の設定値（DATA_AGENT_ALLOWED）を見るだけで{_NO_IO}",
    ("survey.py", "upload_custom_step1", "ensure_manageable"): _ROLE_ONLY,
    ("survey.py", "preview_persona_prompt", "ensure_manageable"): _ROLE_ONLY,
    ("survey.py", "_import", "ensure_manageable"): _ROLE_ONLY,
    ("survey.py", "dwh_extract", "ensure_manageable"): _ROLE_ONLY,
    ("survey.py", "dwh_preview", "ensure_manageable"): _ROLE_ONLY,
    (
        "survey.py",
        "_column_mapping",
        "get_standard_columns",
    ): f"固定の列定義を返すだけで{_NO_IO}",
    (
        "survey.py",
        "custom_dataset_detail",
        "get_standard_columns",
    ): f"固定の列定義を返すだけで{_NO_IO}",
    # api.py
    (
        "api.py",
        "generate_personas",
        "submit",
    ): (
        f"{_OUT_OF_SCOPE}。ロールの確認とジョブの記録（DynamoDB の書き込み）で、判定の"
        "読み取りではない。v1 より前からある形"
    ),
    (
        "api.py",
        "run_discussion",
        "submit",
    ): (
        f"{_OUT_OF_SCOPE}。ロールの確認とジョブの記録（DynamoDB の書き込み）で、判定の"
        "読み取りではない。v1 より前からある形"
    ),
    (
        "api.py",
        "generate_insights",
        "submit",
    ): (
        f"{_OUT_OF_SCOPE}。ロールの確認とジョブの記録（DynamoDB の書き込み）で、判定の"
        "読み取りではない。v1 より前からある形"
    ),
}


def _is_getter_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and bool(_GETTER.match(node.func.id))
    )


def _same_scope(body: list[ast.stmt]) -> list[ast.AST]:
    """Every node of ``body`` outside nested ``def``/``lambda`` bodies."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = [n for n in body if not isinstance(n, _SCOPE_BREAK)]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(
            c for c in ast.iter_child_nodes(node) if not isinstance(c, _SCOPE_BREAK)
        )
    return out


def _manager_names(body: list[ast.stmt]) -> set[str]:
    """Variables that hold a ``get_*_manager()`` result in this scope."""
    names: set[str] = set()
    for node in _same_scope(body):
        if isinstance(node, ast.Assign) and _is_getter_call(node.value):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_getter_call(node.value)
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    return names


def _is_generator(fn: ast.FunctionDef) -> bool:
    """Calling a generator function only builds the generator; its body is not run.

    ``StreamingResponse`` iterates a synchronous generator in a worker thread
    (Starlette's ``iterate_in_threadpool``), so its body is not on the loop.
    """
    return any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _same_scope(fn.body))


def _nested_sync_defs(body: list[ast.stmt]) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in body if isinstance(n, ast.FunctionDef)}


def scan_source(source: str, filename: str) -> dict[tuple[str, str, str], list[int]]:
    """Direct manager calls that run on the event loop, keyed by (file, function, method).

    ``function`` is the ``async def`` holding the call, or the synchronous
    function it calls directly (whose body also runs on the event loop).
    """
    tree = ast.parse(source)
    module_defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_names(fn: ast.AST) -> set[str]:
        names: set[str] = set()
        parent = parents.get(fn)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names |= _manager_names(parent.body)
            parent = parents.get(parent)
        return names

    found: dict[tuple[str, str, str], list[int]] = defaultdict(list)

    def visit(
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        names: set[str],
        callable_defs: dict[str, ast.FunctionDef],
        seen: set[int],
    ) -> None:
        if id(fn) in seen:
            return
        seen.add(id(fn))
        names = names | _manager_names(fn.body)
        local_defs = {**callable_defs, **_nested_sync_defs(fn.body)}
        for node in _same_scope(fn.body):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and (
                _is_getter_call(func.value)
                or (isinstance(func.value, ast.Name) and func.value.id in names)
            ):
                found[(filename, fn.name, func.attr)].append(node.lineno)
            elif isinstance(func, ast.Name) and func.id in local_defs:
                # Calling a sync function directly runs its body on the event loop too
                # (a generator function does not run its body when called, so it is not counted)
                helper = local_defs[func.id]
                if _is_generator(helper):
                    continue
                # A module-level function does not see the caller's variables; a nested def does
                is_module_def = module_defs.get(helper.name) is helper
                visit(helper, set() if is_module_def else names, local_defs, seen)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            visit(node, enclosing_names(node), module_defs, set())
    return dict(found)


def _scan_routers() -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in sorted(_ROUTERS_DIR.glob("*.py")):
        found.update(scan_source(path.read_text(encoding="utf-8"), path.name))
    return found


def _describe(items: dict[tuple[str, str, str], list[int]]) -> str:
    return "\n".join(
        f"  {file}:{lines} {func}() -> .{method}()"
        for (file, func, method), lines in sorted(items.items())
    )


class TestManagerCallsAreOffloaded:
    """Manager calls in async entry points are handed to a thread."""

    def test_no_unlisted_direct_manager_call_on_the_event_loop(self):
        unexpected = {
            key: lines
            for key, lines in _scan_routers().items()
            if key not in ALLOWED_DIRECT_CALLS
        }
        assert not unexpected, (
            "async の入口で Manager をスレッドに渡さずに呼んでいる。"
            "run_in_threadpool / anyio.to_thread.run_sync / _run() に渡すか、"
            "I/O をしないなら理由を付けて ALLOWED_DIRECT_CALLS に入れる:\n"
            + _describe(unexpected)
        )

    def test_allowlist_has_no_stale_entries(self):
        stale = sorted(set(ALLOWED_DIRECT_CALLS) - set(_scan_routers()))
        assert not stale, f"ALLOWED_DIRECT_CALLS に実在しない項目が残っている: {stale}"

    def test_every_allowed_call_has_a_reason(self):
        assert all(reason.strip() for reason in ALLOWED_DIRECT_CALLS.values())


#: Blocking ``queue.Queue`` methods. ``get_nowait`` / ``put_nowait`` / ``empty`` do not block.
_BLOCKING_QUEUE_METHODS = {"get", "put", "join"}


def _is_queue_ctor(node: ast.AST) -> bool:
    """``queue.Queue()`` / ``queue_mod.Queue()`` / ``Queue()`` (thread queues).

    ``asyncio.Queue()`` is awaited, not waited on, so it is excluded.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "Queue"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "Queue"
        and not (isinstance(func.value, ast.Name) and func.value.id == "asyncio")
    )


def _queue_names(body: list[ast.stmt]) -> set[str]:
    """Variables that hold a thread ``Queue`` in this scope."""
    names: set[str] = set()
    for node in _same_scope(body):
        if isinstance(node, ast.Assign) and _is_queue_ctor(node.value):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_queue_ctor(node.value)
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    return names


def scan_blocking_queue_calls(
    source: str, filename: str
) -> dict[tuple[str, str, str], list[int]]:
    """Waiting ``Queue`` calls made directly in an ``async def`` (on the event loop).

    A thread queue's ``get(timeout=...)`` blocks the whole loop while it waits,
    so every other request stalls. Passing the method by reference
    (``asyncio.to_thread(q.get, ...)``) is not a call and is not counted.
    """
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        names = _queue_names(fn.body)
        parent = parents.get(fn)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names |= _queue_names(parent.body)
            parent = parents.get(parent)
        for node in _same_scope(fn.body):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _BLOCKING_QUEUE_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in names
            ):
                found[(filename, fn.name, node.func.attr)].append(node.lineno)
    return dict(found)


class TestQueueWaitsAreOffloaded:
    """An SSE generator waiting on a thread Queue does not block the event loop."""

    def test_no_blocking_queue_call_on_the_event_loop(self):
        found: dict[tuple[str, str, str], list[int]] = {}
        for path in sorted(_ROUTERS_DIR.glob("*.py")):
            found.update(
                scan_blocking_queue_calls(path.read_text(encoding="utf-8"), path.name)
            )
        assert not found, (
            "async の中でスレッドの Queue を待っている（待つ間ほかの要求が止まる）。"
            "asyncio.to_thread(q.get, timeout=...) のようにスレッドで待つ:\n"
            + _describe(found)
        )

    def test_scanner_flags_waits_and_skips_offloaded_ones(self):
        source = """
import asyncio
import queue as queue_mod

async def endpoint(request):
    q: queue_mod.Queue = queue_mod.Queue()

    async def gen():
        q.get(timeout=0.3)
        q.get_nowait()
        q.empty()
        await asyncio.to_thread(q.get, timeout=0.3)

    def worker():
        q.put({"type": "thinking"})

    return gen()

async def other(request):
    d = {}
    d.get("key")
    aq = asyncio.Queue()
    await asyncio.wait_for(aq.get(), timeout=15)
"""
        assert scan_blocking_queue_calls(source, "fake.py") == {
            ("fake.py", "gen", "get"): [9],
        }


class TestScannerIsNotVacuous:
    """The check is not vacuous: it catches violations and ignores calls handed to a thread."""

    _SOURCE = """
import asyncio

def get_thing_manager():
    return object()

def _helper(principal):
    return get_thing_manager().helper_read(principal)

def _offloaded_helper():
    return get_thing_manager().never_on_loop()

async def _run(func, *args):
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)

async def direct(request):
    get_thing_manager().read_now(request)

async def via_variable(request):
    manager = get_thing_manager()
    manager.read_later(request)
    await _run(manager.passed_by_reference, request)
    await _run(lambda: manager.inside_lambda(request))
    await _run(_offloaded_helper)

    def body():
        return manager.inside_nested_def(request)

    await _run(body)

async def via_helper(request):
    return _helper(request)

async def via_nested_helper(request):
    manager = get_thing_manager()

    def on_loop():
        return manager.nested_called_directly(request)

    return on_loop()

async def streaming(request):
    manager = get_thing_manager()

    async def gen():
        yield manager.inside_generator(request)

    return gen()

async def sync_streaming(request):
    manager = get_thing_manager()

    def in_threadpool():
        yield manager.iterated_in_threadpool(request)

    return StreamingResponse(in_threadpool())
"""

    def test_scanner_flags_direct_calls_and_skips_offloaded_ones(self):
        found = scan_source(self._SOURCE, "fake.py")

        assert set(found) == {
            ("fake.py", "direct", "read_now"),
            ("fake.py", "via_variable", "read_later"),
            ("fake.py", "_helper", "helper_read"),
            ("fake.py", "on_loop", "nested_called_directly"),
            ("fake.py", "gen", "inside_generator"),
        }
