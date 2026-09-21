"""Terminal title publication without metadata or UI-thread lookups."""

from __future__ import annotations

from io import StringIO
from queue import Queue
from threading import Event

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output.vt100 import Vt100_Output
from wcwidth import wcswidth

from toolang.cli.toolang.commands.chat.base import ThreadTitle
from toolang.cli.toolang.commands.chat.title import ChatTitle, clip_title


def output(stream: StringIO) -> Vt100_Output:
    return Vt100_Output(stream, lambda: Size(24, 80), term="xterm")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("hello world", "hello world"),
        ("  hello \n\t world  ", "hello world"),
        ("\x1bhello\x07\x00\x7f\x80\x9d\x9c", "hello"),
        ("", ""),
        ("x" * 60, "x" * 60),
    ],
)
def test_title_is_a_safe_single_line(text: str, expected: str) -> None:
    assert clip_title(text) == expected


def test_title_clips_display_columns() -> None:
    title = clip_title("整理思路" * 40)
    assert title.endswith("…")
    assert wcswidth(title) <= 60
    assert clip_title("long", width=1) == "…"
    assert clip_title("long", width=0) == ""


def test_title_updates_tab_and_window_with_osc_0_until_exit() -> None:
    stream = StringIO()
    results: Queue[ThreadTitle] = Queue()
    lookups: list[str] = []

    def lookup(thread_id: str) -> str:
        lookups.append(thread_id)
        return "修复登录问题"

    title = ChatTitle(
        output=output(stream), enabled=True, lookup=lookup, on_result=results.put
    )
    title.start(None)
    title.set_thread("term_x")
    title.refresh()
    title.accept(results.get(timeout=5))
    title.refresh()
    title.set_thread("term_x")
    title.clear()
    title.clear()
    title.refresh()
    assert stream.getvalue() == (
        "\x1b]0;[new chat]\x07\x1b]0;修复登录问题\x07\x1b]0;\x07"
    )
    assert lookups == ["term_x"]
    assert results.empty()


def test_non_tty_title_does_not_write_or_lookup() -> None:
    stream = StringIO()

    def forbidden(*_args: object) -> None:
        raise AssertionError("non-TTY output must not request terminal titles")

    title = ChatTitle(
        output=output(stream), enabled=False, lookup=forbidden, on_result=forbidden
    )
    title.start("term_x")
    title.set_thread("term_y")
    title.refresh()
    title.clear()
    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "failed_lookup", [None, "", "\x07\x1b", RuntimeError("unavailable")]
)
def test_title_retries_at_next_lifecycle_event(
    failed_lookup: str | Exception | None,
) -> None:
    stream = StringIO()
    results: Queue[ThreadTitle] = Queue()
    attempts = [failed_lookup, "ready"]

    def lookup(_thread: str) -> str | None:
        result = attempts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    title = ChatTitle(
        output=output(stream), enabled=True, lookup=lookup, on_result=results.put
    )
    title.start("term_x")
    title.accept(results.get(timeout=5))
    assert stream.getvalue() == "\x1b]0;[new chat]\x07"
    title.refresh()
    title.accept(results.get(timeout=5))
    assert stream.getvalue().endswith("\x1b]0;ready\x07")
    title.clear()


def test_refresh_during_a_pending_lookup_retries_after_empty_result() -> None:
    stream = StringIO()
    results: Queue[ThreadTitle] = Queue()
    entered, released = Event(), Event()
    attempts: list[str] = []

    def lookup(thread_id: str) -> str | None:
        attempts.append(thread_id)
        if len(attempts) == 1:
            entered.set()
            assert released.wait(5)
            return None
        return "accepted"

    title = ChatTitle(
        output=output(stream), enabled=True, lookup=lookup, on_result=results.put
    )
    try:
        title.start("term_x")
        assert entered.wait(5)
        title.refresh()
        released.set()
        title.accept(results.get(timeout=5))
        title.accept(results.get(timeout=5))
        assert attempts == ["term_x", "term_x"]
        assert stream.getvalue().endswith("\x1b]0;accepted\x07")
    finally:
        released.set()
        title.clear()


def test_obsolete_and_closed_title_results_are_ignored() -> None:
    stream = StringIO()
    results: Queue[ThreadTitle] = Queue()
    title = ChatTitle(
        output=output(stream),
        enabled=True,
        lookup=lambda _: "old",
        on_result=results.put,
    )
    title.start("term_x")
    old = results.get(timeout=5)
    title.set_thread("term_y")
    title.accept(old)
    # Returning to the same id must not accept an earlier generation either.
    title.set_thread("term_x")
    title.accept(old)
    assert "old" not in stream.getvalue()
    title.refresh()
    latest = results.get(timeout=5)
    title.clear()
    before = stream.getvalue()
    title.accept(latest)
    assert stream.getvalue() == before


def test_terminal_write_failure_does_not_prevent_retry() -> None:
    class FailingStream(StringIO):
        fails = True

        def write(self, value: str) -> int:
            if self.fails:
                raise OSError("terminal unavailable")
            return super().write(value)

    stream = FailingStream()
    results: Queue[ThreadTitle] = Queue()
    title = ChatTitle(
        output=output(stream),
        enabled=True,
        lookup=lambda _: "ready",
        on_result=results.put,
    )
    title.start("term_x")
    title.accept(results.get(timeout=5))
    stream.fails = False
    title.refresh()
    title.accept(results.get(timeout=5))
    assert stream.getvalue().endswith("\x1b]0;ready\x07")
    title.clear()
