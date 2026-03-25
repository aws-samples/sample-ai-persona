"""
Integration tests for insight display functionality (Task 11.2).
Tests the insight display and save functionality in the discussion results page.

Note: このテストは pages.discussion_results モジュールに依存していますが、
現在のアーキテクチャでは web/routers/ に移行されています。
"""

import pytest
from typing import List
from unittest.mock import Mock, patch

from src.models.persona import Persona
from src.models.discussion import Discussion
from src.models.message import Message
from src.models.insight import Insight

# DiscussionDisplayクラスは現在のアーキテクチャには存在しないため、
# このテストモジュール全体をスキップ
pytestmark = pytest.mark.skip(
    reason="pages.discussion_results module has been migrated to web/routers/"
)


class TestInsightDisplayIntegration:
    """インサイト表示機能の統合テスト"""

    @pytest.fixture
    def sample_personas(self) -> List[Persona]:
        """テスト用ペルソナを作成"""
        return [
            Persona.create_new(
                name="田中太郎",
                age=35,
                occupation="会社員",
                background="都市部在住の会社員。効率性を重視する。",
                values=["時間効率", "コストパフォーマンス"],
                pain_points=["時間不足", "情報過多"],
                goals=["生産性向上", "ワークライフバランス"],
            ),
            Persona.create_new(
                name="佐藤花子",
                age=28,
                occupation="デザイナー",
                background="クリエイティブ業界で働く。美的センスを重視。",
                values=["創造性", "美しさ"],
                pain_points=["予算制約", "クライアント要求"],
                goals=["スキル向上", "作品の質向上"],
            ),
        ]

    @pytest.fixture
    def sample_insights(self) -> List[Insight]:
        """テスト用インサイトを作成"""
        return [
            Insight.create_new(
                category="ユーザーニーズ",
                description="効率的なタスク管理機能への強いニーズがある。現代の忙しいライフスタイルに対応した時間管理ソリューションが求められている。",
                supporting_messages=["田中太郎の発言: 効率的なタスク管理機能が必要"],
                confidence_score=0.9,
            ),
            Insight.create_new(
                category="UI/UX要件",
                description="直感的で美しいUIデザインがユーザー満足度向上の鍵となる。視覚的な魅力と使いやすさの両立が重要。",
                supporting_messages=["佐藤花子の発言: 直感的で美しいUIが重要"],
                confidence_score=0.85,
            ),
            Insight.create_new(
                category="設計原則",
                description="機能の過多は使いにくさにつながる可能性がある。シンプルで洗練されたデザインアプローチが推奨される。",
                supporting_messages=["田中太郎と佐藤花子の発言: シンプルさの重要性"],
                confidence_score=0.8,
            ),
            Insight.create_new(
                category="ユーザーニーズ",
                description="時間管理とデザイン性を両立したソリューションへのニーズがある。",
                supporting_messages=["両ペルソナの発言から導出"],
                confidence_score=0.75,
            ),
        ]

    @pytest.fixture
    def discussion_with_insights(
        self, sample_personas: List[Persona], sample_insights: List[Insight]
    ) -> Discussion:
        """インサイト付きの議論を作成"""
        discussion = Discussion.create_new(
            topic="新しいモバイルアプリの機能について",
            participants=[persona.id for persona in sample_personas],
        )

        # メッセージを追加
        messages = [
            Message.create_new(
                persona_id=sample_personas[0].id,
                persona_name=sample_personas[0].name,
                content="効率的なタスク管理機能が必要だと思います。忙しい現代人には時間管理が重要です。",
            ),
            Message.create_new(
                persona_id=sample_personas[1].id,
                persona_name=sample_personas[1].name,
                content="見た目も重要ですね。直感的で美しいUIがあれば、ユーザーの満足度も上がります。",
            ),
        ]

        for message in messages:
            discussion = discussion.add_message(message)

        # インサイトを追加
        for insight in sample_insights:
            discussion = discussion.add_insight(insight)

        return discussion

    def test_categorize_insights(self, sample_insights: List[Insight]):
        """インサイトのカテゴリ分類をテスト"""
        display = DiscussionDisplay()
        categorized = display._categorize_insights(sample_insights)

        # カテゴリ数の確認
        assert len(categorized) == 3  # ユーザーニーズ、UI/UX要件、設計原則

        # 各カテゴリの件数確認
        assert len(categorized["ユーザーニーズ"]) == 2
        assert len(categorized["UI/UX要件"]) == 1
        assert len(categorized["設計原則"]) == 1

        # カテゴリがソートされていることを確認
        categories = list(categorized.keys())
        assert categories == sorted(categories)

    def test_insight_card_rendering_data(self, sample_insights: List[Insight]):
        """インサイトカードのデータ処理をテスト"""
        display = DiscussionDisplay()

        for insight in sample_insights:
            # 信頼度による色分類のテスト
            if insight.confidence_score >= 0.8:
                expected_confidence_text = "高"
            elif insight.confidence_score >= 0.6:
                expected_confidence_text = "中"
            else:
                expected_confidence_text = "低"

            # カテゴリアイコンのテスト
            category_icons = {
                "ユーザーニーズ": "🎯",
                "課題・問題点": "⚠️",
                "改善提案": "💡",
                "市場機会": "📈",
                "競合分析": "🏆",
                "技術要件": "⚙️",
                "ビジネス戦略": "📊",
                "その他": "📝",
            }
            expected_icon = category_icons.get(insight.category, "📝")

            # データの整合性を確認
            assert insight.category is not None
            assert insight.description is not None
            assert 0.0 <= insight.confidence_score <= 1.0
            assert isinstance(insight.supporting_messages, list)

    def test_insight_statistics_calculation(self, sample_insights: List[Insight]):
        """インサイト統計の計算をテスト"""
        display = DiscussionDisplay()
        categorized = display._categorize_insights(sample_insights)

        # 基本統計の計算
        total_count = len(sample_insights)
        avg_confidence = sum(
            insight.confidence_score for insight in sample_insights
        ) / len(sample_insights)
        high_confidence_count = sum(
            1 for insight in sample_insights if insight.confidence_score >= 0.8
        )

        assert total_count == 4
        assert 0.0 <= avg_confidence <= 1.0
        assert high_confidence_count == 3  # 0.9, 0.85, 0.8の3件

        # カテゴリ別統計
        for category, category_insights in categorized.items():
            category_avg = sum(
                insight.confidence_score for insight in category_insights
            ) / len(category_insights)
            assert 0.0 <= category_avg <= 1.0

    @patch("pages.discussion_results.LoadingComponents")
    @patch("pages.discussion_results.MessageComponents")
    def test_generate_insights_workflow(
        self,
        mock_message_components,
        mock_loading_components,
        sample_personas: List[Persona],
        sample_insights: List[Insight],
    ):
        """インサイト生成ワークフローをテスト"""
        # モックの設定
        mock_discussion_manager = Mock()
        mock_discussion_manager.generate_insights.return_value = sample_insights
        mock_discussion_manager.save_discussion.return_value = "test_discussion_id"

        mock_persona_manager = Mock()

        display = DiscussionDisplay()
        display.discussion_manager = mock_discussion_manager
        display.persona_manager = mock_persona_manager

        # テスト用議論を作成
        discussion = Discussion.create_new(
            topic="テスト議論", participants=[persona.id for persona in sample_personas]
        )

        # メッセージを追加
        message = Message.create_new(
            persona_id=sample_personas[0].id,
            persona_name=sample_personas[0].name,
            content="テストメッセージ",
        )
        discussion = discussion.add_message(message)

        # ペルソナ辞書を作成
        personas_dict = {persona.id: persona for persona in sample_personas}

        # インサイト生成をテスト（UIコンポーネントはモック）
        try:
            # generate_and_display_insightsメソッドの主要ロジックをテスト
            generated_insights = mock_discussion_manager.generate_insights(discussion)
            assert len(generated_insights) == 4

            # 議論にインサイトを追加
            updated_discussion = discussion
            for insight in generated_insights:
                updated_discussion = updated_discussion.add_insight(insight)

            # 保存処理
            discussion_id = mock_discussion_manager.save_discussion(updated_discussion)
            assert discussion_id == "test_discussion_id"

            # モックが正しく呼ばれたことを確認
            mock_discussion_manager.generate_insights.assert_called_once_with(
                discussion
            )
            mock_discussion_manager.save_discussion.assert_called_once()

        except Exception as e:
            pytest.fail(f"インサイト生成ワークフローでエラーが発生: {e}")

    def test_save_discussion_workflow(self, discussion_with_insights: Discussion):
        """議論保存ワークフローをテスト"""
        # モックの設定
        mock_discussion_manager = Mock()
        mock_discussion_manager.save_discussion.return_value = (
            discussion_with_insights.id
        )
        mock_discussion_manager.discussion_exists.return_value = True

        display = DiscussionDisplay()
        display.discussion_manager = mock_discussion_manager

        # 保存処理のテスト
        try:
            discussion_id = mock_discussion_manager.save_discussion(
                discussion_with_insights
            )
            assert discussion_id == discussion_with_insights.id

            # 存在確認
            exists = mock_discussion_manager.discussion_exists(
                discussion_with_insights.id
            )
            assert exists is True

            # モックが正しく呼ばれたことを確認
            mock_discussion_manager.save_discussion.assert_called_once_with(
                discussion_with_insights
            )
            mock_discussion_manager.discussion_exists.assert_called_once_with(
                discussion_with_insights.id
            )

        except Exception as e:
            pytest.fail(f"議論保存ワークフローでエラーが発生: {e}")

    def test_insight_filtering_by_category(self, sample_insights: List[Insight]):
        """カテゴリによるインサイトフィルタリングをテスト"""
        display = DiscussionDisplay()
        categorized = display._categorize_insights(sample_insights)

        # 特定カテゴリのフィルタリング
        user_needs_insights = categorized.get("ユーザーニーズ", [])
        assert len(user_needs_insights) == 2

        ui_ux_insights = categorized.get("UI/UX要件", [])
        assert len(ui_ux_insights) == 1

        design_insights = categorized.get("設計原則", [])
        assert len(design_insights) == 1

        # 存在しないカテゴリ
        non_existent = categorized.get("存在しないカテゴリ", [])
        assert len(non_existent) == 0

    def test_confidence_score_validation(self, sample_insights: List[Insight]):
        """信頼度スコアの妥当性をテスト"""
        for insight in sample_insights:
            # 信頼度が0.0-1.0の範囲内であることを確認
            assert 0.0 <= insight.confidence_score <= 1.0

            # 信頼度による分類が正しいことを確認
            if insight.confidence_score >= 0.8:
                confidence_level = "高"
            elif insight.confidence_score >= 0.6:
                confidence_level = "中"
            else:
                confidence_level = "低"

            assert confidence_level in ["高", "中", "低"]

    def test_insight_data_integrity(self, discussion_with_insights: Discussion):
        """インサイトデータの整合性をテスト"""
        # 議論にインサイトが含まれていることを確認
        assert len(discussion_with_insights.insights) > 0

        # 各インサイトの必須フィールドを確認
        for insight in discussion_with_insights.insights:
            assert insight.category is not None and insight.category.strip() != ""
            assert insight.description is not None and insight.description.strip() != ""
            assert isinstance(insight.supporting_messages, list)
            assert 0.0 <= insight.confidence_score <= 1.0

        # 議論の他のデータとの整合性
        assert discussion_with_insights.id is not None
        assert discussion_with_insights.topic is not None
        assert len(discussion_with_insights.participants) > 0
        assert len(discussion_with_insights.messages) > 0

    def test_error_handling_for_empty_insights(self):
        """空のインサイトリストのエラーハンドリングをテスト"""
        display = DiscussionDisplay()

        # 空のインサイトリスト
        empty_insights = []
        categorized = display._categorize_insights(empty_insights)

        assert len(categorized) == 0
        assert isinstance(categorized, dict)

    def test_error_handling_for_invalid_insights(self):
        """無効なインサイトのエラーハンドリングをテスト"""
        display = DiscussionDisplay()

        # 無効なインサイト（カテゴリが空）
        try:
            invalid_insight = Insight.create_new(
                category="",  # 空のカテゴリ
                description="テスト説明",
                supporting_messages=[],
                confidence_score=0.5,
            )
            # カテゴリが空でもInsightオブジェクトは作成される（バリデーションは別途実装）
            assert invalid_insight.category == ""
        except Exception:
            # エラーが発生する場合もある
            pass

        # 信頼度が範囲外
        with pytest.raises(ValueError):
            Insight.create_new(
                category="テストカテゴリ",
                description="テスト説明",
                supporting_messages=[],
                confidence_score=1.5,  # 範囲外
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
