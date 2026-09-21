"""Process display titles, scoped to an actual CLI invocation."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
import shlex
from urllib.parse import quote

import setproctitle

_TITLE: ContextVar[str | None] = ContextVar("toolang_process_title", default=None)


@contextmanager
def invocation() -> Iterator[None]:
    """Initialize before environment changes and restore embedded callers."""

    try:
        previous = setproctitle.getproctitle()
    except (OSError, RuntimeError, ValueError):
        previous = None
    token = _TITLE.set("too")
    try:
        yield
    finally:
        _TITLE.reset(token)
        if previous is not None:
            _set(previous)


def select(agent: str | None, args: Sequence[str], *, publish: bool = True) -> None:
    if _TITLE.get() is None:
        return
    label = "too"
    if agent:
        label += ":" + "".join(
            quote(char, safe="")
            if char.isspace() or not char.isprintable() or char == "%"
            else char
            for char in agent
        )
    _TITLE.set(label + (" " + shlex.join(args) if args else ""))
    if publish:
        apply()


def apply() -> None:
    if title := _TITLE.get():
        _set(title)


def _set(title: str) -> None:
    try:
        setproctitle.setproctitle(title)
    except (OSError, RuntimeError, ValueError):
        pass  # Display is independent of execution and process ownership.
