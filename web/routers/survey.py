"""
マスアンケート機能のルーター
"""

import json
import logging
import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl
from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.managers.survey_manager import (
    SurveyManager,
    SurveyManagerError,
    SurveyValidationError,
    SurveyExecutionError,
)
from src.models.survey_template import Question, TemplateImage
from src.services.service_factory import service_factory

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

from web.sanitize import render_markdown  # noqa: E402

# マークダウンフィルターを追加
templates.env.filters["markdown"] = render_markdown

executor = ThreadPoolExecutor(max_workers=8)

_survey_manager = None


def get_survey_manager() -> SurveyManager:
    """SurveyManagerのシングルトンインスタンスを取得"""
    global _survey_manager
    if _survey_manager is None:
        db_service = service_factory.get_database_service()
        survey_service = service_factory.get_survey_service()
        _survey_manager = SurveyManager(
            database_service=db_service,
            survey_service=survey_service,
        )
    return _survey_manager


# =========================================================================
# 画面表示エンドポイント
# =========================================================================


@router.get("/", response_class=HTMLResponse)
async def survey_index(request: Request):
    """アンケートTOP画面"""
    return templates.TemplateResponse(
        "survey/index.html",
        {"request": request, "title": "マスアンケート"},
    )


@router.get("/persona-data", response_class=HTMLResponse)
async def persona_data_page(request: Request):
    """ペルソナデータ設定画面"""
    survey_service = service_factory.get_survey_service()
    nemotron_status = {"exists": False, "size_mb": 0}
    custom_datasets = []
    try:
        nemotron_status = survey_service.check_nemotron_dataset_status()
    except Exception as e:
        logger.warning(f"Failed to check Nemotron dataset status: {e}")
    try:
        custom_datasets = survey_service.list_custom_datasets()
    except Exception as e:
        logger.warning(f"Failed to list custom datasets: {e}")
    return templates.TemplateResponse(
        "survey/persona_data.html",
        {
            "request": request,
            "title": "ペルソナデータ設定",
            "nemotron_status": nemotron_status,
            "downloading": _nemotron_download_status["downloading"],
            "custom_datasets": custom_datasets,
        },
    )


_nemotron_download_status: dict = {"downloading": False, "error": None}


def _download_nemotron_background() -> None:
    """バックグラウンドでNemotronデータセットをダウンロードする。"""
    try:
        survey_service = service_factory.get_survey_service()
        survey_service.download_nemotron_dataset()
    except Exception as e:
        logger.error(f"Failed to download Nemotron dataset: {e}")
        _nemotron_download_status["error"] = str(e)
    finally:
        _nemotron_download_status["downloading"] = False


@router.post("/persona-data/download-nemotron", response_class=HTMLResponse)
async def download_nemotron(request: Request):
    """Nemotronデータセットのダウンロードをバックグラウンドで開始"""
    if _nemotron_download_status["downloading"]:
        return templates.TemplateResponse(
            "survey/partials/nemotron_status.html",
            {"request": request, "nemotron_status": {"exists": False, "size_mb": 0}, "downloading": True},
        )
    _nemotron_download_status["downloading"] = True
    _nemotron_download_status["error"] = None
    asyncio.create_task(asyncio.to_thread(_download_nemotron_background))
    return templates.TemplateResponse(
        "survey/partials/nemotron_status.html",
        {"request": request, "nemotron_status": {"exists": False, "size_mb": 0}, "downloading": True},
    )


@router.get("/persona-data/nemotron-status", response_class=HTMLResponse)
async def nemotron_download_status(request: Request):
    """Nemotronダウンロード状況をポーリングで返す"""
    survey_service = service_factory.get_survey_service()
    try:
        status = survey_service.check_nemotron_dataset_status()
    except Exception:
        status = {"exists": False, "size_mb": 0}
    return templates.TemplateResponse(
        "survey/partials/nemotron_status.html",
        {
            "request": request,
            "nemotron_status": status,
            "downloading": _nemotron_download_status["downloading"],
            "error": _nemotron_download_status["error"],
            "success": status.get("exists", False) and not _nemotron_download_status["downloading"],
        },
    )


@router.post("/persona-data/upload-custom", response_class=HTMLResponse)
async def upload_custom_step1(request: Request, file: UploadFile = File(...)):
    """Step1: CSVアップロード → カラム解析 → マッピングUI表示"""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return templates.TemplateResponse(
            "survey/partials/custom_upload_result.html",
            {"request": request, "error": "CSVファイルのみアップロード可能です。"},
        )
    try:
        content = await file.read()
        if len(content) > 500 * 1024 * 1024:
            return templates.TemplateResponse(
                "survey/partials/custom_upload_result.html",
                {"request": request, "error": "ファイルサイズは500MB以下にしてください。"},
            )
        survey_service = service_factory.get_survey_service()
        parsed = survey_service.parse_csv_columns(content)

        # CSVバイト列を一時的にS3に保存（マッピング確定後に使用）
        temp_key = f"persona-dataset/temp/{file.filename}"
        survey_service.s3_service.upload_file(content, temp_key)

        return templates.TemplateResponse(
            "survey/partials/column_mapping.html",
            {
                "request": request,
                "filename": file.filename,
                "temp_key": temp_key,
                "columns": parsed["columns"],
                "samples": parsed["samples"],
                "auto_mapping": parsed["auto_mapping"],
                "standard_columns": survey_service.STANDARD_COLUMNS,
            },
        )
    except Exception as e:
        logger.error(f"Failed to parse CSV: {e}")
        return templates.TemplateResponse(
            "survey/partials/custom_upload_result.html",
            {"request": request, "error": str(e)},
        )


@router.post("/persona-data/preview-prompt", response_class=HTMLResponse)
async def preview_persona_prompt(request: Request):
    """マッピング設定に基づくシステムプロンプトのプレビューを返す。"""
    form = await request.form()
    temp_key = form.get("temp_key", "")

    # マッピング情報を収集
    column_mapping = {}
    for key, value in form.items():
        if key.startswith("mapping_") and value:
            std_col = key[len("mapping_"):]
            column_mapping[std_col] = value

    # その他カラム情報を収集
    extra_count = int(form.get("extra_count", "0"))
    extra_columns = []
    for i in range(extra_count):
        csv_col = form.get(f"extra_column_{i}", "")
        label = form.get(f"extra_label_{i}", "")
        desc = form.get(f"extra_desc_{i}", "")
        if csv_col:
            extra_columns.append({
                "csv_column": csv_col,
                "label": label or csv_col,
                "description": desc,
            })

    try:
        survey_service = service_factory.get_survey_service()
        bucket = survey_service.s3_service.bucket_name
        csv_bytes = survey_service.s3_service.download_file(f"s3://{bucket}/{temp_key}")

        df = pl.read_csv(io.BytesIO(csv_bytes), infer_schema_length=1000, n_rows=1)
        # マッピングに基づきリネーム
        rename_map = {}
        for std_col, csv_col in column_mapping.items():
            if csv_col and csv_col in df.columns and std_col != csv_col:
                rename_map[csv_col] = std_col
        if rename_map:
            df = df.rename(rename_map)

        row = df.row(0, named=True)
        preview_text = survey_service._build_system_prompt(
            row, extra_columns=extra_columns or None
        )

        return templates.TemplateResponse(
            "survey/partials/prompt_preview.html",
            {"request": request, "preview_text": preview_text},
        )
    except Exception as e:
        logger.error(f"Failed to preview prompt: {e}")
        return templates.TemplateResponse(
            "survey/partials/prompt_preview.html",
            {"request": request, "error": str(e)},
        )


@router.post("/persona-data/confirm-mapping", response_class=HTMLResponse)
async def upload_custom_step2(request: Request):
    """Step2: マッピング確定 → Parquet変換 → S3保存"""
    form = await request.form()
    filename = form.get("filename", "")
    temp_key = form.get("temp_key", "")

    # マッピング情報を収集
    column_mapping = {}
    for key, value in form.items():
        if key.startswith("mapping_") and value:
            std_col = key[len("mapping_"):]
            column_mapping[std_col] = value

    # その他カラム情報を収集
    extra_count = int(form.get("extra_count", "0"))
    extra_columns = []
    for i in range(extra_count):
        csv_col = form.get(f"extra_column_{i}", "")
        label = form.get(f"extra_label_{i}", "")
        desc = form.get(f"extra_desc_{i}", "")
        if csv_col:
            extra_columns.append({
                "csv_column": csv_col,
                "label": label or csv_col,
                "description": desc,
            })

    try:
        survey_service = service_factory.get_survey_service()
        # 一時保存したCSVをS3から取得
        bucket = survey_service.s3_service.bucket_name
        csv_bytes = survey_service.s3_service.download_file(f"s3://{bucket}/{temp_key}")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            executor,
            lambda: survey_service.upload_custom_dataset(
                csv_bytes, filename, column_mapping, extra_columns
            ),
        )

        # 一時ファイルを削除
        try:
            survey_service.s3_service.s3_client.delete_object(
                Bucket=survey_service.s3_service.bucket_name, Key=temp_key
            )
        except Exception:
            pass

        custom_datasets = survey_service.list_custom_datasets()
        return templates.TemplateResponse(
            "survey/partials/custom_upload_result.html",
            {
                "request": request,
                "success": True,
                "result": result,
                "custom_datasets": custom_datasets,
            },
        )
    except Exception as e:
        logger.error(f"Failed to upload custom persona data: {e}")
        return templates.TemplateResponse(
            "survey/partials/custom_upload_result.html",
            {"request": request, "error": str(e)},
        )


@router.get(
    "/persona-data/custom/{name}/detail",
    response_class=HTMLResponse,
)
async def custom_dataset_detail(request: Request, name: str):
    """カスタムデータセットのマッピング情報を表示"""
    try:
        survey_service = service_factory.get_survey_service()
        metadata = survey_service.load_dataset_metadata(name)
        return templates.TemplateResponse(
            "survey/partials/custom_dataset_detail.html",
            {
                "request": request,
                "name": name,
                "metadata": metadata,
                "standard_columns": survey_service.STANDARD_COLUMNS,
            },
        )
    except Exception as e:
        logger.error(f"Failed to load dataset detail: {e}")
        return HTMLResponse(
            f'<div class="text-red-600 text-sm p-2">'
            f"詳細の取得に失敗しました: {e}</div>"
        )


@router.delete("/persona-data/custom/{name}", response_class=HTMLResponse)
async def delete_custom_dataset(request: Request, name: str):
    """カスタムデータセットを削除"""
    try:
        survey_service = service_factory.get_survey_service()
        survey_service.delete_custom_dataset(name)
        custom_datasets = survey_service.list_custom_datasets()
        return templates.TemplateResponse(
            "survey/partials/custom_dataset_list.html",
            {"request": request, "custom_datasets": custom_datasets},
        )
    except Exception as e:
        logger.error(f"Failed to delete custom dataset: {e}")
        return HTMLResponse(
            f'<div class="text-red-600 text-sm">削除に失敗しました: {e}</div>'
        )


@router.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request):
    """テンプレート一覧画面"""
    try:
        manager = get_survey_manager()
        template_list = manager.get_all_templates()
    except Exception as e:
        logger.error(f"Failed to get templates: {e}")
        template_list = []
    return templates.TemplateResponse(
        "survey/templates_list.html",
        {"request": request, "title": "テンプレート一覧", "templates": template_list},
    )


@router.get("/templates/new", response_class=HTMLResponse)
async def template_new(request: Request):
    """テンプレート作成画面"""
    return templates.TemplateResponse(
        "survey/template_form.html",
        {"request": request, "title": "テンプレート作成", "template": None},
    )


@router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
async def template_edit(request: Request, template_id: str):
    """テンプレート編集画面"""
    manager = get_survey_manager()
    tmpl = manager.get_template(template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="テンプレートが見つかりません")
    # 既存画像にプレビューURLを付与
    images_with_urls = []
    for img in tmpl.images:
        d = img.to_dict()
        d["preview_url"] = _get_image_preview_url(img.file_path)
        images_with_urls.append(d)
    return templates.TemplateResponse(
        "survey/template_form.html",
        {
            "request": request,
            "title": "テンプレート編集",
            "template": tmpl,
            "questions_json": [q.to_dict() for q in tmpl.questions],
            "images_json": images_with_urls,
        },
    )


@router.get("/start", response_class=HTMLResponse)
async def survey_start_page(request: Request):
    """アンケート開始画面"""
    manager = get_survey_manager()
    try:
        template_list = manager.get_all_templates()
        # テンプレートを辞書形式に変換（JSON化のため）
        templates_dict = [
            {
                "id": t.id,
                "name": t.name,
                "questions": [{"id": q.id} for q in t.questions],
                "images": [{"id": img.id, "name": img.name} for img in t.images],
            }
            for t in template_list
        ]
    except Exception as e:
        logger.error(f"Failed to get templates: {e}")
        template_list = []
        templates_dict = []

    # フィルタ用の属性値を取得
    filter_values = {}
    custom_datasets = []
    nemotron_available = False
    datasource_counts = {}
    try:
        survey_service = service_factory.get_survey_service()
        nemotron_available = survey_service.check_nemotron_dataset_status().get("exists", False)
        custom_datasets = survey_service.list_custom_datasets()
        # 初期表示のデータソースを決定
        if nemotron_available:
            default_ds = "nemotron"
        elif custom_datasets:
            default_ds = f"custom:{custom_datasets[0]['name']}"
        else:
            default_ds = None
        if default_ds:
            filter_values = survey_service.get_available_filter_values(datasource=default_ds)
        # 各データソースのペルソナ数を取得
        if nemotron_available:
            datasource_counts["nemotron"] = survey_service._get_total_count("nemotron")
        for ds in custom_datasets:
            datasource_counts[f"custom:{ds['name']}"] = survey_service._get_total_count(f"custom:{ds['name']}")
    except Exception as e:
        logger.warning(f"Failed to get filter values: {e}")

    return templates.TemplateResponse(
        "survey/start.html",
        {
            "request": request,
            "title": "アンケート開始",
            "templates": template_list,
            "templates_dict": templates_dict,
            "filter_values": filter_values,
            "custom_datasets": custom_datasets,
            "nemotron_available": nemotron_available,
            "datasource_counts": datasource_counts,
        },
    )


def _clean_filters(raw: dict) -> dict | None:
    """フィルタJSONから空値を除去して返す。"""
    cleaned = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            range_cleaned = {}
            for kk, vv in v.items():
                if vv is not None and vv != "":
                    try:
                        range_cleaned[kk] = int(float(vv))
                    except (ValueError, TypeError):
                        pass
            if range_cleaned:
                cleaned[k] = range_cleaned
        elif isinstance(v, list):
            if len(v) > 0:
                cleaned[k] = v
        elif isinstance(v, str) and v:
            cleaned[k] = v
    return cleaned or None


@router.get("/filter-options", response_class=HTMLResponse)
async def filter_options(request: Request):
    """データソースに応じたフィルタ選択肢を返す"""
    datasource = request.query_params.get("datasource", "nemotron")
    survey_service = service_factory.get_survey_service()
    filter_values = {}
    try:
        filter_values = survey_service.get_available_filter_values(datasource=datasource)
    except Exception as e:
        logger.warning(f"Failed to get filter values for {datasource}: {e}")
    return templates.TemplateResponse(
        "survey/partials/filter_fields.html",
        {"request": request, "filter_values": filter_values},
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview_personas(request: Request):
    """フィルタ条件に基づくペルソナプレビュー"""
    form = await request.form()
    filters_json = form.get("filters_json", "{}")
    datasource = form.get("datasource", "nemotron")
    
    logger.info(f"Preview request - filters_json: {filters_json}, datasource: {datasource}")

    try:
        raw = json.loads(filters_json) if filters_json.strip() else {}
        logger.info(f"Preview request - raw filters: {raw}")
        filters = _clean_filters(raw) if raw else None
        logger.info(f"Preview request - cleaned filters: {filters}")
    except json.JSONDecodeError:
        filters = None

    try:
        survey_service = service_factory.get_survey_service()
        total = survey_service.get_filtered_count(datasource=datasource)
        count = survey_service.get_filtered_count(filters, datasource=datasource) if filters else total
        stats = survey_service.get_preview_stats(filters, datasource=datasource)

    except Exception as e:
        logger.error(f"Preview failed: {e}")
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": f"プレビューに失敗しました: {e}"},
        )

    return templates.TemplateResponse(
        "survey/partials/filter_preview.html",
        {
            "request": request,
            "count": count,
            "total": total,
            "stats": stats,
            "has_filter": filters is not None,
        },
    )


@router.get("/results", response_class=HTMLResponse)
async def results_list(request: Request):
    """結果一覧画面"""
    manager = get_survey_manager()
    try:
        surveys = manager.get_all_surveys()
    except Exception as e:
        logger.error(f"Failed to get surveys: {e}")
        surveys = []
    return templates.TemplateResponse(
        "survey/results_list.html",
        {"request": request, "title": "アンケート結果一覧", "surveys": surveys},
    )


@router.get("/results/{survey_id}", response_class=HTMLResponse)
async def result_detail(request: Request, survey_id: str):
    """結果詳細画面"""
    manager = get_survey_manager()
    survey = manager.get_survey(survey_id)
    if survey is None:
        raise HTTPException(status_code=404, detail="アンケートが見つかりません")
    # テンプレートの画像情報を取得（プレビューURL付き）
    survey_template = manager.get_template(survey.template_id)
    image_preview_urls = {}
    if survey_template:
        for img in survey_template.images:
            image_preview_urls[img.id] = _get_image_preview_url(img.file_path)
    return templates.TemplateResponse(
        "survey/result_detail.html",
        {
            "request": request,
            "title": "アンケート結果詳細",
            "survey": survey,
            "survey_template": survey_template,
            "image_preview_urls": image_preview_urls,
        },
    )


@router.delete("/results/{survey_id}")
async def delete_survey(survey_id: str):
    """アンケート削除"""
    manager = get_survey_manager()
    try:
        manager.delete_survey(survey_id)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Failed to delete survey: {e}")
        raise HTTPException(status_code=500, detail="削除に失敗しました")


# =========================================================================
# 画像アップロード
# =========================================================================


@router.post("/templates/upload-image")
async def upload_survey_image(file: UploadFile = File(...)):
    """アンケートテンプレート用画像をアップロード"""
    try:
        from src.managers.file_manager import FileManager

        file_manager = FileManager(
            db_service=service_factory.get_database_service(),
            s3_service=service_factory.get_s3_service(),
        )
        file_content = await file.read()
        metadata = file_manager.upload_survey_image(file_content, file.filename)
        return JSONResponse(
            {
                "file_id": metadata.file_id,
                "file_path": metadata.file_path,
                "mime_type": metadata.mime_type,
                "original_filename": metadata.original_filename,
                "preview_url": _get_image_preview_url(metadata.file_path),
            }
        )
    except Exception as e:
        logger.error(f"Survey image upload error: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/image-preview")
async def image_preview(file_path: str):
    """画像のプレビューURL（署名付きURL）を返す"""
    url = _get_image_preview_url(file_path)
    if not url:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="画像が見つかりません")
    return JSONResponse({"url": url})


def _get_image_preview_url(file_path: str) -> str:
    """file_pathからプレビュー用URLを生成する"""
    if not file_path:
        return ""
    if file_path.startswith("s3://"):
        try:
            s3_service = service_factory.get_s3_service()
            if s3_service:
                return s3_service.generate_presigned_url(file_path, expiration=3600)
        except Exception as e:
            logger.warning(f"Failed to generate presigned URL: {e}")
        return ""
    # ローカルパスの場合
    parts = file_path.replace("\\", "/").split("/")
    filename = parts[-1]
    return f"/survey-images/{filename}"


# =========================================================================
# テンプレートCRUD操作
# =========================================================================


@router.post("/templates", response_class=HTMLResponse)
async def create_template(request: Request):
    """テンプレート保存"""
    form = await request.form()
    name = form.get("name", "").strip()
    questions_json = form.get("questions_json", "[]")
    images_json = form.get("images_json", "[]")

    try:
        questions_data = json.loads(questions_json)
    except json.JSONDecodeError:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": "質問データの形式が不正です"},
            status_code=400,
        )

    try:
        images_data = json.loads(images_json) if images_json.strip() else []
    except json.JSONDecodeError:
        images_data = []

    questions = _parse_questions(questions_data)
    images = _parse_images(images_data)

    manager = get_survey_manager()
    try:
        manager.create_template(
            name=name, questions=questions, images=images or None
        )
    except SurveyValidationError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": str(e)},
            status_code=400,
        )
    except SurveyManagerError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": f"保存に失敗しました: {e}"},
            status_code=500,
        )

    # 成功時はテンプレート一覧にリダイレクト（htmx対応）
    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = "/survey/templates"
    return response


@router.put("/templates/{template_id}", response_class=HTMLResponse)
async def update_template(request: Request, template_id: str):
    """テンプレート更新"""
    form = await request.form()
    name = form.get("name", "").strip()
    questions_json = form.get("questions_json", "[]")
    images_json = form.get("images_json", "[]")

    try:
        questions_data = json.loads(questions_json)
    except json.JSONDecodeError:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": "質問データの形式が不正です"},
            status_code=400,
        )

    try:
        images_data = json.loads(images_json) if images_json.strip() else []
    except json.JSONDecodeError:
        images_data = []

    questions = _parse_questions(questions_data)
    images = _parse_images(images_data)

    manager = get_survey_manager()
    try:
        manager.update_template(
            template_id=template_id,
            name=name,
            questions=questions,
            images=images or None,
        )
    except SurveyValidationError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": str(e)},
            status_code=400,
        )
    except SurveyManagerError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": f"更新に失敗しました: {e}"},
            status_code=500,
        )

    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = "/survey/templates"
    return response


@router.delete("/templates/{template_id}", response_class=HTMLResponse)
async def delete_template(request: Request, template_id: str):
    """テンプレート削除"""
    manager = get_survey_manager()
    try:
        manager.delete_template(template_id)
    except SurveyManagerError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": f"削除に失敗しました: {e}"},
            status_code=500,
        )
    # 削除成功 - カードを空にする
    return HTMLResponse(content="")


# =========================================================================
# アンケート実行
# =========================================================================


def _execute_survey_background(survey_id: str, filters: dict | None, datasource: str = "nemotron") -> None:
    """バックグラウンドでアンケートを実行する（スレッドプールで呼び出す）"""
    try:
        manager = get_survey_manager()
        manager.execute_survey(survey_id=survey_id, filters=filters, datasource=datasource)
    except Exception:
        # execute_survey 内でステータスが error に更新されるため、ここではログのみ
        logger.exception(f"Background survey execution failed: {survey_id}")


@router.post("/execute", response_class=HTMLResponse)
async def execute_survey(request: Request):
    """アンケート実行（ジョブ作成後、バックグラウンドで実行開始）"""
    form = await request.form()
    template_id = form.get("template_id", "")
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    persona_count_str = form.get("persona_count", "100")
    filters_json = form.get("filters_json", "{}")
    datasource = form.get("datasource", "nemotron")

    try:
        persona_count = int(persona_count_str)
    except (ValueError, TypeError):
        response = templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": "ペルソナ数は整数で入力してください"},
            status_code=400,
        )
        response.headers["HX-Retarget"] = "#execute-error"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    try:
        raw = json.loads(filters_json) if filters_json.strip() else None
        filters = _clean_filters(raw) if raw else None
    except json.JSONDecodeError:
        filters = None

    manager = get_survey_manager()

    # 1. アンケートレコードを作成（バリデーション含む、即座に完了）
    try:
        survey = manager.create_survey(
            template_id=template_id,
            name=name or None,
            description=description,
            persona_count=persona_count,
            filters=filters,
        )
    except SurveyValidationError as e:
        response = templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": str(e)},
            status_code=400,
        )
        response.headers["HX-Retarget"] = "#execute-error"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    except SurveyManagerError as e:
        response = templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": f"アンケート作成に失敗しました: {e}"},
            status_code=500,
        )
        response.headers["HX-Retarget"] = "#execute-error"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    # 2. バックグラウンドで実行を開始（デフォルトスレッドプールで実行し、executorを占有しない）
    asyncio.create_task(asyncio.to_thread(_execute_survey_background, survey.id, filters, datasource))

    # 3. 開始メッセージを表示し、結果一覧へ自動遷移
    return templates.TemplateResponse(
        "survey/partials/survey_submitted.html",
        {"request": request, "survey_name": survey.name},
    )


# =========================================================================
# 結果取得・分析
# =========================================================================


@router.get("/results/{survey_id}/download")
async def download_csv(survey_id: str):
    """CSVダウンロード（署名付きURLへリダイレクト）"""
    manager = get_survey_manager()
    try:
        presigned_url = manager.get_download_url(survey_id, expiration=300)
        return RedirectResponse(url=presigned_url)
    except SurveyManagerError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/results/{survey_id}/personas", response_class=HTMLResponse)
async def persona_statistics(request: Request, survey_id: str):
    """調査対象ペルソナ統計データ取得"""
    manager = get_survey_manager()
    try:
        stats = manager.get_persona_statistics(survey_id)
    except SurveyManagerError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": str(e)},
            status_code=400,
        )
    return templates.TemplateResponse(
        "survey/partials/persona_statistics.html",
        {"request": request, "stats": stats},
    )


@router.get("/results/{survey_id}/visual", response_class=HTMLResponse)
async def visual_analysis(request: Request, survey_id: str):
    """ビジュアル分析データ取得"""
    manager = get_survey_manager()
    try:
        data = manager.get_visual_analysis(survey_id)
    except SurveyManagerError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": str(e)},
            status_code=400,
        )
    return templates.TemplateResponse(
        "survey/partials/visual_analysis.html",
        {"request": request, "analysis": data},
    )


@router.post("/results/{survey_id}/report", response_class=HTMLResponse)
async def generate_report(request: Request, survey_id: str):
    """インサイトレポート生成"""
    manager = get_survey_manager()
    try:
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(
            executor, manager.generate_insight_report, survey_id
        )
    except SurveyManagerError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": str(e)},
            status_code=400,
        )
    except SurveyExecutionError as e:
        return templates.TemplateResponse(
            "survey/partials/error_message.html",
            {"request": request, "message": str(e)},
            status_code=500,
        )
    return templates.TemplateResponse(
        "survey/partials/report_display.html",
        {"request": request, "report": report},
    )


# =========================================================================
# ヘルパー関数
# =========================================================================


def _parse_questions(questions_data: list) -> list[Question]:
    """フォームから送信された質問データをQuestionオブジェクトに変換する"""
    questions = []
    for q in questions_data:
        q_type = q.get("question_type", "free_text")
        text = q.get("text", "")
        if q_type == "multiple_choice":
            options = q.get("options", [])
            allow_multiple = bool(q.get("allow_multiple", False))
            max_selections = int(q.get("max_selections", 0))
            questions.append(
                Question.create_multiple_choice(
                    text=text,
                    options=options,
                    allow_multiple=allow_multiple,
                    max_selections=max_selections,
                )
            )
        elif q_type == "scale_rating":
            questions.append(Question.create_scale_rating(text=text))
        else:
            questions.append(Question.create_free_text(text=text))
    return questions


def _parse_images(images_data: list) -> list[TemplateImage]:
    """フォームから送信された画像データをTemplateImageオブジェクトに変換する"""
    images = []
    for img in images_data:
        images.append(
            TemplateImage(
                id=img.get("id", ""),
                name=img.get("name", ""),
                file_path=img.get("file_path", ""),
                mime_type=img.get("mime_type", ""),
                original_filename=img.get("original_filename", ""),
            )
        )
    return images
