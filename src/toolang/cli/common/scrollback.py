"""Renderer for a live input beneath finalized terminal scrollback."""

import asyncio
from collections import deque
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout import Layout
from prompt_toolkit.renderer import CPR_Support, Renderer


class ScrollbackRenderer(Renderer):
    """Keep cursor offsets and height reports tied to the current live origin."""

    def reset(self, _scroll: bool = False, leave_alternate_screen: bool = True) -> None:
        super().reset(_scroll=_scroll, leave_alternate_screen=leave_alternate_screen)
        if not hasattr(self, "_cpr_requests"):
            self._cpr_requests: deque[asyncio.Future[None]] = deque()
        self._stale_cpr_requests = set(self._cpr_requests)

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

    def render(self, app: Application, layout: Layout, is_done: bool = False) -> None:
        self._reflow_cursor()
        super().render(app, layout, is_done=is_done)

    def erase(self, leave_alternate_screen: bool = True) -> None:
        self._reflow_cursor()
        super().erase(leave_alternate_screen=leave_alternate_screen)

    def _reflow_cursor(self) -> None:
        previous_size = self._last_size
        screen = self.last_rendered_screen
        styled = self._style_string_has_style
        columns = max(1, self.output.get_size().columns)
        if (
            previous_size is not None
            and columns < previous_size.columns
            and screen is not None
            and styled is not None
        ):
            # Reflow happens before SIGWINCH. Painted padding counts as content,
            # so even an empty Input row can now occupy several terminal rows.
            # Correct the old cursor offset before prompt-toolkit erases from
            # the live origin; changing the new layout alone leaves stale rows.
            cursor = self._cursor_pos
            rows = 0
            for row_index in range(cursor.y):
                used = max(
                    (
                        column + max(1, cell.width)
                        for column, cell in screen.data_buffer[row_index].items()
                        if cell.char != " " or styled[cell.style]
                    ),
                    default=0,
                )
                used = min(used, previous_size.columns)
                rows += max(1, (used + columns - 1) // columns)
            self._cursor_pos = Point(x=cursor.x % columns, y=rows + cursor.x // columns)
