"""Conversation blocks rendered safely into ordinary terminal scrollback."""

import re
import unicodedata

from rich.align import Align
from rich.console import Group, RenderableType
from rich.constrain import Constrain
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from toolang.cli.common.terminal_surfaces import TerminalSurfaces
from toolang.cli.common.control_bars import CONTROL_BAR_MARK, RUN_CONTROL_ACCENT
from toolang.cli.common.markdown import TerminalMarkdown
from toolang.teaming.schemas import Message, Conversation, target

_ESCAPE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]"
)


def display_text(value: str) -> str:
    value = _ESCAPE.sub("", value)
    return "".join(
        c
        for c in value
        if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
    )


def message_block(
    message: Message,
    identity: str,
    agents: set[str],
    width: int,
    surfaces: TerminalSurfaces,
) -> RenderableType:
    agent = target(message.sender).kind == "agent"
    right = message.sender == identity
    width = max(1, width)
    gutter = min(8, width // 5)
    body_width = max(1, width - gutter)
    header: RenderableType = Text(
        display_text(target(message.sender).name),
        style="bold" if agent else "not dim",
        justify="right" if right else "left",
    )
    body = display_text(message.body)
    content: RenderableType = (
        TerminalMarkdown(
            body,
            code_background=surfaces.code_background,
            inline_code_background=surfaces.inline_code_background,
            code_foreground=None,
            hyperlinks=False,
        )
        if agent
        else Text(body)
    )
    padding = min(2, (body_width - 1) // 2)
    if padding:
        marker = Text(
            "•" if agent else CONTROL_BAR_MARK,
            style="dim" if agent else f"{RUN_CONTROL_ACCENT} not dim",
            justify="right" if right else "left",
        )
        columns = Table.grid(padding=0, expand=True)
        columns.add_column(width=padding, no_wrap=True)
        columns.add_column(ratio=1, overflow="fold")
        columns.add_column(width=padding, no_wrap=True)
        columns.add_row("" if right else marker, header, marker if right else "")
        header = columns
        content = Padding(content, (0, padding))
    if not agent:
        content = Padding(
            content,
            (1, 0, 1, 0),
            style=f"not dim on {surfaces.input_background}",
        )
    return Padding(
        Align(
            Constrain(Group(header, content), body_width),
            align="right" if right else "left",
            width=width,
        ),
        (0, 0, 1, 0),
    )


def conversation_label(info: Conversation) -> str:
    if info.kind == "group":
        return f"#{info.label}"
    return " ↔ ".join(info.members)
