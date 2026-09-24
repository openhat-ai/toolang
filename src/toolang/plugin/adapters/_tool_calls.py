"""Parse JSON function arguments shared by compatible model transports."""

import json
from typing import Any

from toolang.base.errors import ModelResponseError


def parse_tool_arguments(raw: object) -> dict[str, Any]:
    """Accept an empty no-argument call without repairing malformed JSON."""

    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    if raw is None or isinstance(raw, str) and not raw.strip():
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ModelResponseError(
            "tool call arguments were not valid JSON", kind="invalid_json"
        ) from exc
    if not isinstance(parsed, dict):
        raise ModelResponseError(
            "tool call arguments must decode to a JSON object",
            kind="non_object_arguments",
        )
    return dict(parsed)
