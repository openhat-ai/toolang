"""Input and steer bar rendering for the chat TUI."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .chat_tui_theme import (
    _CHAT_INPUT_BG,
    _CHAT_INPUT_DIM_FG,
    _CHAT_INPUT_FG,
    _CHAT_RESET,
    _CHAT_STEER_INPUT_BG,
    _CHAT_STEER_INPUT_DIM_FG,
    _CHAT_STEER_INPUT_FG,
    _chat_ansi_style,
    _chat_display_len,
    _chat_terminal_width,
)


@dataclass(frozen=True)
class _ChatInputBarSegment:
    text: str
    dim: bool = False


@dataclass(frozen=True)
class _ChatInputBarRow:
    segments: tuple[_ChatInputBarSegment, ...] = ()
    bar: bool = True


@dataclass(frozen=True)
class _ChatInputBarSpec:
    kind: Literal["normal", "steer"]
    marker: str
    text: str
    footer: str = ""
    footer_dim: bool = False
    outer_blank: bool = False


@dataclass(frozen=True)
class _ChatRenderedInputBarSegment:
    text: str
    style: str
    fg: str
    bg: str


def _chat_input_bar_ansi_lines(spec: _ChatInputBarSpec) -> list[str]:
    return [_chat_rendered_input_bar_ansi_line(row) for row in _chat_render_input_bar_rows(spec)]


def _chat_input_bar_plain_lines(spec: _ChatInputBarSpec) -> list[str]:
    return [_chat_input_bar_plain_line(row) for row in _chat_input_bar_rows(spec)]


def _chat_input_bar_plain_line(row: _ChatInputBarRow) -> str:
    return "".join(segment.text for segment in row.segments) if row.bar else ""


def _chat_input_bar_ansi_line(row: _ChatInputBarRow, *, kind: Literal["normal", "steer"]) -> str:
    return _chat_rendered_input_bar_ansi_line(_chat_render_input_bar_row(row, kind=kind))


def _chat_input_bar_fragments(spec: _ChatInputBarSpec) -> list[tuple[str, str]]:
    return _chat_join_fragment_rows(_chat_input_bar_fragment_rows(spec))


def _chat_input_bar_fragment_rows(spec: _ChatInputBarSpec) -> list[list[tuple[str, str]]]:
    return [_chat_rendered_input_bar_fragments(row) for row in _chat_render_input_bar_rows(spec)]


def _chat_input_bar_rows(spec: _ChatInputBarSpec) -> list[_ChatInputBarRow]:
    rows: list[_ChatInputBarRow] = []
    if spec.outer_blank:
        rows.append(_ChatInputBarRow(bar=False))
    rows.append(_ChatInputBarRow())
    for index, line in enumerate(spec.text.splitlines() or [""]):
        if index == 0:
            rows.append(
                _ChatInputBarRow(
                    (
                        _ChatInputBarSegment(spec.marker, dim=True),
                        _ChatInputBarSegment(f" {line}"),
                    )
                )
            )
        else:
            rows.append(_ChatInputBarRow((_ChatInputBarSegment(f"  {line}"),)))
    footer = (_ChatInputBarSegment(spec.footer, dim=spec.footer_dim),) if spec.footer else ()
    rows.append(_ChatInputBarRow(footer))
    if spec.outer_blank:
        rows.append(_ChatInputBarRow(bar=False))
    return rows


def _chat_render_input_bar_rows(spec: _ChatInputBarSpec) -> list[list[_ChatRenderedInputBarSegment]]:
    return [_chat_render_input_bar_row(row, kind=spec.kind) for row in _chat_input_bar_rows(spec)]


def _chat_render_input_bar_row(
    row: _ChatInputBarRow, *, kind: Literal["normal", "steer"]
) -> list[_ChatRenderedInputBarSegment]:
    if not row.bar:
        return []
    rendered: list[_ChatRenderedInputBarSegment] = []
    visible_len = 0
    for segment in row.segments:
        if not segment.text:
            continue
        rendered.append(_chat_render_input_bar_segment(segment.text, kind=kind, dim=segment.dim))
        visible_len += _chat_display_len(segment.text)
    padding = " " * max(0, _chat_terminal_width() - visible_len)
    if padding:
        rendered.append(_chat_render_input_bar_segment(padding, kind=kind, dim=False))
    return rendered


def _chat_render_input_bar_segment(
    text: str, *, kind: Literal["normal", "steer"], dim: bool
) -> _ChatRenderedInputBarSegment:
    return _ChatRenderedInputBarSegment(
        text=text,
        style=_chat_input_bar_class(kind, dim=dim),
        fg=_chat_input_bar_foreground(kind, dim=dim),
        bg=_chat_input_bar_background(kind),
    )


def _chat_rendered_input_bar_ansi_line(row: Sequence[_ChatRenderedInputBarSegment]) -> str:
    if not row:
        return ""
    return "".join(f"{_chat_ansi_style(segment.fg, segment.bg)}{segment.text}" for segment in row) + _CHAT_RESET


def _chat_rendered_input_bar_fragments(
    row: Sequence[_ChatRenderedInputBarSegment],
) -> list[tuple[str, str]]:
    return [(segment.style, segment.text) for segment in row]


def _chat_input_bar_foreground(kind: Literal["normal", "steer"], *, dim: bool) -> str:
    if kind == "steer":
        return _CHAT_STEER_INPUT_DIM_FG if dim else _CHAT_STEER_INPUT_FG
    return _CHAT_INPUT_DIM_FG if dim else _CHAT_INPUT_FG


def _chat_input_bar_background(kind: Literal["normal", "steer"]) -> str:
    if kind == "steer":
        return _CHAT_STEER_INPUT_BG
    return _CHAT_INPUT_BG


def _chat_input_bar_class(kind: Literal["normal", "steer"], *, dim: bool) -> str:
    prefix = "normal-input" if kind == "normal" else "steer-input"
    return f"class:{prefix}.dim" if dim else f"class:{prefix}"


def _chat_join_fragment_rows(rows: Sequence[Sequence[tuple[str, str]]]) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        fragments.extend(row)
        if index < len(rows) - 1:
            fragments.append(("", "\n"))
    return fragments
