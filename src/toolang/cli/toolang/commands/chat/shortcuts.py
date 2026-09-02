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


SUBMIT = ChatShortcut(
    "submit",
    (("enter",),),
    "Enter",
    "Send input (submit or queue)",
)
STEER = ChatShortcut(
    "steer",
    (("escape", "enter"),),
    "Meta-Enter",
    "Steer the active run",
)
INSERT_NEWLINE = ChatShortcut(
    "insert_newline",
    (("c-j",),),
    "Ctrl-J",
    "Insert a newline (also Shift-Enter if supported)",
    optional_bindings=(("s-enter",),),
)
SWITCH_AREA = ChatShortcut(
    "switch_area",
    (("tab",), ("s-tab",)),
    "Tab, Shift-Tab",
    "Switch between input and queued inputs",
)
QUEUE_PREVIOUS = ChatShortcut(
    "queue_previous",
    (("up",), ("c-p",)),
    "Up, Ctrl-P",
    "Select the previous queued input",
)
QUEUE_NEXT = ChatShortcut(
    "queue_next",
    (("down",), ("c-n",)),
    "Down, Ctrl-N",
    "Select the next queued input",
)
QUEUE_COLLAPSE = ChatShortcut(
    "queue_collapse",
    ((" ",),),
    "Space",
    "Collapse queued inputs into input",
)
QUEUE_EDIT = ChatShortcut(
    "queue_edit",
    (("e",),),
    "E",
    "Edit the selected queued input",
)
QUEUE_STEER = ChatShortcut(
    "queue_steer",
    STEER.bindings,
    "Meta-Enter",
    "Steer with the selected queued input",
)
QUEUE_DELETE = ChatShortcut(
    "queue_delete",
    (("d",), ("delete",)),
    "D, Delete",
    "Remove the selected queued input",
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
    STEER,
    INSERT_NEWLINE,
    SWITCH_AREA,
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
    "QUEUE_COLLAPSE",
    "QUEUE_DELETE",
    "QUEUE_EDIT",
    "QUEUE_NEXT",
    "QUEUE_PREVIOUS",
    "QUEUE_STEER",
    "QUIT",
    "STEER",
    "SUBMIT",
    "SWITCH_AREA",
    "ChatShortcut",
    "help_lines",
]
