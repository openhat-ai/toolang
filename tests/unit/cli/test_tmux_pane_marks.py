"""Pane-local identity commits, asynchronous updates, and bounded cleanup."""

from queue import Queue
from threading import Event
from time import monotonic

import pytest

from toolang.cli.common.tmux import FIELDS, PaneMarks

ENV = {"TMUX": "/tmp/test,1,0", "TMUX_PANE": "%2"}


class Pane:
    def __init__(self, *, fail: str = "", blocked: bool = False):
        self.events: Queue[tuple[str, str | None]] = Queue()
        self.fail = fail
        self.blocked = blocked
        self.entered, self.release = Event(), Event()

    def set_option(self, key, value):
        self.entered.set()
        if self.blocked:
            assert self.release.wait(5)
        if key == self.fail:
            raise OSError("unreachable")
        self.events.put((key, value))

    def unset_option(self, key):
        self.events.put((key, None))


def events(pane, count):
    return [pane.events.get(timeout=5) for _ in range(count)]


def test_complete_identity_is_committed_last_and_updates_are_deduplicated():
    pane = Pane()
    marks = PaneMarks(
        environment=ENV,
        fields={"kind": "chat", "root": "/root", "agent": "alice"},
        pane_factory=lambda: pane,
    )
    marks.start()
    initial = events(pane, 8)
    assert initial == [
        ("@toolang_kind", ""),
        ("@toolang_root", "/root"),
        ("@toolang_agent", "alice"),
        ("@toolang_thread", ""),
        ("@toolang_run", ""),
        ("@toolang_convo", ""),
        ("@toolang_context", ""),
        ("@toolang_kind", "chat"),
    ]
    marks.update("thread", "chat_x")
    marks.update("thread", "chat_x")
    marks.update("run", "run_x")
    assert events(pane, 2) == [("@toolang_thread", "chat_x"), ("@toolang_run", "run_x")]
    marks.close()
    marks.close()
    marks.update("thread", "late")
    assert events(pane, 7) == [(f"@toolang_{name}", None) for name in FIELDS]
    assert pane.events.empty()


def test_exit_cancels_queued_writes_and_cleanup_is_bounded():
    pane = Pane(blocked=True)
    marks = PaneMarks(
        environment=ENV, fields={"kind": "chat"}, pane_factory=lambda: pane
    )
    marks.start()
    assert pane.entered.wait(5)
    marks.update("thread", "queued")
    before = monotonic()
    marks.close()
    assert monotonic() - before < 0.5
    pane.release.set()
    assert events(pane, 8) == [
        ("@toolang_kind", ""),
        *[(f"@toolang_{name}", None) for name in FIELDS],
    ]


def test_failure_never_publishes_kind_and_cleanup_still_runs():
    pane = Pane(fail="@toolang_root")
    marks = PaneMarks(
        environment=ENV, fields={"kind": "chat"}, pane_factory=lambda: pane
    )
    marks.start()
    assert events(pane, 8) == [
        ("@toolang_kind", ""),
        *[(f"@toolang_{name}", None) for name in FIELDS],
    ]
    marks.close()


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {**ENV, "TOOLANG_TMUX": "0"},
        {**ENV, "TOOLANG_TMUX": " FaLsE "},
        {**ENV, "TOOLANG_TMUX": "no"},
        {**ENV, "TOOLANG_TMUX": "off"},
    ],
)
def test_disabled_marks_do_not_resolve_a_pane(environment):
    def forbidden():
        raise AssertionError("must not contact tmux")

    marks = PaneMarks(
        environment=environment, fields={"kind": "chat"}, pane_factory=forbidden
    )
    marks.start()
    marks.close()


def test_unreachable_pane_is_nonfatal_and_debug_is_opt_in(capsys):
    def missing():
        raise OSError("server gone")

    for debug in ("", "1"):
        marks = PaneMarks(
            environment={**ENV, "TOOLANG_TMUX_DEBUG": debug},
            fields={"kind": "talk"},
            pane_factory=missing,
        )
        marks.start()
        marks.close()
        assert ("server gone" in capsys.readouterr().err) == bool(debug)


def test_worker_start_failure_preserves_titles_and_cleanup(monkeypatch, capsys):
    from io import StringIO

    from toolang.cli.common.terminal_session import TerminalSession

    def fail_start(_worker):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr("toolang.cli.common.tmux.Thread.start", fail_start)
    pane = Pane()
    output = StringIO()
    marks = PaneMarks(
        environment={**ENV, "TOOLANG_TMUX_DEBUG": "1"},
        fields={"kind": "chat"},
        pane_factory=lambda: pane,
    )
    with TerminalSession(label="alice", enabled=True, stream=output, marks=marks):
        marks.update("thread", "chat_x")
    marks.close()
    assert pane.events.empty()
    assert output.getvalue() == "\x1b]0;alice\x07\x1b]0;\x07"
    assert "cannot start new thread" in capsys.readouterr().err
