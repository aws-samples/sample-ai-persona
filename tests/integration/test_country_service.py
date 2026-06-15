"""
country_service の統合テスト（pycountry をラップした国コード処理）。
"""

from src.services import country_service


class TestIsValidCountry:
    def test_valid_alpha2(self):
        assert country_service.is_valid_country("JP") is True
        assert country_service.is_valid_country("US") is True
        assert country_service.is_valid_country("BR") is True

    def test_invalid_returns_false(self):
        assert country_service.is_valid_country("XX") is False  # 架空コード
        assert country_service.is_valid_country("JPN") is False  # alpha-3
        assert country_service.is_valid_country("にほ") is False  # 非ASCII
        assert country_service.is_valid_country("") is False
        assert country_service.is_valid_country(None) is False

    def test_lowercase_accepted_by_pycountry(self):
        # pycountry の alpha_2 検索は大小文字を区別しないため小文字も True。
        # 保存値は sanitize_country で大文字に正規化される。
        assert country_service.is_valid_country("jp") is True


class TestCountryName:
    def test_returns_english_name(self):
        assert country_service.country_name("JP") == "Japan"
        assert country_service.country_name("US") == "United States"

    def test_prefers_common_name(self):
        # common_name 優先で通称を返す
        assert country_service.country_name("KR") == "South Korea"
        assert country_service.country_name("TW") == "Taiwan"
        assert country_service.country_name("VN") == "Vietnam"

    def test_unknown_and_empty(self):
        assert country_service.country_name("XX") == "XX"  # 未知はそのまま
        assert country_service.country_name("") == ""
        assert country_service.country_name(None) == ""


class TestCountryChoices:
    def test_returns_code_name_pairs(self):
        choices = country_service.country_choices()
        assert len(choices) > 200  # 全世界 約249カ国
        # 各要素は (alpha-2, 表示名) のタプル
        for code, name in choices[:5]:
            assert isinstance(code, str) and len(code) == 2
            assert isinstance(name, str) and name

    def test_sorted_by_name(self):
        names = [name for _, name in country_service.country_choices()]
        assert names == sorted(names)

    def test_contains_japan(self):
        codes = {code for code, _ in country_service.country_choices()}
        assert "JP" in codes

    def test_returns_immutable_tuple(self):
        # lru_cache 汚染防止のためイミュータブルな tuple を返す
        assert isinstance(country_service.country_choices(), tuple)

    def test_cached_same_object(self):
        # lru_cache により同一オブジェクトが返る（リクエスト毎の再生成を回避）
        assert country_service.country_choices() is country_service.country_choices()


class TestSanitizeCountry:
    def test_valid_normalized(self):
        assert country_service.sanitize_country("JP") == "JP"
        assert country_service.sanitize_country("us") == "US"  # 大文字化
        assert country_service.sanitize_country(" fr ") == "FR"  # トリム

    def test_invalid_returns_none(self):
        # LLMが規格外を返しても None 化（バッチ生成を止めない）
        assert country_service.sanitize_country("Japan") is None
        assert country_service.sanitize_country("日本") is None
        assert country_service.sanitize_country("JPN") is None
        assert country_service.sanitize_country("にほ") is None
        assert country_service.sanitize_country("") is None
        assert country_service.sanitize_country(None) is None
