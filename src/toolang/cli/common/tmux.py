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

The same module also decides where a chat run happens: ``Launcher``
resolves the agent's session in the user's server, reuses an open window,
and switches the client, so ``too <agent> chat`` lands where the user
expects it. Placement is best-effort in the same way, and shares the marks
kill switch.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from wcwidth import wcwidth

MARK_AGENT = "@toolang_agent"
MARK_THREAD_ID = "@toolang_thread_id"
MARK_THREAD_TITLE = "@toolang_thread_title"
MARK_NAMES = (MARK_AGENT, MARK_THREAD_ID, MARK_THREAD_TITLE)

# The window/pane marks are mirrored at session scope so the launcher can own a
# session without parsing its name.
SESSION_AGENT = MARK_AGENT
SESSION_NAME_FALLBACK = "agent"
_SESSION_CHARS = re.compile(r"[^a-z0-9-]+")
_SESSION_DASHES = re.compile(r"-{2,}")

TITLE_WIDTH = 60
ELLIPSIS = "\u2026"

# tmux detaches an attached client when the session it is showing goes away.
# A session toolang creates holds only chat windows, so chat exiting would take
# the client with it; the option below keeps the client in tmux instead.
DETACH_ON_DESTROY = "detach-on-destroy"

MARKS_ENV = "TOOLANG_TMUX_MARKS"
DEBUG_ENV = "TOOLANG_TMUX_DEBUG"
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

PaneFactory = Callable[[], "TmuxPane"]
ServerFactory = Callable[[], "TmuxServer"]
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

    @property
    def session(self) -> "TmuxSession": ...

    def show_option(self, option: str) -> Any: ...

    def select(self) -> object: ...

    def kill_window(self) -> object: ...

    def set_option(self, option: str, value: str) -> object: ...

    def unset_option(self, option: str) -> object: ...

    def rename_window(self, new_name: str) -> object: ...


class TmuxPane(Protocol):
    """The slice of ``libtmux.Pane`` this module depends on."""

    @property
    def pane_id(self) -> str: ...

    @property
    def window(self) -> TmuxWindow: ...

    @property
    def session_id(self) -> str: ...

    def set_option(self, option: str, value: str) -> object: ...

    def unset_option(self, option: str) -> object: ...

    def set_title(self, title: str) -> object: ...


class TmuxSession(Protocol):
    """The slice of ``libtmux.Session`` placement depends on."""

    @property
    def session_id(self) -> str: ...

    @property
    def session_name(self) -> str: ...

    @property
    def windows(self) -> Sequence[TmuxWindow]: ...

    @property
    def active_window(self) -> TmuxWindow: ...

    def show_option(self, option: str) -> Any: ...

    def set_option(self, option: str, value: str) -> object: ...

    def new_window(
        self,
        window_name: str | None = ...,
        *,
        start_directory: str | None = ...,
        window_shell: str | None = ...,
    ) -> TmuxWindow: ...


class TmuxServer(Protocol):
    """The slice of ``libtmux.Server`` placement depends on."""

    @property
    def sessions(self) -> Sequence[TmuxSession]: ...

    def new_session(
        self,
        session_name: str,
        *,
        attach: bool = ...,
        start_directory: str | None = ...,
        window_command: str | None = ...,
    ) -> TmuxSession: ...

    def switch_client(self, target_session: str) -> object: ...

    def attach_session(self, target_session: str) -> object: ...


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
    try:
        window = pane.window
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
    except Exception as exc:  # a pane without a usable window cannot carry marks
        _debug(f"not publishing marks: {exc}")
        return None


# Placement: the agent's own session in the user's tmux server.
#
# Marks describe a chat that is already running. Placement decides where a chat
# runs at all: one session per agent, one window per chat, in the server the
# user is already in. Every operation stays best-effort, so a missing or broken
# tmux only ever means "run chat in place".


def sanitize_session_name(agent: str) -> str:
    """Derive a session name from an agent name.

    Session names keep lowercase ``[a-z0-9-]`` and every other character becomes
    ``-``. tmux itself rewrites ``.`` and ``:`` in a session name, so a derived
    name never contains them.
    """

    cleaned = _SESSION_CHARS.sub("-", agent.strip().casefold())
    return _SESSION_DASHES.sub("-", cleaned).strip("-") or SESSION_NAME_FALLBACK


@dataclass(slots=True)
class Launcher:
    """Best-effort placement of one chat run in the user's tmux server."""

    agent: str
    _server: TmuxServer
    _pane: TmuxPane

    def agent_session(self) -> TmuxSession | None:
        """The agent's session: ``@toolang_agent`` first, session name second.

        A name only identifies a session nobody else owns, so a name that a
        different agent already marked is not adopted.
        """

        sessions = self._sessions()
        for session in sessions:
            if _text(_option(session, SESSION_AGENT)) == self.agent:
                return session
        name = sanitize_session_name(self.agent)
        for session in sessions:
            if _text(getattr(session, "session_name", None)) != name:
                continue
            if not _text(_option(session, SESSION_AGENT)):
                return session
        return None

    def is_current(self, session: TmuxSession) -> bool:
        """Whether ``session`` is the session this process runs inside."""

        return self._current_session_id() == _text(getattr(session, "session_id", None))

    def list_windows(self, session: TmuxSession) -> Sequence[TmuxWindow]:
        """One session's windows in tmux order, oldest first."""

        try:
            return tuple(getattr(session, "windows", ()) or ())
        except Exception as exc:
            _debug(f"session windows not listed: {exc}")
            return ()

    def thread_window(self, session: TmuxSession, thread_id: str) -> TmuxWindow | None:
        """The newest window in ``session`` marked with ``thread_id``.

        The marks are the index: no thread data is read to answer this.
        """

        found: TmuxWindow | None = None
        for window in self.list_windows(session):
            if _text(_option(window, MARK_THREAD_ID)) == thread_id:
                found = window
        return found

    def ensure_session(
        self, *, command: str, directory: str | None = None
    ) -> tuple[TmuxSession | None, TmuxWindow | None]:
        """The agent's session, created with ``command`` when it is missing.

        A session this call creates already runs the chat command in its first
        window, so that window comes back with the session. An existing session
        returns ``None`` for the window and the caller opens one.
        """

        existing = self.agent_session()
        if existing is not None:
            self._own(existing)
            return existing, None
        name = self._available_name()
        try:
            session = self._server.new_session(
                session_name=name,
                attach=False,
                start_directory=directory,
                window_command=command,
            )
        except Exception as exc:
            _debug(f"session {name} not created: {exc}")
            return None, None
        self._own(session)
        self._stay_attached(session)
        return session, _active_window(session)

    def open_window(
        self, session: TmuxSession, *, command: str, directory: str | None = None
    ) -> TmuxWindow | None:
        """Open one chat window in an existing session."""

        try:
            return session.new_window(start_directory=directory, window_shell=command)
        except Exception as exc:
            _debug(f"chat window not opened: {exc}")
            return None

    def switch_client(self, window: TmuxWindow) -> bool:
        """Point the attached client at ``window``, attaching when none is.

        Returns ``False`` when neither could be done, because the caller then
        has to keep the chat where it started.
        """

        try:
            name = _text(
                getattr(getattr(window, "session", None), "session_name", None)
            )
        except Exception as exc:
            _debug(f"window session not resolved: {exc}")
            name = ""
        try:
            window.select()
        except Exception as exc:
            _debug(f"window not selected: {exc}")
        if not name:
            return False
        try:
            self._server.switch_client(name)
            return True
        except Exception as exc:
            _debug(f"client not switched to {name}: {exc}")
        try:
            self._server.attach_session(name)
        except Exception as exc:
            _debug(f"client not attached to {name}: {exc}")
            return False
        return True

    def close_window(self, window: TmuxWindow) -> None:
        """Discard a chat window this run opened and could not move to."""

        closer = getattr(window, "kill_window", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as exc:
            _debug(f"chat window not closed: {exc}")

    def _sessions(self) -> Sequence[TmuxSession]:
        try:
            return tuple(getattr(self._server, "sessions", ()) or ())
        except Exception as exc:
            _debug(f"sessions not listed: {exc}")
            return ()

    def _current_session_id(self) -> str:
        pane = self._pane
        try:
            value = _text(getattr(pane, "session_id", None))
            if value:
                return value
            return _text(getattr(getattr(pane, "session", None), "session_id", None))
        except Exception as exc:
            _debug(f"current session not resolved: {exc}")
            return ""

    def _available_name(self) -> str:
        """The agent's session name, suffixed when a foreign session took it."""

        base = sanitize_session_name(self.agent)
        taken = {
            _text(getattr(item, "session_name", None)) for item in self._sessions()
        }
        if base not in taken:
            return base
        index = 2
        while f"{base}-{index}" in taken:
            index += 1
        return f"{base}-{index}"

    def _own(self, session: TmuxSession) -> None:
        """Record ``@toolang_agent`` on a session that does not carry it yet."""

        if _text(_option(session, SESSION_AGENT)):
            return
        try:
            session.set_option(SESSION_AGENT, self.agent)
        except Exception as exc:
            _debug(f"session not marked: {exc}")

    def _stay_attached(self, session: TmuxSession) -> None:
        """Keep the client in tmux when this session is destroyed.

        Only sessions toolang creates are touched: chat owns their windows and
        exits with them, so the client should fall back to where it came from
        instead of being detached.
        """

        try:
            session.set_option(DETACH_ON_DESTROY, "off")
        except Exception as exc:
            _debug(f"session not kept on destroy: {exc}")


def resolve_launcher(
    *,
    agent: str,
    environment: Mapping[str, str] | None = None,
    server_factory: ServerFactory | None = None,
    pane_factory: PaneFactory | None = None,
) -> Launcher | None:
    """Return the launcher for the current pane, or ``None`` outside tmux.

    Placement shares the marks kill switch, so ``TOOLANG_TMUX_MARKS=0`` keeps
    chat in the current terminal whatever else is configured.
    """

    environ = os.environ if environment is None else environment
    if not marks_enabled(environ):
        return None
    if not environ.get("TMUX") or not environ.get("TMUX_PANE"):
        return None
    build_server = server_factory if server_factory is not None else _libtmux_server
    build_pane = pane_factory if pane_factory is not None else _libtmux_pane
    try:
        server = build_server()
        pane = build_pane()
    except Exception as exc:
        _debug(f"not placing chat: {exc}")
        return None
    if not _text(getattr(pane, "pane_id", None)):
        _debug("not placing chat: resolved pane has no id")
        return None
    return Launcher(agent=agent, _server=server, _pane=pane)


def _option(target: object, name: str) -> Any:
    """One tmux option value, or ``None`` when it is unset or unreadable."""

    reader = getattr(target, "show_option", None)
    if not callable(reader):
        return None
    try:
        return reader(name)
    except Exception as exc:
        _debug(f"option {name} not read: {exc}")
        return None


def _active_window(session: TmuxSession) -> TmuxWindow | None:
    try:
        window = getattr(session, "active_window", None)
        if window is not None:
            return window
        windows = tuple(getattr(session, "windows", ()) or ())
    except Exception as exc:
        _debug(f"active window not resolved: {exc}")
        return None
    return windows[0] if windows else None


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


def _libtmux_server() -> TmuxServer:
    import libtmux

    return libtmux.Server.from_env()


def _debug(message: str) -> None:
    value = os.environ.get(DEBUG_ENV, "").strip().casefold()
    if not value or value in _DISABLED_VALUES:
        return
    print(f"toolang: tmux: {message}", file=sys.stderr)
