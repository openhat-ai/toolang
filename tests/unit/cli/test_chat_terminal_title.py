"""Chat lookup remains asynchronous and independent of metadata publication."""

from queue import Queue
from threading import Event

import pytest

from toolang.cli.toolang.commands.chat.base import ThreadTitle
from toolang.cli.toolang.commands.chat.title import ChatTitle, chat_label


@pytest.mark.parametrize(
    "agent,title,expected",
    [
        ("gus", "", "gus>"),
        ("gus", "hello world", "gus> hello world"),
        ("alice", " a\n topic ", "alice> a topic"),
        ("alice", " \n\x07 ", "alice>"),
        ("x" * 20, "topic", "x" * 11 + "…> topic"),
        ("\x07", "", "agent>"),
    ],
)
def test_chat_label_marks_input_and_preserves_normalization(
    agent: str, title: str, expected: str
) -> None:
    assert chat_label(agent, title) == expected


@pytest.mark.parametrize("failed", [None, "", "\x07\x1b", RuntimeError("offline")])
def test_lookup_failure_retries_at_normal_event(failed) -> None:
    results: Queue[ThreadTitle] = Queue()
    attempts = [failed, "ready"]
    labels: list[str] = []

    def lookup(_thread):
        result = attempts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def publish(label):
        labels.append(label)
        return True

    title = ChatTitle(
        publish=publish,
        agent="alice",
        enabled=True,
        lookup=lookup,
        on_result=results.put,
    )
    title.start("term_x")
    title.accept(results.get(timeout=5))
    assert labels == ["alice>"]
    title.refresh()
    title.accept(results.get(timeout=5))
    assert labels == ["alice>", "alice> ready"]
    title.refresh()
    assert results.empty()
    title.clear()


def test_pending_and_obsolete_results() -> None:
    results: Queue[ThreadTitle] = Queue()
    entered, released = Event(), Event()
    attempts: list[str] = []
    labels: list[str] = []

    def lookup(thread):
        attempts.append(thread)
        if len(attempts) == 1:
            entered.set()
            assert released.wait(5)
            return None
        return "ready"

    def publish(label):
        labels.append(label)
        return True

    title = ChatTitle(
        publish=publish,
        agent="alice",
        enabled=True,
        lookup=lookup,
        on_result=results.put,
    )
    try:
        title.start("term_x")
        assert entered.wait(5)
        title.refresh()
        released.set()
        title.accept(results.get(timeout=5))
        old = results.get(timeout=5)
        assert labels == ["alice>"]
        labels.clear()
        title.set_thread("term_y")
        title.set_thread("term_x")
        title.accept(old)
        assert not labels
        title.refresh()
        latest = results.get(timeout=5)
        title.clear()
        title.accept(latest)
        assert not labels
    finally:
        released.set()
        title.clear()


def test_failed_publication_retries_and_disabled_lookup_is_silent() -> None:
    results: Queue[ThreadTitle] = Queue()
    writes = []

    def publish(label):
        writes.append(label)
        return len(writes) > 1

    title = ChatTitle(
        publish=publish,
        agent="bob",
        enabled=True,
        lookup=lambda _: "topic",
        on_result=results.put,
    )
    title.start(None)
    title.set_thread("term_x")
    assert not writes  # Identity alone does not update OSC.
    for _ in range(2):
        title.refresh()
        title.accept(results.get(timeout=5))
    assert writes == ["bob> topic", "bob> topic"]
    title.clear()

    def forbidden(*_args):
        raise AssertionError("disabled publication")

    disabled = ChatTitle(
        publish=forbidden,
        agent="bob",
        enabled=False,
        lookup=forbidden,
        on_result=forbidden,
    )
    disabled.start("term_x")
    disabled.refresh()
    disabled.clear()


def test_lookup_worker_start_failure_retries_at_next_normal_event(monkeypatch):
    results: Queue[ThreadTitle] = Queue()
    labels: list[str] = []
    title = ChatTitle(
        publish=lambda label: labels.append(label) or True,
        agent="alice",
        enabled=True,
        lookup=lambda _: "topic",
        on_result=results.put,
    )

    def fail_start(_worker):
        raise RuntimeError("cannot start new thread")

    with monkeypatch.context() as patch:
        patch.setattr(
            "toolang.cli.toolang.commands.chat.title.Thread.start", fail_start
        )
        title.start("term_x")
    assert not labels and results.empty()
    title.refresh()
    title.accept(results.get(timeout=5))
    assert labels == ["alice> topic"]
    title.clear()
