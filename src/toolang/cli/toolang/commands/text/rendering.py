"""Conversation blocks rendered safely into ordinary terminal scrollback."""

import re
import unicodedata

from rich.align import Align
from rich.console import Group, RenderableType
from rich.constrain import Constrain
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

from toolang.cli.common.terminal_surfaces import TerminalSurfaces
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
    header = Text(display_text(message.sender), style="dim")
    body = display_text(message.body)
    content = Group(header, Markdown(body, hyperlinks=False) if agent else Text(body))
    if not agent:
        content = Padding(
            content,
            (0, min(1, body_width // 3)),
            style=f"on {surfaces.input_background}",
        )
    return Padding(
        Align(
            Constrain(content, body_width),
            align="right" if right else "left",
            width=width,
        ),
        (0, 0, 1, 0),
    )
