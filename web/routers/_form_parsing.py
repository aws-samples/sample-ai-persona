"""Shared helper that parses array/map-style keys of htmx forms (used by both the discussion and interview routers)."""

from typing import Optional


def parse_persona_models(form_data) -> Optional[dict[str, str]]:  # type: ignore[no-untyped-def]
    """Parse persona_models[<persona_id>]=<model_id> from form data.

    FastAPI cannot turn keys of this form into a dict automatically, so the raw keys of
    Request.form() are parsed by hand.
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
