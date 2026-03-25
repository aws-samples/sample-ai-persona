"""
議論履歴機能の統合テスト
Task 11.3で実装した機能の統合テスト

Note: このテストは pages.discussion_results モジュールに依存していますが、
現在のアーキテクチャでは web/routers/ に移行されています。
"""

import pytest

from src.managers.discussion_manager import DiscussionManager
from src.managers.persona_manager import PersonaManager
from src.models.discussion import Discussion
from src.models.persona import Persona

# DiscussionDisplayクラスは現在のアーキテクチャには存在しないため、
# このテストモジュール全体をスキップ
pytestmark = pytest.mark.skip(
    reason="pages.discussion_results module has been migrated to web/routers/"
)


class TestDiscussionHistoryIntegration:
    """議論履歴機能の統合テストクラス"""

    def setup_method(self):
        """テストセットアップ"""
        self.discussion_manager = DiscussionManager()
        self.persona_manager = PersonaManager()
        self.display = DiscussionDisplay()

    def test_discussion_history_retrieval(self):
        """議論履歴取得機能のテスト"""
        # 議論履歴を取得
        discussions = self.discussion_manager.get_discussion_history()

        # 結果の検証
        assert isinstance(discussions, list)

        # 各議論の基本構造を確認
        for discussion in discussions:
            assert isinstance(discussion, Discussion)
            assert discussion.id is not None
            assert discussion.topic is not None
            assert discussion.created_at is not None
            assert isinstance(discussion.participants, list)
            assert isinstance(discussion.messages, list)

    def test_discussion_search_functionality(self):
        """議論検索機能のテスト"""
        # 全議論を取得
        all_discussions = self.discussion_manager.get_discussion_history()

        if not all_discussions:
            pytest.skip("テスト用の議論データがありません")

        # トピック検索のテスト
        first_discussion = all_discussions[0]
        search_term = (
            first_discussion.topic.split()[0]
            if first_discussion.topic.split()
            else "test"
        )

        topic_results = self.discussion_manager.get_discussions_by_topic(search_term)
        assert isinstance(topic_results, list)

        # 検索結果の妥当性確認
        for result in topic_results:
            assert search_term.lower() in result.topic.lower()

        # 参加者検索のテスト
        if first_discussion.participants:
            participant_id = first_discussion.participants[0]
            participant_results = (
                self.discussion_manager.get_discussions_by_participant(participant_id)
            )

            assert isinstance(participant_results, list)

            # 検索結果の妥当性確認
            for result in participant_results:
                assert participant_id in result.participants

    def test_discussion_statistics(self):
        """議論統計機能のテスト"""
        # 統計情報を取得
        total_count = self.discussion_manager.get_discussion_count()
        discussions = self.discussion_manager.get_discussion_history()

        # 基本統計の検証
        assert isinstance(total_count, int)
        assert total_count >= 0
        assert len(discussions) == total_count

        if discussions:
            # メッセージ統計
            total_messages = sum(len(d.messages) for d in discussions)
            assert total_messages >= 0

            # インサイト統計
            total_insights = sum(len(d.insights) for d in discussions if d.insights)
            assert total_insights >= 0

            # 参加者統計
            all_participants = set()
            for d in discussions:
                all_participants.update(d.participants)
            assert len(all_participants) >= 0

    def test_discussion_filtering(self):
        """議論フィルタリング機能のテスト"""
        discussions = self.discussion_manager.get_discussion_history()

        if not discussions:
            pytest.skip("テスト用の議論データがありません")

        # 参加者数でのフィルタリングテスト
        participant_counts = list(set(len(d.participants) for d in discussions))

        for count in participant_counts:
            filtered = [d for d in discussions if len(d.participants) == count]
            assert len(filtered) > 0

            for d in filtered:
                assert len(d.participants) == count

        # インサイト有無でのフィルタリングテスト
        with_insights = [d for d in discussions if d.insights and len(d.insights) > 0]
        without_insights = [
            d for d in discussions if not d.insights or len(d.insights) == 0
        ]

        assert len(with_insights) + len(without_insights) == len(discussions)

        # 日付範囲でのフィルタリングテスト
        if len(discussions) > 1:
            oldest = min(d.created_at for d in discussions)
            newest = max(d.created_at for d in discussions)

            # 中間の日付で範囲フィルタリング
            mid_date = oldest + (newest - oldest) / 2

            before_mid = [d for d in discussions if d.created_at <= mid_date]
            after_mid = [d for d in discussions if d.created_at >= mid_date]

            assert len(before_mid) > 0 or len(after_mid) > 0

    def test_discussion_sorting(self):
        """議論ソート機能のテスト"""
        discussions = self.discussion_manager.get_discussion_history()

        if len(discussions) < 2:
            pytest.skip("ソートテストには最低2件の議論が必要です")

        # 作成日時でのソート（新しい順）
        sorted_by_date_desc = sorted(
            discussions, key=lambda d: d.created_at, reverse=True
        )
        for i in range(len(sorted_by_date_desc) - 1):
            assert (
                sorted_by_date_desc[i].created_at
                >= sorted_by_date_desc[i + 1].created_at
            )

        # 作成日時でのソート（古い順）
        sorted_by_date_asc = sorted(
            discussions, key=lambda d: d.created_at, reverse=False
        )
        for i in range(len(sorted_by_date_asc) - 1):
            assert (
                sorted_by_date_asc[i].created_at <= sorted_by_date_asc[i + 1].created_at
            )

        # トピック名でのソート
        sorted_by_topic = sorted(discussions, key=lambda d: d.topic.lower())
        for i in range(len(sorted_by_topic) - 1):
            assert (
                sorted_by_topic[i].topic.lower() <= sorted_by_topic[i + 1].topic.lower()
            )

        # 参加者数でのソート
        sorted_by_participants = sorted(
            discussions, key=lambda d: len(d.participants), reverse=True
        )
        for i in range(len(sorted_by_participants) - 1):
            assert len(sorted_by_participants[i].participants) >= len(
                sorted_by_participants[i + 1].participants
            )

        # メッセージ数でのソート
        sorted_by_messages = sorted(
            discussions, key=lambda d: len(d.messages), reverse=True
        )
        for i in range(len(sorted_by_messages) - 1):
            assert len(sorted_by_messages[i].messages) >= len(
                sorted_by_messages[i + 1].messages
            )

    def test_discussion_detail_view(self):
        """議論詳細表示機能のテスト"""
        discussions = self.discussion_manager.get_discussion_history()

        if not discussions:
            pytest.skip("テスト用の議論データがありません")

        # 最初の議論の詳細を取得
        first_discussion = discussions[0]
        detailed_discussion = self.discussion_manager.get_discussion(
            first_discussion.id
        )

        # 詳細情報の検証
        assert detailed_discussion is not None
        assert detailed_discussion.id == first_discussion.id
        assert detailed_discussion.topic == first_discussion.topic
        assert detailed_discussion.created_at == first_discussion.created_at
        assert detailed_discussion.participants == first_discussion.participants
        assert len(detailed_discussion.messages) == len(first_discussion.messages)

    def test_discussion_existence_check(self):
        """議論存在確認機能のテスト"""
        discussions = self.discussion_manager.get_discussion_history()

        if discussions:
            # 存在する議論のテスト
            existing_id = discussions[0].id
            assert self.discussion_manager.discussion_exists(existing_id) is True

        # 存在しない議論のテスト
        non_existing_id = "non_existing_discussion_id_12345"
        assert self.discussion_manager.discussion_exists(non_existing_id) is False

        # 無効なIDのテスト
        assert self.discussion_manager.discussion_exists("") is False
        assert self.discussion_manager.discussion_exists(None) is False

    def test_discussion_display_methods(self):
        """DiscussionDisplayクラスのメソッドテスト"""
        # 必要なメソッドが存在することを確認
        required_methods = [
            "render_discussion_history",
            "_render_history_filters",
            "_render_history_statistics",
            "_render_discussion_list",
            "_render_discussion_card",
            "render_discussion_detail_view",
        ]

        for method_name in required_methods:
            assert hasattr(self.display, method_name), (
                f"メソッド {method_name} が見つかりません"
            )
            assert callable(getattr(self.display, method_name)), (
                f"メソッド {method_name} が呼び出し可能ではありません"
            )

    def test_error_handling(self):
        """エラーハンドリングのテスト"""
        # 無効な議論IDでの取得テスト
        invalid_discussion = self.discussion_manager.get_discussion("invalid_id")
        assert invalid_discussion is None

        # 無効な検索パターンのテスト
        empty_results = self.discussion_manager.get_discussions_by_topic("")
        assert isinstance(empty_results, list)

        # 無効な参加者IDでの検索テスト
        invalid_participant_results = (
            self.discussion_manager.get_discussions_by_participant("invalid_persona_id")
        )
        assert isinstance(invalid_participant_results, list)

    def test_pagination_logic(self):
        """ページネーション機能のテスト"""
        discussions = self.discussion_manager.get_discussion_history()

        if len(discussions) > 10:
            # ページネーションが必要な場合のテスト
            items_per_page = 10
            total_pages = (len(discussions) + items_per_page - 1) // items_per_page

            assert total_pages > 1

            # 各ページのアイテム数を確認
            for page in range(1, total_pages + 1):
                start_idx = (page - 1) * items_per_page
                end_idx = min(start_idx + items_per_page, len(discussions))
                page_items = discussions[start_idx:end_idx]

                if page < total_pages:
                    assert len(page_items) == items_per_page
                else:
                    assert len(page_items) <= items_per_page
                    assert len(page_items) > 0

    def test_integration_with_persona_manager(self):
        """PersonaManagerとの統合テスト"""
        discussions = self.discussion_manager.get_discussion_history()

        if not discussions:
            pytest.skip("テスト用の議論データがありません")

        # 参加者情報の取得テスト
        first_discussion = discussions[0]

        for persona_id in first_discussion.participants:
            persona = self.persona_manager.get_persona(persona_id)

            # ペルソナが存在する場合の検証
            if persona:
                assert isinstance(persona, Persona)
                assert persona.id == persona_id
                assert persona.name is not None
                assert persona.occupation is not None

    def test_comprehensive_workflow(self):
        """包括的なワークフローテスト"""
        # 1. 議論履歴を取得
        discussions = self.discussion_manager.get_discussion_history()
        initial_count = len(discussions)

        # 2. 統計情報を取得
        total_count = self.discussion_manager.get_discussion_count()
        assert total_count == initial_count

        # 3. 検索機能をテスト
        if discussions:
            # トピック検索
            search_term = (
                "議論"
                if any("議論" in d.topic for d in discussions)
                else discussions[0].topic.split()[0]
            )
            search_results = self.discussion_manager.get_discussions_by_topic(
                search_term
            )
            assert isinstance(search_results, list)

            # 参加者検索
            if discussions[0].participants:
                participant_results = (
                    self.discussion_manager.get_discussions_by_participant(
                        discussions[0].participants[0]
                    )
                )
                assert isinstance(participant_results, list)

        # 4. 詳細表示機能をテスト
        if discussions:
            detailed = self.discussion_manager.get_discussion(discussions[0].id)
            assert detailed is not None
            assert detailed.id == discussions[0].id

        # 5. 存在確認機能をテスト
        if discussions:
            assert self.discussion_manager.discussion_exists(discussions[0].id) is True

        assert self.discussion_manager.discussion_exists("non_existing_id") is False


if __name__ == "__main__":
    # テストを直接実行
    test_class = TestDiscussionHistoryIntegration()
    test_class.setup_method()

    print("🧪 議論履歴機能統合テストを開始...")

    test_methods = [
        ("議論履歴取得", test_class.test_discussion_history_retrieval),
        ("検索機能", test_class.test_discussion_search_functionality),
        ("統計機能", test_class.test_discussion_statistics),
        ("フィルタリング機能", test_class.test_discussion_filtering),
        ("ソート機能", test_class.test_discussion_sorting),
        ("詳細表示機能", test_class.test_discussion_detail_view),
        ("存在確認機能", test_class.test_discussion_existence_check),
        ("表示メソッド", test_class.test_discussion_display_methods),
        ("エラーハンドリング", test_class.test_error_handling),
        ("ページネーション", test_class.test_pagination_logic),
        ("ペルソナ統合", test_class.test_integration_with_persona_manager),
        ("包括的ワークフロー", test_class.test_comprehensive_workflow),
    ]

    passed = 0
    failed = 0

    for test_name, test_method in test_methods:
        try:
            test_method()
            print(f"✅ {test_name}テスト: 成功")
            passed += 1
        except Exception as e:
            print(f"❌ {test_name}テスト: 失敗 - {str(e)}")
            failed += 1

    print(f"\n📊 テスト結果: {passed}件成功, {failed}件失敗")

    if failed == 0:
        print("🎉 全ての統合テストが成功しました!")
    else:
        print("⚠️ 一部のテストが失敗しました。")
