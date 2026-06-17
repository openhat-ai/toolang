"""Terminal styling helpers for the chat TUI."""

from __future__ import annotations

import shutil

from prompt_toolkit.utils import get_cwidth


_CHAT_DIM = "\x1b[2m"
_CHAT_NORMAL_INTENSITY = "\x1b[22m"
_CHAT_RESET = "\x1b[0m"
_CHAT_BOLD = "\x1b[1m"
_CHAT_QUEUE_FG = "#f2f2f2"
_CHAT_QUEUE_BG = "#3a3a3a"
_CHAT_QUEUE_DIM_FG = "#b8b8b8"
_CHAT_INPUT_FG = "#f5f5f5"
_CHAT_INPUT_BG = "#444444"
_CHAT_INPUT_DIM_FG = "#b8b8b8"
_CHAT_STEER_INPUT_FG = "#f5f5f5"
_CHAT_STEER_INPUT_BG = "#2f555d"
_CHAT_STEER_INPUT_DIM_FG = "#b8b8b8"
_CHAT_STATUS_FG = "#f2f2f2"
_CHAT_STATUS_BG = "#5a5a5a"
_CHAT_CURSOR_FG = "#111111"
_CHAT_CURSOR_BG = "#eeeeee"


def _chat_dim(text: str) -> str:
    return f"{_CHAT_DIM}{text}{_CHAT_NORMAL_INTENSITY}"


def _chat_terminal_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def _chat_ui_palette() -> dict[str, str]:
    return {
        "": "",
        "queue": _chat_prompt_style(_CHAT_QUEUE_FG, _CHAT_QUEUE_BG),
        "queue.dim": _chat_prompt_style(_CHAT_QUEUE_DIM_FG, _CHAT_QUEUE_BG),
        "normal-input": _chat_prompt_style(_CHAT_INPUT_FG, _CHAT_INPUT_BG),
        "normal-input.dim": _chat_prompt_style(_CHAT_INPUT_DIM_FG, _CHAT_INPUT_BG),
        "input": _chat_prompt_style(_CHAT_INPUT_FG, _CHAT_INPUT_BG),
        "steer-input": _chat_prompt_style(_CHAT_STEER_INPUT_FG, _CHAT_STEER_INPUT_BG),
        "steer-input.dim": _chat_prompt_style(_CHAT_STEER_INPUT_DIM_FG, _CHAT_STEER_INPUT_BG),
        "cursor": _chat_prompt_style(_CHAT_CURSOR_FG, _CHAT_CURSOR_BG),
        "input.cursor": _chat_prompt_style(_CHAT_CURSOR_FG, _CHAT_CURSOR_BG),
        "status": _chat_prompt_style(_CHAT_STATUS_FG, _CHAT_STATUS_BG),
        "status.model": "fg:#ffd866",
        "status.thunk": "fg:#8fd7ff",
        "status.flow": "fg:#d7b3ff",
        "status.text": "fg:ansigray",
        "status.error": "fg:ansired",
    }


def _chat_prompt_style(fg: str, bg: str) -> str:
    return f"fg:{fg} bg:{bg}"


def _chat_ansi_style(fg: str, bg: str) -> str:
    if fg.startswith("#") or bg.startswith("#"):
        return f"\x1b[{_chat_sgr_color(fg, foreground=True)};{_chat_sgr_color(bg, foreground=False)}m"
    foreground = {
        "ansiblack": "30",
        "ansired": "31",
        "ansigreen": "32",
        "ansiyellow": "33",
        "ansiblue": "34",
        "ansimagenta": "35",
        "ansicyan": "36",
        "ansiwhite": "37",
        "ansibrightblack": "90",
        "ansibrightred": "91",
        "ansibrightgreen": "92",
        "ansibrightyellow": "93",
        "ansibrightblue": "94",
        "ansibrightmagenta": "95",
        "ansibrightcyan": "96",
        "ansibrightwhite": "97",
    }
    background = {
        "ansiblack": "40",
        "ansired": "41",
        "ansigreen": "42",
        "ansiyellow": "43",
        "ansiblue": "44",
        "ansimagenta": "45",
        "ansicyan": "46",
        "ansiwhite": "47",
        "ansibrightblack": "100",
        "ansibrightred": "101",
        "ansibrightgreen": "102",
        "ansibrightyellow": "103",
        "ansibrightblue": "104",
        "ansibrightmagenta": "105",
        "ansibrightcyan": "106",
        "ansibrightwhite": "107",
    }
    return f"\x1b[{foreground[fg]};{background[bg]}m"


def _chat_sgr_color(color: str, *, foreground: bool) -> str:
    if not color.startswith("#") or len(color) != 7:
        raise ValueError(f"unsupported color: {color}")
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    prefix = "38" if foreground else "48"
    return f"{prefix};2;{red};{green};{blue}"


def _chat_visible_text(text: str) -> str:
    visible: list[str] = []
    in_escape = False
    for char in text:
        if char == "\x1b":
            in_escape = True
        elif in_escape and char == "m":
            in_escape = False
        elif not in_escape:
            visible.append(char)
    return "".join(visible)


def _chat_display_len(text: str) -> int:
    return get_cwidth(_chat_visible_text(text))
