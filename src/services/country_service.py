"""
CountryService
ISO 3166-1 の国コード（alpha-2）の検証・表示名取得を担当するサービス。

pycountry（ISO 3166公式データ）をラップし、全世界の国コードに対応する。
国名は英語表記で返す（common_name があればそれを優先し、South Korea / Taiwan
などの通称を返す。なければ ISO 正式名 name）。

都市（city）は pycountry が対応しないため本サービスの対象外（自由文字列のまま）。
性別（gender）は固定3値の自前 enum（src/models/demographics.py）で扱う。
"""

from __future__ import annotations

from functools import lru_cache

import pycountry


def is_valid_country(code: str | None) -> bool:
    """ISO 3166-1 alpha-2 として実在する国コードか判定する。

    架空コード（XX）、alpha-3（JPN）、非ASCIIなどは False を返す。
    """
    if not code:
        return False
    return pycountry.countries.get(alpha_2=code) is not None


def country_name(code: str | None) -> str:
    """国コードを英語の表示名へ変換する。

    common_name（通称）があればそれを優先し、なければ ISO 正式名 name を返す。
    例: KR -> "South Korea", TW -> "Taiwan", JP -> "Japan"。
    未知のコードはそのまま返し、None/空文字は空文字を返す。
    """
    if not code:
        return ""
    country = pycountry.countries.get(alpha_2=code)
    if country is None:
        return code
    return getattr(country, "common_name", None) or country.name


@lru_cache(maxsize=1)
def country_choices() -> tuple[tuple[str, str], ...]:
    """フィルタUI・編集フォーム用の (alpha-2コード, 表示名) 一覧を返す。

    表示名は country_name と同じ common_name 優先ロジック。表示名でソートする。
    国コード一覧は不変なため lru_cache でリクエスト毎の再生成を避ける。
    キャッシュ汚染を防ぐためイミュータブルな tuple を返す。
    """
    choices = [
        (country.alpha_2, getattr(country, "common_name", None) or country.name)
        for country in pycountry.countries
    ]
    return tuple(sorted(choices, key=lambda item: item[1]))


def sanitize_country(code: str | None) -> str | None:
    """AI生成・入力された国コードを正規化し、実在しなければ None を返す。

    trim + 大文字化したうえで is_valid_country を通らなければ None。
    LLMが "Japan" / "日本" / "JPN" のような規格外を返しても、1件の不正値で
    バッチ生成全体を失敗させないための正規化。
    """
    if not code or not code.strip():
        return None
    normalized = code.strip().upper()
    return normalized if is_valid_country(normalized) else None
