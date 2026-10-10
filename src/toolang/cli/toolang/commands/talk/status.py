"""Conversation identity, write access, and the bounded Talk footer."""

from prompt_toolkit.formatted_text import StyleAndTextTuples, fragment_list_to_text

from toolang.cli.common.execution_progress.formatting import display_width, truncate
from toolang.teaming.schemas import Conversation, target

from .rendering import display_text


def conversation_label(info: Conversation, viewer: str) -> str:
    if info.kind == "gc":
        return "#" + display_text(info.label)
    if info.name:
        return "@" + display_text(info.name)
    others = sorted(member for member in info.participants if member != viewer)
    return "@" + ",".join(target(member).name for member in others)


def conversation_status(info: Conversation, viewer: str) -> StyleAndTextTuples:
    label = conversation_label(info, viewer)
    count = f"({len(info.participants)})" if info.kind == "gc" else ""
    return [
        (
            "class:status" if info.allows_sender(viewer) else "class:status dim",
            label[0],
        ),
        ("class:status", label[1:] + count),
    ]


def _truncate_fragments(
    fragments: StyleAndTextTuples, width: int
) -> StyleAndTextTuples:
    if width <= 0:
        return []
    text = fragment_list_to_text(fragments)
    clipped = truncate(text, width)
    if clipped == text:
        return fragments
    remaining = len(clipped[:-1])
    result: StyleAndTextTuples = []
    for fragment in fragments:
        if remaining <= 0:
            break
        style, value = fragment[:2]
        prefix = value[:remaining]
        result.append((style, prefix))
        remaining -= len(prefix)
    result.append(("class:status", "…"))
    return result


def status_line(
    left: StyleAndTextTuples,
    right: str,
    *,
    center: str = "",
    width: int,
) -> StyleAndTextTuples:
    width = max(1, width)
    margin = min(2, (width - 1) // 2)
    inner = width - 2 * margin
    right = truncate(" ".join(display_text(right).split()), inner)
    right_width = display_width(right)
    safe_left: StyleAndTextTuples = [
        (fragment[0], " ".join(display_text(fragment[1]).split())) for fragment in left
    ]
    center = " ".join(display_text(center).split())
    center_width = display_width(center)
    center_start = (width - center_width) // 2
    right_start = width - margin - right_width
    if (
        center
        and center_start >= margin
        and center_start + center_width + bool(right) <= right_start
    ):
        left = _truncate_fragments(safe_left, center_start - margin - 1)
        return [
            ("", " " * margin),
            *left,
            (
                "",
                " "
                * (center_start - margin - display_width(fragment_list_to_text(left))),
            ),
            ("class:status", center),
            ("", " " * (right_start - center_start - center_width)),
            ("class:status", right),
            ("", " " * margin),
        ]
    left = _truncate_fragments(safe_left, inner - right_width - bool(right))
    gap = inner - display_width(fragment_list_to_text(left)) - right_width
    return [
        ("", " " * margin),
        *left,
        ("", " " * gap),
        ("class:status", right),
        ("", " " * margin),
    ]
