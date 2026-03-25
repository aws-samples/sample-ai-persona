"""
議論フローの統合テスト
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from src.services.ai_service import AIService
from src.models.persona import Persona


class TestDiscussionFlow:
    """議論フローの統合テストクラス"""

    def setup_method(self):
        """各テストメソッドの前に実行される初期化"""
        # モック Bedrock クライアントを作成
        self.mock_bedrock_client = Mock()
        self.ai_service = AIService(bedrock_client=self.mock_bedrock_client)

        # テスト用ペルソナデータ
        self.persona1 = Persona(
            id="persona-1",
            name="田中太郎",
            age=35,
            occupation="IT企業の営業マネージャー",
            background="大学卒業後、IT企業に就職し10年間営業職に従事。顧客との関係構築を重視し、効率的な営業プロセスを追求している。",
            values=["効率性", "顧客満足", "チームワーク"],
            pain_points=["時間管理の難しさ", "競合他社との差別化", "新技術への対応"],
            goals=["売上目標の達成", "顧客基盤の拡大", "営業スキルの向上"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

        self.persona2 = Persona(
            id="persona-2",
            name="佐藤花子",
            age=28,
            occupation="フリーランスWebデザイナー",
            background="美術大学卒業後、Web制作会社で3年間勤務した後、フリーランスとして独立。クリエイティブな仕事を重視している。",
            values=["創造性", "自由度", "品質へのこだわり"],
            pain_points=[
                "収入の不安定さ",
                "クライアント獲得の困難",
                "技術トレンドへの追従",
            ],
            goals=["安定した収入の確保", "スキルアップ", "ブランド力の向上"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

    def test_complete_discussion_flow(self):
        """完全な議論フローのテスト（議論実行→インサイト抽出）"""
        # 議論のモックレスポンス
        discussion_response = """
[田中太郎]: 新しいWebサービスについて議論しましょう。営業の立場から言うと、顧客が求めているのは使いやすさと効率性だと思います。
[佐藤花子]: デザイナーの視点では、ユーザーインターフェースの美しさと直感的な操作性が重要ですね。見た目の印象で第一印象が決まります。
[田中太郎]: その通りです。ただ、機能が多すぎると逆に使いにくくなる場合もあります。シンプルで分かりやすい設計が必要だと思います。
[佐藤花子]: 同感です。ミニマルなデザインでありながら、必要な機能はしっかりと提供する。そのバランスが重要ですね。
[田中太郎]: 価格設定も重要な要素です。競合他社と比較して適切な価格帯を設定する必要があります。
[佐藤花子]: 価格だけでなく、サポート体制も差別化要因になると思います。ユーザーが困った時にすぐに解決できる仕組みが必要です。
"""

        # インサイトのモックレスポンス
        insights_response = """
- [顧客ニーズ] ユーザーは使いやすさと効率性を最も重視している
- [商品開発] シンプルで直感的なユーザーインターフェースの設計が必要
- [商品開発] 機能の多さよりも使いやすさを優先すべき
- [マーケティング] 見た目の第一印象が重要な購買決定要因となる
- [マーケティング] 競合他社との価格比較が購買に大きく影響する
- [顧客ニーズ] 充実したサポート体制が差別化要因として重要
- [市場機会] ミニマルデザインと機能性のバランスを取った商品に需要がある
"""

        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            # 最初の呼び出し（議論）と2回目の呼び出し（インサイト）を設定
            mock_retry.side_effect = [discussion_response, insights_response]

            # 1. 議論を実行
            personas = [self.persona1, self.persona2]
            topic = "新しいWebサービスの企画について"

            messages = self.ai_service.facilitate_discussion(personas, topic)

            # 議論結果の検証
            assert len(messages) == 6
            assert messages[0].persona_name == "田中太郎"
            assert messages[1].persona_name == "佐藤花子"
            assert "使いやすさと効率性" in messages[0].content
            assert "ユーザーインターフェース" in messages[1].content

            # 2. インサイトを抽出
            insights = self.ai_service.extract_insights(messages)

            # インサイト結果の検証（構造化データ）
            assert len(insights) == 7
            assert isinstance(insights, list)
            assert all(isinstance(insight, dict) for insight in insights)
            assert all("description" in insight for insight in insights)
            assert all("confidence_score" in insight for insight in insights)

            # 内容の検証
            descriptions = [insight["description"] for insight in insights]
            assert any("使いやすさと効率性" in desc for desc in descriptions)
            assert any("ユーザーインターフェース" in desc for desc in descriptions)
            assert any("価格比較" in desc for desc in descriptions)
            assert any("サポート体制" in desc for desc in descriptions)

            # API呼び出し回数の確認
            assert mock_retry.call_count == 2

    def test_discussion_with_multiple_personas(self):
        """複数ペルソナでの議論テスト"""
        # 3人目のペルソナを追加
        persona3 = Persona(
            id="persona-3",
            name="山田次郎",
            age=42,
            occupation="マーケティングディレクター",
            background="広告代理店で15年間マーケティング業務に従事。データ分析に基づいた戦略立案を得意とする。",
            values=["データ重視", "戦略的思考", "ROI最大化"],
            pain_points=["予算制約", "効果測定の困難", "市場変化への対応"],
            goals=["マーケティングROIの向上", "ブランド認知度向上", "新規顧客獲得"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

        discussion_response = """
[田中太郎]: 営業の観点から、顧客は価格と品質のバランスを重視しています。
[佐藤花子]: デザインの美しさも重要な要素です。視覚的な魅力が購買意欲を高めます。
[山田次郎]: マーケティングデータを見ると、ターゲット層は20-30代の女性が中心ですね。
[田中太郎]: その層に向けた営業アプローチを考える必要がありますね。
[佐藤花子]: 若い女性向けなら、SNS映えするデザインが効果的だと思います。
[山田次郎]: SNSマーケティングの予算配分も重要な検討事項です。
"""

        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.return_value = discussion_response

            personas = [self.persona1, self.persona2, persona3]
            topic = "新商品のマーケティング戦略"

            messages = self.ai_service.facilitate_discussion(personas, topic)

            # 3人全員が発言していることを確認
            persona_names = {msg.persona_name for msg in messages}
            assert "田中太郎" in persona_names
            assert "佐藤花子" in persona_names
            assert "山田次郎" in persona_names

            # 各ペルソナの特徴が反映されていることを確認
            contents = [msg.content for msg in messages]
            all_content = " ".join(contents)
            assert "営業" in all_content or "顧客" in all_content
            assert "デザイン" in all_content or "視覚" in all_content
            assert "マーケティング" in all_content or "データ" in all_content

    def test_error_handling_in_discussion_flow(self):
        """議論フローでのエラーハンドリングテスト"""
        personas = [self.persona1, self.persona2]
        topic = "テストトピック"

        # 議論でエラーが発生した場合
        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.side_effect = Exception("API エラー")

            with pytest.raises(Exception, match="議論進行中にエラーが発生"):
                self.ai_service.facilitate_discussion(personas, topic)

        # 正常な議論の後、インサイト抽出でエラーが発生した場合
        discussion_response = "[田中太郎]: これは十分に長いテスト発言です。商品について詳細に議論しています。\n[佐藤花子]: 私も同様に長いメッセージで、マーケティング戦略について意見を述べています。"

        with patch.object(self.ai_service, "_retry_with_backoff") as mock_retry:
            mock_retry.side_effect = [
                discussion_response,
                Exception("インサイト抽出エラー"),
            ]

            # 議論は成功
            messages = self.ai_service.facilitate_discussion(personas, topic)
            assert len(messages) == 2

            # インサイト抽出は失敗
            with pytest.raises(Exception, match="インサイト抽出中にエラーが発生"):
                self.ai_service.extract_insights(messages)


if __name__ == "__main__":
    pytest.main([__file__])
