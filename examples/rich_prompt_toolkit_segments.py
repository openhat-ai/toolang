"""Probe Rich Segment to prompt-toolkit formatted text interop.

Run non-interactive output:

    uv run python examples/rich_prompt_toolkit_segments.py

Run the prompt-toolkit view in a real terminal:

    uv run python examples/rich_prompt_toolkit_segments.py --interactive
"""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Iterable

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.shortcuts import print_formatted_text
from rich.color import Color
from rich.console import Console, RenderableType
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from rich.theme import Theme


CHAT_THEME = Theme(
    {
        "chat.input": "#f5f5f5 on #444444",
        "chat.input.dim": "#b8b8b8 on #444444",
        "chat.model": "bold #00a3b5",
        "chat.muted": "#777777",
        "chat.tool": "#268b45",
        "chat.tool.marker": "bold #268b45",
    }
)


def terminal_width(default: int = 72) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def chat_bar(segments: Iterable[tuple[str, str]], *, style: str) -> Text:
    """Build the kind of full-width input/status bar chat blocks need."""
    text = Text(style=style)
    for value, segment_style in segments:
        text.append(value, style=segment_style)
    text.pad_right(max(0, terminal_width() - text.cell_len))
    return text


def sample_renderable() -> Text:
    return Text.assemble(
        chat_bar([], style="chat.input"),
        "\n",
        chat_bar(
            [(">", "chat.input.dim"), (" hello from rich segments", "chat.input")],
            style="chat.input",
        ),
        "\n",
        chat_bar(
            [("  waiting run_demo for queue · position 1", "chat.input.dim")],
            style="chat.input",
        ),
        "\n\n",
        ("• ", "chat.model"),
        ("thinking...", "chat.muted"),
        "\n",
        ("› ", "chat.tool.marker"),
        ("ran shell__execute", "chat.tool"),
    )


def render_segments(renderable: RenderableType, *, width: int | None = None) -> list[Segment]:
    console = Console(
        width=width or terminal_width(),
        color_system="truecolor",
        force_terminal=True,
        legacy_windows=False,
        theme=CHAT_THEME,
    )
    return list(console.render(renderable, console.options))


def rich_style_to_prompt_toolkit(style: Style | None) -> str:
    if style is None:
        return ""

    parts: list[str] = []
    if style.color:
        parts.append(_prompt_toolkit_color(style.color))
    if style.bgcolor:
        parts.append(f"bg:{_prompt_toolkit_color(style.bgcolor)}")
    if style.bold:
        parts.append("bold")
    if style.italic:
        parts.append("italic")
    if style.underline:
        parts.append("underline")
    if style.dim:
        parts.append("dim")
    if style.reverse:
        parts.append("reverse")
    return " ".join(parts)


def rich_segments_to_prompt_toolkit(segments: Iterable[Segment]) -> FormattedText:
    fragments: list[tuple[str, str]] = []
    for segment in segments:
        if segment.control or not segment.text:
            continue
        fragments.append((rich_style_to_prompt_toolkit(segment.style), segment.text))
    return FormattedText(fragments)


def _prompt_toolkit_color(color: Color) -> str:
    triplet = color.get_truecolor()
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def dump() -> None:
    renderable = sample_renderable()
    console = Console(
        width=terminal_width(),
        color_system="truecolor",
        force_terminal=True,
        legacy_windows=False,
        theme=CHAT_THEME,
    )
    segments = render_segments(renderable)
    fragments = rich_segments_to_prompt_toolkit(segments)

    print("Rich terminal preview:", flush=True)
    console.print(renderable)
    print()

    print("prompt-toolkit terminal preview:", flush=True)
    print_formatted_text(fragments)
    print()

    print("Rich segments:", flush=True)
    for segment in segments:
        print(repr(segment))
    print("\nprompt-toolkit fragments:")
    for fragment in fragments:
        print(repr(fragment))


def interactive() -> None:
    bindings = KeyBindings()

    @bindings.add("q")
    @bindings.add("c-c")
    def _exit(event: KeyPressEvent) -> None:
        event.app.exit()

    control = FormattedTextControl(
        lambda: rich_segments_to_prompt_toolkit(render_segments(sample_renderable()))
    )
    root = HSplit(
        [
            Window(
                FormattedTextControl(
                    [("class:hint", "Rich Segment -> prompt-toolkit fragments. Press q to exit.\n\n")]
                ),
                height=3,
            ),
            Window(control, always_hide_cursor=True),
        ]
    )
    Application(
        layout=Layout(root),
        key_bindings=bindings,
        full_screen=False,
    ).run()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()
    if args.interactive:
        interactive()
    else:
        dump()


if __name__ == "__main__":
    main()
