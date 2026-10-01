from __future__ import annotations

import errno
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer._click.exceptions import ClickException
from typer._click.utils import strip_ansi

from toolang.catalog.agent import LocalAgents
from toolang.common.layout import AgentLayout
from toolang.up import process as agents
from toolang.cli.common.context import CliContext
import toolang.cli.toolang.commands.home as home_command
import toolang.cli.toolang.main as cli


class _Stream:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _create_agent(root: Path, name: str = "alice") -> Path:
    return LocalAgents(root / "agents").create(name, content="")


def _context(root: Path, agent: str | None = "alice") -> Any:
    if agent in {"visiting", "roaming"}:
        layout = (
            AgentLayout(root / "visiting", "alice", "visiting")
            if agent == "visiting"
            else AgentLayout.roaming(root / "alice.too")
        )
        layout.home.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(obj=CliContext(root=root, agent="alice", layout=layout))
    return SimpleNamespace(obj=CliContext(root=root, agent=agent))


def test_home_opens_root_without_an_agent_and_respects_root_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOOLANG_ROOT", raising=False)
    monkeypatch.setenv("SHELL", "/custom/shell")
    monkeypatch.setattr(home_command, "_require_interactive_terminal", lambda *_: None)
    roots = [tmp_path / ".toolang", tmp_path / "env root", tmp_path / "explicit root"]
    for root in roots:
        root.mkdir()
        (root / ".env").write_text("ROOT_ONLY=secret\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(home_command.subprocess, "run", run)

    assert cli.main(["home"]) == 0
    monkeypatch.setenv("TOOLANG_ROOT", str(roots[1]))
    assert cli.main(["home"]) == 0
    assert cli.main(["--root", str(roots[2]), "home"]) == 0
    assert cli.main(["--root", "explicit root", "home"]) == 0

    assert [kwargs["cwd"].resolve() for _, kwargs in calls] == [*roots, roots[2]]
    assert all(
        args == ["/custom/shell", "-i"]
        and kwargs == {"cwd": kwargs["cwd"], "check": False}
        for args, kwargs in calls
    )
    assert all(not (root / "agents").exists() for root in roots)


@pytest.mark.parametrize("is_file", [False, True])
def test_home_reports_an_unusable_root_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    is_file: bool,
) -> None:
    root = tmp_path / "root"
    if is_file:
        root.write_text("unchanged", encoding="utf-8")
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(home_command, "_require_interactive_terminal", lambda *_: None)

    # An invalid cwd prevents subprocess from executing the interactive shell.
    assert cli.main(["--root", str(root), "home"]) == 1

    output = strip_ansi(capsys.readouterr().err)
    assert "could not start shell '/bin/sh'" in output
    expected_errno = errno.ENOTDIR if is_file else errno.ENOENT
    assert f"[Errno {expected_errno}]" in output
    assert root.is_file() if is_file else not root.exists()
    if is_file:
        assert root.read_text(encoding="utf-8") == "unchanged"


def test_home_help_describes_optional_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(home_command.sys, "argv", ["too"])

    assert cli.main(["home", "--help"]) == 0

    output = strip_ansi(capsys.readouterr().out)
    assert "Usage: too [AGENT] home [OPTIONS]" in output
    assert "Open a shell in agent home" in output
    assert "Agent name, .too file, reference, or URL" in output
    assert "* AGENT" not in output
    assert "root" not in output.lower()


@pytest.mark.parametrize(
    "arguments",
    [
        ["home", "alice"],
        ["agent:missing", "home"],
        ["./agent.too", "home"],
        ["home", "https://example.com/agent.too"],
    ],
)
def test_home_rejects_unsupported_targets_without_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr(
        home_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid target started a shell"),
    )

    assert cli.main(arguments) != 0


def test_home_uses_root_selection_and_inherits_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_root = tmp_path / "environment-root"
    explicit_root = tmp_path / "explicit-root"
    environment_home = _create_agent(environment_root)
    explicit_home = _create_agent(explicit_root)
    (explicit_home / ".env").write_text("AGENT_ONLY=secret\n", encoding="utf-8")
    monkeypatch.setenv("TOOLANG_ROOT", str(environment_root))
    monkeypatch.setenv("SHELL", "/custom/shell")
    monkeypatch.setattr(home_command, "_require_interactive_terminal", lambda *_: None)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(home_command.subprocess, "run", run)

    assert cli.main(["alice", "home"]) == 0
    assert cli.main(["--root", str(explicit_root), "alice", "home"]) == 0
    assert cli.main(["--root", str(explicit_root), "agent:alice", "home"]) == 0

    assert [args for args, _kwargs in calls] == [
        ["/custom/shell", "-i"],
        ["/custom/shell", "-i"],
        ["/custom/shell", "-i"],
    ]
    assert [kwargs["cwd"] for _args, kwargs in calls] == [
        environment_home,
        explicit_home,
        explicit_home,
    ]
    assert all(kwargs["check"] is False for _args, kwargs in calls)
    assert all("env" not in kwargs for _args, kwargs in calls)
    assert all(
        not {"stdin", "stdout", "stderr"}.intersection(kwargs)
        for _args, kwargs in calls
    )


@pytest.mark.parametrize("agent", [None, "alice", "visiting", "roaming"])
def test_home_falls_back_to_sh_when_shell_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str | None
) -> None:
    resident_home = _create_agent(tmp_path)
    context = _context(tmp_path, agent)
    home = (
        context.obj.layout.home
        if context.obj.layout is not None
        else resident_home
        if agent
        else tmp_path
    )
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(home_command, "_require_interactive_terminal", lambda *_: None)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(home_command.subprocess, "run", run)

    home_command.home(context)

    assert calls == [(["/bin/sh", "-i"], {"cwd": home, "check": False})]


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"),
    [(False, True), (True, False)],
)
@pytest.mark.parametrize("agent", [None, "alice", "visiting", "roaming"])
def test_home_refuses_non_tty_streams_before_starting_a_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str | None,
    stdin_tty: bool,
    stdout_tty: bool,
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setattr(home_command.sys, "stdin", _Stream(stdin_tty))
    monkeypatch.setattr(home_command.sys, "stdout", _Stream(stdout_tty))
    monkeypatch.setattr(
        home_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("non-TTY invocation started a shell"),
    )

    with pytest.raises(ClickException, match="requires an interactive terminal"):
        home_command.home(_context(tmp_path, agent))


def test_home_rejects_an_invalid_explicit_agent_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr(
        home_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid agent started a shell"),
    )

    assert cli.main(["agent:..", "home"]) == 2


def test_home_rejects_a_missing_agent_before_launching_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        home_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("missing agent started a shell"),
    )

    with pytest.raises(ClickException, match="Agent alice not found"):
        home_command.home(_context(tmp_path))


@pytest.mark.parametrize(("returncode", "expected"), [(17, 17), (-2, 130)])
@pytest.mark.parametrize("agent", [None, "alice", "visiting", "roaming"])
def test_home_propagates_nonzero_child_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str | None,
    returncode: int,
    expected: int,
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setattr(home_command, "_require_interactive_terminal", lambda *_: None)
    monkeypatch.setattr(
        home_command.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, returncode),
    )

    with pytest.raises(typer.Exit) as exc_info:
        home_command.home(_context(tmp_path, agent))

    assert exc_info.value.exit_code == expected


@pytest.mark.parametrize("agent", [None, "alice", "visiting", "roaming"])
def test_home_reports_an_unlaunchable_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str | None
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setenv("SHELL", "/missing/shell")
    monkeypatch.setattr(home_command, "_require_interactive_terminal", lambda *_: None)

    def run(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("not found")

    monkeypatch.setattr(home_command.subprocess, "run", run)

    with pytest.raises(ClickException, match="could not start shell '/missing/shell'"):
        home_command.home(_context(tmp_path, agent))


@pytest.mark.parametrize("selector_kind", ["roaming", "url", "reference"])
def test_home_prepares_nonresident_home_and_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector_kind: str
) -> None:
    source_text = "agic:\n  Reply directly.\n"
    fetches: list[str] = []
    if selector_kind == "roaming":
        source = tmp_path / "project" / "alice.too"
        source.parent.mkdir()
        source.write_text(source_text)
        layout = AgentLayout.roaming(source)
        selector = str(source)
    else:
        selector = (
            "https://agents.example/alice.too"
            if selector_kind == "url"
            else "agents.example/alice"
        )
        layout = AgentLayout(tmp_path / "visiting", "alice", "visiting")
        monkeypatch.setattr(
            AgentLayout, "visiting", classmethod(lambda _cls, source, name: layout)
        )

    def fetch(ref: agents.AgentRef, **_kwargs: Any) -> str:
        fetches.append(ref.render())
        return source_text

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert layout.program.read_text() == source_text
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(agents, "fetch_agent_ref", fetch)
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "unrelated"))
    monkeypatch.setenv("SHELL", "/custom/shell")
    monkeypatch.setattr(home_command, "_require_interactive_terminal", lambda *_: None)
    monkeypatch.setattr(home_command.subprocess, "run", run)

    assert not layout.home.exists()
    assert cli.main([selector, "home"]) == 0
    assert cli.main([selector, "home"]) == 0

    assert (
        calls == [(["/custom/shell", "-i"], {"cwd": layout.home, "check": False})] * 2
    )
    assert not layout.runtime.exists()
    assert not (tmp_path / "unrelated").exists()
    if selector_kind == "roaming":
        assert layout.program.is_symlink()
        assert layout.program.resolve() == source
        assert layout.config.is_file()
        assert fetches == []
    else:
        assert fetches == ["https://agents.example/alice.too"]


def test_home_reports_remote_preparation_failure_without_launching(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    layout = AgentLayout(tmp_path, "alice", "visiting")
    monkeypatch.setattr(
        AgentLayout, "visiting", classmethod(lambda _cls, source, name: layout)
    )

    def fetch(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("remote source unavailable")

    monkeypatch.setattr(agents, "fetch_agent_ref", fetch)
    monkeypatch.setattr(
        home_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("failed preparation started a shell"),
    )

    assert cli.main(["https://agents.example/alice.too", "home"]) == 1
    assert "remote source unavailable" in capsys.readouterr().err
    assert not layout.home.exists()


def test_home_resident_name_is_available_through_explicit_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_home = _create_agent(tmp_path, "home")
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr(home_command, "_require_interactive_terminal", lambda *_: None)
    working_directories: list[Path] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        working_directories.append(kwargs["cwd"])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(home_command.subprocess, "run", run)

    assert cli.main(["agent:home", "home"]) == 0
    assert working_directories == [expected_home]
    assert "shell" not in cli.routing.COMMAND_SPECS
