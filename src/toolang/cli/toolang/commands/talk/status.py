"""Conversation presence and the bounded two-sided Talk footer."""

from prompt_toolkit.formatted_text import StyleAndTextTuples, fragment_list_to_text

from toolang.cli.common.execution_progress.formatting import display_width, truncate
from toolang.teaming.schemas import Conversation, target

from .rendering import display_text


def conversation_status(
    info: Conversation, viewer: str, online: set[str] | None
) -> StyleAndTextTuples:
    if info.kind == "group":
        name = display_text(info.label).removeprefix("gc_")
        count = len(online.intersection(info.members)) if online is not None else None
        return [
            ("class:status", f"#{name}("),
            (
                "class:status.online" if count else "class:status",
                str(count) if count is not None else "?",
            ),
            ("class:status", f"/{len(info.members)})"),
        ]
    others = sorted(member for member in info.members if member != viewer)
    fragments: StyleAndTextTuples = []
    for member in others:
        if fragments:
            fragments.append(("class:status", ","))
        name = ("@" if len(others) == 1 else "") + target(member).name
        style = "class:status.online" if online and member in online else "class:status"
        fragments.append((style, name))
    return fragments


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
    left: StyleAndTextTuples, right: str, *, width: int, warning: bool
) -> StyleAndTextTuples:
    width = max(1, width)
    margin = min(2, (width - 1) // 2)
    inner = width - 2 * margin
    right = truncate(" ".join(display_text(right).split()), inner)
    right_width = display_width(right)
    safe_left: StyleAndTextTuples = [
        (fragment[0], " ".join(display_text(fragment[1]).split())) for fragment in left
    ]
    left = _truncate_fragments(safe_left, inner - right_width - bool(right))
    gap = inner - display_width(fragment_list_to_text(left)) - right_width
    return [
        ("", " " * margin),
        *left,
        ("", " " * gap),
        ("class:status.warning" if warning else "class:status", right),
        ("", " " * margin),
    ]
