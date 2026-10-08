"""Conversation labels and the bounded three-part Text footer."""

from prompt_toolkit.formatted_text import StyleAndTextTuples

from toolang.cli.common.execution_progress.formatting import display_width, truncate
from toolang.teaming.schemas import Conversation, target

from .rendering import display_text


def conversation_name(info: Conversation, viewer: str) -> str:
    if info.kind == "group":
        return display_text(info.label)
    others = sorted(member for member in info.members if member != viewer)
    return "dm_" + "_".join(target(member).name for member in others)


def status_line(
    left: str, center: str, right: str, *, width: int, warning: bool
) -> StyleAndTextTuples:
    width = max(1, width)
    margin = min(2, (width - 1) // 2)
    inner = width - 2 * margin
    left, center, right = (
        " ".join(display_text(value).split()) for value in (left, center, right)
    )
    if display_width(right) + 2 > inner:
        right = ""
    right_width = display_width(right)
    side_width = max(display_width(left), right_width)
    center = truncate(center, inner - 2 * (side_width + 1))
    left = truncate(left, inner - right_width - bool(right))
    left_width = display_width(left)
    if center:
        center_width = display_width(center)
        center_start = (inner - center_width) // 2
        before = center_start - left_width
        after = inner - center_start - center_width - right_width
    else:
        before, after = inner - left_width - right_width, 0
    return [
        ("", " " * margin),
        ("class:status.warning" if warning else "class:status", left),
        ("", " " * before),
        ("class:status", center),
        ("", " " * after),
        ("class:status", right),
        ("", " " * margin),
    ]
