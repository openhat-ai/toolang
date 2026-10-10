"""Renderer for a live input beneath finalized terminal scrollback."""

import asyncio
from collections import deque
from collections.abc import Mapping
from typing import Any, cast
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.output import Output
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.renderer import CPR_Support, Renderer
from prompt_toolkit.styles import Attrs


def _can_erase_space(attrs: Attrs) -> bool:
    return not any(
        (
            attrs.color,
            attrs.bold,
            attrs.underline,
            attrs.strike,
            attrs.italic,
            attrs.blink,
            attrs.reverse,
            attrs.hidden,
            attrs.dim,
        )
    )


def _rewrapped_position(
    row: Mapping[int, Char], used: int, columns: int, offset: int | None = None
) -> Point:
    end = used if offset is None else min(offset, used)
    column = x = y = 0
    while column < end:
        cell = row.get(column)
        width = max(1, cell.width) if cell is not None else 1
        if x + width > columns:
            x = 0
            y += 1
        x += width
        column += width
    next_cell = row.get(column)
    next_width = max(1, next_cell.width) if next_cell is not None else 1
    if offset is not None and offset < used and x + next_width > columns:
        x = 0
        y += 1
    return Point(x=x, y=y)


class _PaddingOutput:
    """Use erase-character for trailing background padding."""

    def __init__(self, output: Output) -> None:
        self.output = output
        self._size = output.get_size()
        self._pending: list[tuple[str, Any]] = []
        self._erase_spaces = True

    def get_size(self) -> Size:
        return self._size

    def _flush_spaces(self, *, erase: bool) -> None:
        pending, self._pending = self._pending, []
        for operation, value in pending:
            if operation == "spaces":
                text, erasable = value
                if erase and erasable:
                    self.output.write_raw(f"\x1b[{len(text)}X")
                    self.output.cursor_forward(len(text))
                else:
                    self.output.write(text)
            elif operation == "attributes":
                self.output.set_attributes(*value)
            else:
                self.output.reset_attributes()

    def write(self, data: str) -> None:
        if data and data.strip(" ") == "":
            if self._pending and self._pending[-1][0] == "spaces":
                text, erasable = self._pending[-1][1]
                self._pending[-1] = ("spaces", (text + data, erasable))
            else:
                self._pending.append(("spaces", (data, self._erase_spaces)))
        else:
            # Preserve literal spaces inside text, including drafts and labels.
            self._flush_spaces(erase="\n" in data or "\r" in data)
            self.output.write(data)

    def set_attributes(self, attrs: Attrs, color_depth: ColorDepth) -> None:
        if self._pending:
            self._pending.append(("attributes", (attrs, color_depth)))
        else:
            self.output.set_attributes(attrs, color_depth)
        self._erase_spaces = _can_erase_space(attrs)

    def reset_attributes(self) -> None:
        if self._pending:
            self._pending.append(("reset", None))
        else:
            self.output.reset_attributes()
        self._erase_spaces = True

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.output, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            self._flush_spaces(
                erase=name not in {"cursor_forward", "cursor_backward", "write_raw"}
            )
            return attribute(*args, **kwargs)

        return call


class ScrollbackApplication(Application[None]):
    """Let the renderer handle size changes, including refresh-before-SIGWINCH."""

    def _on_resize(self) -> None:
        self._redraw()


class ScrollbackRenderer(Renderer):
    """Keep cursor offsets and height reports tied to the current live origin."""

    def clear(self) -> None:
        """Preserve visible history and redraw the live area at the screen top."""
        self.erase()
        output = self.output
        # Scroll the cleared live origin into history as one separator row.
        output.write_raw("\r\n" * output.get_size().rows)
        output.erase_screen()
        output.cursor_goto(0, 0)
        output.flush()
        self.request_absolute_cursor_position()

    def reset(self, _scroll: bool = False, leave_alternate_screen: bool = True) -> None:
        super().reset(_scroll=_scroll, leave_alternate_screen=leave_alternate_screen)
        if not hasattr(self, "_cpr_requests"):
            self._cpr_requests: deque[asyncio.Future[None]] = deque()
        self._stale_cpr_requests = set(self._cpr_requests)
        self._resize_bottom_gap: int | None = None
        self._resize_reported = False
        self._last_content_height = 0

    def request_absolute_cursor_position(self) -> None:
        # CPR has no request IDs. After a timeout, sending more queries would
        # make a missing old reply indistinguishable from a fresh one. Render
        # without CPR until those replies drain, without adding more waiters.
        if any(
            request.done() or request not in self._waiting_for_cpr_futures
            for request in self._cpr_requests
        ):
            return
        pending_count = len(self._waiting_for_cpr_futures)
        super().request_absolute_cursor_position()
        if len(self._waiting_for_cpr_futures) > pending_count:
            # Unlike the base wait queue, this FIFO survives timeouts. Each
            # query still owns one reply even after its future is cancelled.
            self._cpr_requests.append(self._waiting_for_cpr_futures[-1])
        elif self._min_available_height:
            self._min_available_height = min(
                self._min_available_height, self.output.get_size().rows
            )
            self._resize_reported = self._resize_bottom_gap is not None

    def report_absolute_cursor_row(self, row: int) -> None:
        if not self._cpr_requests:
            return
        request = self._cpr_requests.popleft()
        self.cpr_support = CPR_Support.SUPPORTED
        pending = request in self._waiting_for_cpr_futures
        current = (
            pending and not request.done() and request not in self._stale_cpr_requests
        )
        self._stale_cpr_requests.discard(request)
        if pending:
            self._waiting_for_cpr_futures.remove(request)
        if not request.done():
            request.set_result(None)
        if current:
            self._min_available_height = self.output.get_size().rows - row + 1
            self._resize_reported = self._resize_bottom_gap is not None
            if self._resize_reported:
                get_app().invalidate()

    def render(self, app: Application, layout: Layout, is_done: bool = False) -> None:
        output, app_output = self.output, app.output
        # Width callbacks and the renderer must see the same size throughout
        # a frame, even when the terminal changes again during layout.
        self.output = app.output = cast(Output, _PaddingOutput(output))
        try:
            size = self.output.get_size()
            if self._last_size is not None and self._last_size != size:
                self.erase(leave_alternate_screen=False)
                self.request_absolute_cursor_position()
            if self._resize_reported:
                self._realign_after_resize(layout)
            content_height = min(
                size.rows,
                layout.container.preferred_height(size.columns, size.rows).preferred,
            )
            super().render(app, layout, is_done=is_done)
            if not is_done:
                self._last_content_height = content_height
        finally:
            self.output = output
            app.output = app_output

    def erase(self, leave_alternate_screen: bool = True) -> None:
        bottom_gap = self._resize_bottom_gap if not leave_alternate_screen else None
        screen = self.last_rendered_screen
        if self._last_size is not None and self._last_size != self.output.get_size():
            bottom_gap = self._resize_bottom_gap
            if bottom_gap is None and screen is not None:
                bottom_gap = max(
                    0,
                    max(self._min_available_height, screen.height)
                    - self._last_content_height,
                )
        self._reflow_cursor()
        self.output.write("\r")
        self._cursor_pos = Point(x=0, y=self._cursor_pos.y)
        super().erase(leave_alternate_screen=leave_alternate_screen)
        self._resize_bottom_gap = bottom_gap

    def _realign_after_resize(self, layout: Layout) -> None:
        # Erasing reflowed rows leaves extra room below the old live origin.
        # Move the origin through that room instead of retaining it as canvas
        # height, which otherwise accumulates across resize cycles.
        size = self.output.get_size()
        height = layout.container.preferred_height(size.columns, size.rows).preferred
        available = self._min_available_height
        padding = max(0, available - (self._resize_bottom_gap or 0) - height)
        self._resize_bottom_gap = None
        self._resize_reported = False
        if padding:
            super().erase(leave_alternate_screen=False)
            self.output.cursor_down(padding)
            self._min_available_height = available - padding

    def _reflow_cursor(self) -> None:
        previous_size = self._last_size
        screen = self.last_rendered_screen
        attrs = self._attrs_for_style
        columns = max(1, self.output.get_size().columns)
        if (
            previous_size is not None
            and columns < previous_size.columns
            and screen is not None
            and attrs is not None
        ):
            # Reflow happens before SIGWINCH. Erased background padding is not
            # text; count only written cells when recovering the live origin.
            cursor = self._cursor_pos
            rows = 0
            for row_index in range(cursor.y + 1):
                row = screen.data_buffer[row_index]
                used = min(
                    previous_size.columns,
                    max(
                        (
                            column + max(1, cell.width)
                            for column, cell in row.items()
                            if cell.char != " "
                            or not _can_erase_space(attrs[cell.style])
                        ),
                        default=0,
                    ),
                )
                point = _rewrapped_position(
                    row, used, columns, cursor.x if row_index == cursor.y else None
                )
                if row_index == cursor.y:
                    self._cursor_pos = Point(x=point.x, y=rows + point.y)
                else:
                    rows += point.y + 1
