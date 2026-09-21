"""Publish toolang state as tmux options while chat runs in a pane.

A chat that runs inside a tmux pane records what it hosts, each value at its own
scope, so tmux-side views can read it with ``#{@toolang_thread}`` and friends.
The pane carries the pad kind (``chat`` today), its window carries the thread id,
and the agent is a session option the launcher writes. A window with
no value of its own reads its session's, so the agent is visible across the whole
session while the thread marks stay on the one window that shows it.

Identity marks are user options. Terminal titles are published separately by
chat through OSC 0; tmux views can display the native ``pane_title``.

Detection and targeting are delegated to ``libtmux``, which reads ``TMUX`` and
``TMUX_PANE`` from the environment, so this module carries no socket or protocol
code. Metadata publication is best-effort: outside tmux, or when tmux disappears
mid-session, callers keep working and publication errors stay out of the UI.

The same module also decides where a chat run happens: ``Launcher``
resolves the agent's session in the user's server and reuses an open window.
It selects targets within the invoking session and switches clients across sessions.
Placement shares the marks kill switch; creation and selection errors reach the
launcher so it can report them at the invoking terminal.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from toolang.cli.common.errors import TmuxPlacementError

MARK_AGENT = "@toolang_agent"
MARK_THREAD = "@toolang_thread"
MARK_PAD = "@toolang_pad"

# One value per scope: the agent belongs to the session, the thread to the window
# that shows it, and the pad to the pane it runs in. The scopes stay separate
# because tmux inherits user options, so a window with no value of its own reads
# its session's; a shared name would bleed a session-wide value into every
# window a window-scoped format reads. A pad is a thread view (readable and
# writable); ``chat`` is the only kind today, later ones are ``shell``, ``logs``
# and friends, all in the same window under the same thread id.
PAD_CHAT = "chat"
# The window name a brand-new chat starts under, before its thread id exists.
WINDOW_NAME_FALLBACK = "new_chat"
PANE_MARKS = frozenset({MARK_PAD})
WINDOW_MARKS = frozenset({MARK_THREAD})

# The agent is a session option: the launcher writes it when it determines the
# agent's session, and every window in that session reads it by inheritance.
SESSION_AGENT = MARK_AGENT
SESSION_NAME_FALLBACK = "agent"
_SESSION_CHARS = re.compile(r"[^a-z0-9-]+")
_SESSION_DASHES = re.compile(r"-{2,}")

# tmux detaches an attached client when the session it is showing goes away.
# A session toolang creates holds only chat windows, so chat exiting would take
# the client with it; the option below keeps the client in tmux instead.
DETACH_ON_DESTROY = "detach-on-destroy"

ENABLED_ENV = "TOOLANG_TMUX"
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
    def session(self) -> "TmuxSession": ...

    @property
    def panes(self) -> Sequence["TmuxPane"]: ...

    def show_option(self, option: str) -> Any: ...

    def set_option(self, option: str, value: str) -> object: ...

    def unset_option(self, option: str) -> object: ...

    def rename_window(self, new_name: str) -> object: ...

    def split(
        self,
        *,
        start_directory: str | None = ...,
        shell: str | None = ...,
        attach: bool = ...,
    ) -> "TmuxPane": ...


class TmuxPane(Protocol):
    """The slice of ``libtmux.Pane`` this module depends on."""

    @property
    def pane_id(self) -> str: ...

    @property
    def window(self) -> TmuxWindow: ...

    @property
    def session_id(self) -> str: ...

    def show_option(self, option: str) -> Any: ...

    def set_option(self, option: str, value: str) -> object: ...

    def unset_option(self, option: str) -> object: ...

    def select(self) -> object: ...

    def respawn(
        self, *, shell: str | None = ..., start_directory: str | None = ...
    ) -> object: ...


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

    def select_window(self, target_window: str) -> object: ...

    def new_window(
        self,
        window_name: str | None = ...,
        *,
        start_directory: str | None = ...,
        window_shell: str | None = ...,
        attach: bool = ...,
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


@dataclass(slots=True)
class Marks:
    """Best-effort writer for the toolang marks, each at its own scope.

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
    _written: dict[str, str] = field(default_factory=dict)
    _named: str = ""

    def set(self, name: str, value: str) -> None:
        """Write one mark at its scope; a name without a scope is a bug."""

        if not value or self._written.get(name) == value:
            return
        if self._attempt(name, value, self._writer(name)):
            self._written[name] = value

    def name_window(self, name: str) -> None:
        """Name the container window after its thread, once per name.

        The name is a container convention and outlives chat, so it is never
        restored; a window whose thread changed is renamed again.
        """

        if not name or name == self._named:
            return
        if _rename_window(self._rename_window, name):
            self._named = name

    def clear(self, *names: str) -> None:
        """Remove the marks this instance wrote."""

        requested = names or tuple(self._written)
        for name in requested:
            if name not in self._written:
                continue
            self._remove(name)

    def _attempt(self, name: str, value: str, writer: OptionWriter) -> bool:
        try:
            writer(name, value)
        except Exception as exc:  # best-effort: tmux may be gone or unreachable
            _debug(f"option {name} not written: {exc}")
            return False
        return True

    def _writer(self, name: str) -> OptionWriter:
        """The option writer for ``name``'s scope."""

        if name in PANE_MARKS:
            return self._set_pane
        if name in WINDOW_MARKS:
            return self._set_window
        raise ValueError(f"mark has no scope: {name}")

    def _remover(self, name: str) -> OptionRemover | None:
        """The option remover for ``name``'s scope, if it has one."""

        if name in PANE_MARKS:
            return self._unset_pane
        if name in WINDOW_MARKS:
            return self._unset_window
        raise ValueError(f"mark has no scope: {name}")

    def _remove(self, name: str) -> None:
        remover = self._remover(name)
        try:
            if remover is None:
                self._writer(name)(name, "")
            else:
                remover(name)
        except Exception as exc:
            _debug(f"option {name} not cleared: {exc}")
        del self._written[name]


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
    if not tmux_enabled(environ):
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
            _named=_text(_option(window, MARK_THREAD)),
        )
    except Exception as exc:  # a pane without a usable window cannot carry marks
        _debug(f"not publishing marks: {exc}")
        return None


# Placement: the agent's own session in the user's tmux server.
#
# Marks describe a chat that is already running. Placement decides where a chat
# runs at all: one session per agent, one window per chat, in the server the
# user is already in. Creation and selection failures are reported to the caller;
# they must not launch a duplicate chat or invalidate a prepared target.


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
    """Own chat placement, target identity, process startup, and navigation."""

    agent: str
    _server: TmuxServer
    _pane: TmuxPane

    def place_chat(
        self, *, thread_id: str, argv: Sequence[str], directory: str
    ) -> bool:
        """Ensure and enter one target; return True only to run in this pane."""

        command = self.chat_command(argv)
        operation = "prepare"
        try:
            session = self.agent_session()
            window = self.thread_window(session, thread_id) if session else None
            action = "located"
            if window is not None:
                pad = self.chat_pad(window)
                if pad is not None and self.is_current_pane(pad.pane_id):
                    return True
                if pad is None:
                    # The exact thread window can use the invoking shell pane.
                    current = next(
                        (p for p in window.panes if self.is_current_pane(p.pane_id)),
                        None,
                    )
                    if current is not None and not _identity_option(current, MARK_PAD):
                        return True
                    pad = self.chat_pad(window, dead=True)
                    if pad is not None:
                        operation = "restart"
                        pad.respawn(shell=command, start_directory=directory)
                        action = "reused"
                    else:
                        operation = "create"
                        pad = window.split(
                            start_directory=directory, shell=command, attach=False
                        )
                        action = "created"
            else:
                operation = "create"
                if session is None:
                    session = self._server.new_session(
                        session_name=self._available_name(),
                        attach=False,
                        start_directory=directory,
                        window_command=command,
                    )
                    operation = "mark"
                    self._own(session)
                    session.set_option(DETACH_ON_DESTROY, "off")
                    window = session.active_window
                    if window is None:
                        raise TmuxPlacementError("Created session has no chat window")
                else:
                    window = session.new_window(
                        start_directory=directory, window_shell=command, attach=False
                    )
                operation = "mark"
                window.set_option(MARK_THREAD, thread_id)
                window.rename_window(thread_id)
                pad = window.panes[0]
                action = "created"
            operation = "mark"
            pad.set_option(MARK_PAD, PAD_CHAT)
        except Exception as exc:
            raise TmuxPlacementError(f"failed to {operation} chat pane: {exc}") from exc

        self._enter_target(window, pad, action=action)
        return False

    @staticmethod
    def chat_command(argv: Sequence[str]) -> str:
        """Install failure retention in the new pane before executing Chat.

        The pane ID is expanded by the new pane's shell, never by the caller.
        Child Chat disables both placement and publication; this launcher owns
        its identity. The process exit status remains visible to tmux.
        """

        script = (
            'if ! tmux set-option -p -t "$TMUX_PANE" remain-on-exit failed; then '
            "printf '%s\\n' 'Could not configure chat pane; press Enter to close.' >&2; "
            "read -r reply; exit 1; fi; "
            f"exec env {ENABLED_ENV}=0 {shlex.join(argv)}"
        )
        # Explicit POSIX shell also works when tmux's default-shell is fish.
        return shlex.join(["/bin/sh", "-c", script])

    def _enter_target(self, window: TmuxWindow, pane: TmuxPane, *, action: str) -> None:
        try:
            session = window.session
            notice = (
                f"{action} chat pane {pane.pane_id} in "
                f"{session.session_name}:{window.window_name}"
            )
        except Exception as exc:
            raise TmuxPlacementError(
                f"Could not inspect prepared tmux chat target: {exc}"
            ) from exc
        try:
            self.select_target(window, pane=pane)
        except TmuxPlacementError as exc:
            raise TmuxPlacementError(f"{notice}; {exc}") from exc
        print(notice)

    def agent_session(self) -> TmuxSession | None:
        """The agent's session: its ``@toolang_agent`` mark first, name second.

        A name only identifies a session nobody else owns, so a name that a
        different agent already marked is not adopted. Determining the session
        records the mark on it, so the next lookup reads the mark instead of the
        name.
        """

        sessions = self._server.sessions
        for session in sessions:
            if _identity_option(session, SESSION_AGENT) == self.agent:
                return session
        name = sanitize_session_name(self.agent)
        for session in sessions:
            if _text(getattr(session, "session_name", None)) != name:
                continue
            if _identity_option(session, SESSION_AGENT):
                continue
            self._own(session)
            return session
        return None

    def is_current(self, session: TmuxSession) -> bool:
        """Whether ``session`` is the session this process runs inside."""

        return self._pane.session_id == session.session_id

    def is_current_pane(self, pane_id: str) -> bool:
        """Whether the target is the invoking pane."""

        return bool(pane_id) and pane_id == self._pane.pane_id

    def thread_window(self, session: TmuxSession, thread_id: str) -> TmuxWindow | None:
        """The newest window in ``session`` marked with ``thread_id``.

        The marks are the index: no thread data is read to answer this.
        """

        found: TmuxWindow | None = None
        for window in session.windows:
            if _identity_option(window, MARK_THREAD) == thread_id:
                found = window
        return found

    def chat_pad(self, window: TmuxWindow, *, dead: bool = False) -> TmuxPane | None:
        """The pane of ``window`` that runs a chat pad, if any.

        A retained failed pane keeps its mark but is not live. Every pane is
        checked: a chat running in a background pane still means the
        thread is open, and starting a second chat there would be wrong.
        """

        for pane in window.panes:
            if (
                _identity_option(pane, MARK_PAD) == PAD_CHAT
                and (getattr(pane, "pane_dead", "0") == "1") == dead
            ):
                return pane
        return None

    def select_target(
        self, window: TmuxWindow, *, pane: TmuxPane | None = None
    ) -> bool:
        """Select the target, switching one client only across sessions.

        A skipped or failed selection does not invalidate the prepared target.
        Qualify the window with its session so linked windows are unambiguous.
        """

        try:
            session = window.session
            if pane is not None and self.is_current_pane(pane.pane_id):
                return True
            current_window = getattr(self._pane, "window", None)
            if (
                not self.is_current(session)
                or getattr(current_window, "window_id", None) != window.window_id
            ):
                session.select_window(window.window_id)
            if pane is not None:
                pane.select()
            if not self.is_current(session):
                self._server.switch_client(session.session_id)
        except Exception as exc:
            raise TmuxPlacementError(f"failed to switch: {exc}") from exc
        return True

    def _available_name(self) -> str:
        """The agent's session name, suffixed when a foreign session took it."""

        base = sanitize_session_name(self.agent)
        taken = {
            _text(getattr(item, "session_name", None)) for item in self._server.sessions
        }
        if base not in taken:
            return base
        index = 2
        while f"{base}-{index}" in taken:
            index += 1
        return f"{base}-{index}"

    def _own(self, session: TmuxSession) -> None:
        """Record ``@toolang_agent`` on a session that does not carry it yet."""

        if _identity_option(session, SESSION_AGENT):
            return
        try:
            session.set_option(SESSION_AGENT, self.agent)
        except Exception as exc:
            raise TmuxPlacementError(
                f"Could not mark tmux agent session: {exc}"
            ) from exc


def resolve_launcher(
    *,
    agent: str,
    environment: Mapping[str, str] | None = None,
    server_factory: ServerFactory | None = None,
    pane_factory: PaneFactory | None = None,
) -> Launcher | None:
    """Return the launcher for the current pane, or ``None`` outside tmux.

    Placement shares the marks kill switch, so ``TOOLANG_TMUX=0`` keeps
    chat in the current terminal whatever else is configured.
    """

    environ = os.environ if environment is None else environment
    if not tmux_enabled(environ):
        return None
    if not environ.get("TMUX") or not environ.get("TMUX_PANE"):
        return None
    build_server = server_factory if server_factory is not None else _libtmux_server
    build_pane = pane_factory if pane_factory is not None else _libtmux_pane
    try:
        server = build_server()
        pane = build_pane()
        if not _text(getattr(pane, "pane_id", None)):
            raise ValueError("Current tmux pane has no ID")
    except Exception as exc:
        raise TmuxPlacementError(f"Could not resolve current tmux pane: {exc}") from exc
    return Launcher(agent=agent, _server=server, _pane=pane)


def _rename_window(rename: TextSetter | None, name: str) -> bool:
    """Best-effort rename through one window's own setter."""

    if rename is None:
        return False
    try:
        rename(name)
    except Exception as exc:
        _debug(f"window not renamed: {exc}")
        return False
    return True


def _identity_option(target: TmuxSession | TmuxWindow | TmuxPane, name: str) -> str:
    """Only an unset mark is absent; failed lookups must not create duplicates."""

    from libtmux.exc import InvalidOption, UnknownOption

    try:
        return _text(target.show_option(name))
    except (InvalidOption, UnknownOption):
        return ""


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


def tmux_enabled(environment: Mapping[str, str]) -> bool:
    """Whether tmux placement, naming, and identity publication are enabled."""

    return environment.get(ENABLED_ENV, "").strip().casefold() not in _DISABLED_VALUES


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
