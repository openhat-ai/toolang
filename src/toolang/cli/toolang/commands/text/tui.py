"""Live input and retained/live messages on the terminal's normal screen."""

import asyncio
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from rich.text import Text
from valkey.exceptions import ValkeyError

from toolang.cli.common.console import terminal_console
from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from toolang.cli.common.input import InputBox
from toolang.cli.common.input_history import InputHistoryStore
from toolang.cli.common.scrollback import ScrollbackRenderer
from toolang.cli.common.terminal_surfaces import TerminalSurfaces
from toolang.common.files import atomic_write_text
from toolang.messaging.client import MessagingClient
from toolang.messaging.errors import MessagingError
from toolang.messaging.schemas import Message

from .rendering import display_text, message_block


class TextTui:
    def __init__(
        self,
        client: MessagingClient,
        group: str,
        human: str,
        state: Path,
        surfaces: TerminalSurfaces,
        *,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
    ):
        self.client, self.group, self.human = client, group, human
        self.surfaces = surfaces
        self.max_width = max_width
        self.draft = state / "draft.txt"
        self.status = "Connecting…"
        self.pending = False
        self.cursor = "0-0"
        self.agents: set[str] = set()
        self.prompt = InputBox(
            self.invalidate,
            history_store=InputHistoryStore(state / "input.jsonl"),
            on_input=self.save_draft,
            normalize=lambda text: text,
            get_max_rows=lambda: max(3, self.app.output.get_size().rows - 1),
        )
        if self.draft.exists():
            self.prompt.replace_input(self.draft.read_text(encoding="utf-8"))
        keys = KeyBindings()
        focus = has_focus(self.prompt.buffer)

        @keys.add("enter", filter=focus)
        def send(_event: Any) -> None:
            if self.prompt.buffer.text.strip() and not self.pending:
                self.pending = True
                self.app.create_background_task(self.send(self.prompt.buffer.text))

        @keys.add("c-j", filter=focus)
        def newline(_event: Any) -> None:
            self.prompt._insert_newline()

        @keys.add("c-p", filter=focus)
        def previous(_event: Any) -> None:
            self.prompt._previous_history()

        @keys.add("c-n", filter=focus)
        def next_entry(_event: Any) -> None:
            self.prompt._next_history()

        @keys.add("up", filter=focus)
        def up(_event: Any) -> None:
            self.prompt.buffer.cursor_up()

        @keys.add("down", filter=focus)
        def down(_event: Any) -> None:
            self.prompt.buffer.cursor_down()

        @keys.add("c-c")
        @keys.add("c-q")
        @keys.add("c-d", filter=Condition(lambda: not self.prompt.has_input()))
        def quit_app(_event: Any) -> None:
            self.app.exit()

        @keys.add("c-l")
        def clear(_event: Any) -> None:
            self.app.renderer.clear()

        self.app: Application[None] = Application(
            layout=Layout(
                HSplit(
                    [
                        self.prompt.container(),
                        Window(FormattedTextControl(self.status_text), height=1),
                    ]
                ),
                focused_element=self.prompt.buffer,
            ),
            key_bindings=keys,
            full_screen=False,
            erase_when_done=True,
            mouse_support=False,
            style=Style.from_dict(
                {
                    "input": f"bg:{surfaces.input_background}",
                    "input.placeholder": "dim",
                    "control.run": "bg:ansibrightcyan",
                }
            ),
        )
        self.app.renderer = ScrollbackRenderer(
            self.app.renderer.style,
            self.app.output,
            full_screen=False,
            mouse_support=False,
            cpr_not_supported_callback=self.app.cpr_not_supported_callback,
        )

    def invalidate(self) -> None:
        if hasattr(self, "app"):
            self.app.invalidate()

    def status_text(self) -> str:
        return display_text(
            f" {self.group} · {self.human} · {self.status} · Ctrl+J newline · Ctrl+Q quit"
        )

    def save_draft(self) -> None:
        try:
            atomic_write_text(self.draft, self.prompt.buffer.text)
        except OSError as exc:
            self.status = f"Draft could not be saved: {exc}"
        self.invalidate()

    async def send(self, body: str) -> None:
        self.status = "Sending…"
        self.invalidate()
        try:
            receipt = await self.client.send(self.group, sender=self.human, body=body)
        except (MessagingError, ValkeyError) as exc:
            self.status = str(exc)
            await self.print_notice(self.status)
        else:
            self.prompt.accept_submission(body)
            self.save_draft()
            self.status = f"Sent {receipt['message']['id']}"
        finally:
            self.pending = False
            self.invalidate()

    async def print_notice(self, text: str) -> None:
        await run_in_terminal(
            lambda: terminal_console(width=self.app.output.get_size().columns).print(
                Text(display_text(text), style="yellow")
            )
        )

    async def show(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        for sid, fields in entries:
            try:
                message = Message.decode(fields["data"])
            except (KeyError, MessagingError):
                await self.print_notice(f"Skipped malformed message at {sid}")
            else:

                def write() -> None:
                    width = max(
                        1, min(self.app.output.get_size().columns, self.max_width)
                    )
                    terminal_console(width=width).print(
                        message_block(
                            message, self.group, self.agents, width, self.surfaces
                        )
                    )

                await run_in_terminal(write)
            self.cursor = sid

    async def follow(self) -> None:
        initialized = False
        reconnecting = False
        delay = 0.5
        refresh_at = 0.0
        last_gap = None
        while True:
            try:
                if (
                    not initialized
                    or reconnecting
                    or asyncio.get_running_loop().time() >= refresh_at
                ):
                    self.agents = set(await self.client.agents())
                    refresh_at = asyncio.get_running_loop().time() + 10
                if not initialized:
                    entries = await self.client.history(self.group)
                    if len(entries) == 200:
                        await self.print_notice(
                            "Showing the latest 200 retained messages"
                        )
                    await self.show(entries)
                    initialized = True
                else:
                    gap = await self.client.check_cursor(self.group, self.cursor)
                    if gap and gap != last_gap:
                        await self.print_notice(gap)
                    last_gap = gap
                    await self.show(
                        await self.client.read(self.group, after=self.cursor)
                    )
                if reconnecting or self.status == "Connecting…":
                    self.status = "Connected"
                    self.invalidate()
                reconnecting, delay = False, 0.5
            except ValkeyError:
                if not reconnecting:
                    await self.print_notice(
                        "Disconnected; reconnecting. Your draft is retained."
                    )
                self.status = "Reconnecting…"
                self.invalidate()
                reconnecting = True
                delay = min(delay * 2, 5)
            except MessagingError as exc:
                self.status = str(exc)
                self.invalidate()
                await self.print_notice(self.status)
                return
            await asyncio.sleep(delay)

    async def run(self) -> None:
        await self.app.run_async(
            pre_run=lambda: self.app.create_background_task(self.follow())
        )
        self.save_draft()
