from __future__ import annotations

from typing import Any

from toolang.errors import ToolangError

from .build import PromptBuild

__all__ = ["execute_prompt_build"]


def execute_prompt_build(build: PromptBuild) -> str:
    openai_client = _create_openai_client()
    response = _create_response(
        openai_client,
        model=build.model,
        messages=build.messages,
    )
    return _coerce_response_text(response)


def _create_openai_client() -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ToolangError(
            "The 'openai' package is not installed. Run 'uv add openai' to enable toolang invoke."
        ) from exc
    return OpenAI()


def _coerce_response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    collected: list[str] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []):
            content_type = getattr(content, "type", None)
            if content_type in {"output_text", "text"} and getattr(content, "text", None):
                collected.append(content.text)

    if collected:
        return "".join(collected)
    raise ToolangError("Model response did not contain text output.")


def _create_response(
    openai_client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
) -> Any:
    return openai_client.responses.create(
        model=model,
        input=messages,
    )
