"""
ペルソナの性別（gender）の許容値と表示名マッピング。

gender は固定の選択肢（3値）の自前 enum。
バリデーション・テンプレート表示・生成プロンプトから共通参照する。

国（country）は ISO 3166 ベースで全世界対応するため、外部ライブラリ pycountry を
ラップした src/services/country_service.py で扱う（Models は標準ライブラリのみに保つ）。
"""

from __future__ import annotations

# 性別の許容値（保存値）と日本語表示名のマッピング
GENDER_LABELS: dict[str, str] = {
    "male": "男性",
    "female": "女性",
    "other": "その他",
}

# 性別の許容値の集合
VALID_GENDERS: frozenset[str] = frozenset(GENDER_LABELS.keys())


def gender_label(value: str | None) -> str:
    """性別コードを日本語表示名へ変換する。未知の値はそのまま返す。"""
    if not value:
        return ""
    return GENDER_LABELS.get(value, value)


def sanitize_gender(value: str | None) -> str | None:
    """AI生成された性別値を検証し、許容値でなければ None を返す。

    生成プロンプトは固定値を要求するが、LLMが規格外（例: "M"）を返すことが
    あるため、保存前に正規化する。これによりバッチ生成が1件の不正値で
    全滅することを防ぐ。
    """
    if value in VALID_GENDERS:
        return value
    return None
