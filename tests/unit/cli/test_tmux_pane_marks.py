"""Tmux marks: detection, clipping, scopes, and the chat mark lifecycle."""

from __future__ import annotations

from threading import Event
from typing import Any, cast

import pytest

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


def test_resolve_marks_without_tmux_reads_no_pane() -> None:
    assert tmux.resolve_marks(environment={}, pane_factory=_boom) is None


def test_resolve_marks_without_a_pane_variable_reads_no_pane() -> None:
    assert (
        tmux.resolve_marks(environment={"TMUX": TMUX_ENV["TMUX"]}, pane_factory=_boom)
        is None
    )


def test_resolve_marks_honours_the_disable_switch() -> None:
    environment = {**TMUX_ENV, tmux.ENABLED_ENV: "0"}

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
    assert tmux.MARK_THREAD in tmux.WINDOW_MARKS
    assert tmux.WINDOW_MARKS == {"@toolang_thread"}
    assert tmux.MARK_AGENT not in tmux.PANE_MARKS | tmux.WINDOW_MARKS


def test_marks_write_once_per_value_at_its_own_scope() -> None:
    pane = RecordingPane()
    marks = _marks(pane)

    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.set(tmux.MARK_THREAD, "")
    marks.set(tmux.MARK_THREAD, "term_x")

    assert pane.writes == [(tmux.MARK_PAD, tmux.PAD_CHAT)]
    assert pane.window.writes == [(tmux.MARK_THREAD, "term_x")]


def test_marks_clear_only_what_they_wrote_at_its_scope() -> None:
    pane = RecordingPane()
    marks = _marks(pane)

    marks.set(tmux.MARK_PAD, tmux.PAD_CHAT)
    marks.set(tmux.MARK_THREAD, "term_x")
    marks.clear(tmux.MARK_PAD, tmux.MARK_THREAD)

    assert pane.writes == [(tmux.MARK_PAD, tmux.PAD_CHAT)]
    assert pane.unset == [tmux.MARK_PAD]
    assert pane.window.unset == [tmux.MARK_THREAD]


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


def test_chat_marks_publish_identity_and_clear_only_the_pad() -> None:
    pane = RecordingPane()
    marks = ChatMarks(marks=_marks(pane))
    marks.start("term_x")
    marks.set_thread("term_x")
    assert marks.active
    assert marks.thread_id == "term_x"
    assert pane.writes == [("@toolang_pad", "chat")]
    assert pane.window.writes == [("@toolang_thread", "term_x")]
    assert pane.window.renames == ["term_x"]
    marks.clear()
    marks.clear()
    marks.set_thread("term_after_exit")
    assert pane.unset == ["@toolang_pad"]
    assert pane.window.unset == []
    assert pane.window.writes == [("@toolang_thread", "term_x")]
    assert marks.thread_id is None


def test_chat_marks_publish_thread_identity_only_when_it_exists() -> None:
    pane = RecordingPane()
    marks = ChatMarks(marks=_marks(pane))
    marks.start(None)
    assert pane.writes == [("@toolang_pad", "chat")]
    assert pane.window.writes == []
    marks.set_thread("term_new")
    marks.set_thread("term_other")
    assert pane.window.writes == [
        ("@toolang_thread", "term_new"),
        ("@toolang_thread", "term_other"),
    ]
    assert pane.window.renames == ["term_new", "term_other"]
    marks.clear()


def test_disabled_chat_marks_do_nothing() -> None:
    marks = ChatMarks.disabled()
    marks.start_background()
    marks.start("term_x")
    marks.set_thread("term_y")
    marks.clear()
    assert marks.active is False
    assert marks.thread_id is None


@pytest.mark.parametrize("value", ("1", "true", "yes"))
def test_tmux_enabled_accepts_truthy_values(value: str) -> None:
    environment: dict[str, Any] = {tmux.ENABLED_ENV: value}

    assert tmux.tmux_enabled(environment) is True


@pytest.mark.parametrize("value", ("0", "false", "no", "off", "OFF"))
def test_tmux_enabled_rejects_falsy_values(value: str) -> None:
    environment: dict[str, Any] = {tmux.ENABLED_ENV: value}

    assert tmux.tmux_enabled(environment) is False


def test_old_tmux_switch_is_not_supported() -> None:
    assert tmux.tmux_enabled({"TOOLANG_TMUX_MARKS": "0"})
    assert not tmux.tmux_enabled({"TOOLANG_TMUX": "0", "TOOLANG_TMUX_MARKS": "1"})


def test_background_metadata_cleanup_follows_an_inflight_write() -> None:
    entered, released, cleaned = Event(), Event(), Event()
    writes: list[tuple[str, str]] = []

    def set_pane(name: str, value: str) -> None:
        entered.set()
        assert released.wait(5)
        writes.append((name, value))

    def unset_pane(name: str) -> None:
        writes.append((name, "unset"))
        cleaned.set()

    marks = ChatMarks(
        marks=tmux.Marks(
            pane_id="%1",
            window_id="@1",
            _set_pane=set_pane,
            _set_window=lambda name, value: writes.append((name, value)),
            _unset_pane=unset_pane,
        )
    )
    try:
        marks.start_background()
        marks.start("term_x")
        assert entered.wait(5)
        marks.set_thread("term_y")
        # Exit is bounded even though the worker cannot finish this write yet.
        marks.clear()
        assert not cleaned.is_set()
        marks.start("term_after_exit")
        marks.set_thread("term_after_exit")
    finally:
        released.set()
        marks.clear()
    assert cleaned.wait(5)
    assert writes == [("@toolang_pad", "chat"), ("@toolang_pad", "unset")]


@pytest.mark.parametrize(
    "input_tty, output_tty", [(False, False), (True, False), (False, True)]
)
def test_scripted_chat_never_publishes_metadata(
    monkeypatch: Any, input_tty: bool, output_tty: bool, capsys: Any
) -> None:
    """Redirected input or output must bypass tmux even inside a pane."""

    from contextlib import contextmanager

    from toolang.cli.toolang.commands.chat import main
    from toolang.execution.types import SessionSetting

    class Client:
        def initial_setting(self) -> SessionSetting:
            return SessionSetting(model=None, runnable=None)

        def thread_title(self, thread_id: str) -> str | None:
            raise AssertionError("scripted chat must not read terminal titles")

    class Layout:
        name = "c"

    @contextmanager
    def runtime(*_args: Any, **_kwargs: Any) -> Any:
        yield Client()

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(main, "context_layout", lambda _ctx: Layout())
    monkeypatch.setattr(main, "_chat_runtime", runtime)
    monkeypatch.setenv("TMUX", TMUX_ENV["TMUX"])
    monkeypatch.setenv("TMUX_PANE", TMUX_ENV["TMUX_PANE"])

    def no_marks(**_kwargs: Any) -> None:
        raise AssertionError("scripted chat must not resolve tmux marks")

    monkeypatch.setattr(main, "resolve_marks", no_marks)
    monkeypatch.setattr(main, "load_runtime_environ", lambda *_a, **_k: {})
    monkeypatch.setattr(main, "resolve_progress_max_width", lambda _environ: 80)
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: input_tty)
    monkeypatch.setattr(main.sys.stdout, "isatty", lambda: output_tty)
    monkeypatch.setattr("builtins.input", end_input)

    main._chat_interactive(cast(Any, None), thread_id="term_x")

    assert "\x1b]" not in capsys.readouterr().out
