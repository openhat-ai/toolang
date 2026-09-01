"""Canonical Toolang-owned keyboard shortcuts for terminal Chat."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChatShortcut:
    """One documented Chat action and its prompt-toolkit key sequences."""

    name: str
    bindings: tuple[tuple[str, ...], ...]
    label: str
    summary: str
    optional_bindings: tuple[tuple[str, ...], ...] = ()


SUBMIT = ChatShortcut("submit", (("enter",),), "Enter", "Submit input")
INSERT_NEWLINE = ChatShortcut(
    "insert_newline",
    (("escape", "enter"), ("c-j",)),
    "Alt-Enter, Ctrl-J",
    "Insert a newline",
)
SHIFT_NEWLINE = ChatShortcut(
    "shift_newline",
    (),
    "Shift-Enter",
    "Insert a newline when supported by the terminal",
    optional_bindings=(("s-enter",),),
)
PREVIOUS_HISTORY = ChatShortcut(
    "previous_history",
    (("up",), ("c-p",)),
    "Up, Ctrl-P",
    "Previous history at the first line; otherwise move up",
)
NEXT_HISTORY = ChatShortcut(
    "next_history",
    (("down",), ("c-n",)),
    "Down, Ctrl-N",
    "Next history at the last line; otherwise move down",
)
DISMISS_STATUS = ChatShortcut(
    "dismiss_status",
    (("escape",),),
    "Esc",
    "Dismiss a transient status message",
)
CANCEL_RUN = ChatShortcut(
    "cancel_run",
    (("escape", "escape"),),
    "Esc Esc",
    "Cancel the active run",
)
INTERRUPT = ChatShortcut(
    "interrupt",
    (("c-c",),),
    "Ctrl-C",
    "Clear input; otherwise cancel a run; press twice to exit",
)
EOF = ChatShortcut(
    "eof",
    (("c-d",),),
    "Ctrl-D",
    "Exit when input is empty and no run is active",
)
CLEAR = ChatShortcut(
    "clear",
    (("c-l",),),
    "Ctrl-L",
    "Clear the display when no run is active",
)
QUIT = ChatShortcut(
    "quit",
    (("c-q",),),
    "Ctrl-Q",
    "Exit Chat immediately",
)

CHAT_SHORTCUTS = (
    SUBMIT,
    INSERT_NEWLINE,
    SHIFT_NEWLINE,
    PREVIOUS_HISTORY,
    NEXT_HISTORY,
    DISMISS_STATUS,
    CANCEL_RUN,
    INTERRUPT,
    EOF,
    CLEAR,
    QUIT,
)


def help_lines() -> tuple[str, ...]:
    """Return aligned, presentation-neutral shortcut help rows."""

    width = max(len(shortcut.label) for shortcut in CHAT_SHORTCUTS)
    return tuple(
        f"{shortcut.label:<{width}}  {shortcut.summary}" for shortcut in CHAT_SHORTCUTS
    )


__all__ = [
    "CANCEL_RUN",
    "CHAT_SHORTCUTS",
    "CLEAR",
    "DISMISS_STATUS",
    "EOF",
    "INSERT_NEWLINE",
    "INTERRUPT",
    "NEXT_HISTORY",
    "PREVIOUS_HISTORY",
    "QUIT",
    "SHIFT_NEWLINE",
    "SUBMIT",
    "ChatShortcut",
    "help_lines",
]
