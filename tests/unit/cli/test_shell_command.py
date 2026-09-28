from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer._click.exceptions import ClickException

from toolang.catalog.agent import LocalAgents
from toolang.cli.common.context import CliContext
import toolang.cli.toolang.commands.shell as shell_command
import toolang.cli.toolang.main as cli


class _Stream:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _create_agent(root: Path, name: str = "alice") -> Path:
    return LocalAgents(root / "agents").create(name, content="")


def _context(root: Path, agent: str = "alice") -> Any:
    return SimpleNamespace(obj=CliContext(root=root, agent=agent))


def test_shell_uses_root_selection_and_inherits_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_root = tmp_path / "environment-root"
    explicit_root = tmp_path / "explicit-root"
    environment_home = _create_agent(environment_root)
    explicit_home = _create_agent(explicit_root)
    (explicit_home / ".env").write_text("AGENT_ONLY=secret\n", encoding="utf-8")
    monkeypatch.setenv("TOOLANG_ROOT", str(environment_root))
    monkeypatch.setenv("SHELL", "/custom/shell")
    monkeypatch.setattr(shell_command, "_require_interactive_terminal", lambda *_: None)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(shell_command.subprocess, "run", run)

    assert cli.main(["alice", "shell"]) == 0
    assert cli.main(["--root", str(explicit_root), "alice", "shell"]) == 0

    assert [args for args, _kwargs in calls] == [
        ["/custom/shell", "-i"],
        ["/custom/shell", "-i"],
    ]
    assert [kwargs["cwd"] for _args, kwargs in calls] == [
        environment_home,
        explicit_home,
    ]
    assert all(kwargs["check"] is False for _args, kwargs in calls)
    assert all("env" not in kwargs for _args, kwargs in calls)
    assert all(
        not {"stdin", "stdout", "stderr"}.intersection(kwargs)
        for _args, kwargs in calls
    )


def test_shell_falls_back_to_sh_when_shell_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _create_agent(tmp_path)
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(shell_command, "_require_interactive_terminal", lambda *_: None)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(shell_command.subprocess, "run", run)

    shell_command.shell(_context(tmp_path))

    assert calls == [(["/bin/sh", "-i"], {"cwd": home, "check": False})]


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"),
    [(False, True), (True, False)],
)
def test_shell_refuses_non_tty_streams_before_starting_a_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin_tty: bool,
    stdout_tty: bool,
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setattr(shell_command.sys, "stdin", _Stream(stdin_tty))
    monkeypatch.setattr(shell_command.sys, "stdout", _Stream(stdout_tty))
    monkeypatch.setattr(
        shell_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("non-TTY invocation started a shell"),
    )

    with pytest.raises(ClickException, match="requires an interactive terminal"):
        shell_command.shell(_context(tmp_path))


def test_shell_rejects_an_invalid_explicit_agent_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr(
        shell_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid agent started a shell"),
    )

    assert cli.main(["agent:..", "shell"]) == 2


def test_shell_rejects_a_missing_agent_before_launching_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shell_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("missing agent started a shell"),
    )

    with pytest.raises(ClickException, match="Agent alice not found"):
        shell_command.shell(_context(tmp_path))


@pytest.mark.parametrize(("returncode", "expected"), [(17, 17), (-2, 130)])
def test_shell_propagates_nonzero_child_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected: int,
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setattr(shell_command, "_require_interactive_terminal", lambda *_: None)
    monkeypatch.setattr(
        shell_command.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, returncode),
    )

    with pytest.raises(typer.Exit) as exc_info:
        shell_command.shell(_context(tmp_path))

    assert exc_info.value.exit_code == expected


def test_shell_reports_an_unlaunchable_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setenv("SHELL", "/missing/shell")
    monkeypatch.setattr(shell_command, "_require_interactive_terminal", lambda *_: None)

    def run(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("not found")

    monkeypatch.setattr(shell_command.subprocess, "run", run)

    with pytest.raises(ClickException, match="could not start shell '/missing/shell'"):
        shell_command.shell(_context(tmp_path))
