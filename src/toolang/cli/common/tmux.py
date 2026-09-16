"""Publish toolang state as tmux options while chat runs in a pane.

A chat that runs inside a tmux pane records what it is hosting, on the pane and
on its window, so tmux-side views can read it with ``#{@toolang_thread_id}`` and
friends. Both scopes carry the same three values: the pane option describes the
process that owns the pane (it survives a window that holds several panes), and
the window option is the session-level metadata that a window-scoped format or a
launcher reads without resolving the active pane.

The window name and pane title are set too, but only while the window holds a
single pane: tmux renders ``window_name: "pane_title"`` for single-pane windows,
so a chat window reads as ``term_xxx: "hello world"`` with no configuration.

Detection and targeting are delegated to ``libtmux``, which reads ``TMUX`` and
``TMUX_PANE`` from the environment, so this module carries no socket or protocol
code. Every call is best-effort: outside tmux, or when tmux disappears mid
session, callers keep working and nothing reaches the UI.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from wcwidth import wcwidth

MARK_AGENT = "@toolang_agent"
MARK_THREAD_ID = "@toolang_thread_id"
MARK_THREAD_TITLE = "@toolang_thread_title"
MARK_NAMES = (MARK_AGENT, MARK_THREAD_ID, MARK_THREAD_TITLE)

TITLE_WIDTH = 60
ELLIPSIS = "\u2026"

MARKS_ENV = "TOOLANG_TMUX_MARKS"
DEBUG_ENV = "TOOLANG_TMUX_DEBUG"
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

PaneFactory = Callable[[], "TmuxPane"]
OptionWriter = Callable[[str, str], object]
OptionRemover = Callable[[str], object]
TextSetter = Callable[[str], object]


class TmuxWindow(Protocol):
    """The slice of ``libtmux.Window`` this module depends on."""

    @property
    def window_id(self) -> str: ...

    @property
    def window_name(self) -> str: ...

    @property
    def panes(self) -> Sequence[object]: ...

    def set_option(self, option: str, value: str) -> object: ...

    def unset_option(self, option: str) -> object: ...

    def rename_window(self, new_name: str) -> object: ...


class TmuxPane(Protocol):
    """The slice of ``libtmux.Pane`` this module depends on."""

    @property
    def pane_id(self) -> str: ...

    @property
    def window(self) -> TmuxWindow: ...

    def set_option(self, option: str, value: str) -> object: ...

    def unset_option(self, option: str) -> object: ...

    def set_title(self, title: str) -> object: ...


@dataclass(slots=True)
class Marks:
    """Best-effort writer for the toolang marks on one pane and its window.

    Writes are idempotent: a value is sent only when it differs from the last
    value written through this instance, empty values are never written, and a
    failing call is reported only under ``TOOLANG_TMUX_DEBUG``.
    """

    pane_id: str
    window_id: str
    _set_pane: OptionWriter
    _set_window: OptionWriter
    _unset_pane: OptionRemover | None = None
    _unset_window: OptionRemover | None = None
    _rename_window: TextSetter | None = None
    _set_pane_title: TextSetter | None = None
    _window_name: str = ""
    _window_panes: int = 1
    _written: dict[str, str] = field(default_factory=dict)
    _renamed: bool = False
    _title: str = ""

    @property
    def labels_window(self) -> bool:
        """Whether this window can carry a name and a pane title."""

        return self._window_panes == 1

    def set(self, name: str, value: str) -> None:
        if not value or self._written.get(name) == value:
            return
        wrote_pane = self._attempt(name, value, self._set_pane)
        wrote_window = self._attempt(name, value, self._set_window)
        if wrote_pane or wrote_window:
            self._written[name] = value

    def name_window(self, name: str) -> None:
        """Name the window after the thread, once, for single-pane windows."""

        if not name or self._renamed or not self.labels_window:
            return
        rename = self._rename_window
        if rename is None:
            return
        try:
            rename(name)
        except Exception as exc:
            _debug(f"window not renamed: {exc}")
            return
        self._renamed = True

    def title_pane(self, title: str) -> None:
        """Title the pane after the thread for single-pane windows."""

        if not title or title == self._title or not self.labels_window:
            return
        set_title = self._set_pane_title
        if set_title is None:
            return
        try:
            set_title(title)
        except Exception as exc:
            _debug(f"pane title not set: {exc}")
            return
        self._title = title

    def clear(self, *names: str) -> None:
        """Remove the marks this instance wrote and restore the window name."""

        requested = names or tuple(self._written)
        for name in requested:
            if name not in self._written:
                continue
            self._remove(name)
        self._restore_window_name()

    def _attempt(self, name: str, value: str, writer: OptionWriter) -> bool:
        try:
            writer(name, value)
        except Exception as exc:  # best-effort: tmux may be gone or unreachable
            _debug(f"option {name} not written: {exc}")
            return False
        return True

    def _remove(self, name: str) -> None:
        for unset, write in (
            (self._unset_pane, self._set_pane),
            (self._unset_window, self._set_window),
        ):
            try:
                if unset is None:
                    write(name, "")
                else:
                    unset(name)
            except Exception as exc:
                _debug(f"option {name} not cleared: {exc}")
                continue
        del self._written[name]

    def _restore_window_name(self) -> None:
        if not self._renamed:
            return
        self._renamed = False
        rename = self._rename_window
        if rename is None or not self._window_name:
            return
        try:
            rename(self._window_name)
        except Exception as exc:
            _debug(f"window name not restored: {exc}")


def resolve_marks(
    *,
    environment: Mapping[str, str] | None = None,
    pane_factory: PaneFactory | None = None,
) -> Marks | None:
    """Return a writer for the current pane and window, or ``None``.

    ``environment`` and ``pane_factory`` exist for tests and for call sites that
    already hold a resolved environment; the defaults read ``os.environ`` and ask
    ``libtmux`` for the pane the process is running in.
    """

    environ = os.environ if environment is None else environment
    if not marks_enabled(environ):
        return None
    if not environ.get("TMUX") or not environ.get("TMUX_PANE"):
        return None
    factory = pane_factory if pane_factory is not None else _libtmux_pane
    try:
        pane = factory()
    except Exception as exc:
        _debug(f"not publishing marks: {exc}")
        return None
    pane_id = _text(getattr(pane, "pane_id", None))
    if not pane_id:
        _debug("not publishing marks: resolved pane has no id")
        return None
    window = getattr(pane, "window", None)
    return Marks(
        pane_id=pane_id,
        window_id=_text(getattr(window, "window_id", None)),
        _set_pane=pane.set_option,
        _set_window=_window_method(window, "set_option"),
        _unset_pane=_optional(pane, "unset_option"),
        _unset_window=_optional(window, "unset_option"),
        _rename_window=_optional(window, "rename_window"),
        _set_pane_title=_optional(pane, "set_title"),
        _window_name=_text(getattr(window, "window_name", None)),
        _window_panes=len(getattr(window, "panes", ()) or ()),
    )


def marks_enabled(environment: Mapping[str, str]) -> bool:
    """Whether this process should publish the marks."""

    return environment.get(MARKS_ENV, "").strip().casefold() not in _DISABLED_VALUES


def clip_title(text: str, *, width: int = TITLE_WIDTH) -> str:
    """Collapse whitespace and clip one title to ``width`` terminal columns.

    A title can be an entire authored message, so it is reduced to a single line
    before it becomes a tmux option. Clipping counts display columns (CJK counts
    as two) and marks the cut with an ellipsis that fits inside ``width``.
    """

    flat = " ".join(text.split())
    if width <= 0:
        return ""
    if _display_width(flat) <= width:
        return flat
    if width <= _display_width(ELLIPSIS):
        return ELLIPSIS[:width]
    clipped: list[str] = []
    used = 0
    for char in flat:
        step = max(wcwidth(char), 0)
        if used + step > width - _display_width(ELLIPSIS):
            break
        clipped.append(char)
        used += step
    return "".join(clipped).rstrip() + ELLIPSIS


def _window_method(window: object, name: str) -> OptionWriter:
    method = getattr(window, name, None)
    if not callable(method):
        raise ValueError(f"tmux window does not support {name}")
    return method


def _optional(target: object, name: str) -> Any:
    """A lightweight optional callable lookup on a duck-typed tmux object."""

    method = getattr(target, name, None)
    return method if callable(method) else None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _display_width(text: str) -> int:
    return sum(max(wcwidth(char), 0) for char in text)


def _libtmux_pane() -> TmuxPane:
    import libtmux

    return libtmux.Pane.from_env()


def _debug(message: str) -> None:
    value = os.environ.get(DEBUG_ENV, "").strip().casefold()
    if not value or value in _DISABLED_VALUES:
        return
    print(f"toolang: tmux: {message}", file=sys.stderr)
