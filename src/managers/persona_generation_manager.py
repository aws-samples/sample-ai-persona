"""ペルソナ生成ワークフローを管理するManager"""

import logging
import re
import uuid
from datetime import datetime
from typing import Any, TYPE_CHECKING

from cachetools import TTLCache  # type: ignore[import-untyped]

from pydantic import BaseModel, Field

from ..models.errors import CodedError, ErrorCode
from ..models.persona import Persona
from ..config import config
from ..services.agent_service import (
    AgentService,
    AgentServiceError,
    GenerationCapacityError,
)
from ..services.database_service import DatabaseService
from ..services.service_factory import service_factory
from ..prompts.persona_generation_prompts import (
    CSV_ANALYSIS_INSTRUCTIONS,
    CUSTOM_PROMPT_SECTION,
    DATA_TYPE_PROMPTS,
    DWH_AUTO_LINK_INSTRUCTIONS,
    PERSONA_GENERATION_SYSTEM_PROMPT_TEMPLATE,
    STRUCTURED_OUTPUT_PROMPT,
    USER_PROMPT_TEMPLATE,
)
from .shared.file_utils import (
    analyze_csv_schema,
    cleanup_temp_files,
    detect_binding_key,
    extract_text_from_bytes,
    get_csv_preview,
    infer_behavior_data_type,
    save_temp_csv,
)

if TYPE_CHECKING:
    from ..services.dataset_analysis.service import DatasetAnalysisService


logger = logging.getLogger(__name__)


# Persona (src/models/persona.py) の create_new() パラメータと同一フィールド構成。
# Why: Strands SDK の structured_output() は Pydantic BaseModel を要求するが、Persona は dataclass のため直接渡せない。
# Personaのフィールド変更時はここも同期すること。
class _PersonaOutput(BaseModel):
    name: str = Field(description="名前（国・地域に即した自然な名前。日本語表記）")
    age: int = Field(description="年齢")
    gender: str | None = Field(
        default=None, description="性別（male / female / other）"
    )
    country: str | None = Field(
        default=None,
        description="居住国（ISO 3166-1 alpha-2の2文字コード。例: JP, US）",
    )
    city: str | None = Field(
        default=None, description="居住都市名（日本語。不明なら省略）"
    )
    occupation: str = Field(description="職業")
    background: str = Field(description="背景・経歴")
    values: list[str] = Field(description="価値観（データから導出できるもの）")
    pain_points: list[str] = Field(description="課題・悩み（データから導出できるもの）")
    goals: list[str] = Field(description="目標・願望（データから導出できるもの）")


class _PersonaListOutput(BaseModel):
    personas: list[_PersonaOutput] = Field(description="生成されたペルソナのリスト")


class PersonaGenerationManagerError(CodedError):
    pass


class PersonaGenerationCapacityError(PersonaGenerationManagerError):
    """生成数・入力量が多すぎて処理しきれなかった場合のエラー。

    Service層のGenerationCapacityError（出力トークン上限超過・タイムアウト）を
    変換したもの。
    """

    code = ErrorCode.GENERATION_CAPACITY_EXCEEDED


class PersonaGenerationManager:
    """ペルソナ生成ワークフロー全体のオーケストレーション"""

    def __init__(
        self,
        agent_service: AgentService | None = None,
        database_service: DatabaseService | None = None,
        dataset_analysis_service: "DatasetAnalysisService | None" = None,
    ):
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )
        self.dataset_analysis_service = (
            dataset_analysis_service or service_factory.get_dataset_analysis_service()
        )
        self._personas_cache: TTLCache = TTLCache(
            maxsize=1000, ttl=config.PERSONA_CACHE_TTL_SECONDS
        )
        self._behavior_datasets_cache: TTLCache = TTLCache(
            maxsize=100, ttl=config.PERSONA_CACHE_TTL_SECONDS
        )

    # ------------------------------------------------------------------
    # 公開API
    # ------------------------------------------------------------------

    def generate_and_cache(
        self,
        file_contents: list[tuple[bytes, str]],
        data_type: str,
        persona_count: int,
        data_description: str | None = None,
        custom_prompt: str | None = None,
        event_queue: Any = None,
        auto_link_behavior: bool = False,
    ) -> tuple[list[Persona], list[dict[str, str]]]:
        """ペルソナを生成し、一時キャッシュに格納する。

        Returns:
            (personas, thinking_log)
        """
        self._validate_generation_input(
            data_type, persona_count, file_contents, data_description
        )

        if data_type == "dwh":
            if auto_link_behavior:
                persona_count = 1
            personas, thinking_log = self._generate_from_dwh(
                analysis_angle=data_description or "",
                persona_count=persona_count,
                custom_prompt=custom_prompt,
                event_queue=event_queue,
                auto_link_behavior=auto_link_behavior,
            )
        else:
            personas, thinking_log = self._generate_from_files(
                file_contents=file_contents,
                data_type=data_type,
                persona_count=persona_count,
                data_description=data_description,
                custom_prompt=custom_prompt,
            )

        for persona in personas:
            self._validate_generated_persona(persona)

        gen_ctx = self._build_generation_context(
            data_type=data_type,
            data_description=data_description,
            custom_prompt=custom_prompt,
            source_files=[fn for _, fn in file_contents],
            persona_count=persona_count,
            auto_link_behavior=auto_link_behavior,
        )
        for persona in personas:
            persona.generation_log = thinking_log
            persona.generation_context = gen_ctx
            self._personas_cache[persona.id] = persona

        return personas, thinking_log

    def get_cached_persona(self, persona_id: str) -> Persona | None:
        """一時キャッシュからペルソナを取得する"""
        return self._personas_cache.get(persona_id)  # type: ignore[no-any-return]

    def pop_cached_persona(self, persona_id: str) -> Persona | None:
        """一時キャッシュからペルソナを取得し削除する"""
        return self._personas_cache.pop(persona_id, None)  # type: ignore[no-any-return]

    def get_cached_behavior_datasets(
        self, persona_id: str
    ) -> list[dict[str, Any]] | None:
        """行動データセット候補キャッシュを取得する"""
        return self._behavior_datasets_cache.get(persona_id)  # type: ignore[no-any-return]

    def pop_cached_behavior_datasets(
        self, persona_id: str
    ) -> list[dict[str, Any]] | None:
        """行動データセット候補キャッシュを取得し削除する"""
        return self._behavior_datasets_cache.pop(persona_id, None)  # type: ignore[no-any-return]

    def build_and_cache_behavior_datasets(
        self,
        persona_id: str,
        persona_name: str,
        csv_urls: list[str],
        thinking_log: list[dict[str, str]],
        csv_url_labels: list[str],
    ) -> list[dict[str, Any]]:
        """CSV URLリストからデータセット候補を構築しキャッシュする"""
        candidates = self._build_behavior_dataset_candidates(
            csv_urls=csv_urls,
            persona_name=persona_name,
            thinking_log=thinking_log,
            csv_url_labels=csv_url_labels,
        )
        if candidates:
            self._behavior_datasets_cache[persona_id] = candidates
        return candidates

    # ------------------------------------------------------------------
    # 生成ワークフロー内部
    # ------------------------------------------------------------------

    def _validate_generation_input(
        self,
        data_type: str,
        persona_count: int,
        file_contents: list[tuple[bytes, str]],
        data_description: str | None,
    ) -> None:
        if persona_count < 1 or persona_count > 10:
            raise PersonaGenerationManagerError(
                f"persona_count {persona_count} out of range 1-10",
                code=ErrorCode.GENERATION_PERSONA_COUNT_INVALID,
            )
        if data_type == "dwh":
            if not data_description or not data_description.strip():
                raise PersonaGenerationManagerError(
                    "data_description is blank for dwh generation",
                    code=ErrorCode.GENERATION_DATA_DESCRIPTION_REQUIRED,
                )
        else:
            if not file_contents:
                raise PersonaGenerationManagerError(
                    "file_contents is empty for file-based generation",
                    code=ErrorCode.GENERATION_FILES_REQUIRED,
                )

    def _generate_from_files(
        self,
        file_contents: list[tuple[bytes, str]],
        data_type: str,
        persona_count: int,
        data_description: str | None,
        custom_prompt: str | None,
    ) -> tuple[list[Persona], list[dict[str, str]]]:
        """ファイルベースのペルソナ生成"""
        logger.info(
            f"ファイルベースペルソナ生成開始 (data_type={data_type}, count={persona_count}, files={len(file_contents)})"
        )

        # 抽出は自前の finally で temp を掃除する（失敗時に呼び出し側 finally へ到達
        # しないため）。抽出成功後は下の try/finally が temp の唯一の所有者になる。
        combined_text, source_descriptors = self._extract_file_texts(file_contents)
        csv_temp_paths = [d["path"] for d in source_descriptors]

        # tool 構築（build_source_tools）が失敗しても temp を残さないため、抽出直後から
        # 全処理を try/finally で囲む（round 11）。
        try:
            # global kill switch と CSV の有無の AND で有効判定する。
            enable_dataset_analysis = (
                bool(source_descriptors) and config.ENABLE_DATASET_ANALYSIS
            )
            active_descriptors = source_descriptors if enable_dataset_analysis else []

            system_prompt = self._build_system_prompt(
                data_type, data_description, custom_prompt
            )
            tools = self._determine_tools(
                data_type, active_descriptors, event_queue=None
            )
            user_prompt = self._build_user_prompt(
                combined_text, persona_count, active_descriptors or None
            )

            agent = self.agent_service.create_generation_agent(
                system_prompt=system_prompt,
                tools=tools if tools else None,
            )
            result, thinking_log = self.agent_service.run_persona_generation(
                agent=agent,
                prompt=user_prompt,
                structured_prompt=STRUCTURED_OUTPUT_PROMPT,
                output_schema=_PersonaListOutput,
            )
            personas = self._convert_to_personas(result)
        except GenerationCapacityError as e:
            raise PersonaGenerationCapacityError(
                "persona generation capacity exceeded (file input)",
                context={"persona_count": persona_count},
            ) from e
        except AgentServiceError as e:
            raise PersonaGenerationManagerError(
                f"agent service failed during file-based generation "
                f"({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        except PersonaGenerationManagerError:
            raise
        except Exception as e:
            raise PersonaGenerationManagerError(
                f"file-based persona generation failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        finally:
            cleanup_temp_files(csv_temp_paths)

        logger.info(f"ファイルベースペルソナ生成完了: {len(personas)}個")
        return personas, thinking_log

    def _generate_from_dwh(
        self,
        analysis_angle: str,
        persona_count: int,
        custom_prompt: str | None = None,
        event_queue: Any = None,
        auto_link_behavior: bool = False,
    ) -> tuple[list[Persona], list[dict[str, str]]]:
        """DWH連携のペルソナ生成"""
        logger.info(
            f"DWH ペルソナ生成開始 (angle={analysis_angle!r}, count={persona_count}, auto_link={auto_link_behavior})"
        )

        callback_handler = None
        if event_queue is not None:

            def _queue_callback(**kwargs: Any) -> None:
                data = kwargs.get("data", "")
                complete = kwargs.get("complete", False)
                if data:
                    event_queue.put({"type": "thinking", "content": data})
                if complete and data:
                    event_queue.put({"type": "thinking_done", "content": ""})

            callback_handler = _queue_callback

        data_text = f"分析の切り口: {analysis_angle}"
        if auto_link_behavior:
            data_text += DWH_AUTO_LINK_INSTRUCTIONS

        system_prompt = self._build_system_prompt("dwh", analysis_angle, custom_prompt)
        tools = self._determine_tools("dwh", [], event_queue=event_queue)
        user_prompt = self._build_user_prompt(data_text, persona_count)

        try:
            agent = self.agent_service.create_generation_agent(
                system_prompt=system_prompt,
                tools=tools if tools else None,
                callback_handler=callback_handler,
            )
            result, thinking_log = self.agent_service.run_persona_generation(
                agent=agent,
                prompt=user_prompt,
                structured_prompt=STRUCTURED_OUTPUT_PROMPT,
                output_schema=_PersonaListOutput,
            )
            personas = self._convert_to_personas(result)
        except GenerationCapacityError as e:
            raise PersonaGenerationCapacityError(
                "persona generation capacity exceeded (dwh)",
                context={"persona_count": persona_count},
            ) from e
        except AgentServiceError as e:
            raise PersonaGenerationManagerError(
                f"data agent integration failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e
        except Exception as e:
            raise PersonaGenerationManagerError(
                f"dwh persona generation failed ({type(e).__name__})",
                code=ErrorCode.GENERATION_OPERATION_FAILED,
            ) from e

        logger.info(f"DWH ペルソナ生成完了: {len(personas)}個")
        return personas, thinking_log

    def _convert_to_personas(self, result: _PersonaListOutput) -> list[Persona]:
        """Pydantic出力 → Personaドメインモデルへの変換"""
        from ..models.demographics import sanitize_gender
        from ..services.country_service import sanitize_country

        personas: list[Persona] = []
        for p in result.personas:
            persona = Persona.create_new(
                name=p.name,
                age=p.age,
                occupation=p.occupation,
                background=p.background,
                values=p.values,
                pain_points=p.pain_points,
                goals=p.goals,
                gender=sanitize_gender(p.gender),
                country=sanitize_country(p.country),
                city=p.city,
            )
            personas.append(persona)
        return personas

    def _build_system_prompt(
        self,
        data_type: str,
        data_description: str | None,
        custom_prompt: str | None,
    ) -> str:
        """データ種別に応じたsystem_prompt構築"""
        role_prompt = DATA_TYPE_PROMPTS.get(data_type)
        if role_prompt is None:
            role_prompt = f"以下は「{data_description or 'ユーザー提供データ'}」です。データ内容を分析してペルソナを生成してください。"

        system_prompt = PERSONA_GENERATION_SYSTEM_PROMPT_TEMPLATE.format(
            role_prompt=role_prompt
        )

        if custom_prompt:
            system_prompt += CUSTOM_PROMPT_SECTION.format(custom_prompt=custom_prompt)

        return system_prompt

    def _build_user_prompt(
        self,
        data_text: str,
        persona_count: int,
        source_descriptors: list[dict[str, Any]] | None = None,
    ) -> str:
        """ユーザー向けプロンプト構築"""
        prompt = USER_PROMPT_TEMPLATE.format(
            persona_count=persona_count, data_text=data_text
        )

        if source_descriptors:
            # LLM へは別名（dataset_id）と列名＋推定型のみ提示する（path・SQLは出さない）。
            csv_info = "\n".join(
                f'- dataset_id "{d["alias"]}"（列: {self._format_source_columns(d)}）'
                for d in source_descriptors
            )
            prompt += CSV_ANALYSIS_INSTRUCTIONS.format(csv_info=csv_info)

        prompt += f"\n{persona_count}個のペルソナを生成してください。"
        return prompt

    @staticmethod
    def _format_source_columns(descriptor: dict[str, Any]) -> str:
        """source 記述子の列を `name (type)` 形式で列挙する（型が無ければ名前のみ）。"""
        detail = descriptor.get("columns_detail")
        if detail:
            return ", ".join(
                f"{c['name']} ({c['data_type']})" if c.get("data_type") else c["name"]
                for c in detail
            )
        return ", ".join(descriptor.get("columns", []))

    def _determine_tools(
        self,
        data_type: str,
        source_descriptors: list[dict[str, Any]],
        event_queue: Any,
    ) -> list[Any]:
        """生成に使用するツールリストを決定する"""

        tools: list[Any] = []

        if data_type == "dwh":
            from ..services.data_agent_service import create_data_agent_tool

            if not config.DATA_AGENT_RUNTIME_ARN:
                raise PersonaGenerationManagerError(
                    "DATA_AGENT_RUNTIME_ARN is not configured",
                    code=ErrorCode.DATA_AGENT_NOT_CONFIGURED,
                )
            data_agent_tool = create_data_agent_tool(
                config.DATA_AGENT_RUNTIME_ARN,
                config.DATA_AGENT_REGION,
                event_queue=event_queue,
            )
            tools.append(data_agent_tool)

        if source_descriptors:
            tools.extend(
                self.dataset_analysis_service.build_source_tools(source_descriptors)
            )

        return tools

    def _extract_file_texts(
        self, file_contents: list[tuple[bytes, str]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """ファイルからテキスト抽出。Returns: (combined_text, source_descriptors)

        source_descriptors は CSV ごとの {"alias", "path", "columns"}。alias は
        Manager がここで採番し（source_1, source_2, ...）、Service へ渡す唯一の識別子
        とする（元ファイル名・path は LLM へ出さない）。
        """
        texts: list[str] = []
        csv_temp_paths: list[str] = []
        source_descriptors: list[dict[str, Any]] = []

        # 検証で raise する場合、ここで作成した一時CSVは呼び出し側の finally に
        # 到達しないため、送出前に自前でクリーンアップする。
        try:
            source_index = 0
            for content, filename in file_contents:
                if filename.lower().endswith(".csv"):
                    source_index += 1
                    alias = f"source_{source_index}"
                    csv_path = save_temp_csv(content)
                    csv_temp_paths.append(csv_path)
                    # 列推論は UTF-8 正規化済みの temp から行う（Shift_JIS/EUC-JP 対応）。
                    with open(csv_path, "rb") as f:
                        utf8_bytes = f.read()
                    columns, _ = analyze_csv_schema(utf8_bytes)
                    source_descriptors.append(
                        {
                            "alias": alias,
                            "path": csv_path,
                            # allowlist 用は列名のみ（build_source_tools が使う）。
                            "columns": [c.name for c in columns],
                            # プロンプト表示用は名前＋推定型（CSVには説明文が無いため型のみ）。
                            "columns_detail": [
                                {"name": c.name, "data_type": c.data_type}
                                for c in columns
                            ],
                        }
                    )
                    preview = get_csv_preview(content, max_lines=20)
                    # 見出しは「先頭20行プレビュー」に留める。全データへの
                    # analyze_dataset アクセス可否は有効判定に同期する
                    # CSV_ANALYSIS_INSTRUCTIONS（_build_user_prompt）でのみ主張し、
                    # 無効時に存在しないツールを示唆しない。
                    texts.append(
                        f"--- {alias} (CSV, 先頭20行プレビュー) ---\n{preview}"
                    )
                else:
                    # 内容不足は抽出後にしか判定できない（PDF/DOCXはバイト列時点で
                    # 文字数が分からないため）。CSVはプレビュー20行のみを保持し全データは
                    # DuckDBツール経由なので、この判定の対象外とする。
                    text = extract_text_from_bytes(content, filename)
                    if len(text.strip()) < 10:
                        raise PersonaGenerationManagerError(
                            f"extracted content of {filename!r} below minimum 10 chars",
                            code=ErrorCode.INTERVIEW_FILE_CONTENT_TOO_SHORT,
                        )
                    texts.append(f"--- {filename} ---\n{text}")

            combined_text = "\n\n".join(texts)

            # 合計文字数 = 抽出後テキスト長 = 入力トークン予算が真の上限。
            # 複数ファイル・PDF/Word経由の合計もここで自然にカバーする。
            if len(combined_text) > config.PERSONA_SOURCE_MAX_CHARS:
                raise PersonaGenerationCapacityError(
                    f"combined source text {len(combined_text)} chars exceeds "
                    f"{config.PERSONA_SOURCE_MAX_CHARS}",
                    context={"max_chars": config.PERSONA_SOURCE_MAX_CHARS},
                )
        except Exception:
            # PersonaGenerationManagerError に加え、analyze_csv_schema /
            # get_csv_preview が投げる _csv.Error・UnicodeDecodeError 等の
            # 想定外例外でも temp CSV を残さない（呼び出し側 finally は
            # 戻り値未確定のため到達しない）。
            cleanup_temp_files(csv_temp_paths)
            raise

        return combined_text, source_descriptors

    def _build_generation_context(
        self,
        data_type: str,
        data_description: str | None,
        custom_prompt: str | None,
        source_files: list[str],
        persona_count: int,
        auto_link_behavior: bool,
    ) -> dict[str, Any]:
        """生成コンテキスト（メタデータ）の構築"""
        ctx: dict[str, Any] = {
            "data_type": data_type,
            "data_description": data_description,
            "custom_prompt": custom_prompt,
            "source_files": source_files,
            "persona_count": persona_count,
            "generated_at": datetime.now().isoformat(),
        }
        if auto_link_behavior:
            ctx["auto_link_behavior"] = True
        return ctx

    def _validate_generated_persona(self, persona: Persona) -> None:
        """生成されたペルソナの基本バリデーション"""
        if not persona.name or not persona.name.strip():
            raise PersonaGenerationManagerError(
                "generated persona has no name",
                code=ErrorCode.PERSONA_INVALID,
            )
        if not persona.id:
            raise PersonaGenerationManagerError(
                "generated persona has no id",
                code=ErrorCode.PERSONA_INVALID,
            )

    # ------------------------------------------------------------------
    # 行動データ紐付け
    # ------------------------------------------------------------------

    def _build_behavior_dataset_candidates(
        self,
        csv_urls: list[str],
        persona_name: str,
        thinking_log: list[dict[str, str]],
        csv_url_labels: list[str],
    ) -> list[dict[str, Any]]:
        """CSV URLリストからデータセット候補を構築する"""
        from ..services.data_agent_service import DataAgentService

        fallback_col, fallback_val = self._extract_user_id_from_log(thinking_log)

        candidates: list[dict[str, Any]] = []
        type_counter: int = 0
        label_counts: dict[str, int] = {}

        for idx, url in enumerate(csv_urls):
            try:
                csv_bytes = DataAgentService.download_csv(url)
                columns, row_count = analyze_csv_schema(csv_bytes)
                if row_count == 0:
                    continue

                col_names = [c.name for c in columns]

                data_type_label = ""
                if csv_url_labels and idx < len(csv_url_labels):
                    data_type_label = self._extract_label_from_tool_call(
                        csv_url_labels[idx]
                    )

                if not data_type_label:
                    data_type_label = infer_behavior_data_type(col_names)

                if not data_type_label:
                    type_counter += 1
                    data_type_label = f"行動データ{type_counter}"

                binding_key_col, binding_key_val = detect_binding_key(
                    col_names, csv_bytes
                )
                if not binding_key_col and fallback_col:
                    binding_key_col, binding_key_val = fallback_col, fallback_val

                label_counts[data_type_label] = label_counts.get(data_type_label, 0) + 1
                if label_counts[data_type_label] > 1:
                    dataset_name = f"{persona_name}_{data_type_label}{label_counts[data_type_label]}"
                else:
                    dataset_name = f"{persona_name}_{data_type_label}"

                candidates.append(
                    {
                        "temp_id": str(uuid.uuid4()),
                        "name": dataset_name,
                        "data_type_label": data_type_label,
                        "csv_bytes": csv_bytes,
                        "columns": columns,
                        "row_count": row_count,
                        "binding_key_column": binding_key_col,
                        "binding_key_value": binding_key_val,
                    }
                )
            except Exception as e:
                logger.warning(f"行動データCSVダウンロード/解析エラー: {e}")
                continue

        return candidates

    def _extract_label_from_tool_call(self, detail: str) -> str:
        """tool_callのdetailからデータ種別ラベルを抽出する"""
        m = re.search(r"の(.+?)を.*CSV", detail)
        if m:
            return m.group(1).strip()
        m = re.search(r"(.+?)をCSV", detail)
        if m:
            label = m.group(1).strip()
            if len(label) > 20:
                parts = re.split(r"の", label)
                if parts:
                    label = parts[-1]
            return label
        return ""

    def _extract_user_id_from_log(
        self, thinking_log: list[dict[str, str]]
    ) -> tuple[str, str]:
        """思考ログからcustomer_id/user_idとその値を抽出する"""
        patterns = [
            r"customer_id\s*[=:]\s*['\"]?([a-zA-Z0-9\-_]+)['\"]?",
            r"user_id\s*[=:]\s*['\"]?([a-zA-Z0-9\-_]+)['\"]?",
        ]
        all_text = " ".join(
            entry.get("content", "") + " " + entry.get("detail", "")
            for entry in thinking_log
        )
        for pattern in patterns:
            matches = re.findall(pattern, all_text)
            if matches:
                col_name = "customer_id" if "customer_id" in pattern else "user_id"
                return col_name, matches[-1]
        return "", ""
