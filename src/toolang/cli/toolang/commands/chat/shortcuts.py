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
    aliases: tuple[str, ...] = ()

    @property
    def help_label(self) -> str:
        suffix = f" ({', '.join(self.aliases)})" if self.aliases else ""
        return self.label + suffix

    def hint(self, action: str) -> str:
        """Keep inline hints short; list alternative keys only in full help."""
        return f"[{self.label}] {action}"


SUBMIT = ChatShortcut(
    "submit",
    (("enter",),),
    "Enter",
    "Send input (submit or queue)",
)
STEER = ChatShortcut(
    "steer",
    (("escape", "enter"),),
    "Meta+Enter",
    "Steer active run",
)
INSERT_NEWLINE = ChatShortcut(
    "insert_newline",
    (("c-j",),),
    "Ctrl+J",
    "Insert a newline (also Shift+Enter if supported)",
    optional_bindings=(("s-enter",),),
)
SWITCH_AREA = ChatShortcut(
    "switch_area",
    (("tab",), ("s-tab",)),
    "Tab",
    "Switch focus (input/queue)",
    aliases=("Shift+Tab",),
)
QUEUE_PREVIOUS = ChatShortcut(
    "queue_previous",
    (("up",), ("c-p",)),
    "↑",
    "Select previous input",
    aliases=("Ctrl+P",),
)
QUEUE_NEXT = ChatShortcut(
    "queue_next",
    (("down",), ("c-n",)),
    "↓",
    "Select next input",
    aliases=("Ctrl+N",),
)
QUEUE_TOGGLE = ChatShortcut(
    "queue_toggle",
    ((" ",),),
    "Space",
    "Expand or collapse",
)
QUEUE_EDIT = ChatShortcut(
    "queue_edit",
    (("e",),),
    "e",
    "Edit selected input",
)
QUEUE_STEER = ChatShortcut(
    "queue_steer",
    STEER.bindings,
    STEER.label,
    "Steer with selected input",
)
QUEUE_DELETE = ChatShortcut(
    "queue_delete",
    (("d",), ("delete",)),
    "d",
    "Delete selected input",
    aliases=("Del",),
)
PREVIOUS_HISTORY = ChatShortcut(
    "previous_history",
    (("up",), ("c-p",)),
    QUEUE_PREVIOUS.label,
    "Previous history on first line; otherwise move up",
    aliases=QUEUE_PREVIOUS.aliases,
)
NEXT_HISTORY = ChatShortcut(
    "next_history",
    (("down",), ("c-n",)),
    QUEUE_NEXT.label,
    "Next history on last line; otherwise move down",
    aliases=QUEUE_NEXT.aliases,
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
    "Ctrl+C",
    "Clear input; otherwise cancel run; press twice to exit",
)
EOF = ChatShortcut(
    "eof",
    (("c-d",),),
    "Ctrl+D",
    "Exit when input is empty and no run is active",
)
CLEAR = ChatShortcut(
    "clear",
    (("c-l",),),
    "Ctrl+L",
    "Clear the display when no run is active",
)
QUIT = ChatShortcut(
    "quit",
    (("c-q",),),
    "Ctrl+Q",
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
QUEUE_SHORTCUTS = (
    QUEUE_TOGGLE,
    QUEUE_PREVIOUS,
    QUEUE_NEXT,
    QUEUE_EDIT,
    QUEUE_STEER,
    QUEUE_DELETE,
)


def help_lines() -> tuple[str, ...]:
    """Return aligned, presentation-neutral shortcut help rows."""

    width = max(
        len(shortcut.help_label) for shortcut in (*CHAT_SHORTCUTS, *QUEUE_SHORTCUTS)
    )
    return (
        *(
            f"{shortcut.help_label:<{width}}  {shortcut.summary}"
            for shortcut in CHAT_SHORTCUTS
        ),
        "",
        "Queue focused:",
        *(
            f"{shortcut.help_label:<{width}}  {shortcut.summary}"
            for shortcut in QUEUE_SHORTCUTS
        ),
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
    "QUEUE_DELETE",
    "QUEUE_EDIT",
    "QUEUE_NEXT",
    "QUEUE_PREVIOUS",
    "QUEUE_SHORTCUTS",
    "QUEUE_STEER",
    "QUEUE_TOGGLE",
    "QUIT",
    "STEER",
    "SUBMIT",
    "SWITCH_AREA",
    "ChatShortcut",
    "help_lines",
]
