"""Tmux marks: detection, clipping, scopes, and the chat mark lifecycle."""

from __future__ import annotations

from typing import Any, cast

import pytest
from wcwidth import wcswidth

from toolang.cli.common import tmux
from toolang.cli.toolang.commands.chat.marks import ChatMarks

TMUX_ENV = {"TMUX": "/tmp/tmux-1/sock,1,0", "TMUX_PANE": "%3"}


class RecordingWindow:
    """A window that records what is written to it."""

    def __init__(self, *, window_id: str = "@1") -> None:
        self.window_id = window_id
        self.writes: list[tuple[str, str]] = []
        self.unset: list[str] = []
        self.renames: list[str] = []

    def set_option(self, option: str, value: str) -> object:
        self.writes.append((option, value))
        return self

    def unset_option(self, option: str) -> object:
        self.unset.append(option)
        return self

    def rename_window(self, new_name: str) -> object:
        self.renames.append(new_name)
        return self


class RecordingPane:
    """A pane that records options and its window."""

    def __init__(
        self,
        *,
        pane_id: str = "%3",
        window: RecordingWindow | None = None,
    ) -> None:
        self.pane_id = pane_id
        self.window = RecordingWindow() if window is None else window
        self.writes: list[tuple[str, str]] = []
        self.unset: list[str] = []

    def set_option(self, option: str, value: str) -> object:
        self.writes.append((option, value))
        return self

    def unset_option(self, option: str) -> object:
        self.unset.append(option)
        return self


def _marks(pane: RecordingPane) -> tmux.Marks:
    """The writer ``resolve_marks`` builds for a pane like this one."""

    marks = tmux.resolve_marks(environment=TMUX_ENV, pane_factory=lambda: pane)
    assert marks is not None
    return marks


def _boom() -> tmux.TmuxPane:
    raise AssertionError("pane factory must not be called")


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("hello world", "hello world"),
        ("  hello \n\t world  ", "hello world"),
        ("one\n\ntwo", "one two"),
        ("", ""),
    ),
)
def test_clip_title_collapses_whitespace(text: str, expected: str) -> None:
    assert tmux.clip_title(text) == expected


def test_clip_title_keeps_a_title_that_fits() -> None:
    text = "x" * tmux.TITLE_WIDTH

    assert tmux.clip_title(text) == text


def test_clip_title_counts_wide_characters_as_two_columns() -> None:
    text = "整理思路" * 40

    clipped = tmux.clip_title(text)

    assert clipped.endswith(tmux.ELLIPSIS)
    assert wcswidth(clipped) <= tmux.TITLE_WIDTH


def test_clip_title_handles_a_zero_width() -> None:
    assert tmux.clip_title("anything", width=0) == ""


def test_resolve_marks_without_tmux_reads_no_pane() -> None:
    assert tmux.resolve_marks(environment={}, pane_factory=_boom) is None


def test_resolve_marks_without_a_pane_variable_reads_no_pane() -> None:
    assert (
        tmux.resolve_marks(environment={"TMUX": TMUX_ENV["TMUX"]}, pane_factory=_boom)
        is None
    )


def test_resolve_marks_honours_the_disable_switch() -> None:
    environment = {**TMUX_ENV, tmux.MARKS_ENV: "0"}

    assert tmux.resolve_marks(environment=environment, pane_factory=_boom) is None


def test_resolve_marks_uses_the_running_pane_and_window() -> None:
    pane = RecordingPane()

    marks = _marks(pane)

    assert marks.pane_id == pane.pane_id
    assert marks.window_id == pane.window.window_id


def test_resolve_marks_ignores_a_failing_lookup() -> None:
    def factory() -> tmux.TmuxPane:
        raise RuntimeError("no server running")

    assert tmux.resolve_marks(environment=TMUX_ENV, pane_factory=factory) is None


def test_resolve_marks_ignores_a_window_that_cannot_carry_options() -> None:
    class OddWindow:
        window_id = "@1"

    class OddPane:
        pane_id = "%3"

        def __init__(self) -> None:
            self.window = OddWindow()

        def set_option(self, option: str, value: str) -> object:
            raise AssertionError("must not write")

    resolved = tmux.resolve_marks(
        environment=TMUX_ENV, pane_factory=cast(Any, lambda: OddPane())
    )

    assert resolved is None


def test_resolve_marks_ignores_a_pane_without_an_id() -> None:
    class Anonymous:
        pane_id = ""

        def __init__(self) -> None:
            self.window = RecordingWindow()

        def set_option(self, option: str, value: str) -> object:
            raise AssertionError("must not write")

    assert (
        tmux.resolve_marks(environment=TMUX_ENV, pane_factory=cast(Any, Anonymous))
        is None
    )


def test_mark_scopes_are_disjoint() -> None:
    """Each mark belongs to exactly one scope."""

    assert tmux.MARK_PAD in tmux.PANE_MARKS
    assert tmux.MARK_THREAD_ID in tmux.WINDOW_MARKS
    assert tmux.MARK_THREAD_TITLE in tmux.WINDOW_MARKS
    assert tmux.MARK_AGENT not in tmux.PANE_MARKS | tmux.WINDOW_MARKS


def test_marks_write_once_per_value_at_its_own_scope() -> None:
    pane = RecordingPane()
    marks = _marks(pane)

    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.set(tmux.MARK_THREAD_ID, "")
    marks.set(tmux.MARK_THREAD_ID, "term_x")

    assert pane.writes == [(tmux.MARK_PAD, tmux.PAD_CHAT)]
    assert pane.window.writes == [(tmux.MARK_THREAD_ID, "term_x")]


def test_marks_clear_only_what_they_wrote_at_its_scope() -> None:
    pane = RecordingPane()
    marks = _marks(pane)

    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.set(tmux.MARK_THREAD_ID, "term_x")
    marks.clear(tmux.MARK_PAD, tmux.MARK_THREAD_ID)

    assert pane.writes == [(tmux.MARK_PAD, tmux.PAD_CHAT)]
    assert pane.unset == [tmux.MARK_PAD]
    assert pane.window.unset == [tmux.MARK_THREAD_ID]


def test_marks_name_the_container_window_once_per_name() -> None:
    pane = RecordingPane()
    marks = _marks(pane)

    marks.name_window("term_x")
    marks.name_window("term_x")
    marks.name_window("term_y")

    # a container name outlives chat, so clear leaves it alone
    marks.clear()

    assert pane.window.renames == ["term_x", "term_y"]


def test_marks_reject_a_name_without_a_scope() -> None:
    marks = _marks(RecordingPane())

    with pytest.raises(ValueError):
        marks.set(tmux.MARK_AGENT, "c")


def test_marks_fall_back_to_an_empty_value_without_unset() -> None:
    class MinimalWindow:
        window_id = "@1"

        def __init__(self) -> None:
            self.writes: list[tuple[str, str]] = []

        def set_option(self, option: str, value: str) -> object:
            self.writes.append((option, value))
            return self

    class MinimalPane:
        pane_id = "%3"

        def __init__(self) -> None:
            self.window = MinimalWindow()
            self.writes: list[tuple[str, str]] = []

        def set_option(self, option: str, value: str) -> object:
            self.writes.append((option, value))
            return self

    pane = MinimalPane()
    marks = _marks(cast(Any, pane))

    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.clear(tmux.MARK_PAD)

    assert pane.writes == [(tmux.MARK_PAD, tmux.PAD_CHAT), (tmux.MARK_PAD, "")]
    assert pane.window.writes == []


def test_marks_survive_a_failing_write() -> None:
    attempts: list[tuple[str, str]] = []

    def failing(option: str, value: str) -> None:
        attempts.append((option, value))
        raise OSError("server exited")

    marks = tmux.Marks(
        pane_id="%3",
        window_id="@1",
        _set_pane=failing,
        _set_window=failing,
    )

    # a value that fails to write is not remembered, so the next call retries it
    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)

    assert attempts == [(tmux.MARK_PAD, tmux.PAD_CHAT), (tmux.MARK_PAD, tmux.PAD_CHAT)]


def test_marks_survive_a_failing_clear() -> None:
    attempted: list[str] = []

    def record(option: str, _value: str) -> object:
        return option

    def failing_unset(option: str) -> None:
        attempted.append(option)
        raise OSError("server exited")

    marks = tmux.Marks(
        pane_id="%3",
        window_id="@1",
        _set_pane=record,
        _set_window=record,
        _unset_pane=failing_unset,
        _unset_window=failing_unset,
    )

    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.clear(tmux.MARK_PAD)

    assert attempted == [tmux.MARK_PAD]


def test_chat_marks_publish_pad_thread_and_title() -> None:
    pane = RecordingPane()
    marks = ChatMarks(
        marks=_marks(pane),
        title_lookup=lambda thread_id: "hello world",
    )

    marks.start("term_x")

    assert pane.writes == [(tmux.MARK_PAD, tmux.PAD_CHAT)]
    assert pane.window.writes == [
        (tmux.MARK_THREAD_ID, "term_x"),
        (tmux.MARK_THREAD_TITLE, "hello world"),
    ]
    assert pane.window.renames == ["term_x"]


def test_chat_marks_publish_a_new_thread_id_before_its_title() -> None:
    pane = RecordingPane()
    lookups: list[str] = []

    def lookup(thread_id: str) -> str | None:
        lookups.append(thread_id)
        return "hello world"

    marks = ChatMarks(marks=_marks(pane), title_lookup=lookup)

    marks.start(None)
    assert pane.writes == [(tmux.MARK_PAD, tmux.PAD_CHAT)]
    assert lookups == []

    marks.set_thread("term_new")
    assert pane.window.writes[-1] == (tmux.MARK_THREAD_ID, "term_new")
    assert pane.window.renames == ["term_new"]
    assert lookups == []

    marks.refresh_title()
    assert pane.window.writes[-1] == (tmux.MARK_THREAD_TITLE, "hello world")

    marks.refresh_title()
    assert lookups == ["term_new"]


def test_chat_marks_retry_a_title_that_is_not_there_yet() -> None:
    pane = RecordingPane()
    titles: list[str | None] = [None, "hello world"]
    marks = ChatMarks(
        marks=_marks(pane),
        title_lookup=lambda thread_id: titles.pop(0),
    )

    marks.set_thread("term_new")
    marks.refresh_title()
    marks.refresh_title()

    assert [
        value for name, value in pane.window.writes if name == tmux.MARK_THREAD_TITLE
    ] == ["hello world"]


def test_chat_marks_ignore_a_failing_title_lookup() -> None:
    pane = RecordingPane()

    def lookup(thread_id: str) -> str | None:
        raise RuntimeError("store unavailable")

    marks = ChatMarks(marks=_marks(pane), title_lookup=lookup)

    marks.set_thread("term_x")
    marks.refresh_title()

    assert pane.window.writes == [(tmux.MARK_THREAD_ID, "term_x")]


def test_chat_marks_clear_only_the_pad_on_exit() -> None:
    pane = RecordingPane()
    marks = ChatMarks(marks=_marks(pane), title_lookup=lambda thread_id: "hello world")

    marks.start("term_x")
    marks.clear()

    # the thread marks and the container name outlive chat
    assert pane.unset == [tmux.MARK_PAD]
    assert pane.window.unset == []
    assert pane.window.renames == ["term_x"]


def test_chat_marks_rename_the_container_for_a_new_thread() -> None:
    pane = RecordingPane()
    marks = ChatMarks(marks=_marks(pane), title_lookup=lambda _id: None)

    marks.start("term_x")
    marks.clear()
    marks.set_thread("term_y")

    assert [
        value
        for name, value in pane.window.writes
        if name == tmux.MARK_THREAD_ID and value
    ] == ["term_x", "term_y"]
    assert pane.window.renames == ["term_x", "term_y"]


def test_disabled_chat_marks_do_nothing() -> None:
    marks = ChatMarks.disabled()

    marks.start("term_x")
    marks.set_thread("term_y")
    marks.refresh_title()
    marks.clear()

    assert marks.active is False
    assert marks.thread_id is None


def test_chat_marks_expose_their_state() -> None:
    pane = RecordingPane()
    marks = ChatMarks(marks=_marks(pane), title_lookup=lambda _id: None)

    assert marks.active is True
    marks.set_thread("term_x")

    assert marks.thread_id == "term_x"


@pytest.mark.parametrize("value", ("1", "true", "yes"))
def test_marks_enabled_accepts_truthy_values(value: str) -> None:
    environment: dict[str, Any] = {tmux.MARKS_ENV: value}

    assert tmux.marks_enabled(environment) is True


@pytest.mark.parametrize("value", ("0", "false", "no", "off", "OFF"))
def test_marks_enabled_rejects_falsy_values(value: str) -> None:
    environment: dict[str, Any] = {tmux.MARKS_ENV: value}

    assert tmux.marks_enabled(environment) is False


def test_scripted_chat_publishes_and_clears_the_marks(monkeypatch: Any) -> None:
    """The non-tty chat path marks exactly like the TUI does."""

    from contextlib import contextmanager

    from toolang.cli.toolang.commands.chat import main
    from toolang.execution.types import SessionSetting

    pane = RecordingPane()
    marks = _marks(pane)

    class Client:
        def initial_setting(self) -> SessionSetting:
            return SessionSetting(model=None, runnable=None)

        def thread_title(self, thread_id: str) -> str | None:
            return "hello world"

    class Layout:
        name = "c"

    @contextmanager
    def runtime(*_args: Any, **_kwargs: Any) -> Any:
        yield Client()

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(main, "context_layout", lambda _ctx: Layout())
    monkeypatch.setattr(main, "_chat_runtime", runtime)
    monkeypatch.setattr(main, "resolve_marks", lambda **_kwargs: marks)
    monkeypatch.setattr(main, "load_runtime_environ", lambda *_a, **_k: {})
    monkeypatch.setattr(main, "resolve_progress_max_width", lambda _environ: 80)
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(main.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", end_input)

    main._chat_interactive(cast(Any, None), thread_id="term_x")

    assert pane.writes == [(tmux.MARK_PAD, tmux.PAD_CHAT)]
    assert pane.window.writes == [
        (tmux.MARK_THREAD_ID, "term_x"),
        (tmux.MARK_THREAD_TITLE, "hello world"),
    ]
    assert pane.unset == [tmux.MARK_PAD]
    assert pane.window.unset == []
    assert pane.window.renames == ["term_x"]
