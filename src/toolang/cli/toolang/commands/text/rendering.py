"""Conversation blocks rendered safely into ordinary terminal scrollback."""

import re
import unicodedata

from rich.align import Align
from rich.console import Group, RenderableType
from rich.constrain import Constrain
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from toolang.cli.common.terminal_surfaces import TerminalSurfaces
from toolang.cli.common.control_bars import CONTROL_BAR_MARK, RUN_CONTROL_ACCENT
from toolang.messaging.schemas import Message, conversation

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
    group: str,
    agents: set[str],
    width: int,
    surfaces: TerminalSurfaces,
) -> RenderableType:
    info = conversation(group)
    agent = message.sender in agents
    right = message.sender == info.names[-1] if info.kind == "dm" else not agent
    width = max(1, width)
    gutter = min(8, width // 5)
    body_width = max(1, width - gutter)
    header = Text(
        display_text(message.sender),
        style="bold" if agent else "not dim",
        justify="right" if right and not agent else "left",
    )
    body = display_text(message.body)
    if agent:
        content = Markdown(body, hyperlinks=False)
    else:
        padding = min(2, (body_width - 1) // 2)
        content = Text(body)
        if padding:
            columns = Table.grid(padding=0, expand=True)
            columns.add_column(ratio=1, overflow="fold")
            columns.add_column(width=padding, no_wrap=True)
            columns.add_row(
                content,
                Text(
                    CONTROL_BAR_MARK,
                    style=f"{RUN_CONTROL_ACCENT} not dim",
                    justify="right",
                ),
            )
            content = columns
        content = Padding(
            content,
            (1, 0, 1, padding),
            style=f"not dim on {surfaces.input_background}",
        )
    if not right and body_width > 2:
        columns = Table.grid(padding=0, expand=True)
        columns.add_column(width=2, no_wrap=True)
        columns.add_column(ratio=1, overflow="fold")
        columns.add_row(Text("•", style="dim"), content)
        content = columns
        header = Padding(header, (0, 0, 0, 2))
    return Padding(
        Align(
            Constrain(Group(header, content), body_width),
            align="right" if right else "left",
            width=width,
        ),
        (0, 0, 1, 0),
    )
