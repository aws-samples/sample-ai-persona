"""
ペルソナ関連のルーター
"""

import logging
import asyncio
import json
from typing import Any, Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from src.managers.file_manager import FileManager, FileUploadError, FileSecurityError
from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.managers.persona_memory_manager import (
    PersonaMemoryManager,
    PersonaMemoryManagerError,
)  # noqa: E501
from src.managers.persona_generation_manager import PersonaGenerationManager  # noqa: E501
from src.models.persona import Persona
from ._pagination import decode_cursor, encode_cursor

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

templates.env.globals["country_name"] = country_service.country_name
templates.env.globals["country_choices"] = country_service.country_choices
templates.env.globals["gender_label"] = gender_label
templates.env.globals["GENDER_LABELS"] = GENDER_LABELS

# スレッドプールエグゼキューター（同期的なAI処理を非同期で実行するため）
executor = ThreadPoolExecutor(max_workers=8)

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
        {"request": request, "title": "AIペルソナ生成"},
    )


@router.get("/management", response_class=HTMLResponse)
async def persona_management_page(request: Request) -> Any:
    """ペルソナ管理ページ（ペルソナ一覧は htmx で遅延ロード）"""
    return templates.TemplateResponse(
        request,
        "persona/management.html",
        {"request": request, "title": "ペルソナ管理"},
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...)) -> Any:
    """ファイルアップロード処理（htmx対応）"""
    try:
        file_content = await file.read()
        file_manager = get_file_manager()

        saved_path, file_text, metadata = file_manager.upload_interview_file(
            file_content,
            file.filename or "uploaded_file",
            allow_duplicates=False,
        )

        # アップロード成功時のパーシャルHTMLを返す
        return templates.TemplateResponse(
            request,
            "persona/partials/upload_success.html",
            {
                "request": request,
                "file_name": file.filename,
                "file_size": len(file_content),
                "file_text": file_text,
                "file_id": metadata.file_id,
                "char_count": len(file_text),
                "word_count": len(file_text.split()),
                "line_count": len(file_text.splitlines()),
            },
        )
    except FileSecurityError as e:
        logger.warning(f"ファイルセキュリティエラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": f"セキュリティエラー: {str(e)}"},
            status_code=400,
        )
    except FileUploadError as e:
        logger.warning(f"ファイルアップロードエラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": f"アップロードエラー: {str(e)}"},
            status_code=400,
        )
    except Exception as e:
        logger.error(f"予期しないエラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                "request": request,
                "error": "ファイルのアップロード中にエラーが発生しました",
            },
            status_code=500,
        )


def _generate_personas_sync(
    file_contents: list[tuple[bytes, str]],
    data_type: str,
    persona_count: int,
    data_description: str | None,
    custom_prompt: str | None,
) -> tuple[list, list[dict[str, str]]]:
    """同期的な統一ペルソナ生成処理（スレッドプールで実行）"""
    gen_manager = get_persona_generation_manager()
    return gen_manager.generate_and_cache(
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

    # DWH（データ分析エージェント連携）の場合
    if data_type == "dwh":
        logger.info(
            f"DWH ペルソナ生成開始(SSE) - angle={analysis_angle!r}, count={persona_count}, auto_link={is_auto_link}"
        )

        import queue as queue_mod

        event_queue: queue_mod.Queue = queue_mod.Queue()

        def _run_dwh_generation() -> tuple[list, list[dict[str, str]]]:
            gen_manager = get_persona_generation_manager()
            return gen_manager.generate_and_cache(
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
            collected_csv_urls: list[str] = []
            csv_url_labels: list[str] = []
            last_tool_call_detail: str = ""

            # Agent 実行中: queue からリアルタイムイベントを読み出す
            while not future.done():
                try:
                    evt = event_queue.get(timeout=0.3)
                    evt_type = evt.get("type", "")
                    content = evt.get("content", "")
                    if evt_type == "csv_url":
                        url = evt.get("url", "")
                        if url:
                            collected_csv_urls.append(url)
                            csv_url_labels.append(last_tool_call_detail)
                    elif evt_type == "tool_call":
                        last_tool_call_detail = evt.get("detail", "")
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
                    if evt_type == "csv_url":
                        url = evt.get("url", "")
                        if url:
                            collected_csv_urls.append(url)
                            csv_url_labels.append(last_tool_call_detail)
                    elif evt_type == "tool_call":
                        last_tool_call_detail = evt.get("detail", "")
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
                generated_personas, thinking_log = future.result()
                logger.info(f"{len(generated_personas)}個のDWHペルソナ生成成功")

                # 行動データ自動紐付け: csv_urlイベントから候補データセットを生成
                gen_manager = get_persona_generation_manager()
                candidate_datasets: list[dict[str, Any]] = []
                if is_auto_link and collected_csv_urls and len(generated_personas) == 1:
                    persona = generated_personas[0]
                    candidate_datasets = gen_manager.build_and_cache_behavior_datasets(
                        persona_id=persona.id,
                        persona_name=persona.name,
                        csv_urls=collected_csv_urls,
                        thinking_log=thinking_log,
                        csv_url_labels=csv_url_labels,
                    )
                    if candidate_datasets:
                        logger.info(
                            f"行動データセット候補 {len(candidate_datasets)}件を生成 (persona={persona.name})"
                        )

                if len(generated_personas) == 1:
                    html = templates.get_template(
                        "persona/partials/generated_persona.html"
                    ).render(
                        request=request,
                        persona=generated_personas[0],
                        thinking_log=thinking_log,
                        candidate_datasets=candidate_datasets,
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

            except Exception:
                logger.exception("DWH ペルソナ生成エラー")
                yield _sse_event(
                    "error",
                    "ペルソナ生成中にエラーが発生しました。しばらくしてから再試行してください。",
                )

        return StreamingResponse(dwh_event_generator(), media_type="text/event-stream")

    # ファイル読み込み（既存フロー）
    file_contents: list[tuple[bytes, str]] = []
    if files:
        for f in files:
            content = await f.read()
            if content and f.filename:
                file_contents.append((content, f.filename))

    if not file_contents:
        return _sse_error("ファイルをアップロードしてください")

    logger.info(
        f"統一ペルソナ生成開始(SSE) - data_type={data_type}, count={persona_count}, files={len(file_contents)}"
    )

    async def event_generator() -> Any:
        yield _sse_event("progress", "データを分析中...")

        # バックグラウンドで生成実行
        future = executor.submit(
            _generate_personas_sync,
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
            generated_personas, thinking_log = future.result()

            logger.info(f"{len(generated_personas)}個のペルソナ生成成功")

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

        except Exception:
            # 詳細なエラー内容はサーバーログにのみ出力し、クライアントには一般的なメッセージを返す
            logger.error("ペルソナ生成エラーが発生しました。", exc_info=True)
            yield _sse_event(
                "error",
                "ペルソナ生成中にエラーが発生しました。時間をおいて再度お試しください。",
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
    """ペルソナ保存処理（htmx対応）"""
    try:
        persona_manager = get_persona_manager()

        # フォームデータからペルソナを作成（create_newを使用してタイムスタンプを自動設定）
        persona = Persona.create_new(
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

        # キャッシュから生成ログを引き継ぐ
        gen_manager = get_persona_generation_manager()
        cached = gen_manager.pop_cached_persona(persona_id)
        if cached:
            persona.generation_log = cached.generation_log
            persona.generation_context = cached.generation_context

        persona_manager.save_persona(persona)

        # 行動データセットの保存・紐付け（Router が2つのManagerを調整）
        if selected_behavior_datasets:
            selected_ids = {
                t.strip() for t in selected_behavior_datasets.split(",") if t.strip()
            }
            cached_datasets = gen_manager.pop_cached_behavior_datasets(persona_id)
            if cached_datasets:
                from src.managers.dataset_manager import DatasetManager

                dataset_manager = DatasetManager()
                bindings_data: list[dict[str, Any]] = []
                for ds_info in cached_datasets:
                    if ds_info["temp_id"] not in selected_ids:
                        continue
                    try:
                        dataset = dataset_manager.upload_csv(
                            file_content=ds_info["csv_bytes"],
                            filename=f"behavior_{ds_info['temp_id']}.csv",
                            name=ds_info["name"],
                            description=f"{ds_info['data_type_label']}（自動取得）",
                        )
                        binding_keys: dict[str, str] = {}
                        if ds_info.get("binding_key_column") and ds_info.get(
                            "binding_key_value"
                        ):
                            binding_keys[ds_info["binding_key_column"]] = ds_info[
                                "binding_key_value"
                            ]
                        bindings_data.append(
                            {"dataset_id": dataset.id, "binding_keys": binding_keys}
                        )
                    except Exception as e:
                        logger.error(
                            f"行動データセット保存エラー ({ds_info['name']}): {e}"
                        )
                if bindings_data:
                    dataset_manager.set_persona_bindings(persona.id, bindings_data)

        return templates.TemplateResponse(
            request,
            "partials/success.html",
            {
                "request": request,
                "message": f"ペルソナ「{name}」を保存しました",
                "redirect_url": "/persona/management",
            },
        )
    except Exception as e:
        logger.error(f"ペルソナ保存エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": "ペルソナの保存中にエラーが発生しました"},
            status_code=500,
        )


@router.get("/{persona_id}/edit", response_class=HTMLResponse)
async def get_persona_edit_form(request: Request, persona_id: str) -> Any:
    """ペルソナ編集フォームパーシャル（htmx対応）"""
    try:
        persona_manager = get_persona_manager()
        persona = persona_manager.get_persona(persona_id)

        if not persona:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "ペルソナが見つかりません"},
                status_code=404,
            )

        return templates.TemplateResponse(
            request,
            "persona/partials/edit_form.html",
            {"request": request, "persona": persona},
        )
    except Exception as e:
        logger.error(f"ペルソナ編集フォーム取得エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": "編集フォームの取得に失敗しました"},
            status_code=500,
        )


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
    """ペルソナ更新処理（htmx対応）- 詳細画面用"""
    try:
        persona_manager = get_persona_manager()

        updated_persona = persona_manager.update_persona(
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

        if updated_persona:
            # htmxスワップ用：ヘッダー + ボディを返す
            return templates.TemplateResponse(
                request,
                "persona/partials/detail_swap.html",
                {
                    "request": request,
                    "persona": updated_persona,
                    "message": "ペルソナを更新しました",
                },
            )
        else:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "ペルソナが見つかりません"},
                status_code=404,
            )
    except PersonaManagerError as e:
        logger.error(f"ペルソナ更新エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": f"更新エラー: {str(e)}"},
            status_code=400,
        )
    except Exception as e:
        logger.error(f"ペルソナ更新エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": "ペルソナの更新中にエラーが発生しました"},
            status_code=500,
        )


@router.get("/{persona_id}", response_class=HTMLResponse)
async def get_persona_detail(request: Request, persona_id: str) -> Any:
    """ペルソナ詳細ページ"""
    try:
        persona_manager = get_persona_manager()
        persona = persona_manager.get_persona(persona_id)

        if not persona:
            raise HTTPException(status_code=404, detail="ペルソナが見つかりません")

        return templates.TemplateResponse(
            request,
            "persona/detail.html",
            {
                "request": request,
                "title": f"ペルソナ: {persona.name}",
                "persona": persona,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"ペルソナ取得エラー: {e}")
        raise HTTPException(
            status_code=500, detail="ペルソナの取得中にエラーが発生しました"
        )


@router.delete("/{persona_id}", response_class=HTMLResponse)
async def delete_persona(request: Request, persona_id: str) -> Any:
    """ペルソナ削除処理（htmx対応）"""
    try:
        persona_manager = get_persona_manager()
        success = persona_manager.delete_persona(persona_id)

        if success:
            referer = request.headers.get("hx-current-url", "")
            if f"/persona/{persona_id}" in referer:
                return HTMLResponse(
                    content="",
                    headers={"HX-Redirect": "/persona/management"},
                )
            return templates.TemplateResponse(
                request,
                "partials/success.html",
                {"request": request, "message": "ペルソナを削除しました"},
            )
        else:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "ペルソナの削除に失敗しました"},
                status_code=400,
            )
    except Exception as e:
        logger.error(f"ペルソナ削除エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": "ペルソナの削除中にエラーが発生しました"},
            status_code=500,
        )


@router.get("/list/partial", response_class=HTMLResponse)
async def get_persona_list_partial(
    request: Request,
    search: Optional[str] = None,
    cursor: Optional[str] = None,
    selectable: bool = False,
    append: bool = False,
) -> Any:
    """ペルソナ一覧パーシャル（htmx対応・カーソル型ページング）。

    append=True は「もっと見る」で追加読込する差分 HTML を返す（既存の
    グリッドに追記 + hx-swap-oob で次ボタンを差し替え）。
    """
    try:
        persona_manager = get_persona_manager()
        search_query = (search or "").strip()
        total_count: Optional[int] = None
        if search_query:
            # 検索時は全件 scan フォールバックし、Python 側で部分一致フィルタ（最大100件）
            personas, _ = persona_manager.get_all_personas(search_all=True)
            search_lower = search_query.lower()
            personas = [
                p
                for p in personas
                if search_lower in p.name.lower()
                or search_lower in p.occupation.lower()
                or search_lower in p.background.lower()
            ][:100]
            next_cursor_encoded: Optional[str] = None
        else:
            personas, next_cursor = persona_manager.get_all_personas(
                limit=21, cursor=decode_cursor(cursor)
            )
            next_cursor_encoded = encode_cursor(next_cursor)
            if not append:
                try:
                    total_count = persona_manager.get_persona_count()
                except Exception:
                    total_count = None

        return templates.TemplateResponse(
            request,
            "persona/partials/persona_list.html",
            {
                "request": request,
                "personas": personas,
                "next_cursor": next_cursor_encoded,
                "selectable": selectable,
                "search": search_query,
                "is_append": append,
                "total_count": total_count,
            },
        )
    except Exception as e:
        logger.error(f"ペルソナ一覧取得エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": "ペルソナ一覧の取得に失敗しました"},
            status_code=500,
        )


# ============================================
# Memory Endpoints
# ============================================

MEMORIES_PER_PAGE = 10


class MemoryUIError(Exception):
    """Memory UI操作エラー"""

    pass


def _get_user_friendly_error_message(error: Exception) -> str:
    """
    エラーをユーザーフレンドリーなメッセージに変換

    Args:
        error: 発生した例外

    Returns:
        ユーザーフレンドリーなエラーメッセージ
    """
    if isinstance(error, (PersonaManagerError, PersonaMemoryManagerError)):
        return str(error)

    if isinstance(error, (ConnectionError, TimeoutError)):
        return "ネットワーク接続エラーが発生しました。接続を確認してください。"

    return "予期しないエラーが発生しました。後でもう一度お試しください。"


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

        memories, current_page, total_pages = memory_manager.get_memories(
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
        logger.warning(f"Memory error for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_list.html",
            {
                "request": request,
                "persona_id": persona_id,
                "memories": [],
                "error": str(e),
                "strategy_type": strategy_type,
            },
        )

    except Exception as e:
        error_msg = _get_user_friendly_error_message(e)
        logger.error(
            f"Error getting memories for persona {persona_id}: {e}", exc_info=True
        )
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
        success = memory_manager.delete_memory(persona_id, memory_id)

        if success:
            return HTMLResponse(content="", status_code=200)
        else:
            return templates.TemplateResponse(
                request,
                "persona/partials/memory_delete_error.html",
                {
                    "request": request,
                    "memory_id": memory_id,
                    "error": "記憶の削除に失敗しました。記憶が見つからないか、既に削除されている可能性があります。",
                },
                status_code=400,
            )

    except PersonaMemoryManagerError as e:
        error_msg = _get_user_friendly_error_message(e)
        logger.error(f"Error deleting memory {memory_id}: {e}", exc_info=True)
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_delete_error.html",
            {"request": request, "memory_id": memory_id, "error": error_msg},
            status_code=400,
        )

    except Exception as e:
        error_msg = _get_user_friendly_error_message(e)
        logger.error(f"Error deleting memory {memory_id}: {e}", exc_info=True)
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_delete_error.html",
            {"request": request, "memory_id": memory_id, "error": error_msg},
            status_code=500,
        )


@router.delete("/{persona_id}/memories", response_class=HTMLResponse)
async def delete_all_persona_memories(
    request: Request, persona_id: str, strategy_type: str = "summary"
) -> Any:
    """ペルソナの全記憶を削除（htmx対応）"""
    memory_manager = get_persona_memory_manager()
    try:
        deleted_count = memory_manager.delete_all_memories(persona_id, strategy_type)

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
        error_msg = _get_user_friendly_error_message(e)
        logger.error(
            f"Error deleting all memories for persona {persona_id}: {e}", exc_info=True
        )

        memories = memory_manager.safe_get_memories(persona_id, strategy_type)

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

    except Exception as e:
        error_msg = _get_user_friendly_error_message(e)
        logger.error(
            f"Error deleting all memories for persona {persona_id}: {e}", exc_info=True
        )

        memories = memory_manager.safe_get_memories(persona_id, strategy_type)

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
    try:
        memory_manager = get_persona_memory_manager()

        memory_manager.add_knowledge(
            persona_id=persona_id, topic_name=topic_name, topic_content=topic_content
        )

        topic_name_clean = topic_name.strip()
        logger.info(
            f"Manual knowledge added for persona {persona_id}: {topic_name_clean}"
        )

        memories, current_page, total_pages = memory_manager.get_memories(
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
        logger.warning(f"Memory error adding knowledge for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_add_error.html",
            {"request": request, "error": str(e)},
            status_code=400,
        )

    except (ConnectionError, TimeoutError) as e:
        error_msg = _get_user_friendly_error_message(e)
        logger.error(f"Network error adding memory for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_add_error.html",
            {"request": request, "error": error_msg},
            status_code=503,
        )

    except Exception as e:
        error_msg = _get_user_friendly_error_message(e)
        logger.error(
            f"Error adding memory for persona {persona_id}: {e}", exc_info=True
        )
        return templates.TemplateResponse(
            request,
            "persona/partials/memory_add_error.html",
            {"request": request, "error": error_msg},
            status_code=500,
        )


@router.post("/{persona_id}/memories/upload-preview", response_class=HTMLResponse)
async def upload_knowledge_file_preview(
    request: Request, persona_id: str, file: UploadFile = File(...)
) -> Any:
    """
    知識ファイルをアップロードしてプレビュー表示（htmx対応）

    Args:
        persona_id: ペルソナID
        file: アップロードされたファイル
    """
    try:
        # ファイル内容を読み込み
        file_content = await file.read()

        # FileManagerで変換
        file_manager = get_file_manager()
        file_metadata, markdown_content = file_manager.upload_knowledge_file(
            file_content,
            file.filename,  # type: ignore[arg-type]
        )

        # 内容の文字数チェック（10000文字制限）
        if len(markdown_content) > 10000:
            logger.warning(
                f"Knowledge file content too long for persona {persona_id}: "
                f"{len(markdown_content)} chars (limit: 10000)"
            )
            return templates.TemplateResponse(
                request,
                "persona/partials/knowledge_file_error.html",
                {
                    "request": request,
                    "error": (
                        f"変換後の内容が長すぎます（{len(markdown_content)}文字）。"
                        f"1つの知識の内容は10000文字以内である必要があります。"
                        f"より小さいファイルを使用するか、ファイルを分割してアップロードしてください。"
                    ),
                },
            )

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
        logger.warning(f"Knowledge file upload error for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            request,
            "persona/partials/knowledge_file_error.html",
            {"request": request, "error": str(e)},
        )

    except FileSecurityError as e:
        logger.warning(f"Knowledge file security error for persona {persona_id}: {e}")
        return templates.TemplateResponse(
            request,
            "persona/partials/knowledge_file_error.html",
            {"request": request, "error": f"セキュリティエラー: {str(e)}"},
        )

    except Exception as e:
        logger.error(
            f"Unexpected error uploading knowledge file for persona {persona_id}: {e}",
            exc_info=True,
        )
        return templates.TemplateResponse(
            request,
            "persona/partials/knowledge_file_error.html",
            {
                "request": request,
                "error": "ファイルのアップロード中にエラーが発生しました",
            },
        )


# ナレッジベース紐付け関連エンドポイント


@router.get("/{persona_id}/kb-binding", response_class=HTMLResponse)
async def get_kb_binding(request: Request, persona_id: str) -> Any:
    """ペルソナのナレッジベース紐付け情報を取得"""
    persona_manager = get_persona_manager()
    knowledge_bases, binding = persona_manager.get_kb_binding(persona_id)

    return templates.TemplateResponse(
        request,
        "persona/partials/kb_binding.html",
        {
            "request": request,
            "persona_id": persona_id,
            "knowledge_bases": knowledge_bases,
            "binding": binding,
        },
    )


@router.post("/{persona_id}/kb-binding", response_class=HTMLResponse)
async def create_kb_binding(
    request: Request,
    persona_id: str,
    kb_id: str = Form(...),
    metadata_filters_json: str = Form(default="{}"),
) -> Any:
    """ナレッジベース紐付けを作成（既存があれば上書き）"""
    persona_manager = get_persona_manager()

    try:
        metadata_filters = (
            json.loads(metadata_filters_json) if metadata_filters_json else {}
        )
    except json.JSONDecodeError:
        metadata_filters = {}

    persona_manager.create_kb_binding(
        persona_id=persona_id,
        kb_id=kb_id,
        metadata_filters=metadata_filters,
    )

    return await get_kb_binding(request, persona_id)


@router.delete("/{persona_id}/kb-binding/{binding_id}", response_class=HTMLResponse)
async def delete_kb_binding(request: Request, persona_id: str, binding_id: str) -> Any:
    """ナレッジベース紐付けを解除"""
    persona_manager = get_persona_manager()
    persona_manager.delete_kb_binding(binding_id)

    return await get_kb_binding(request, persona_id)


# データセット紐付け関連エンドポイント


@router.get("/{persona_id}/dataset-bindings", response_class=HTMLResponse)
async def get_dataset_bindings(request: Request, persona_id: str) -> Any:
    """ペルソナのデータセット紐付け一覧を取得"""
    persona_manager = get_persona_manager()
    datasets, bindings_map = persona_manager.get_dataset_bindings(persona_id)

    return templates.TemplateResponse(
        request,
        "persona/partials/dataset_bindings.html",
        {
            "request": request,
            "persona_id": persona_id,
            "datasets": datasets,
            "bindings_map": bindings_map,
        },
    )


@router.post("/{persona_id}/dataset-bindings", response_class=HTMLResponse)
async def create_dataset_binding(
    request: Request,
    persona_id: str,
    dataset_id: str = Form(...),
    key_name: str = Form(default=""),
    key_value: str = Form(default=""),
) -> Any:
    """データセット紐付けを作成"""
    persona_manager = get_persona_manager()

    try:
        persona_manager.create_dataset_binding(
            persona_id=persona_id,
            dataset_id=dataset_id,
            key_name=key_name,
            key_value=key_value,
        )
    except PersonaManagerError as e:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": str(e)},
        )

    response = await get_dataset_bindings(request, persona_id)
    response.headers["HX-Retarget"] = "#dataset-binding-content"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@router.delete(
    "/{persona_id}/dataset-bindings/{binding_id}", response_class=HTMLResponse
)
async def delete_dataset_binding(
    request: Request, persona_id: str, binding_id: str
) -> Any:
    """データセット紐付けを削除"""
    persona_manager = get_persona_manager()
    persona_manager.delete_dataset_binding(binding_id)

    return await get_dataset_bindings(request, persona_id)


@router.get(
    "/{persona_id}/dataset-bindings/{binding_id}/preview", response_class=HTMLResponse
)
async def preview_dataset_binding(
    request: Request, persona_id: str, binding_id: str
) -> Any:
    """紐付けデータセットのプレビュー表示"""
    try:
        from src.managers.dataset_manager import DatasetManager

        manager = DatasetManager()
        data = manager.preview_binding_data(persona_id, binding_id)
        return templates.TemplateResponse(
            request,
            "persona/partials/dataset_preview.html",
            {"request": request, **data},
        )
    except Exception as e:
        logger.error(f"Dataset preview error: {e}")
        return templates.TemplateResponse(
            request,
            "persona/partials/dataset_preview.html",
            {
                "request": request,
                "columns": [],
                "rows": [],
                "total_count": 0,
                "error": "データの取得に失敗しました",
            },
        )


@router.post("/save-selected", response_class=HTMLResponse)
async def save_selected_personas(request: Request, persona_ids: str = Form(...)) -> Any:
    """選択された複数ペルソナを保存（htmx対応）"""
    try:
        # カンマ区切りのIDリストをパース
        id_list = [pid.strip() for pid in persona_ids.split(",") if pid.strip()]

        if not id_list:
            logger.warning("保存するペルソナが選択されていません")
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "保存するペルソナを選択してください"},
                status_code=400,
            )

        logger.info(f"{len(id_list)}個のペルソナ保存開始")

        persona_manager = get_persona_manager()
        saved_count = 0

        # 各ペルソナを保存
        gen_manager = get_persona_generation_manager()
        for persona_id in id_list:
            try:
                # TTLキャッシュからペルソナを取得
                persona = gen_manager.get_cached_persona(persona_id)
                if persona:
                    persona_manager.save_persona(persona)
                    saved_count += 1
                    logger.info(f"ペルソナ保存成功: {persona.name} (ID: {persona_id})")
                else:
                    logger.warning(f"ペルソナが見つかりません (ID: {persona_id})")
            except Exception as e:
                logger.error(f"ペルソナ保存エラー (ID: {persona_id}): {e}")
                # 個別のエラーは続行
                continue

        # 保存後、キャッシュから削除
        for persona_id in id_list:
            gen_manager.pop_cached_persona(persona_id)

        if saved_count == 0:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {"request": request, "error": "ペルソナの保存に失敗しました"},
                status_code=500,
            )

        logger.info(f"{saved_count}個のペルソナ保存完了")

        # 成功メッセージを返す
        return templates.TemplateResponse(
            request,
            "persona/partials/save_success.html",
            {"request": request, "saved_count": saved_count},
        )

    except Exception as e:
        logger.error(f"予期しないエラー: {e}")
        import traceback

        logger.error(f"エラー詳細: {traceback.format_exc()}")
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"request": request, "error": "ペルソナの保存中にエラーが発生しました"},
            status_code=500,
        )
