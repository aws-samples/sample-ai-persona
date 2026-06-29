"""ペルソナ生成ワークフローを管理するManager"""

import logging
import re
import uuid
from datetime import datetime
from typing import Any

from cachetools import TTLCache  # type: ignore[import-untyped]

from ..models.persona import Persona
from ..services.agent_service import AgentService, AgentServiceError
from ..services.database_service import DatabaseService
from ..services.service_factory import service_factory
from .prompts.persona_generation_prompts import (
    CSV_ANALYSIS_INSTRUCTIONS,
    CUSTOM_PROMPT_SECTION,
    DATA_TYPE_PROMPTS,
    DWH_AUTO_LINK_INSTRUCTIONS,
    PERSONA_GENERATION_SYSTEM_PROMPT_TEMPLATE,
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


logger = logging.getLogger(__name__)


class PersonaGenerationManagerError(Exception):
    pass


class PersonaGenerationManager:
    """ペルソナ生成ワークフロー全体のオーケストレーション"""

    def __init__(
        self,
        agent_service: AgentService | None = None,
        database_service: DatabaseService | None = None,
    ):
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )
        self._personas_cache: TTLCache = TTLCache(maxsize=1000, ttl=1800)
        self._behavior_datasets_cache: TTLCache = TTLCache(maxsize=100, ttl=1800)

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
    ) -> tuple[list[Persona], list[dict[str, str]], list[dict[str, Any]]]:
        """ペルソナを生成し、一時キャッシュに格納する。

        Returns:
            (personas, thinking_log, candidate_datasets)
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

        candidate_datasets: list[dict[str, Any]] = []
        return personas, thinking_log, candidate_datasets

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

    def save_behavior_datasets(
        self,
        saved_persona_id: str,
        cache_persona_id: str,
        selected_ids: set[str],
    ) -> int:
        """選択された行動データセットを保存しペルソナに紐付ける。

        Returns: 紐付けたデータセット数
        """
        from .dataset_manager import DatasetManager

        cached_datasets = self._behavior_datasets_cache.pop(cache_persona_id, None)
        if not cached_datasets:
            logger.warning(
                "行動データセットのキャッシュが見つかりません（期限切れの可能性）"
            )
            return 0

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
                logger.info(f"行動データセット保存: {dataset.name} (ID: {dataset.id})")
            except Exception as e:
                logger.error(f"行動データセット保存エラー ({ds_info['name']}): {e}")

        if bindings_data:
            dataset_manager.set_persona_bindings(saved_persona_id, bindings_data)
            logger.info(
                f"ペルソナ {saved_persona_id} に {len(bindings_data)}件のデータセットを紐付け"
            )

        return len(bindings_data)

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
                "ペルソナ数は1-10の範囲で指定してください"
            )
        if data_type == "dwh":
            if not data_description or not data_description.strip():
                raise PersonaGenerationManagerError("分析の切り口を入力してください")
        else:
            if not file_contents:
                raise PersonaGenerationManagerError("ファイルが選択されていません")

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

        combined_text, csv_temp_paths = self._extract_file_texts(file_contents)
        use_mcp = len(csv_temp_paths) > 0

        try:
            personas, thinking_log = self.agent_service.generate_personas_with_agent(
                data_text=combined_text,
                data_type=data_type,
                persona_count=persona_count,
                data_description=data_description,
                custom_prompt=custom_prompt,
                use_mcp=use_mcp,
                csv_paths=csv_temp_paths if csv_temp_paths else None,
            )
        except AgentServiceError as e:
            raise PersonaGenerationManagerError(f"エージェントサービスエラー: {e}")
        except Exception as e:
            raise PersonaGenerationManagerError(f"予期しないエラー: {e}")
        finally:
            cleanup_temp_files(csv_temp_paths)

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

        try:
            personas, thinking_log = self.agent_service.generate_personas_with_agent(
                data_text=data_text,
                data_type="dwh",
                persona_count=persona_count,
                custom_prompt=custom_prompt,
                callback_handler=callback_handler,
                event_queue=event_queue,
            )
        except AgentServiceError as e:
            raise PersonaGenerationManagerError(
                f"データ分析エージェント連携エラー: {e}"
            )
        except Exception as e:
            raise PersonaGenerationManagerError(f"DWH ペルソナ生成エラー: {e}")

        return personas, thinking_log

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
        csv_paths: list[str] | None = None,
    ) -> str:
        """ユーザー向けプロンプト構築"""
        prompt = USER_PROMPT_TEMPLATE.format(
            persona_count=persona_count, data_text=data_text
        )

        if csv_paths:
            csv_info = "\n".join(
                f"- `{p}` （queryツールで `SELECT * FROM read_csv('{p}')` で参照可能）"
                for p in csv_paths
            )
            prompt += CSV_ANALYSIS_INSTRUCTIONS.format(csv_info=csv_info)

        prompt += f"\n{persona_count}個のペルソナを生成してください。"
        return prompt

    def _determine_tools(
        self, data_type: str, use_mcp: bool, event_queue: Any
    ) -> list[Any]:
        """生成に使用するツールリストを決定する"""
        from ..config import config

        tools: list[Any] = []

        if data_type == "dwh":
            from ..services.data_agent_service import create_data_agent_tool

            if not config.DATA_AGENT_RUNTIME_ARN:
                raise PersonaGenerationManagerError(
                    "データ分析エージェントの接続設定がされていません。設定画面から Runtime ARN を設定してください"
                )
            data_agent_tool = create_data_agent_tool(
                config.DATA_AGENT_RUNTIME_ARN,
                config.DATA_AGENT_REGION,
                event_queue=event_queue,
            )
            tools.append(data_agent_tool)

        if use_mcp:
            from ..services.mcp_server_manager import get_mcp_manager

            mcp_manager = get_mcp_manager()
            if not mcp_manager.is_running():
                mcp_manager.start()
            if mcp_manager.is_running():
                mcp_tools = mcp_manager.get_tools()
                if mcp_tools:
                    tools.extend(mcp_tools)

        return tools

    def _extract_file_texts(
        self, file_contents: list[tuple[bytes, str]]
    ) -> tuple[str, list[str]]:
        """ファイルからテキスト抽出。Returns: (combined_text, csv_temp_paths)"""
        texts: list[str] = []
        csv_temp_paths: list[str] = []

        for content, filename in file_contents:
            if filename.lower().endswith(".csv"):
                csv_path = save_temp_csv(content)
                csv_temp_paths.append(csv_path)
                preview = get_csv_preview(content, max_lines=20)
                texts.append(
                    f"--- {filename} (CSV, 全データは分析ツールで参照可能) ---\n{preview}"
                )
            else:
                text = extract_text_from_bytes(content, filename)
                texts.append(f"--- {filename} ---\n{text}")

        return "\n\n".join(texts), csv_temp_paths

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
            raise PersonaGenerationManagerError("ペルソナ名が設定されていません")
        if not persona.id:
            raise PersonaGenerationManagerError(
                "生成されたペルソナにIDが設定されていません"
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
        from .dataset_manager import DatasetManager

        fallback_col, fallback_val = self._extract_user_id_from_log(thinking_log)

        dataset_manager = DatasetManager()
        candidates: list[dict[str, Any]] = []
        type_counter: int = 0
        label_counts: dict[str, int] = {}

        for idx, url in enumerate(csv_urls):
            try:
                csv_bytes = dataset_manager.download_csv_from_url(url)
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
