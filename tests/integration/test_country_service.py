"""Integration tests for country_service (country code handling wrapping pycountry)."""

from src.services import country_service


class TestIsValidCountry:
    def test_valid_alpha2(self):
        assert country_service.is_valid_country("JP") is True
        assert country_service.is_valid_country("US") is True
        assert country_service.is_valid_country("BR") is True

    def test_invalid_returns_false(self):
        assert country_service.is_valid_country("XX") is False  # fictitious code
        assert country_service.is_valid_country("JPN") is False  # alpha-3
        assert country_service.is_valid_country("にほ") is False  # non-ASCII
        assert country_service.is_valid_country("") is False
        assert country_service.is_valid_country(None) is False

    def test_lowercase_accepted_by_pycountry(self):
        # pycountry's alpha_2 lookup is case-insensitive, so lowercase is also True.
        # The stored value is normalized to uppercase by sanitize_country.
        assert country_service.is_valid_country("jp") is True


class TestCountryName:
    def test_returns_english_name(self):
        assert country_service.country_name("JP") == "Japan"
        assert country_service.country_name("US") == "United States"

    def test_prefers_common_name(self):
        # common_name is preferred and returns the common name
        assert country_service.country_name("KR") == "South Korea"
        assert country_service.country_name("TW") == "Taiwan"
        assert country_service.country_name("VN") == "Vietnam"

    def test_unknown_and_empty(self):
        assert (
            country_service.country_name("XX") == "XX"
        )  # unknown values are returned as is
        assert country_service.country_name("") == ""
        assert country_service.country_name(None) == ""


class TestCountryChoices:
    def test_returns_code_name_pairs(self):
        choices = country_service.country_choices()
        assert len(choices) > 200  # worldwide, about 249 countries
        # each element is a tuple of (alpha-2, display name)
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
        # returns an immutable tuple to avoid polluting lru_cache
        assert isinstance(country_service.country_choices(), tuple)

    def test_cached_same_object(self):
        # lru_cache returns the same object (avoids rebuilding on every request)
        assert country_service.country_choices() is country_service.country_choices()


class TestSanitizeCountry:
    def test_valid_normalized(self):
        assert country_service.sanitize_country("JP") == "JP"
        assert country_service.sanitize_country("us") == "US"  # uppercased
        assert country_service.sanitize_country(" fr ") == "FR"  # trimmed

    def test_invalid_returns_none(self):
        # a nonstandard value from the LLM becomes None (does not stop batch generation)
        assert country_service.sanitize_country("Japan") is None
        assert country_service.sanitize_country("日本") is None
        assert country_service.sanitize_country("JPN") is None
        assert country_service.sanitize_country("にほ") is None
        assert country_service.sanitize_country("") is None
        assert country_service.sanitize_country(None) is None
