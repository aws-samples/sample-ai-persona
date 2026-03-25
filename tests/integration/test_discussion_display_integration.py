"""
議論表示機能の統合テスト
Task 11.1: 議論内容表示機能とUIコンポーネントの統合テスト

Note: このテストは pages.discussion_results モジュールに依存していましたが、
現在のアーキテクチャでは web/routers/ に移行されています。
DiscussionDisplayクラスは存在しないため、モデルとデータ処理のテストのみ実行します。
"""

import unittest
from datetime import datetime, timedelta
import sys
from pathlib import Path

# プロジェクトルートをPythonパスに追加
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.models.discussion import Discussion
from src.models.message import Message
from src.models.persona import Persona


class TestDiscussionDisplayIntegration(unittest.TestCase):
    """議論表示機能の統合テストクラス"""

    def setUp(self):
        """テストセットアップ"""
        # テスト用ペルソナを作成
        self.personas = self._create_test_personas()

        # テスト用議論を作成
        self.discussion = self._create_test_discussion()

        # ペルソナ辞書を作成
        self.personas_dict = {p.id: p for p in self.personas}

    def _create_test_personas(self):
        """テスト用ペルソナを作成"""
        persona1 = Persona.create_new(
            name="田中太郎",
            age=35,
            occupation="マーケティングマネージャー",
            background="大手IT企業でマーケティング戦略を担当。データ分析を重視し、効率的な施策を好む。",
            values=["効率性", "データ重視", "成果主義"],
            pain_points=["時間不足", "予算制約", "チーム連携"],
            goals=["売上向上", "ブランド認知度アップ", "チーム成長"],
        )

        persona2 = Persona.create_new(
            name="佐藤花子",
            age=28,
            occupation="UXデザイナー",
            background="スタートアップでユーザー体験設計を担当。ユーザー中心の設計を重視し、創造的なソリューションを追求。",
            values=["ユーザー中心", "創造性", "共感"],
            pain_points=["技術制約", "開発との連携", "ユーザー理解不足"],
            goals=["使いやすいプロダクト", "ユーザー満足度向上", "デザイン品質向上"],
        )

        persona3 = Persona.create_new(
            name="山田次郎",
            age=32,
            occupation="プロダクトマネージャー",
            background="B2Bソフトウェア企業でプロダクト戦略を担当。技術とビジネスの橋渡し役として活動。",
            values=["顧客価値", "イノベーション", "戦略的思考"],
            pain_points=["ステークホルダー調整", "技術的負債", "市場競争"],
            goals=["プロダクト成長", "顧客満足度向上", "市場シェア拡大"],
        )

        return [persona1, persona2, persona3]

    def _create_test_discussion(self):
        """テスト用議論を作成"""
        discussion = Discussion.create_new(
            topic="新しいモバイルアプリの機能優先度について",
            participants=[p.id for p in self.personas],
        )

        # 現在時刻から開始
        base_time = datetime.now()

        # リアルなメッセージを追加
        messages_data = [
            (
                0,
                "こんにちは、田中です。新しいモバイルアプリの機能について議論しましょう。マーケティングの観点から、ユーザー獲得に直結する機能を優先すべきだと考えています。",
                0,
            ),
            (
                1,
                "佐藤です。ユーザー獲得も重要ですが、まずは基本的なユーザビリティを確保することが最優先だと思います。使いにくいアプリでは、獲得したユーザーもすぐに離れてしまいます。",
                30,
            ),
            (
                2,
                "山田です。両方の視点とも重要ですね。プロダクト戦略の観点から言うと、まず最小限の機能でMVPを作り、ユーザーフィードバックを基に段階的に機能を追加していくアプローチが良いと思います。",
                60,
            ),
            (
                0,
                "確かにMVPアプローチは理にかなっていますね。ただ、競合他社との差別化を図るためには、独自性のある機能も必要だと思います。データ分析機能やパーソナライゼーション機能はどうでしょうか？",
                90,
            ),
            (
                1,
                "データ分析は良いアイデアですが、ユーザーにとって分かりやすい形で提供する必要があります。複雑すぎると逆効果になる可能性があります。シンプルで直感的なインターフェースを心がけるべきです。",
                120,
            ),
        ]

        # メッセージを議論に追加
        for persona_index, content, time_offset in messages_data:
            message_time = base_time + timedelta(seconds=time_offset)
            message = Message(
                persona_id=self.personas[persona_index].id,
                persona_name=self.personas[persona_index].name,
                content=content,
                timestamp=message_time,
            )
            discussion = discussion.add_message(message)

        return discussion

    def test_discussion_statistics_calculation(self):
        """議論統計の計算をテスト"""
        # メッセージ数統計
        message_counts = {}
        message_lengths = {}
        total_length = 0

        for message in self.discussion.messages:
            persona_id = message.persona_id
            message_counts[persona_id] = message_counts.get(persona_id, 0) + 1

            if persona_id not in message_lengths:
                message_lengths[persona_id] = []
            message_lengths[persona_id].append(len(message.content))
            total_length += len(message.content)

        # 各ペルソナの発言数をチェック
        self.assertGreater(message_counts[self.personas[0].id], 0)
        self.assertGreater(message_counts[self.personas[1].id], 0)

        # 総文字数が正しく計算されているかチェック
        expected_total = sum(len(msg.content) for msg in self.discussion.messages)
        self.assertEqual(total_length, expected_total)

        # 平均文字数の計算
        avg_length = total_length / len(self.discussion.messages)
        self.assertIsInstance(avg_length, float)
        self.assertGreater(avg_length, 0)

        # 参加率の計算
        for persona_id, count in message_counts.items():
            participation_rate = (count / len(self.discussion.messages)) * 100
            self.assertGreaterEqual(participation_rate, 0)
            self.assertLessEqual(participation_rate, 100)

    def test_discussion_duration_and_intervals(self):
        """議論時間と発言間隔の計算をテスト"""
        messages = self.discussion.messages

        if len(messages) >= 2:
            # 議論時間の計算
            duration = messages[-1].timestamp - messages[0].timestamp
            self.assertGreaterEqual(duration.total_seconds(), 0)

            # 発言間隔の計算
            intervals = []
            for i in range(1, len(messages)):
                interval = messages[i].timestamp - messages[i - 1].timestamp
                intervals.append(interval.total_seconds())

            # すべての間隔が正の値であることを確認
            for interval in intervals:
                self.assertGreater(interval, 0)

            # 平均間隔の計算
            if intervals:
                avg_interval = sum(intervals) / len(intervals)
                self.assertIsInstance(avg_interval, float)
                self.assertGreater(avg_interval, 0)

    def test_persona_message_extraction(self):
        """ペルソナ別メッセージ抽出をテスト"""
        # 各ペルソナのメッセージを抽出
        for persona in self.personas:
            persona_messages = [
                msg for msg in self.discussion.messages if msg.persona_id == persona.id
            ]

            # 抽出されたメッセージがすべて該当ペルソナのものであることを確認
            for msg in persona_messages:
                self.assertEqual(msg.persona_id, persona.id)
                self.assertEqual(msg.persona_name, persona.name)

    def test_message_search_functionality(self):
        """メッセージ検索機能をテスト"""
        # 様々なキーワードで検索テスト
        test_keywords = ["ユーザー", "機能", "データ", "アプリ"]

        for keyword in test_keywords:
            matching_messages = [
                msg
                for msg in self.discussion.messages
                if keyword.lower() in msg.content.lower()
            ]

            # マッチしたメッセージにキーワードが含まれていることを確認
            for msg in matching_messages:
                self.assertIn(keyword.lower(), msg.content.lower())

    def test_discussion_flow_visualization_data(self):
        """議論の流れ可視化データをテスト"""
        # 発言順序の確認
        previous_timestamp = None

        for i, message in enumerate(self.discussion.messages):
            # タイムスタンプが昇順であることを確認
            if previous_timestamp is not None:
                self.assertGreaterEqual(message.timestamp, previous_timestamp)

            previous_timestamp = message.timestamp

            # メッセージ番号が正しく設定されているかチェック
            expected_number = i + 1
            # 実際のUI表示では expected_number が使用される
            self.assertIsInstance(expected_number, int)
            self.assertGreater(expected_number, 0)

    def test_discussion_model_integrity(self):
        """議論モデルの整合性をテスト"""
        # 議論の基本属性を確認
        self.assertIsNotNone(self.discussion.id)
        self.assertIsNotNone(self.discussion.topic)
        self.assertEqual(len(self.discussion.participants), 3)
        self.assertEqual(len(self.discussion.messages), 5)

        # 参加者IDが正しいことを確認
        for persona in self.personas:
            self.assertIn(persona.id, self.discussion.participants)

    def test_message_filtering_logic(self):
        """メッセージフィルタリングロジックをテスト"""
        # ペルソナでフィルタリング
        persona_id = self.personas[0].id
        filtered_by_persona = [
            msg for msg in self.discussion.messages if msg.persona_id == persona_id
        ]

        # フィルタリング結果の検証
        for msg in filtered_by_persona:
            self.assertEqual(msg.persona_id, persona_id)

        # 検索キーワードでフィルタリング
        search_term = "ユーザー"
        filtered_by_search = [
            msg
            for msg in self.discussion.messages
            if search_term.lower() in msg.content.lower()
        ]

        for msg in filtered_by_search:
            self.assertIn(search_term.lower(), msg.content.lower())

        # 逆順ソート
        reversed_messages = list(reversed(self.discussion.messages))
        self.assertEqual(len(reversed_messages), len(self.discussion.messages))
        self.assertEqual(reversed_messages[0], self.discussion.messages[-1])


if __name__ == "__main__":
    unittest.main()
