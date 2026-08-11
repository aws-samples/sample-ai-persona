"""htmxフォームの配列/マップ形式キーを解析する共有ヘルパー（discussion / interview 両ルーターで使用）。"""

from typing import Optional


def parse_persona_models(form_data) -> Optional[dict[str, str]]:  # type: ignore[no-untyped-def]
    """フォームデータから persona_models[<persona_id>]=<model_id> を解析する。

    FastAPIはこの形式のキーを自動でdict化できないため、Request.form()の生キーを
    手動でパースする。
    """
    prefix = "persona_models["
    persona_models: dict[str, str] = {}
    for key in form_data.keys():
        if not key.startswith(prefix) or not key.endswith("]"):
            continue
        persona_id = key[len(prefix) : -1]
        model_id = form_data.get(key, "").strip()
        if persona_id and model_id:
            persona_models[persona_id] = model_id

    return persona_models if persona_models else None
