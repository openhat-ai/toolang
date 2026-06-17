"""Markdown rendering helpers for the chat TUI."""

from __future__ import annotations

from collections.abc import Sequence
import io

from rich.console import Console
from rich.markdown import Markdown

from .chat_tui_theme import _chat_terminal_width, _chat_visible_text


def _chat_render_markdown_lines(text: str) -> list[str]:
    stream = io.StringIO()
    section_titles = _chat_markdown_section_titles(text)
    try:
        console = Console(
            file=stream,
            force_terminal=True,
            color_system="standard",
            width=_chat_markdown_width(),
            soft_wrap=False,
        )
        console.print(Markdown(text), width=_chat_markdown_width(), end="")
    except Exception:
        return text.splitlines()
    rendered = stream.getvalue().rstrip("\n")
    return _chat_compact_markdown_lines(rendered.splitlines(), section_titles=section_titles)


def _chat_compact_markdown_lines(lines: Sequence[str], *, section_titles: set[str]) -> list[str]:
    compact: list[str] = []
    normalized_lines = [line.rstrip() for line in lines]
    for index, normalized in enumerate(normalized_lines):
        visible = _chat_visible_text(normalized)
        if not visible.strip():
            if _chat_should_keep_markdown_blank(normalized_lines, index, section_titles=section_titles):
                if compact and compact[-1] != "":
                    compact.append("")
            continue
        compact.append(normalized)
    return compact


def _chat_should_keep_markdown_blank(lines: Sequence[str], index: int, *, section_titles: set[str]) -> bool:
    previous = _chat_previous_visible_line(lines, index)
    next_line = _chat_next_visible_line(lines, index)
    return _chat_is_section_title(previous, section_titles) or _chat_is_section_title(next_line, section_titles)


def _chat_previous_visible_line(lines: Sequence[str], index: int) -> str | None:
    for candidate in reversed(lines[:index]):
        visible = _chat_visible_text(candidate).strip()
        if visible:
            return visible
    return None


def _chat_next_visible_line(lines: Sequence[str], index: int) -> str | None:
    for candidate in lines[index + 1 :]:
        visible = _chat_visible_text(candidate).strip()
        if visible:
            return visible
    return None


def _chat_is_section_title(line: str | None, section_titles: set[str]) -> bool:
    return line is not None and line in section_titles


def _chat_markdown_section_titles(text: str) -> set[str]:
    titles: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        prefix, _, title = stripped.partition(" ")
        if 1 <= len(prefix) <= 6 and set(prefix) == {"#"} and title.strip():
            titles.add(title.strip())
    return titles


def _chat_markdown_width() -> int:
    return min(100, max(40, _chat_terminal_width() - 4))
