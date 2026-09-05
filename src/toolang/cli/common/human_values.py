"""Shared deterministic human rendering for durable values."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
from typing import cast

from rich.console import RenderableType
from rich.text import Text

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.lang.types import Array

from .execution_progress import ProgressBlock, ProgressRow
from .execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from .execution_progress.rich_rendering import progress_block_renderable


_PART_TYPES = (
    TextPart,
    ImagePart,
    AudioPart,
    DocumentPart,
    ToolCallPart,
    ToolResultPart,
)
_RichRenderer = Callable[[object], RenderableType | None]
_ScalarRenderer = Callable[[object], str | None]


def parts_response_text(parts: Sequence[Part]) -> str:
    """Return the durable Chat response text or its structured fallback."""

    text = "".join(part.text for part in parts if isinstance(part, TextPart)).strip()
    if text or not parts:
        return text
    return json.dumps(
        [part.to_data() for part in parts],
        ensure_ascii=False,
        indent=2,
    )


def response_renderable(
    text: str,
    *,
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
    prefix: str = "• ",
    code_background: str = "bright_black",
    code_foreground: str | None = "bright_white",
) -> RenderableType | None:
    """Render one finalized Chat-style response without live state."""

    if not text:
        return None
    return progress_block_renderable(
        ProgressBlock(
            "response",
            (
                ProgressRow(
                    text,
                    "normal",
                    format="markdown",
                    prefix=prefix,
                ),
            ),
        ),
        live=False,
        max_width=max_width,
        code_background=code_background,
        code_foreground=code_foreground,
    )


def parts_response_renderable(
    parts: Sequence[Part],
    *,
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
    prefix: str = "• ",
    code_background: str = "bright_black",
    code_foreground: str | None = "bright_white",
) -> RenderableType | None:
    """Render Parts through the same finalized presentation used by Chat."""

    return response_renderable(
        parts_response_text(parts),
        max_width=max_width,
        prefix=prefix,
        code_background=code_background,
        code_foreground=code_foreground,
    )


def human_value_renderable(
    value: object,
    type_name: str,
) -> RenderableType | None:
    """Return a registered Rich renderer for one durable runtime value."""

    renderer = _RICH_RENDERERS.get(type_name)
    return renderer(value) if renderer is not None else None


def human_scalar_text(value: object, type_name: str) -> str | None:
    """Return registered natural scalar text, or defer to generic JSON."""

    renderer = _SCALAR_RENDERERS.get(type_name)
    return renderer(value) if renderer is not None else None


def _parts(value: object, type_name: str) -> tuple[Part, ...] | None:
    if type_name == "Part" and isinstance(value, _PART_TYPES):
        return (value,)
    if type_name != "Part[]" or not isinstance(value, Array | list | tuple):
        return None
    parts = tuple(value)
    return (
        cast(tuple[Part, ...], parts)
        if all(isinstance(part, _PART_TYPES) for part in parts)
        else None
    )


def _part_renderable(value: object, type_name: str) -> RenderableType | None:
    parts = _parts(value, type_name)
    return parts_response_renderable(parts, prefix="") if parts is not None else None


def _text_renderable(value: object) -> RenderableType | None:
    return Text(value) if isinstance(value, str) and "\n" in value else None


def _text_scalar(value: object) -> str | None:
    return value if isinstance(value, str) else None


_RICH_RENDERERS: dict[str, _RichRenderer] = {
    "Part": lambda value: _part_renderable(value, "Part"),
    "Part[]": lambda value: _part_renderable(value, "Part[]"),
    "Text": _text_renderable,
}
_SCALAR_RENDERERS: dict[str, _ScalarRenderer] = {
    "Text": _text_scalar,
}
