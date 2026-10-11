"""Ordered, best-effort pane-local metadata; never place or rename terminals."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from queue import Queue
import sys
from threading import Event, Thread
from typing import Protocol

ENABLED_ENV = "TOOLANG_TMUX"
DEBUG_ENV = "TOOLANG_TMUX_DEBUG"
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})
FIELDS = ("kind", "root", "agent", "thread", "run", "convo", "context")


class TmuxPane(Protocol):
    def set_option(self, option: str, value: str) -> object: ...

    def unset_option(self, option: str) -> object: ...


def tmux_enabled(environment: Mapping[str, str]) -> bool:
    return environment.get(ENABLED_ENV, "").strip().lower() not in _DISABLED_VALUES


class PaneMarks:
    """One worker owns detection, publication, identity updates, and cleanup.

    Closing cancels queued writes and waits at most 200 ms for cleanup. A slow
    tmux call may finish later on the daemon worker, followed by removal.
    """

    def __init__(
        self,
        *,
        environment: Mapping[str, str],
        fields: Mapping[str, str],
        pane_factory: Callable[[], TmuxPane] | None = None,
    ) -> None:
        self._environment = dict(environment)
        self._fields = {name: fields.get(name, "") for name in FIELDS}
        self._pane_factory = pane_factory
        self._pane: TmuxPane | None = None
        self._pending: Queue[tuple[str, str] | None] = Queue()
        self._worker: Thread | None = None
        self._closed = Event()
        self._written: dict[str, str] = {}

    def start(self) -> None:
        if self._worker is not None or self._closed.is_set():
            return
        if not tmux_enabled(self._environment) or not all(
            self._environment.get(key) for key in ("TMUX", "TMUX_PANE")
        ):
            return
        self._worker = Thread(target=self._run, name="terminal-pane-marks", daemon=True)
        try:
            self._worker.start()
        except RuntimeError as exc:
            self._worker = None
            self._closed.set()
            self._debug(exc)

    def update(self, field: str, value: str) -> None:
        if field not in {"thread", "run"}:
            raise ValueError(f"not a mutable pane field: {field}")
        if self._worker is not None and not self._closed.is_set():
            self._pending.put((field, value))

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._pending.put(None)
        if self._worker is not None:
            self._worker.join(timeout=0.2)

    def _run(self) -> None:
        try:
            if self._pane_factory is None:
                from libtmux import Pane

                self._pane = Pane.from_env(self._environment)
            else:
                self._pane = self._pane_factory()
            # Empty local values mask inherited legacy options. Kind commits
            # the identity only after every required field has been written.
            if not self._set("kind", ""):
                return
            for name in FIELDS[1:]:
                if self._closed.is_set() or not self._set(name, self._fields[name]):
                    return
            if self._closed.is_set() or not self._set("kind", self._fields["kind"]):
                return
            while (update := self._pending.get()) is not None:
                if not self._closed.is_set() and not self._set(*update):
                    # Never advertise an identity whose required update failed.
                    self._set("kind", "")
                    return
        except Exception as exc:
            self._debug(exc)
        finally:
            if self._pane is not None:
                for name in FIELDS:
                    try:
                        self._pane.unset_option(f"@toolang_{name}")
                    except Exception as exc:
                        self._debug(exc)

    def _set(self, name: str, value: str) -> bool:
        if self._written.get(name) == value:
            return True
        assert self._pane is not None
        try:
            self._pane.set_option(f"@toolang_{name}", value)
        except Exception as exc:
            self._debug(exc)
            return False
        self._written[name] = value
        return True

    def _debug(self, error: Exception) -> None:
        value = self._environment.get(DEBUG_ENV, "").strip().lower()
        if value and value not in _DISABLED_VALUES:
            try:
                print(f"toolang tmux: {error}", file=sys.stderr)
            except (OSError, ValueError):
                pass
