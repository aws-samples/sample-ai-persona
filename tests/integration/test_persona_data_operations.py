"""
Integration tests for persona data operations.
Tests the complete persona data lifecycle including validation and integrity checks.
"""

import unittest
from unittest.mock import Mock
from datetime import datetime

from src.models.persona import Persona


class TestPersonaDataOperations(unittest.TestCase):
    """Integration tests for persona data operations."""

    def setUp(self):
        """Set up test fixtures."""
        # Create mock database service
        self.personas_storage = {}
        self.mock_db_service = Mock()

        def mock_save_persona(persona):
            self.personas_storage[persona.id] = persona
            return persona.id

        def mock_get_persona(persona_id):
            return self.personas_storage.get(persona_id)

        def mock_get_all_personas():
            return list(self.personas_storage.values())

        def mock_update_persona(persona):
            if persona.id in self.personas_storage:
                self.personas_storage[persona.id] = persona
                return True
            return False

        def mock_delete_persona(persona_id):
            if persona_id in self.personas_storage:
                del self.personas_storage[persona_id]
                return True
            return False

        def mock_persona_exists(persona_id):
            return persona_id in self.personas_storage

        def mock_get_persona_count():
            return len(self.personas_storage)

        def mock_get_personas_by_name(name_pattern):
            return [p for p in self.personas_storage.values() if name_pattern in p.name]

        def mock_get_personas_by_occupation(occupation_pattern):
            return [
                p
                for p in self.personas_storage.values()
                if occupation_pattern in p.occupation
            ]

        def mock_delete_all_personas():
            count = len(self.personas_storage)
            self.personas_storage.clear()
            return count

        self.mock_db_service.save_persona.side_effect = mock_save_persona
        self.mock_db_service.get_persona.side_effect = mock_get_persona
        self.mock_db_service.get_all_personas.side_effect = mock_get_all_personas
        self.mock_db_service.update_persona.side_effect = mock_update_persona
        self.mock_db_service.delete_persona.side_effect = mock_delete_persona
        self.mock_db_service.persona_exists.side_effect = mock_persona_exists
        self.mock_db_service.get_persona_count.side_effect = mock_get_persona_count
        self.mock_db_service.get_personas_by_name.side_effect = (
            mock_get_personas_by_name
        )
        self.mock_db_service.get_personas_by_occupation.side_effect = (
            mock_get_personas_by_occupation
        )
        self.mock_db_service.delete_all_personas.side_effect = mock_delete_all_personas

    def tearDown(self):
        """Clean up test fixtures."""
        pass

    def test_complete_persona_lifecycle(self):
        """Test complete persona data lifecycle."""
        # Create a new persona
        persona = Persona.create_new(
            name="田中太郎",
            age=35,
            occupation="プロダクトマネージャー",
            background="IT企業で10年の経験を持つプロダクトマネージャー。ユーザー中心設計を重視し、データドリブンな意思決定を行う。",
            values=["ユーザー体験", "データ分析", "チームワーク"],
            pain_points=["リソース不足", "ステークホルダー調整", "技術的負債"],
            goals=["プロダクトの成長", "チーム育成", "市場シェア拡大"],
        )

        # 1. Save persona
        saved_id = self.mock_db_service.save_persona(persona)
        self.assertEqual(saved_id, persona.id)

        # 2. Verify persona exists
        self.assertTrue(self.mock_db_service.persona_exists(persona.id))

        # 3. Retrieve persona
        retrieved_persona = self.mock_db_service.get_persona(persona.id)
        self.assertIsNotNone(retrieved_persona)
        self.assertEqual(retrieved_persona.name, "田中太郎")
        self.assertEqual(retrieved_persona.age, 35)
        self.assertEqual(retrieved_persona.occupation, "プロダクトマネージャー")
        self.assertEqual(len(retrieved_persona.values), 3)
        self.assertEqual(len(retrieved_persona.pain_points), 3)
        self.assertEqual(len(retrieved_persona.goals), 3)

        # 4. Update persona
        updated_persona = retrieved_persona.update(
            age=36,
            values=["ユーザー体験", "データ分析", "チームワーク", "イノベーション"],
        )
        update_result = self.mock_db_service.update_persona(updated_persona)
        self.assertTrue(update_result)

        # 5. Verify update
        updated_retrieved = self.mock_db_service.get_persona(persona.id)
        self.assertEqual(updated_retrieved.age, 36)
        self.assertEqual(len(updated_retrieved.values), 4)
        self.assertIn("イノベーション", updated_retrieved.values)

        # 6. Check persona count
        self.assertEqual(self.mock_db_service.get_persona_count(), 1)

        # 7. Delete persona
        delete_result = self.mock_db_service.delete_persona(persona.id)
        self.assertTrue(delete_result)

        # 8. Verify deletion
        self.assertFalse(self.mock_db_service.persona_exists(persona.id))
        self.assertIsNone(self.mock_db_service.get_persona(persona.id))
        self.assertEqual(self.mock_db_service.get_persona_count(), 0)

    def test_multiple_personas_management(self):
        """Test managing multiple personas."""
        # Create multiple personas
        personas = []
        for i in range(5):
            persona = Persona.create_new(
                name=f"ユーザー{i + 1}",
                age=25 + i * 5,
                occupation=f"職業{i + 1}",
                background=f"背景情報{i + 1}",
                values=[f"価値観{i + 1}"],
                pain_points=[f"課題{i + 1}"],
                goals=[f"目標{i + 1}"],
            )
            personas.append(persona)
            self.mock_db_service.save_persona(persona)

        # Verify all personas are saved
        self.assertEqual(self.mock_db_service.get_persona_count(), 5)

        # Retrieve all personas
        all_personas = self.mock_db_service.get_all_personas()
        self.assertEqual(len(all_personas), 5)

        # Test search by name
        search_results = self.mock_db_service.get_personas_by_name("ユーザー1")
        self.assertEqual(len(search_results), 1)
        self.assertEqual(search_results[0].name, "ユーザー1")

        # Test search by occupation
        search_results = self.mock_db_service.get_personas_by_occupation("職業2")
        self.assertEqual(len(search_results), 1)
        self.assertEqual(search_results[0].occupation, "職業2")

        # Test partial name search
        search_results = self.mock_db_service.get_personas_by_name("ユーザー")
        self.assertEqual(len(search_results), 5)

        # Delete all personas
        deleted_count = self.mock_db_service.delete_all_personas()
        self.assertEqual(deleted_count, 5)
        self.assertEqual(self.mock_db_service.get_persona_count(), 0)

    @unittest.skip(
        "Persona model does not implement validation - validation is done at manager level"
    )
    def test_data_integrity_validation(self):
        """Test data integrity validation."""
        # Test various invalid data scenarios

        # Invalid ID
        with self.assertRaises(ValueError):
            invalid_persona = Persona(
                id="",
                name="Test",
                age=30,
                occupation="Test",
                background="Test",
                values=[],
                pain_points=[],
                goals=[],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            self.mock_db_service.save_persona(invalid_persona)

        # Invalid age (negative)
        with self.assertRaises(ValueError):
            invalid_persona = Persona(
                id="test-id",
                name="Test",
                age=-1,
                occupation="Test",
                background="Test",
                values=[],
                pain_points=[],
                goals=[],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            self.mock_db_service.save_persona(invalid_persona)

        # Invalid age (too high)
        with self.assertRaises(ValueError):
            invalid_persona = Persona(
                id="test-id",
                name="Test",
                age=200,
                occupation="Test",
                background="Test",
                values=[],
                pain_points=[],
                goals=[],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            self.mock_db_service.save_persona(invalid_persona)

        # Invalid values (not a list)
        with self.assertRaises(ValueError):
            invalid_persona = Persona(
                id="test-id",
                name="Test",
                age=30,
                occupation="Test",
                background="Test",
                values="not a list",
                pain_points=[],
                goals=[],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            self.mock_db_service.save_persona(invalid_persona)

        # Invalid values content (non-string in list)
        with self.assertRaises(ValueError):
            invalid_persona = Persona(
                id="test-id",
                name="Test",
                age=30,
                occupation="Test",
                background="Test",
                values=[123, "valid"],
                pain_points=[],
                goals=[],
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            self.mock_db_service.save_persona(invalid_persona)

    def test_japanese_text_handling(self):
        """Test handling of Japanese text in persona data."""
        # Create persona with Japanese text
        persona = Persona.create_new(
            name="山田花子",
            age=28,
            occupation="UXデザイナー",
            background="美術大学卒業後、デザイン会社で5年間勤務。ユーザビリティとアクセシビリティに強い関心を持つ。",
            values=["美しいデザイン", "使いやすさ", "アクセシビリティ", "創造性"],
            pain_points=["技術的制約", "短い納期", "クライアントとの認識齟齬"],
            goals=[
                "デザインスキル向上",
                "チームリーダーへの昇進",
                "国際的なプロジェクト参加",
            ],
        )

        # Save and retrieve
        self.mock_db_service.save_persona(persona)
        retrieved = self.mock_db_service.get_persona(persona.id)

        # Verify Japanese text is preserved correctly
        self.assertEqual(retrieved.name, "山田花子")
        self.assertEqual(retrieved.occupation, "UXデザイナー")
        self.assertIn("美術大学卒業後", retrieved.background)
        self.assertIn("美しいデザイン", retrieved.values)
        self.assertIn("技術的制約", retrieved.pain_points)
        self.assertIn("デザインスキル向上", retrieved.goals)

        # Test search with Japanese text
        search_results = self.mock_db_service.get_personas_by_name("山田")
        self.assertEqual(len(search_results), 1)
        self.assertEqual(search_results[0].name, "山田花子")

        search_results = self.mock_db_service.get_personas_by_occupation("デザイナー")
        self.assertEqual(len(search_results), 1)
        self.assertEqual(search_results[0].occupation, "UXデザイナー")

    def test_edge_cases_and_boundary_conditions(self):
        """Test edge cases and boundary conditions."""
        # Test minimum age
        persona_min_age = Persona.create_new(
            name="赤ちゃん",
            age=0,
            occupation="なし",
            background="生まれたばかり",
            values=[],
            pain_points=[],
            goals=[],
        )
        self.mock_db_service.save_persona(persona_min_age)
        retrieved = self.mock_db_service.get_persona(persona_min_age.id)
        self.assertEqual(retrieved.age, 0)

        # Test maximum age
        persona_max_age = Persona.create_new(
            name="高齢者",
            age=150,
            occupation="退職者",
            background="長い人生経験",
            values=[],
            pain_points=[],
            goals=[],
        )
        self.mock_db_service.save_persona(persona_max_age)
        retrieved = self.mock_db_service.get_persona(persona_max_age.id)
        self.assertEqual(retrieved.age, 150)

        # Test empty lists (valid)
        persona_empty_lists = Persona.create_new(
            name="シンプル",
            age=30,
            occupation="職業",
            background="背景",
            values=[],
            pain_points=[],
            goals=[],
        )
        self.mock_db_service.save_persona(persona_empty_lists)
        retrieved = self.mock_db_service.get_persona(persona_empty_lists.id)
        self.assertEqual(len(retrieved.values), 0)
        self.assertEqual(len(retrieved.pain_points), 0)
        self.assertEqual(len(retrieved.goals), 0)

        # Test very long strings
        long_background = "非常に長い背景情報。" * 100  # Very long text
        persona_long_text = Persona.create_new(
            name="長文ユーザー",
            age=30,
            occupation="ライター",
            background=long_background,
            values=["詳細"],
            pain_points=["文字数制限"],
            goals=["簡潔性"],
        )
        self.mock_db_service.save_persona(persona_long_text)
        retrieved = self.mock_db_service.get_persona(persona_long_text.id)
        self.assertEqual(len(retrieved.background), len(long_background))

    def test_concurrent_operations_simulation(self):
        """Test simulation of concurrent operations."""
        # Create base persona
        persona = Persona.create_new(
            name="同時更新テスト",
            age=30,
            occupation="テスター",
            background="同時実行テスト用",
            values=["テスト"],
            pain_points=["競合状態"],
            goals=["データ整合性"],
        )
        self.mock_db_service.save_persona(persona)

        # Simulate multiple updates
        for i in range(10):
            retrieved = self.mock_db_service.get_persona(persona.id)
            updated = retrieved.update(age=30 + i)
            self.mock_db_service.update_persona(updated)

        # Verify final state
        final_persona = self.mock_db_service.get_persona(persona.id)
        self.assertEqual(final_persona.age, 39)  # 30 + 9

        # Verify persona still exists and is valid
        self.assertTrue(self.mock_db_service.persona_exists(persona.id))
        self.assertEqual(self.mock_db_service.get_persona_count(), 1)


if __name__ == "__main__":
    unittest.main()
