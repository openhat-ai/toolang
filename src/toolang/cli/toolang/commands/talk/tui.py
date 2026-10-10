"""Live input and retained/live messages on the terminal's normal screen."""

import asyncio
import os
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, HorizontalAlign, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from rich.text import Text

from toolang.cli.common.console import terminal_console
from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from toolang.cli.common.execution_progress.formatting import truncate
from toolang.cli.common.input import InputBox
from toolang.cli.common.input_history import InputHistoryStore
from toolang.cli.common.scrollback import ScrollbackRenderer
from toolang.cli.common.status import error_status_line
from toolang.cli.common.terminal_surfaces import TerminalSurfaces
from toolang.common.files import atomic_write_text
from toolang.teaming.client import HubClient
from toolang.teaming.errors import BackendUnavailable, MessagingError
from toolang.teaming.schemas import Conversation, Message, target

from .rendering import display_text, message_block
from .status import conversation_label, conversation_status, status_line


class TalkTui:
    def __init__(
        self,
        client: HubClient,
        conversation: Conversation,
        human: str,
        state: Path,
        surfaces: TerminalSurfaces,
        *,
        read_only: bool,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
        selection: str | None = None,
    ):
        self.client, self.conversation_id, self.human = client, conversation.id, human
        self.surfaces = surfaces
        self.read_only = read_only
        self.conversation = conversation
        self.selection = selection
        self.team: list[dict[str, Any]] = []
        self.max_width = max_width
        self.draft = state / "draft.txt"
        self._draft_loaded = False
        self.connection = "Connecting…"
        self.status = ""
        self.pending = False
        self.cursor = "0-0"
        self.prompt = InputBox(
            self.invalidate,
            placeholder="write a message",
            history_store=InputHistoryStore(state / "input.jsonl"),
            on_input=self.save_draft,
            normalize=lambda text: text,
            get_max_rows=lambda: max(
                3, self.app.output.get_size().rows - 1 - self._input_gap_rows()
            ),
            get_width=self.content_width,
        )
        self.restore_draft()
        keys = KeyBindings()
        focus = has_focus(self.prompt.buffer) & Condition(lambda: not self.read_only)

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
        @keys.add(
            "c-d",
            filter=Condition(lambda: self.read_only or not self.prompt.has_input()),
        )
        def quit_app(_event: Any) -> None:
            self.app.exit()

        @keys.add("c-l")
        def clear(_event: Any) -> None:
            self.app.renderer.clear()

        self.footer = Window(
            FormattedTextControl(self.status_text, focusable=True),
            height=1,
            wrap_lines=False,
        )
        composer = ConditionalContainer(
            HSplit([Window(height=self._input_gap_rows), self.prompt.container()]),
            filter=Condition(lambda: not self.read_only),
        )
        self.app: Application[None] = Application(
            layout=Layout(
                VSplit(
                    [
                        HSplit(
                            [composer, self.footer],
                            width=self.content_width,
                        ),
                    ],
                    align=HorizontalAlign.LEFT,
                ),
                focused_element=self.footer if read_only else self.prompt.buffer,
            ),
            key_bindings=keys,
            full_screen=False,
            erase_when_done=True,
            mouse_support=False,
            refresh_interval=0.5,
            style=Style.from_dict(
                {
                    "input": f"bg:{surfaces.input_background}",
                    "input.placeholder": "dim",
                    "control.run": "bg:ansibrightcyan",
                    "status": "nodim",
                    "status.error.marker": "fg:ansired",
                    "status.error": "fg:ansired",
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

    def content_width(self) -> int:
        return max(1, min(self.app.output.get_size().columns, self.max_width))

    def _input_gap_rows(self) -> int:
        # Keep the three-row input and footer usable in very short terminals.
        return int(self.app.output.get_size().rows >= 5)

    def status_text(self) -> StyleAndTextTuples:
        error = self.status
        if not error and self.connection not in {"Connected", "Connecting…"}:
            error = self.connection
        if error:
            return [*error_status_line(display_text(error), width=self.content_width())]
        right = (
            target(self.human).name
            if self.connection == "Connected"
            else self.connection
        )
        return status_line(
            conversation_status(self.conversation, self.human),
            right,
            center=self.conversation_id,
            width=self.content_width(),
        )

    def save_draft(self) -> None:
        if self.read_only:
            return
        try:
            atomic_write_text(self.draft, self.prompt.buffer.text)
        except OSError as exc:
            self.status = f"Draft could not be saved: {exc}"
        self.invalidate()

    def restore_draft(self) -> None:
        if self.read_only or self._draft_loaded:
            return
        if self.draft.exists():
            self.prompt.replace_input(self.draft.read_text(encoding="utf-8"))
        self._draft_loaded = True

    def update_membership(self, info: Conversation) -> None:
        read_only = not info.allows_sender(self.human)
        if read_only == self.read_only:
            return
        self.save_draft()
        self.read_only = read_only
        if not read_only:
            self.restore_draft()
        self.app.layout.focus(self.footer if read_only else self.prompt.buffer)

    async def send(self, body: str) -> None:
        if self.read_only:
            return
        self.status = ""
        self.invalidate()
        try:
            await self.client.send(
                self.conversation_id,
                body=body,
                participants=list(self.conversation.participants)
                if self.selection
                else None,
            )
        except MessagingError as exc:
            self.status = str(exc)
            await self.print_notice(str(exc))
        else:
            self.selection = None
            self.connection = "Connected"
            self.prompt.accept_submission(body)
            self.save_draft()
        finally:
            self.pending = False
            self.invalidate()

    async def print_notice(self, text: str) -> None:
        await run_in_terminal(
            lambda: terminal_console(width=self.content_width()).print(
                Text(display_text(text), style="yellow")
            )
        )

    async def show(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        if not entries:
            return

        def write() -> None:
            width = self.content_width()
            console = terminal_console(width=width)
            for sid, fields in entries:
                try:
                    message = Message.decode(fields["data"])
                except (KeyError, MessagingError):
                    console.print(
                        Text(f"Skipped malformed message at {sid}", style="yellow")
                    )
                else:
                    console.print(
                        message_block(message, self.human, width, self.surfaces)
                    )

        await run_in_terminal(write)
        self.cursor = entries[-1][0]

    async def follow(self) -> None:
        initialized = False
        delay = 0.5
        last_gap = None
        while True:
            try:
                self.team = await self.client.team()
                if self.selection:
                    resolved = await self.client.resolve(self.selection)
                    if not resolved.exists:
                        self.connection = "Connected"
                        self.invalidate()
                        await asyncio.sleep(0.5)
                        continue
                    self.selection = None
                info = await self.client.conversation(self.conversation_id)
                if info != self.conversation:
                    self.update_membership(info)
                    self.conversation = info
                    self.write_title(conversation_label(info, self.human))
                    self.invalidate()
                if not initialized:
                    entries = await self.client.history(self.conversation_id)
                    if len(entries) == 200:
                        await self.print_notice(
                            "Showing the latest 200 retained messages"
                        )
                    await self.show(entries)
                    initialized = True
                else:
                    gap = await self.client.check_cursor(
                        self.conversation_id, self.cursor
                    )
                    if gap and gap != last_gap:
                        await self.print_notice(gap)
                    last_gap = gap
                    await self.show(
                        await self.client.read(self.conversation_id, after=self.cursor)
                    )
                if self.connection != "Connected":
                    self.connection = "Connected"
                    self.invalidate()
                delay = 0.5
            except BackendUnavailable:
                self.connection = "Reconnecting…"
                self.invalidate()
                delay = min(delay * 2, 5)
            except MessagingError as exc:
                self.connection = "Stopped"
                self.status = str(exc)
                self.invalidate()
                await self.print_notice(self.status)
                return
            await asyncio.sleep(delay)

    async def run(self) -> None:
        title_written = self.write_title(
            conversation_label(self.conversation, self.human)
        )
        try:
            await self.app.run_async(
                pre_run=lambda: self.app.create_background_task(self.follow())
            )
        finally:
            if title_written:
                self.write_title("")
            self.save_draft()

    def write_title(self, title: str) -> bool:
        try:
            if not (
                os.isatty(self.app.output.fileno())
                and os.isatty(self.app.input.fileno())
            ):
                return False
            title = truncate(" ".join(display_text(title).split()), 120)
            # OSC 0 updates iTerm2 tabs/windows and tmux pane_title, as Chat does.
            self.app.output.write_raw(f"\x1b]0;{title}\x07")
            self.app.output.flush()
        except (OSError, ValueError, NotImplementedError):
            return False
        return True
