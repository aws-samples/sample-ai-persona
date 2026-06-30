"""レポート生成・管理Manager。

議論結果からのレポート生成、CRUD、フォローアップ分析を担当する。
classic/agent両モードの議論で共通利用される。
"""

import logging
from typing import Any, Dict, List, Optional

from ..models.discussion import Discussion
from ..models.discussion_report import DiscussionReport
from ..services.ai_service import AIService
from ..services.agent_service import AgentService
from ..services.database_service import DatabaseService
from ..services.service_factory import service_factory
from ..prompts.report_prompts import (
    build_data_driven_followup_system_prompt,
    build_data_driven_system_prompt,
    build_report_system_prompt,
)


class ReportManagerError(Exception):
    """レポートManager固有の例外。"""

    pass


class ReportManager:
    """レポート生成・管理を担当するManager。"""

    def __init__(
        self,
        ai_service: Optional[AIService] = None,
        agent_service: Optional[AgentService] = None,
        database_service: Optional[DatabaseService] = None,
    ):
        self.ai_service = ai_service or service_factory.get_ai_service()
        self.agent_service = agent_service or service_factory.get_agent_service()
        self.database_service = (
            database_service or service_factory.get_database_service()
        )
        self.logger = logging.getLogger(__name__)

    def get_discussion(self, discussion_id: str) -> Optional[Discussion]:
        """議論をDBから取得する。"""
        return self.database_service.get_discussion(discussion_id)

    def _get_report_context(self, discussion: Discussion) -> tuple:
        """レポート生成用のインサイトデータとペルソナデータを取得する。"""
        insights_data = [
            {
                "category": ins.category,
                "description": ins.description,
                "confidence_score": ins.confidence_score,
            }
            for ins in discussion.insights
        ]
        personas_data = []
        for pid in discussion.participants:
            persona = self.database_service.get_persona(pid)
            if persona:
                personas_data.append(
                    {
                        "name": persona.name,
                        "age": persona.age,
                        "occupation": persona.occupation,
                        "values": persona.values,
                        "pain_points": persona.pain_points,
                        "goals": persona.goals,
                    }
                )
        return insights_data, personas_data

    def _build_report_user_content(
        self,
        messages: List[Any],
        insights: List[Dict[str, Any]],
        topic: str,
        personas: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """レポート生成用のユーザーコンテキストをMarkdown形式で構築する。"""
        context_parts = [f"## 議論トピック\n{topic}\n"]

        if personas:
            context_parts.append("## 参加ペルソナのプロフィール")
            for p in personas:
                context_parts.append(
                    f"### {p['name']}（{p['age']}歳 / {p['occupation']}）\n"
                    f"- 価値観: {', '.join(p.get('values', []))}\n"
                    f"- 課題: {', '.join(p.get('pain_points', []))}\n"
                    f"- 目標: {', '.join(p.get('goals', []))}"
                )

        context_parts.append("\n## 議論ログ")
        for msg in messages:
            context_parts.append(f"**{msg.persona_name}**: {msg.content}")
        context_parts.append("\n## 抽出済みインサイト")
        for ins in insights:
            context_parts.append(
                f"- [{ins.get('category', '')}] {ins.get('description', '')} "
                f"(信頼度: {ins.get('confidence_score', 0)})"
            )

        return "\n".join(context_parts)

    def generate_report_streaming(
        self,
        discussion_id: str,
        template_type: str,
        custom_prompt: Optional[str] = None,
        event_queue: Any = None,
        session_id: Optional[str] = None,
        is_followup: bool = False,
    ) -> Any:
        """議論からレポートをストリーミング生成する。

        Args:
            discussion_id: 議論ID
            template_type: テンプレート種別 (summary/review/custom/data_driven)
            custom_prompt: カスタムプロンプト
            event_queue: リアルタイムイベント用queue（data_driven時）
            session_id: AgentCore Memory STMセッションID（data_driven時）
            is_followup: フォローアップ分析かどうか

        Yields:
            str: テキストチャンク
        """
        discussion = self.database_service.get_discussion(discussion_id)
        if not discussion:
            raise ReportManagerError("議論が見つかりません")

        insights_data, personas_data = self._get_report_context(discussion)

        if template_type == "data_driven":
            effective_session_id = session_id or f"report-{discussion_id}"
            yield from self._generate_data_driven_report(
                messages=discussion.messages,
                insights=insights_data,
                topic=discussion.topic,
                custom_prompt=custom_prompt,
                personas=personas_data,
                event_queue=event_queue,
                session_id=effective_session_id,
                is_followup=is_followup,
            )
        else:
            system_prompt = build_report_system_prompt(
                discussion.topic, template_type, custom_prompt
            )
            user_content = self._build_report_user_content(
                discussion.messages, insights_data, discussion.topic, personas_data
            )
            converse_messages = [{"role": "user", "content": [{"text": user_content}]}]
            yield from self.ai_service.generate_standard_report_streaming(
                system_prompt=system_prompt,
                converse_messages=converse_messages,
            )

    def _generate_data_driven_report(
        self,
        messages: Any,
        insights: Any,
        topic: str,
        custom_prompt: Optional[str],
        personas: Any,
        event_queue: Any = None,
        session_id: Optional[str] = None,
        is_followup: bool = False,
    ) -> Any:
        """データドリブンレポートの生成をAgentServiceに委譲する。"""
        if is_followup:
            system_prompt = build_data_driven_followup_system_prompt(
                topic, custom_prompt
            )
        else:
            system_prompt = build_data_driven_system_prompt(topic, custom_prompt)

        user_content = self._build_report_user_content(
            messages, insights, topic, personas
        )

        yield from self.agent_service.run_report_agent_streaming(
            system_prompt=system_prompt,
            user_content=user_content,
            event_queue=event_queue,
            session_id=session_id,
        )

    def generate_followup_report_streaming(
        self,
        discussion_id: str,
        followup_prompt: str,
        previous_report: str,
        event_queue: Any = None,
        session_id: Optional[str] = None,
    ) -> Any:
        """前回レポートを踏まえたフォローアップ分析をストリーミング生成する。

        STMセッションが有効なら前回履歴が自動復元されるため追加指示のみ渡す。
        STMが期限切れの場合はprevious_reportをフォールバックとしてプロンプトに注入。
        """
        effective_session_id = session_id or f"report-{discussion_id}"
        stm_available = self._check_stm_session(effective_session_id)

        if stm_available:
            self.logger.info(
                f"フォローアップ: STMセッション継続 (session_id={effective_session_id})"
            )
            effective_prompt = (
                f"追加指示: {followup_prompt}\n\n"
                "重要: 前回の会話でテーブル構造やデータ分析は実施済みです。"
                "テーブル一覧の再確認は不要です。追加指示に直接関連するクエリのみ実行してください。"
            )
        else:
            self.logger.info(
                f"フォローアップ: STMセッション未検出、フォールバック使用 "
                f"(session_id={effective_session_id})"
            )
            effective_prompt = (
                "以下は先に生成した分析レポートです。"
                "このレポートを前提として追加分析を行ってください:\n\n"
                f"---\n{previous_report}\n---\n\n"
                f"追加指示: {followup_prompt}\n\n"
                "重要: 前回レポートでテーブル構造は確認済みです。"
                "テーブル一覧の再確認は不要です。"
                "追加指示に直接関連するクエリのみ実行してください。"
            )

        yield from self.generate_report_streaming(
            discussion_id=discussion_id,
            template_type="data_driven",
            custom_prompt=effective_prompt,
            event_queue=event_queue,
            session_id=effective_session_id,
            is_followup=True,
        )

    def _check_stm_session(self, session_id: str) -> bool:
        """AgentCore Memory STMにセッション履歴が存在するか確認する。"""
        return service_factory.check_stm_session_exists(session_id)

    def save_report(self, discussion_id: str, report: DiscussionReport) -> None:
        """生成済みレポートをDBに保存する。

        Raises:
            ReportManagerError: 議論が見つからない場合や上限超過
        """
        discussion = self.database_service.get_discussion(discussion_id)
        if not discussion:
            raise ReportManagerError("議論が見つかりません")

        if len(discussion.reports) >= 3:
            raise ReportManagerError(
                "レポートは最大3件まで保存できます。不要なレポートを削除してください。"
            )

        discussion.reports.append(report)
        self.database_service.save_discussion(discussion)

    def update_report_content(
        self, discussion_id: str, report_id: str, content: str
    ) -> None:
        """既存レポートの内容を更新する。

        Raises:
            ReportManagerError: 議論やレポートが見つからない場合
        """
        discussion = self.database_service.get_discussion(discussion_id)
        if not discussion:
            raise ReportManagerError("議論が見つかりません")

        updated = False
        for i, report in enumerate(discussion.reports):
            if report.id == report_id:
                from dataclasses import replace

                discussion.reports[i] = replace(report, content=content)
                updated = True
                break

        if not updated:
            raise ReportManagerError("レポートが見つかりません")

        self.database_service.save_discussion(discussion)

    def delete_report(self, discussion_id: str, report_id: str) -> bool:
        """議論からレポートを削除する。

        Returns:
            True if deleted

        Raises:
            ReportManagerError: 議論やレポートが見つからない場合
        """
        discussion = self.database_service.get_discussion(discussion_id)
        if not discussion:
            raise ReportManagerError("議論が見つかりません")

        original_len = len(discussion.reports)
        discussion.reports = [r for r in discussion.reports if r.id != report_id]

        if len(discussion.reports) == original_len:
            raise ReportManagerError("レポートが見つかりません")

        self.database_service.save_discussion(discussion)
        return True
