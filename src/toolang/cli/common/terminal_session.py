"""CLI boundary and lifecycle for independent title and pane publishers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import sys
from types import TracebackType
from typing import IO

from toolang.cli.config import resolve_title_prefix
from toolang.common.config_sources import ConfigSource, config_sources, read_config
from toolang.common.layout import AgentLayout

from .context import load_root_environ, load_runtime_environ, user_call
from .terminal_title import TerminalTitle
from .tmux import PaneMarks


class TerminalSession:
    """Start once, update only explicit surfaces, and close both publishers."""

    def __init__(
        self,
        *,
        label: str = "",
        prefix: str = "",
        enabled: bool = False,
        stream: IO[str] | None = None,
        marks: PaneMarks | None = None,
    ) -> None:
        self.label = label
        self.enabled = enabled
        self._prefix = prefix
        self._stream = stream if stream is not None else sys.stdout
        self._marks = marks
        self._title: TerminalTitle | None = None
        self._closed = False

    def start(
        self,
        label: str | None = None,
        *,
        write: Callable[[str], object] | None = None,
        flush: Callable[[], object] | None = None,
        enabled: bool = True,
    ) -> None:
        if self._closed or self._title is not None:
            return
        self.enabled = self.enabled and enabled
        self._title = TerminalTitle(
            prefix=self._prefix,
            write=write or self._stream.write,
            flush=flush or self._stream.flush,
            enabled=self.enabled,
        )
        if self.enabled and self._marks is not None:
            self._marks.start()
        self.set_title(self.label if label is None else label)

    def set_title(self, label: str) -> bool:
        return self._title.set(label) if self._title is not None else False

    def set_thread(self, thread_id: str) -> None:
        if self.enabled and not self._closed and self._marks is not None:
            self._marks.update("thread", thread_id)

    def set_run(self, run_id: str) -> None:
        if self.enabled and not self._closed and self._marks is not None:
            self._marks.update("run", run_id)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._title is not None:
            self._title.close()
        if self._marks is not None:
            self._marks.close()

    def __enter__(self) -> TerminalSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def prepare_terminal(
    kind: str,
    *,
    layout: AgentLayout | None = None,
    root: Path | None = None,
    title_prefix: str | None = None,
    label: str = "",
    thread: str = "",
    convo: str = "",
    context: str = "",
    eligible: bool = True,
    stream: IO[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> TerminalSession:
    """Resolve CLI configuration once; publishers receive only concrete values."""
    process = os.environ if environment is None else environment
    if layout is not None:
        root = layout.root
        sources = user_call(config_sources, layout)
        environ = load_runtime_environ(layout, base_environ=process)
    else:
        if root is None:
            raise ValueError("terminal publication requires a root or agent layout")
        path = root / "config.toml"
        sources = (ConfigSource(path, user_call(read_config, path)),)
        environ = load_root_environ(root, base_environ=process)
    prefix = user_call(
        resolve_title_prefix,
        kind,
        sources=sources,
        environment=environ,
        option=title_prefix,
    )
    output = stream if stream is not None else sys.stdout
    enabled = (
        eligible and sys.stdin.isatty() and sys.stdout.isatty() and output.isatty()
    )
    return TerminalSession(
        label=label,
        prefix=prefix,
        enabled=enabled,
        stream=output,
        marks=PaneMarks(
            environment=process,
            fields={
                "kind": kind,
                "root": str(root.resolve()),
                "agent": layout.name if layout is not None else "",
                "thread": thread,
                "convo": convo,
                "context": context,
            },
        )
        if enabled
        else None,
    )
