"""
ペルソナ関連のルーター
"""

import logging
import asyncio
import json
from typing import Any, Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from src.config import config
from src.managers.file_manager import FileManager, FileUploadError, FileSecurityError
from src.managers.persona_manager import (
    PersonaDetail,
    PersonaManager,
    PersonaManagerError,
)
from src.managers.persona_memory_manager import (
    PersonaMemoryManager,
    PersonaMemoryManagerError,
)  # noqa: E501
from src.managers.persona_generation_manager import (  # noqa: E501
    GenerationResult,
    PersonaGenerationManager,
    PersonaGenerationCapacityError,
    PersonaGenerationManagerError,
)
from src.models.authz import Principal
from src.models.errors import CodedError, ErrorCode, ErrorKind
from src.models.persona import Persona
from src.models.persona_candidate import CandidateSaveResult
from web.error_messages import (
    error_kind_of,
    field_of,
    http_status_for,
    is_correctable,
    is_transient,
    mark_renderable,
    toast_response,
    user_message_for,
    user_message_for_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

# main.pyと同じmarkdownフィルターを登録
from web.sanitize import render_markdown  # noqa: E402

templates.env.filters["markdown"] = render_markdown

# 表示用ヘルパーをテンプレートのグローバル関数として登録。
# country_service は ISO国コード→名前の純粋なデータ参照（ビジネスロジックなし）であり、
# render_markdown と同様に表示ヘルパーとして Router から直接利用する
# （アーキ規約「Router→Manager経由」の表示ヘルパー例外）。
from src.services import country_service  # noqa: E402
from src.models.demographics import gender_label, GENDER_LABELS  # noqa: E402
from ._persona_avatar import persona_avatar_url  # noqa: E402

templates.env.globals["country_name"] = country_service.country_name
templates.env.globals["country_choices"] = country_service.country_choices
templates.env.globals["gender_label"] = gender_label
templates.env.globals["GENDER_LABELS"] = GENDER_LABELS
templates.env.globals["persona_avatar_url"] = persona_avatar_url

# スレッドプールエグゼキューター（同期的なAI処理を非同期で実行するため）
executor = ThreadPoolExecutor(max_workers=8)


async def _run(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous Manager call (DynamoDB / S3) off the event loop (NFR9.20)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))


# シングルトンマネージャーインスタンス（モジュールレベルで共有）
_persona_manager: PersonaManager | None = None
_persona_memory_manager: PersonaMemoryManager | None = None
_file_manager: FileManager | None = None
_persona_generation_manager: PersonaGenerationManager | None = None


def get_persona_manager() -> PersonaManager:
    """PersonaManagerのシングルトンインスタンスを取得"""
    global _persona_manager
    if _persona_manager is None:
        _persona_manager = PersonaManager()
    return _persona_manager


def _principal(request: Request) -> Principal:
    """The signed-in user that ``AuthMiddleware`` put on the request (C1)."""
    principal: Principal = request.state.principal
    return principal


def _save_candidates(
    principal: Principal, candidate_ids: list[str], **save_kwargs: Any
) -> CandidateSaveResult:
    """Claim, save and finish candidates in one worker thread.

    The three steps must not be separate awaits: if the request is cancelled
    while the save runs, a ``finally`` on the event loop would release the
    claim mid-save and a resubmission could save the same candidate twice.
    In one synchronous call the claim is released only after the save returns.
    """
    gen_manager = get_persona_generation_manager()
    claim = gen_manager.claim_candidates(principal, candidate_ids)
    result: CandidateSaveResult | None = None
    try:
        result = get_persona_manager().save_generated_personas(
            principal, claim, **save_kwargs
        )
    finally:
        gen_manager.finish_candidates(claim, result)
    return result


def get_persona_memory_manager() -> PersonaMemoryManager:
    """PersonaMemoryManagerのシングルトンインスタンスを取得"""
    global _persona_memory_manager
    if _persona_memory_manager is None:
        _persona_memory_manager = PersonaMemoryManager()
    return _persona_memory_manager


def get_file_manager() -> FileManager:
    """FileManagerのシングルトンインスタンスを取得"""
    global _file_manager
    if _file_manager is None:
        _file_manager = FileManager()
    return _file_manager


def get_persona_generation_manager() -> PersonaGenerationManager:
    """PersonaGenerationManagerのシングルトンインスタンスを取得"""
    global _persona_generation_manager
    if _persona_generation_manager is None:
        _persona_generation_manager = PersonaGenerationManager()
    return _persona_generation_manager


@router.get("/generation", response_class=HTMLResponse)
async def persona_generation_page(request: Request) -> Any:
    """ペルソナ生成ページ"""
    return templates.TemplateResponse(
        request,
        "persona/generation.html",
        {
            "request": request,
            "title": "AIペルソナ生成",
            # アップロード制限をカタログの単一ソース（config）から表示に渡す
            "max_file_size_mb": config.PERSONA_SOURCE_MAX_BYTES // (1024 * 1024),
            "max_source_chars": config.PERSONA_SOURCE_MAX_CHARS,
        },
    )


#: The tab panel of the management page (``list_tabpanel``); tab switches and
#: ``listReload`` target it and get only the panel content back.
LIST_PANEL_ID = "persona-list"


def _with_list_reload(response: Response) -> Response:
    """Add ``listReload`` of the persona list to the response's ``HX-Trigger``.

    The existing triggers (the toast) are kept; the list re-reads its own
    ``data-reload-url`` (``frontend.md``).
    """
    raw = response.headers.get("HX-Trigger")
    triggers: dict[str, Any] = json.loads(raw) if raw else {}
    triggers["listReload"] = {"target": f"#{LIST_PANEL_ID}"}
    response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


@router.get("/management", response_class=HTMLResponse)
async def persona_management_page(request: Request, tab: Optional[str] = None) -> Any:
    """ペルソナ管理ページ（タブ別の一覧、M6、BR3）。

    タブの切り替えと一覧の読み直し（HX-Target が一覧部分）には一覧部分だけを返す。
    member の tab=all は「自分のもの」になる（BR3.1）。
    """
    loop = asyncio.get_running_loop()
    panel_only = request.headers.get("HX-Target") == LIST_PANEL_ID
    try:
        listing = await loop.run_in_executor(
            executor,
            get_persona_manager().list_personas,
            _principal(request),
            tab,
            None,
        )
    except CodedError as e:
        logger.warning("Persona list fetch error", exc_info=True)
        message = user_message_for(e, default="ペルソナ一覧の取得に失敗しました")
        if panel_only:
            return mark_renderable(
                templates.TemplateResponse(
                    request,
                    "partials/error_banner.html",
                    {"request": request, "error": message},
                    status_code=503,
                )
            )
        return templates.TemplateResponse(
            request,
            "persona/management.html",
            {
                "request": request,
                "title": "ペルソナ管理",
                "listing": None,
                "tab": tab,
                "error": message,
            },
        )
    context = {
        "request": request,
        "title": "ペルソナ管理",
        "listing": listing,
        "selectable": False,
        "search": "",
        "is_append": False,
    }
    if panel_only:
        return templates.TemplateResponse(
            request, "persona/partials/persona_list.html", context
        )
    return templates.TemplateResponse(request, "persona/management.html", context)


def _generate_personas_sync(
    principal: Principal,
    file_contents: list[tuple[bytes, str]],
    data_type: str,
    persona_count: int,
    data_description: str | None,
    custom_prompt: str | None,
) -> GenerationResult:
    """同期的な統一ペルソナ生成処理（スレッドプールで実行）。候補は生成した人のもの（BR1.1）"""
    gen_manager = get_persona_generation_manager()
    return gen_manager.generate(
        principal,
        file_contents=file_contents,
        data_type=data_type,
        persona_count=persona_count,
        data_description=data_description,
        custom_prompt=custom_prompt,
    )


@router.post("/generate", response_class=StreamingResponse)
async def generate_persona(
    request: Request,
    data_type: str = Form(...),
    persona_count: int = Form(1),
    data_description: str = Form(""),
    custom_prompt: str = Form(""),
    analysis_angle: str = Form(""),
    auto_link_behavior: str = Form(""),
    files: list[UploadFile] = File(None),
) -> Any:
    """統一ペルソナ生成（SSEストリーミング）"""

    is_auto_link = data_type == "dwh" and auto_link_behavior == "true"
    principal = _principal(request)

    # DWH（データ分析エージェント連携）の場合
    if data_type == "dwh":
        logger.info(
            f"Starting DWH persona generation (SSE) - angle={analysis_angle!r}, count={persona_count}, auto_link={is_auto_link}"
        )

        import queue as queue_mod

        event_queue: queue_mod.Queue = queue_mod.Queue()

        def _run_dwh_generation() -> GenerationResult:
            gen_manager = get_persona_generation_manager()
            return gen_manager.generate(
                principal,
                file_contents=[],
                data_type=data_type,
                persona_count=persona_count,
                data_description=analysis_angle,
                custom_prompt=custom_prompt or None,
                event_queue=event_queue,
                auto_link_behavior=is_auto_link,
            )

        async def dwh_event_generator() -> Any:
            yield _sse_event("progress", "データ分析エージェントに問い合わせ中...")

            future = executor.submit(_run_dwh_generation)

            # Agent 実行中: queue からリアルタイムイベントを読み出す。
            # 待つのはスレッドで行う（イベントループの上で待つと他の要求が止まる）。
            # 行動データの CSV（csv_url）は Manager が控えて候補に置く（BR1.2）
            while not future.done():
                try:
                    evt = await asyncio.to_thread(event_queue.get, timeout=0.3)
                    evt_type = evt.get("type", "")
                    content = evt.get("content", "")
                    if evt_type == "tool_call":
                        yield _sse_event(
                            "thinking",
                            json.dumps(
                                {
                                    "type": "tool_call",
                                    "content": content,
                                    "detail": evt.get("detail", ""),
                                },
                                ensure_ascii=False,
                            ),
                        )
                    elif evt_type == "tool_result" and content:
                        yield _sse_event(
                            "thinking",
                            json.dumps(
                                {
                                    "type": "tool_result",
                                    "tool_name": evt.get("tool_name", ""),
                                    "content": content,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    elif evt_type == "thinking" and content:
                        yield _sse_event(
                            "thinking",
                            json.dumps(
                                {"type": "thinking", "content": content},
                                ensure_ascii=False,
                            ),
                        )
                except queue_mod.Empty:
                    yield _sse_event("keepalive", "")

            # queue に残っているイベントを flush
            while not event_queue.empty():
                try:
                    evt = event_queue.get_nowait()
                    evt_type = evt.get("type", "")
                    content = evt.get("content", "")
                    if evt_type in ("tool_call", "thinking") and content:
                        yield _sse_event(
                            "thinking",
                            json.dumps(
                                {
                                    "type": evt_type,
                                    "content": content,
                                    "detail": evt.get("detail", ""),
                                },
                                ensure_ascii=False,
                            ),
                        )
                except queue_mod.Empty:
                    break

            try:
                result = future.result()
                generated_personas = result.personas
                thinking_log = result.thinking_log
                logger.info(
                    f"Successfully generated {len(generated_personas)} DWH personas"
                )

                if len(generated_personas) == 1:
                    html = templates.get_template(
                        "persona/partials/generated_persona.html"
                    ).render(
                        request=request,
                        persona=generated_personas[0],
                        thinking_log=thinking_log,
                        candidate_datasets=list(result.behavior_datasets),
                    )
                else:
                    html = templates.get_template(
                        "persona/partials/persona_candidates.html"
                    ).render(
                        request=request,
                        personas=generated_personas,
                        thinking_log=thinking_log,
                    )

                yield _sse_event("result", html)
                yield _sse_event("done", "")

            except PersonaGenerationCapacityError as e:
                logger.warning(
                    "DWH persona generation capacity exceeded", exc_info=True
                )
                yield _sse_event("error", user_message_for(e))
            except PersonaGenerationManagerError as e:
                logger.warning("DWH persona generation error", exc_info=True)
                yield _sse_event(
                    "error",
                    user_message_for(
                        e,
                        default="ペルソナ生成中にエラーが発生しました。"
                        "しばらくしてから再試行してください。",
                    ),
                )
            except Exception as e:
                logger.exception("DWH persona generation error")
                yield _sse_event(
                    "error",
                    user_message_for(
                        e,
                        default="ペルソナ生成中にエラーが発生しました。"
                        "しばらくしてから再試行してください。",
                    ),
                )

        return StreamingResponse(
            dwh_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ファイル読み込み（既存フロー）
    # 形式・サイズ・空は生バイト時点で判定できる粗ガードとしてここで検証する。
    # 内容不足・合計文字数は抽出後にしか判定できないため Manager 層で行う。
    file_contents: list[tuple[bytes, str]] = []
    file_manager = get_file_manager()
    try:
        if files:
            for f in files:
                content = await f.read()
                if content and f.filename:
                    file_manager.validate_persona_source_file(f.filename, content)
                    file_contents.append((content, f.filename))
    except FileUploadError as e:
        logger.warning("Persona source file validation failed", exc_info=True)
        return _sse_error(user_message_for(e))

    if not file_contents:
        return _sse_error("ファイルをアップロードしてください")

    logger.info(
        f"Starting unified persona generation (SSE) - data_type={data_type}, count={persona_count}, files={len(file_contents)}"
    )

    async def event_generator() -> Any:
        yield _sse_event("progress", "データを分析中...")

        # バックグラウンドで生成実行
        future = executor.submit(
            _generate_personas_sync,
            principal,
            file_contents,
            data_type,
            persona_count,
            data_description or None,
            custom_prompt or None,
        )

        # 完了まで keepalive を送信し続ける
        while not future.done():
            await asyncio.sleep(3)
            yield _sse_event("keepalive", "")

        try:
            result = future.result()
            generated_personas = result.personas
            thinking_log = result.thinking_log

            logger.info(f"Successfully generated {len(generated_personas)} personas")

            # 思考ログを送信
            for entry in thinking_log:
                yield _sse_event("thinking", json.dumps(entry, ensure_ascii=False))

            # 結果HTMLを送信
            if len(generated_personas) == 1:
                html = templates.get_template(
                    "persona/partials/generated_persona.html"
                ).render(
                    request=request,
                    persona=generated_personas[0],
                    thinking_log=thinking_log,
                )
            else:
                html = templates.get_template(
                    "persona/partials/persona_candidates.html"
                ).render(
                    request=request,
                    personas=generated_personas,
                    thinking_log=thinking_log,
                )

            yield _sse_event("result", html)
            yield _sse_event("done", "")

        except PersonaGenerationCapacityError as e:
            logger.warning("Persona generation capacity exceeded", exc_info=True)
            yield _sse_event("error", user_message_for(e))
        except PersonaGenerationManagerError as e:
            logger.warning("Persona generation error", exc_info=True)
            yield _sse_event(
                "error",
                user_message_for(
                    e,
                    default="ペルソナ生成中にエラーが発生しました。"
                    "時間をおいて再度お試しください。",
                ),
            )
        except Exception as e:
            # 詳細なエラー内容はサーバーログにのみ出力し、クライアントには一般的なメッセージを返す
            logger.error("Persona generation error occurred", exc_info=True)
            yield _sse_event(
                "error",
                user_message_for(
                    e,
                    default="ペルソナ生成中にエラーが発生しました。"
                    "時間をおいて再度お試しください。",
                ),
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse_event(event_type: str, data: str) -> str:
    """SSEイベントをフォーマット（複数行データ対応）"""
    lines = data.split("\n") if data else [""]
    data_lines = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event_type}\n{data_lines}\n\n"


def _sse_error(message: str) -> StreamingResponse:
    """SSEエラーレスポンスを返す"""

    async def gen() -> Any:
        yield _sse_event("error", message)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/save", response_class=HTMLResponse)
async def save_persona(
    request: Request,
    persona_id: str = Form(...),
    name: str = Form(...),
    age: int = Form(...),
    occupation: str = Form(...),
    background: str = Form(...),
    values: str = Form(...),
    pain_points: str = Form(...),
    goals: str = Form(...),
    gender: str = Form(""),
    country: str = Form(""),
    city: str = Form(""),
    tags: str = Form(""),
    selected_behavior_datasets: str = Form(""),
) -> Any:
    """生成候補を 1 体保存する（htmx対応、WF2）。

    押さえ → 保存 → 片付けを 1 つのスレッドで順に呼ぶ（片付けは成否にかかわらず必ず呼ぶ。
    要求が途中で切れても、片付けは保存が終わってから）。
    persona_id は候補の ID。他人の候補・期限切れ・二重送信は同じ 404 のトースト（BR1.3）。
    """
    principal = _principal(request)
    try:
        # フォームデータからペルソナを作成（create_newを使用してタイムスタンプを自動設定）
        edited = Persona.create_new(
            name=name,
            age=age,
            occupation=occupation,
            background=background,
            values=[v.strip() for v in values.split("\n") if v.strip()],
            pain_points=[p.strip() for p in pain_points.split("\n") if p.strip()],
            goals=[g.strip() for g in goals.split("\n") if g.strip()],
            gender=gender.strip() or None,
            country=country.strip().upper() or None,
            city=city.strip() or None,
            tags=[t.strip() for t in tags.split("\n") if t.strip()],
        )
        selected = [
            t.strip() for t in selected_behavior_datasets.split(",") if t.strip()
        ]

        result = await _run(
            _save_candidates,
            principal,
            [persona_id],
            edited=edited,
            selected_temp_ids=selected,
        )

        message = f"ペルソナ「{name}」を保存しました"
        if result.failed_datasets:
            message += "（行動データ「{}」は保存できませんでした）".format(
                "」「".join(result.failed_datasets)
            )
        return templates.TemplateResponse(
            request,
            "partials/success.html",
            {
                "request": request,
                "message": message,
                "redirect_url": "/persona/management",
            },
        )
    except CodedError as e:
        # 候補がない（他人・期限切れ・保存中）は 404、それ以外も種類で状態を決める。
        # 本文を返さないトーストにして、生成結果を画面に残す（#117）
        logger.warning("Persona save error", exc_info=True)
        return toast_response(e, status_code=http_status_for(e.code))
    except Exception as e:
        # 再試行で解決しうるエラーは生成結果を消さずトーストで通知する
        # （保存し直せるよう、生成済みペルソナを画面に残す）
        logger.error("Persona save error", exc_info=True)
        return toast_response(
            e, default="ペルソナの保存中にエラーが発生しました", status_code=500
        )


@router.get("/{persona_id}/edit", response_class=HTMLResponse)
async def get_persona_edit_form(request: Request, persona_id: str) -> Any:
    """ペルソナ編集フォームパーシャル（htmx対応）。管理できる人だけ（BR6.2）"""
    try:
        loop = asyncio.get_running_loop()
        persona = await loop.run_in_executor(
            executor,
            get_persona_manager().get_persona_for_edit,
            _principal(request),
            persona_id,
        )

        return templates.TemplateResponse(
            request,
            "persona/partials/edit_form.html",
            {"request": request, "persona": persona},
        )
    except CodedError as e:
        kind = error_kind_of(e)
        if kind is ErrorKind.FORBIDDEN:
            raise  # 403 は共通の例外ハンドラ（トースト）
        if kind is ErrorKind.NOT_FOUND:
            return _not_found_banner(request)
        logger.warning("Persona edit form fetch error", exc_info=True)
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for(
                        e, default="編集フォームの取得に失敗しました"
                    ),
                },
                status_code=500,
            )
        )
    except Exception as e:
        logger.error(f"Persona edit form fetch error: {e}")
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for(
                        e, default="編集フォームの取得に失敗しました"
                    ),
                },
                status_code=500,
            )
        )


def _not_found_banner(request: Request) -> Any:
    """読めない・存在しないペルソナの領域置換（同じ応答、NFR1.14）。"""
    return mark_renderable(
        templates.TemplateResponse(
            request,
            "partials/error_banner.html",
            {
                "request": request,
                "error": user_message_for_code(ErrorCode.PERSONA_NOT_FOUND),
                "back_url": "/persona/management",
                "back_label": "ペルソナ一覧に戻る",
            },
            status_code=404,
        )
    )


async def _render_edit_form_with_error(
    request: Request,
    persona_id: str,
    *,
    message: str,
    error_field: str | None,
    submitted: dict[str, Any],
) -> Any | None:
    """送信値を保持したまま編集フォームを再描画する（Issue #117）。

    入力を直せば解決するエラーでフォームを汎用エラーパーシャルに置換すると、
    11項目の入力がすべて失われてやり直しになる。送信値をそのまま埋め戻し、
    エラーは該当フィールドの横に出す。

    例外そのものではなく解決済みの文言とフィールドキーを受け取るのは、例外を
    Router のヘルパーに渡すと `str(e)` 露出の検査（tests/api/test_error_exposure）
    が効かなくなるため。文言解決は呼び出し側で `user_message_for()` に閉じる。

    Returns:
        再描画した応答。ヘッダー部に使う `persona` を取得できずフォームを
        描画できない場合は ``None``（呼び出し側でトーストに退避する）。
    """
    try:
        persona = await _run(
            get_persona_manager().get_persona_for_edit, _principal(request), persona_id
        )
    except Exception:
        logger.warning("Failed to fetch persona for edit form re-render", exc_info=True)
        return None

    return mark_renderable(
        templates.TemplateResponse(
            request,
            "persona/partials/edit_form.html",
            {
                "request": request,
                "persona": persona,
                "form": submitted,
                "form_error": message,
                "error_field": error_field,
            },
            status_code=400,
        )
    )


def _detail_context(detail: PersonaDetail) -> dict[str, Any]:
    """The header values the detail page, its reload and the edit swap share (R-07)."""
    return {
        "persona": detail.persona,
        "manageable": detail.manageable,
        "owner_label": detail.owner_label,
        "shared_with": detail.shared_with,
        "avatar_upload_enabled": get_persona_manager().avatar_upload_enabled(),
    }


@router.put("/{persona_id}", response_class=HTMLResponse)
async def update_persona(
    request: Request,
    persona_id: str,
    name: str = Form(...),
    age: int = Form(...),
    occupation: str = Form(...),
    background: str = Form(...),
    values: str = Form(...),
    pain_points: str = Form(...),
    goals: str = Form(...),
    gender: str = Form(""),
    country: str = Form(""),
    city: str = Form(""),
    tags: str = Form(""),
) -> Any:
    """ペルソナ更新処理（htmx対応）- 詳細画面用。管理できる人だけ（BR6.2、BR6.7）"""
    try:
        persona_manager = get_persona_manager()

        updated_persona = await _run(
            persona_manager.update_persona,
            _principal(request),
            persona_id=persona_id,
            name=name,
            age=age,
            occupation=occupation,
            background=background,
            values=[v.strip() for v in values.split("\n") if v.strip()],
            pain_points=[p.strip() for p in pain_points.split("\n") if p.strip()],
            goals=[g.strip() for g in goals.split("\n") if g.strip()],
            gender=gender.strip(),
            country=country.strip().upper(),
            city=city.strip(),
            tags=[t.strip() for t in tags.split("\n") if t.strip()],
        )

        # htmxスワップ用：ヘッダー + ボディを返す。所有者・共有先の行も詳細ページと
        # 同じ値で描く（保存後に行が消えないように、R-07）
        detail = await _run(
            persona_manager.describe_persona, _principal(request), updated_persona
        )
        return templates.TemplateResponse(
            request,
            "persona/partials/detail_swap.html",
            {
                "request": request,
                **_detail_context(detail),
                "message": "ペルソナを更新しました",
            },
        )
    except CodedError as e:
        logger.warning("Persona update error", exc_info=True)
        kind = error_kind_of(e)
        if kind is ErrorKind.FORBIDDEN:
            raise  # 403 は共通の例外ハンドラ（トースト）
        if kind is ErrorKind.NOT_FOUND:
            return _not_found_banner(request)
        if is_correctable(e):
            # 入力を直せば解決するエラーは、送信値を保持したまま編集フォームを
            # 再描画する。汎用パーシャルに置換すると11項目の入力が失われる
            rendered = await _render_edit_form_with_error(
                request,
                persona_id,
                message=user_message_for(e),
                error_field=field_of(e),
                submitted={
                    "name": name,
                    "age": age,
                    "occupation": occupation,
                    "background": background,
                    "values": values,
                    "pain_points": pain_points,
                    "goals": goals,
                    "gender": gender,
                    "country": country,
                    "city": city,
                    "tags": tags,
                },
            )
            if rendered is not None:
                return rendered
            # フォームを描画できない場合も入力は壊さない
        # 再試行で解決しうるエラーは編集フォームを消さずトーストで通知する
        return toast_response(e)
    except Exception as e:
        logger.error("Persona update error", exc_info=True)
        return toast_response(
            e, default="ペルソナの更新中にエラーが発生しました", status_code=500
        )


@router.get("/{persona_id}", response_class=HTMLResponse)
async def get_persona_detail(request: Request, persona_id: str) -> Any:
    """ペルソナ詳細ページ。読める人だけ（BR6.2）、操作は管理できる人だけ（BR6.4）"""
    try:
        persona_manager = get_persona_manager()
        loop = asyncio.get_running_loop()
        detail = await loop.run_in_executor(
            executor,
            persona_manager.get_persona_detail,
            _principal(request),
            persona_id,
        )
        return templates.TemplateResponse(
            request,
            "persona/detail.html",
            {
                "request": request,
                "title": f"ペルソナ: {detail.persona.name}",
                **_detail_context(detail),
            },
        )
    except (HTTPException, CodedError):
        # 読めない・存在しない（同じ 404）は共通の例外ハンドラがエラー画面にする
        raise
    except Exception as e:
        logger.error(f"Persona fetch error: {e}")
        raise HTTPException(
            status_code=500,
            detail=user_message_for(
                e, default="ペルソナの取得中にエラーが発生しました"
            ),
        )


@router.get("/{persona_id}/header", response_class=HTMLResponse)
async def get_persona_detail_header(request: Request, persona_id: str) -> Any:
    """詳細のヘッダーの中身だけ（共有の保存後の listReload、U4 レビュー）。

    判定は詳細と同じ（読める人だけ、BR6.2）。読み直しに失敗しても今の表示は消さない
    （本文なしのトースト。htmx は 2xx 以外を差し替えない）。
    """
    try:
        persona_manager = get_persona_manager()
        loop = asyncio.get_running_loop()
        detail = await loop.run_in_executor(
            executor,
            persona_manager.get_persona_detail,
            _principal(request),
            persona_id,
        )
        return templates.TemplateResponse(
            request,
            "persona/partials/detail_header_body.html",
            {
                "request": request,
                **_detail_context(detail),
            },
        )
    except (HTTPException, CodedError):
        raise
    except Exception as e:
        logger.error("Persona header fetch error", exc_info=True)
        return toast_response(
            e, default="ペルソナの取得中にエラーが発生しました", status_code=500
        )


@router.delete("/{persona_id}", response_class=HTMLResponse)
async def delete_persona(request: Request, persona_id: str) -> Any:
    """ペルソナ削除処理（htmx対応）。管理できる人だけ（BR6.2、BR4）

    一覧から削除して 404（別タブで削除済み・読めない）になったときは、読めない対象と
    同じ 404 のトーストに一覧の読み直し（listReload）を添えて、残ったカードを消す。
    """
    referer = request.headers.get("hx-current-url", "")
    from_detail = f"/persona/{persona_id}" in referer
    try:
        persona_manager = get_persona_manager()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            executor, persona_manager.delete_persona, _principal(request), persona_id
        )

        if from_detail:
            return HTMLResponse(
                content="",
                headers={"HX-Redirect": "/persona/management"},
            )
        return templates.TemplateResponse(
            request,
            "partials/success.html",
            {"request": request, "message": "ペルソナを削除しました"},
        )
    except CodedError as e:
        kind = error_kind_of(e)
        if (
            kind is ErrorKind.NOT_FOUND
            and request.headers.get("HX-Request")
            and not from_detail
        ):
            # 共通の例外ハンドラと同じ 404 のトースト + 一覧の読み直し（R-04）
            return _with_list_reload(toast_response(e, status_code=404))
        if kind in (ErrorKind.NOT_FOUND, ErrorKind.FORBIDDEN):
            # 読めない・存在しないは同じ 404、共有されているだけは 403（共通の例外ハンドラ）
            raise
        logger.error("Persona delete error", exc_info=True)
        return toast_response(e, status_code=500)
    except Exception as e:
        # 再試行で解決しうるエラーは一覧を消さずトーストで通知する
        logger.error("Persona delete error", exc_info=True)
        return toast_response(
            e, default="ペルソナの削除中にエラーが発生しました", status_code=500
        )


# =========================================================================
# アイコン画像
# =========================================================================


def _render_avatar_editor(
    request: Request, persona: Persona, message: str | None = None
) -> Any:
    """アイコン編集パーシャルを返す。message があれば成功トーストも出す。"""
    headers = (
        {
            "HX-Trigger": json.dumps(
                {"showToast": {"message": message, "type": "success"}}
            )
        }
        if message
        else None
    )
    return templates.TemplateResponse(
        request,
        "persona/partials/avatar_editor.html",
        {
            "request": request,
            "persona": persona,
            "avatar_upload_enabled": get_persona_manager().avatar_upload_enabled(),
            # 変えられた人だけがここに来る（BR4.3）
            "manageable": True,
        },
        headers=headers,
    )


@router.post("/{persona_id}/avatar", response_class=HTMLResponse)
async def upload_persona_avatar(
    request: Request, persona_id: str, file: UploadFile = File(...)
) -> Any:
    """アイコン画像をアップロードして差し替える（htmx対応）。管理できる人だけ（BR4.3）"""
    try:
        content = await file.read()
        loop = asyncio.get_running_loop()
        persona = await loop.run_in_executor(
            executor,
            get_persona_manager().set_avatar,
            _principal(request),
            persona_id,
            content,
        )
        return _render_avatar_editor(request, persona, "アイコン画像を変更しました")
    except PersonaManagerError as e:
        logger.warning("Persona avatar upload rejected", exc_info=True)
        if is_correctable(e):
            # 入力を直せば解決するエラーはフォーム内の専用領域だけ差し替える
            return mark_renderable(
                templates.TemplateResponse(
                    request,
                    "partials/error_inline.html",
                    {"request": request, "error": user_message_for(e)},
                    status_code=400,
                    headers={"HX-Retarget": "find .avatar-form-error"},
                )
            )
        return toast_response(e)
    except CodedError:
        # 読めない（404）・管理できない（403）は共通の例外ハンドラが答える
        raise
    except Exception as e:
        logger.error("Persona avatar upload error", exc_info=True)
        return toast_response(
            e, default="アイコン画像の変更中にエラーが発生しました", status_code=500
        )


@router.delete("/{persona_id}/avatar", response_class=HTMLResponse)
async def delete_persona_avatar(request: Request, persona_id: str) -> Any:
    """アイコン画像を削除して自動生成アバターに戻す（htmx対応）。管理できる人だけ"""
    try:
        loop = asyncio.get_running_loop()
        persona = await loop.run_in_executor(
            executor,
            get_persona_manager().delete_avatar,
            _principal(request),
            persona_id,
        )
        return _render_avatar_editor(request, persona, "自動アバターに戻しました")
    except CodedError as e:
        if error_kind_of(e) in (ErrorKind.NOT_FOUND, ErrorKind.FORBIDDEN):
            raise
        logger.error("Persona avatar delete error", exc_info=True)
        return toast_response(e, status_code=500)
    except Exception as e:
        logger.error("Persona avatar delete error", exc_info=True)
        return toast_response(
            e, default="アイコン画像の削除中にエラーが発生しました", status_code=500
        )


@router.get("/{persona_id}/avatar")
async def get_persona_avatar(request: Request, persona_id: str) -> Any:
    """アイコン画像の署名付きURLへリダイレクトする。読めなければ 404 で URL を作らない（BR4.4）"""
    try:
        loop = asyncio.get_running_loop()
        url = await loop.run_in_executor(
            executor,
            get_persona_manager().get_avatar_url,
            _principal(request),
            persona_id,
        )
    except Exception as e:
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            return Response(status_code=404)
        logger.error("Persona avatar url error", exc_info=True)
        return Response(status_code=500)
    # 署名付きURLの有効期限より十分短くキャッシュさせる（期限切れURLへの誘導を防ぐ）
    return RedirectResponse(
        url, status_code=302, headers={"Cache-Control": "private, max-age=300"}
    )


@router.get("/list/partial", response_class=HTMLResponse)
async def get_persona_list_partial(
    request: Request,
    search: Optional[str] = None,
    cursor: Optional[str] = None,
    selectable: bool = False,
    append: bool = False,
    tab: Optional[str] = None,
) -> Any:
    """ペルソナ一覧パーシャル（htmx対応・カーソル型ページング）。

    append=True は「もっと見る」で追加読込する差分 HTML を返す（既存の
    グリッドに追記 + hx-swap-oob で次ボタンを差し替え）。
    """
    try:
        persona_manager = get_persona_manager()
        principal = _principal(request)
        search_query = (search or "").strip()
        loop = asyncio.get_running_loop()
        if search_query:
            listing = await loop.run_in_executor(
                executor,
                persona_manager.search_personas,
                principal,
                tab,
                search_query,
            )
        else:
            listing = await loop.run_in_executor(
                executor,
                persona_manager.list_personas,
                principal,
                tab,
                cursor,
            )

        return templates.TemplateResponse(
            request,
            "persona/partials/persona_list.html",
            {
                "request": request,
                "listing": listing,
                "selectable": selectable,
                "search": search_query,
                "is_append": append,
            },
        )
    except Exception as e:
        logger.error(f"Persona list fetch error: {e}")
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_banner.html",
                {
                    "request": request,
                    "error": user_message_for(
                        e, default="ペルソナ一覧の取得に失敗しました"
                    ),
                },
                status_code=500,
            )
        )


# ============================================
# Memory Endpoints
# ============================================

MEMORIES_PER_PAGE = 10


class MemoryUIError(Exception):
    """Memory UI操作エラー"""

    pass


# 記憶の 5 つの入口は、管理できないペルソナ（共有されただけの人・無関係の人）に
# ないものと同じ 404 を返す（BR4.1、AC4.2.4）。PersonaMemoryManagerError のうち
# NOT_FOUND は PERSONA_NOT_FOUND だけなので、種類で見て共通の例外ハンドラへ渡す。


@router.get("/{persona_id}/memories", response_class=HTMLResponse)
async def get_persona_memories(
    request: Request, persona_id: str, page: int = 1, strategy_type: str = "summary"
) -> Any:
    """
    ペルソナの長期記憶一覧を取得（htmx対応）

    Args:
        persona_id: ペルソナID
        page: ページ番号
        strategy_type: 戦略タイプ（"summary" または "semantic"）

    Requirements:
        - 7.1: ペルソナ詳細画面で長期記憶セクションを表示
        - 7.2: タイムスタンプと内容サマリーを表示
        - 7.3: 記憶がない場合は適切なメッセージを表示
        - 7.4: ページネーションをサポート
        - 10.3: エラー時にユーザーにエラーメッセージを表示
    """
    try:
        memory_manager = get_persona_memory_manager()

        memories, current_page, total_pages = await _run(
            memory_manager.get_memories,
            _principal(request),
            persona_id=persona_id,
            strategy_type=strategy_type,
            page=page,
            per_page=MEMORIES_PER_PAGE,
        )

        return templates.TemplateResponse(
            request,
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": memories,
                "current_page": current_page,
                "total_pages": total_pages,
                "strategy_type": strategy_type,
            },
        )

    except PersonaMemoryManagerError as e:
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            raise  # 管理できない・ないペルソナは同じ 404（BR4.1）
        logger.warning("Memory error for persona %s", persona_id, exc_info=True)
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": [],
                "error": user_message_for(e),
                "strategy_type": strategy_type,
            },
        )

    except CodedError:
        # 判定の読み取りの失敗（503）などは共通の例外ハンドラに任せる（R-05）
        raise

    except Exception as e:
        error_msg = user_message_for(
            e, default="予期しないエラーが発生しました。後でもう一度お試しください。"
        )
        logger.error("Error getting memories for persona %s", persona_id, exc_info=True)
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": [],
                "error": error_msg,
                "strategy_type": strategy_type,
            },
        )


@router.delete("/{persona_id}/memories/{memory_id}", response_class=HTMLResponse)
async def delete_persona_memory(
    request: Request, persona_id: str, memory_id: str
) -> Any:
    """ペルソナの特定の記憶を削除（htmx対応）"""
    memory_manager = get_persona_memory_manager()
    try:
        success = await _run(
            memory_manager.delete_memory, _principal(request), persona_id, memory_id
        )

        if success:
            return HTMLResponse(content="", status_code=200)
        else:
            return mark_renderable(
                templates.TemplateResponse(
                    request,
                    "persona/partials/memory_delete_error.html",
                    {
                        "request": request,
                        "memory_id": memory_id,
                        "error": user_message_for_code(
                            ErrorCode.MEMORY_ALREADY_DELETED
                        ),
                    },
                    status_code=400,
                )
            )

    except PersonaMemoryManagerError as e:
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            raise  # 管理できない・ないペルソナは同じ 404（BR4.1）
        logger.warning("Memory error deleting memory %s", memory_id, exc_info=True)
        # 削除の hx-swap は outerHTML で、失敗時にエラーカードが記憶アイテム
        # そのものを置換する。再試行で解決しうるエラー（サービス障害・タイム
        # アウト）ではその記憶はまだ存在するため、アイテムを消してしまうと
        # 一覧の状態が実データと食い違い、再試行の導線も失われる。TRANSIENT は
        # DOMを書き換えずトーストで通知する（#117）
        if is_transient(e):
            return toast_response(e)
        return mark_renderable(
            templates.TemplateResponse(
                request,
                "persona/partials/memory_delete_error.html",
                {
                    "request": request,
                    "memory_id": memory_id,
                    "error": user_message_for(e),
                },
                status_code=400,
            )
        )

    except CodedError:
        # 判定の読み取りの失敗（503）などは共通の例外ハンドラに任せる（R-05）
        raise

    except Exception as e:
        # 削除に失敗した記憶はまだ存在するため、アイテムをエラー表示で
        # 置換せずトーストで通知する（一覧の状態と実データを一致させる）
        logger.error("Error deleting memory %s", memory_id, exc_info=True)
        return toast_response(
            e,
            default="予期しないエラーが発生しました。後でもう一度お試しください。",
            status_code=500,
        )


@router.delete("/{persona_id}/memories", response_class=HTMLResponse)
async def delete_all_persona_memories(
    request: Request, persona_id: str, strategy_type: str = "summary"
) -> Any:
    """ペルソナの全記憶を削除（htmx対応）"""
    memory_manager = get_persona_memory_manager()
    principal = _principal(request)
    try:
        deleted_count = await _run(
            memory_manager.delete_all_memories, principal, persona_id, strategy_type
        )

        item_name = "知識" if strategy_type == "semantic" else "記憶"
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": [],
                "message": f"{deleted_count}件の{item_name}を削除しました"
                if deleted_count > 0
                else f"削除する{item_name}がありませんでした",
                "current_page": 1,
                "total_pages": 1,
                "strategy_type": strategy_type,
            },
        )

    except PersonaMemoryManagerError as e:
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            raise  # 管理できない・ないペルソナは同じ 404（BR4.1）
        error_msg = user_message_for(
            e, default="予期しないエラーが発生しました。後でもう一度お試しください。"
        )
        logger.error(
            "Error deleting all memories for persona %s", persona_id, exc_info=True
        )

        memories = await _run(
            memory_manager.safe_get_memories, principal, persona_id, strategy_type
        )

        return templates.TemplateResponse(
            request,
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": memories,
                "error": error_msg,
                "current_page": 1,
                "total_pages": 1,
                "strategy_type": strategy_type,
            },
        )

    except CodedError:
        # 判定の読み取りの失敗（503）などは共通の例外ハンドラに任せる（R-05）
        raise

    except Exception as e:
        error_msg = user_message_for(
            e, default="予期しないエラーが発生しました。後でもう一度お試しください。"
        )
        logger.error(
            "Error deleting all memories for persona %s", persona_id, exc_info=True
        )

        memories = await _run(
            memory_manager.safe_get_memories, principal, persona_id, strategy_type
        )

        return templates.TemplateResponse(
            request,
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": memories,
                "error": error_msg,
                "current_page": 1,
                "total_pages": 1,
                "strategy_type": strategy_type,
            },
        )


@router.post("/{persona_id}/memories", response_class=HTMLResponse)
async def add_persona_memory(
    request: Request,
    persona_id: str,
    topic_name: str = Form(...),
    topic_content: str = Form(...),
    strategy_type: str = Form(default="semantic"),
) -> Any:
    """ペルソナに手動で知識を追加（htmx対応）"""
    principal = _principal(request)
    try:
        memory_manager = get_persona_memory_manager()

        await _run(
            memory_manager.add_knowledge,
            principal,
            persona_id=persona_id,
            topic_name=topic_name,
            topic_content=topic_content,
        )

        topic_name_clean = topic_name.strip()
        # 題名は記憶の中身の一部のためログに出さない（NFR3.11）
        logger.info(f"Manual knowledge added for persona {persona_id}")

        memories, current_page, total_pages = await _run(
            memory_manager.get_memories,
            principal,
            persona_id=persona_id,
            strategy_type="semantic",
            page=1,
            per_page=MEMORIES_PER_PAGE,
        )

        return templates.TemplateResponse(
            request,
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": memories,
                "current_page": current_page,
                "total_pages": total_pages,
                "strategy_type": "semantic",
                "message": f"知識「{topic_name_clean}」を追加しました。反映まで数分かかります。",
            },
        )

    except PersonaMemoryManagerError as e:
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            raise  # 管理できない・ないペルソナは同じ 404（BR4.1）
        logger.warning(
            "Memory error adding knowledge for persona %s", persona_id, exc_info=True
        )
        if is_transient(e):
            # 入力フォームを含む記憶一覧を置換せず、入力内容を保持する
            return toast_response(e)
        # 入力を直せば解決するエラーは、フォーム内の専用領域だけを差し替える。
        # hx-target（記憶一覧）を置換すると Alpine の showForm が false に戻って
        # 入力欄が閉じ、入力内容も失われる（Issue #117）。
        # find 相対セレクタで送信元フォーム内の領域を指す。手動入力タブと
        # ファイルプレビューが同時にDOM上へ存在するため、絶対 id では別フォーム
        # 側の領域が選ばれてエラーが見えなくなる。
        response = mark_renderable(
            templates.TemplateResponse(
                request,
                "partials/error_inline.html",
                {"request": request, "error": user_message_for(e)},
                status_code=400,
            )
        )
        response.headers["HX-Retarget"] = "find .memory-form-error"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    except (ConnectionError, TimeoutError) as e:
        logger.error(
            "Network error adding memory for persona %s", persona_id, exc_info=True
        )
        return toast_response(e, status_code=503)

    except CodedError:
        # 判定の読み取りの失敗（503）などは共通の例外ハンドラに任せる（R-05）
        raise

    except Exception as e:
        logger.error("Error adding memory for persona %s", persona_id, exc_info=True)
        return toast_response(
            e,
            default="予期しないエラーが発生しました。後でもう一度お試しください。",
            status_code=500,
        )


@router.post("/{persona_id}/memories/upload-preview", response_class=HTMLResponse)
async def upload_knowledge_file_preview(
    request: Request, persona_id: str, file: UploadFile = File(...)
) -> Any:
    """
    知識ファイルをアップロードしてプレビュー表示（htmx対応）

    管理できる人だけ（BR4.1）。ファイルはこの要求の中で変換するだけで保存しない（BR5.9）。

    Args:
        persona_id: ペルソナID
        file: アップロードされたファイル
    """
    try:
        # 変換の前に判定する（共有された人・読めない人は 404）
        await _run(
            get_persona_memory_manager().ensure_memory_manageable,
            _principal(request),
            persona_id,
        )

        # ファイル内容を読み込み
        file_content = await file.read()

        # FileManagerで変換
        file_manager = get_file_manager()
        # 変換（PDF 等の読み取り）も重いのでスレッドで行う
        markdown_content = await _run(
            file_manager.convert_file_to_markdown,
            file_content,
            file.filename,  # type: ignore[arg-type]
        )

        # 1 つの知識の上限は手入力と同じ（判定は Manager。文字数を見るだけ）
        get_persona_memory_manager().check_knowledge_file_content(markdown_content)

        # ファイル名から拡張子を除去してトピック名を生成
        topic_name = Path(file.filename).stem  # type: ignore[arg-type]

        logger.info(
            f"Knowledge file uploaded for preview: {file.filename} (persona: {persona_id})"
        )

        return templates.TemplateResponse(
            request,
            "persona/partials/knowledge_file_preview.html",
            {
                "request": request,
                "persona_id": persona_id,
                "topic_name": topic_name,
                "markdown_content": markdown_content,
                "file_name": file.filename,
                "content_length": len(markdown_content),
            },
        )

    except FileUploadError as e:
        logger.warning(
            "Knowledge file upload error for persona %s", persona_id, exc_info=True
        )
        return templates.TemplateResponse(
            request,
            "persona/partials/knowledge_file_error.html",
            {"request": request, "error": user_message_for(e)},
        )

    except FileSecurityError as e:
        logger.warning(
            "Knowledge file security error for persona %s", persona_id, exc_info=True
        )
        return templates.TemplateResponse(
            request,
            "persona/partials/knowledge_file_error.html",
            {"request": request, "error": user_message_for(e)},
        )

    except PersonaMemoryManagerError as e:
        if error_kind_of(e) is ErrorKind.NOT_FOUND:
            raise  # 管理できない・ないペルソナは同じ 404（BR4.1）
        logger.warning("Knowledge preview rejected", exc_info=True)
        return templates.TemplateResponse(
            request,
            "persona/partials/knowledge_file_error.html",
            {"request": request, "error": user_message_for(e)},
        )

    except CodedError:
        # 照合の障害（503）などは共通の例外ハンドラに任せる
        raise

    except Exception as e:
        logger.error(
            "Unexpected error uploading knowledge file for persona %s",
            persona_id,
            exc_info=True,
        )
        return templates.TemplateResponse(
            request,
            "persona/partials/knowledge_file_error.html",
            {
                "request": request,
                "error": user_message_for(
                    e, default="ファイルのアップロード中にエラーが発生しました"
                ),
            },
        )


# ナレッジベース紐付け関連エンドポイント


@router.get("/{persona_id}/kb-binding", response_class=HTMLResponse)
async def get_kb_binding(request: Request, persona_id: str) -> Any:
    """ペルソナのナレッジベース紐付けを出す。読める人だけ、操作は管理できる人だけ（BR2.1）

    読めない・存在しない（404）は共通の例外ハンドラが答える。
    """
    view = await _run(
        get_persona_manager().get_kb_binding, _principal(request), persona_id
    )
    return templates.TemplateResponse(
        request,
        "persona/partials/kb_binding.html",
        {"request": request, "persona_id": persona_id, "view": view},
    )


@router.post("/{persona_id}/kb-binding", response_class=HTMLResponse)
async def create_kb_binding(
    request: Request,
    persona_id: str,
    kb_id: str = Form(...),
    metadata_filters_json: str = Form(default="{}"),
) -> Any:
    """ナレッジベース紐付けを作成（既存があれば上書き、BR2.9）。

    管理できない（403）、読めない・使えない KB（404）は共通の例外ハンドラが答える。
    """
    try:
        metadata_filters = (
            json.loads(metadata_filters_json) if metadata_filters_json else {}
        )
    except json.JSONDecodeError:
        metadata_filters = {}
    if not isinstance(metadata_filters, dict):
        metadata_filters = {}

    await _run(
        get_persona_manager().create_kb_binding,
        _principal(request),
        persona_id,
        kb_id,
        metadata_filters=metadata_filters,
    )
    return await get_kb_binding(request, persona_id)


@router.delete("/{persona_id}/kb-binding", response_class=HTMLResponse)
async def delete_kb_binding(request: Request, persona_id: str) -> Any:
    """ナレッジベース紐付けを解除する（BR2.2、BR2.5）。KB は 1 つなので ID を取らない"""
    await _run(get_persona_manager().delete_kb_binding, _principal(request), persona_id)
    return await get_kb_binding(request, persona_id)


# データセット紐付け関連エンドポイント


@router.get("/{persona_id}/dataset-bindings", response_class=HTMLResponse)
async def get_dataset_bindings(request: Request, persona_id: str) -> Any:
    """ペルソナのデータセット紐付けを出す。読める人だけ、操作は管理できる人だけ（BR2.1）"""
    view = await _run(
        get_persona_manager().get_dataset_bindings, _principal(request), persona_id
    )
    return templates.TemplateResponse(
        request,
        "persona/partials/dataset_bindings.html",
        {"request": request, "persona_id": persona_id, "view": view},
    )


@router.post("/{persona_id}/dataset-bindings", response_class=HTMLResponse)
async def create_dataset_binding(
    request: Request,
    persona_id: str,
    dataset_id: str = Form(...),
    key_name: str = Form(default=""),
    key_value: str = Form(default=""),
) -> Any:
    """データセット紐付けを作成（BR2.2〜BR2.4）。

    管理できない（403）・読めない（404）は共通の例外ハンドラが答える。データセットが
    使えない・列がないときは、ダイアログ内の専用領域に文言を出す。
    """
    try:
        await _run(
            get_persona_manager().create_dataset_binding,
            _principal(request),
            persona_id,
            dataset_id,
            key_name=key_name,
            key_value=key_value,
        )
    except PersonaManagerError as e:
        logger.warning("Dataset binding error", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_inline.html",
            {"request": request, "error": user_message_for(e)},
        )

    response = await get_dataset_bindings(request, persona_id)
    response.headers["HX-Retarget"] = "#dataset-binding-content"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@router.delete(
    "/{persona_id}/dataset-bindings/{dataset_id}", response_class=HTMLResponse
)
async def delete_dataset_binding(
    request: Request, persona_id: str, dataset_id: str
) -> Any:
    """データセット紐付けを削除する（BR2.2、BR2.5）。専用のデータセットは一緒に消える（BR6.4）"""
    await _run(
        get_persona_manager().delete_dataset_binding,
        _principal(request),
        persona_id,
        dataset_id,
    )
    return await get_dataset_bindings(request, persona_id)


_PREVIEW_FAILED = "データの取得に失敗しました"


def _preview_error(request: Request, message: str) -> Any:
    """The preview partial with no rows and the catalog wording."""
    return templates.TemplateResponse(
        request,
        "persona/partials/dataset_preview.html",
        {
            "request": request,
            "columns": [],
            "rows": [],
            "total_count": 0,
            "error": message,
        },
    )


@router.get(
    "/{persona_id}/dataset-bindings/{dataset_id}/preview", response_class=HTMLResponse
)
async def preview_dataset_binding(
    request: Request, persona_id: str, dataset_id: str
) -> Any:
    """紐付けデータセットのプレビュー表示（BR2.6）。

    ペルソナを読めなければ 404（共通の例外ハンドラ）。紐付けがない・使えない組織資産は
    行を出さずに文言だけを出す。
    """
    try:
        data = await _run(
            get_persona_manager().preview_binding_data,
            _principal(request),
            persona_id,
            dataset_id,
        )
        return templates.TemplateResponse(
            request,
            "persona/partials/dataset_preview.html",
            {"request": request, **data},
        )
    except PersonaManagerError as e:
        logger.warning("Dataset preview error", exc_info=True)
        return _preview_error(request, user_message_for(e, default=_PREVIEW_FAILED))
    except CodedError:
        # 読めない（404）・照合の障害（503）は共通の例外ハンドラが答える
        raise
    except Exception as e:
        logger.error("Dataset preview error", exc_info=True)
        return _preview_error(request, user_message_for(e, default=_PREVIEW_FAILED))


@router.get("/{persona_id}/export")
async def export_persona(request: Request, persona_id: str) -> Any:
    """ペルソナを Markdown でダウンロードさせる。読める人すべて（BR7.1、AC4.1.2）。

    読めない・存在しない（404）は共通の例外ハンドラが答える。日本語のファイル名は
    RFC 5987 の ``filename*`` で渡し、``filename`` には ASCII の代わりの名前を置く。
    """
    export = await _run(
        get_persona_manager().export_persona, _principal(request), persona_id
    )
    disposition = "attachment; filename=\"persona.md\"; filename*=UTF-8''" + quote(
        export.filename, safe=""
    )
    return Response(
        content=export.body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )


@router.post("/save-selected", response_class=HTMLResponse)
async def save_selected_personas(request: Request, persona_ids: str = Form(...)) -> Any:
    """選択された複数の候補を保存する（htmx対応、WF2、BR1.9）。

    押さえ → 保存 → 片付けを 1 つのスレッドで順に呼ぶ。1 件でも押さえられなければ何も保存しない。
    保存できた件数と、保存できなかった候補の名前を出す。
    """
    principal = _principal(request)
    try:
        id_list = [pid.strip() for pid in persona_ids.split(",") if pid.strip()]
        result = await _run(_save_candidates, principal, id_list)

        logger.info(f"Completed saving {result.saved_count} personas")
        return templates.TemplateResponse(
            request,
            "persona/partials/save_success.html",
            {
                "request": request,
                "saved_count": result.saved_count,
                "failed_names": list(result.failed_names),
            },
        )

    except CodedError as e:
        # 押さえられない（他人・期限切れ・保存中）は 404、未選択は 400 のトースト。
        # 候補一覧と選択状態は画面に残す
        logger.warning("Saving selected personas failed", exc_info=True)
        return toast_response(e, status_code=http_status_for(e.code))
    except Exception as e:
        # 再試行で解決しうるエラーは候補一覧を消さずトーストで通知する
        # （選択状態を保ったまま保存し直せるようにする）
        logger.error("Unexpected error saving personas", exc_info=True)
        return toast_response(
            e, default="ペルソナの保存中にエラーが発生しました", status_code=500
        )
