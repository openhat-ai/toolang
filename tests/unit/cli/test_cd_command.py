from __future__ import annotations

import errno
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.core import TyperGroup
from typer._click.exceptions import ClickException
from typer._click.utils import strip_ansi

from toolang.up import source as agent_sources

from toolang.catalog.agent import LocalAgents
from toolang.common.layout import AgentLayout, AgentPlacement
from toolang.cli.common.context import CliContext
import toolang.cli.toolang.commands.cd as cd_command
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


@pytest.mark.parametrize(
    ("placement", "root", "instance_id", "display_path"),
    [
        (None, "/toolang-user/.toolang", None, "~/.toolang"),
        (
            "resident",
            "/toolang-user/.toolang",
            None,
            "~/.toolang/agents/gus",
        ),
        (
            "visiting",
            "/tmp/toolang/gus@v0123456789abcdef",
            "v0123456789abcdef",
            "/tmp/toolang/gus@v0123456789abcdef/agents/gus",
        ),
        (
            "roaming",
            "/toolang-project/.toolang/gus@r0123456789abcdef",
            "r0123456789abcdef",
            "/toolang-project/.toolang/gus@r0123456789abcdef/agents/gus",
        ),
    ],
)
def test_cd_announces_destination_once_before_launching(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    placement: AgentPlacement | None,
    root: str,
    instance_id: str | None,
    display_path: str,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: Path("/toolang-user"))
    root_path = Path(root)
    layout = (
        AgentLayout(root_path, "gus", placement, instance_id=instance_id)
        if placement
        else None
    )
    context = typer.Context(
        typer.main.get_command(cli.app),
        obj=CliContext(
            root=root_path,
            agent=layout.instance_name if layout else None,
            layout=layout,
        ),
    )
    monkeypatch.setattr(cd_command.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cd_command.sys.stdout, "isatty", lambda: True)
    destination = "gus's home" if placement else "the Toolang root"
    expected = (
        f"Opening a shell in {destination}: {display_path}\nType exit to return.\n"
    )

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output = capsys.readouterr()
        assert output.out == expected
        assert output.err == ""
        assert kwargs["cwd"] == (layout.home if layout else root_path)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cd_command.subprocess, "run", run)

    cd_command.cd(context)

    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_cd_opens_root_without_an_agent_and_respects_root_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOOLANG_ROOT", raising=False)
    monkeypatch.setenv("SHELL", "/custom/shell")
    monkeypatch.setattr(cd_command, "_require_interactive_terminal", lambda *_: None)
    roots = [tmp_path / ".toolang", tmp_path / "env root", tmp_path / "explicit root"]
    for root in roots:
        root.mkdir()
        (root / ".env").write_text("ROOT_ONLY=secret\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cd_command.subprocess, "run", run)

    assert cli.main(["cd"]) == 0
    monkeypatch.setenv("TOOLANG_ROOT", str(roots[1]))
    assert cli.main(["cd"]) == 0
    assert cli.main(["--root", str(roots[2]), "cd"]) == 0
    assert cli.main(["--root", "explicit root", "cd"]) == 0

    assert [kwargs["cwd"].resolve() for _, kwargs in calls] == [*roots, roots[2]]
    assert all(
        args == ["/custom/shell", "-i"]
        and kwargs == {"cwd": kwargs["cwd"], "check": False}
        for args, kwargs in calls
    )
    assert all(not (root / "agents").exists() for root in roots)


@pytest.mark.parametrize("agent", [None, "alice", "visiting", "roaming"])
@pytest.mark.parametrize(
    "display_error",
    [FileNotFoundError, RuntimeError],
)
def test_cd_path_display_failure_preserves_shell_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: str | None,
    display_error: type[Exception],
) -> None:
    resident_home = _create_agent(tmp_path)
    context = _context(tmp_path, agent)
    directory = (
        context.obj.layout.home
        if context.obj.layout
        else resident_home
        if agent
        else tmp_path
    )
    monkeypatch.setattr(cd_command, "_require_interactive_terminal", lambda *_: None)
    monkeypatch.setenv("SHELL", "/custom/shell")

    def unavailable_display(_path: Path) -> str:
        raise display_error("Path display unavailable")

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cd_command, "shorten_home_path", unavailable_display)
    monkeypatch.setattr(cd_command.subprocess, "run", run)

    cd_command.cd(context)

    assert calls == [(["/custom/shell", "-i"], {"cwd": directory, "check": False})]
    output = capsys.readouterr()
    assert f": {directory}\nType exit to return.\n" in output.out
    assert output.err == ""


@pytest.mark.parametrize("is_file", [False, True])
def test_cd_reports_an_unusable_root_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    is_file: bool,
) -> None:
    root = tmp_path / "root"
    if is_file:
        root.write_text("unchanged", encoding="utf-8")
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(cd_command, "_require_interactive_terminal", lambda *_: None)

    # An invalid cwd prevents subprocess from executing the interactive shell.
    assert cli.main(["--root", str(root), "cd"]) == 1

    output = strip_ansi(capsys.readouterr().err)
    assert "could not start shell '/bin/sh'" in output
    expected_errno = errno.ENOTDIR if is_file else errno.ENOENT
    assert f"[Errno {expected_errno}]" in output
    assert root.is_file() if is_file else not root.exists()
    if is_file:
        assert root.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_cd_help_describes_optional_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], help_flag: str
) -> None:
    monkeypatch.setattr(cd_command.sys, "argv", ["too"])

    assert cli.main(["cd", help_flag]) == 0

    output = strip_ansi(capsys.readouterr().out)
    assert "Usage: too [AGENT] cd [OPTIONS]" in output
    assert (
        "Enter an agent home, or the Toolang root if no agent is specified."
        in " ".join(output.split())
    )
    assert "Agent name, .too path, GitHub ref, or URL" in output
    assert "* AGENT" not in output
    assert "omit for the Toolang root" not in output
    assert "Examples:" not in output
    assert "Type exit" not in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["cd", "alice"],
        ["agent:missing", "cd"],
        ["./agent.too", "cd"],
        ["cd", "https://example.com/agent.too"],
    ],
)
def test_cd_rejects_unsupported_targets_without_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr(
        cd_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid target started a shell"),
    )

    assert cli.main(arguments) != 0


def test_cd_uses_root_selection_and_inherits_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_root = tmp_path / "environment-root"
    explicit_root = tmp_path / "explicit-root"
    environment_home = _create_agent(environment_root)
    explicit_home = _create_agent(explicit_root)
    (explicit_home / ".env").write_text("AGENT_ONLY=secret\n", encoding="utf-8")
    monkeypatch.setenv("TOOLANG_ROOT", str(environment_root))
    monkeypatch.setenv("SHELL", "/custom/shell")
    monkeypatch.setattr(cd_command, "_require_interactive_terminal", lambda *_: None)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cd_command.subprocess, "run", run)

    assert cli.main(["alice", "cd"]) == 0
    assert cli.main(["--root", str(explicit_root), "alice", "cd"]) == 0
    assert cli.main(["--root", str(explicit_root), "agent:alice", "cd"]) == 0

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
def test_cd_falls_back_to_sh_when_shell_is_unset(
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
    monkeypatch.setattr(cd_command, "_require_interactive_terminal", lambda *_: None)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cd_command.subprocess, "run", run)

    cd_command.cd(context)

    assert calls == [(["/bin/sh", "-i"], {"cwd": home, "check": False})]


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"),
    [(False, True), (True, False)],
)
@pytest.mark.parametrize("agent", [None, "alice", "visiting", "roaming"])
def test_cd_refuses_non_tty_streams_before_starting_a_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str | None,
    stdin_tty: bool,
    stdout_tty: bool,
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setattr(cd_command.sys, "stdin", _Stream(stdin_tty))
    monkeypatch.setattr(cd_command.sys, "stdout", _Stream(stdout_tty))
    monkeypatch.setattr(
        cd_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("non-TTY invocation started a shell"),
    )

    with pytest.raises(ClickException, match="requires an interactive terminal"):
        cd_command.cd(_context(tmp_path, agent))


def test_cd_rejects_an_invalid_explicit_agent_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr(
        cd_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid agent started a shell"),
    )

    assert cli.main(["agent:..", "cd"]) == 2


def test_cd_rejects_a_missing_agent_before_launching_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cd_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("missing agent started a shell"),
    )

    with pytest.raises(ClickException, match="Agent alice not found"):
        cd_command.cd(_context(tmp_path))


@pytest.mark.parametrize(("returncode", "expected"), [(17, 17), (-2, 130)])
@pytest.mark.parametrize("agent", [None, "alice", "visiting", "roaming"])
def test_cd_propagates_nonzero_child_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str | None,
    returncode: int,
    expected: int,
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setattr(cd_command, "_require_interactive_terminal", lambda *_: None)
    monkeypatch.setattr(
        cd_command.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, returncode),
    )

    with pytest.raises(typer.Exit) as exc_info:
        cd_command.cd(_context(tmp_path, agent))

    assert exc_info.value.exit_code == expected


@pytest.mark.parametrize("agent", [None, "alice", "visiting", "roaming"])
def test_cd_reports_an_unlaunchable_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str | None
) -> None:
    _create_agent(tmp_path)
    monkeypatch.setenv("SHELL", "/missing/shell")
    monkeypatch.setattr(cd_command, "_require_interactive_terminal", lambda *_: None)

    def run(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("not found")

    monkeypatch.setattr(cd_command.subprocess, "run", run)

    with pytest.raises(ClickException, match="could not start shell '/missing/shell'"):
        cd_command.cd(_context(tmp_path, agent))


@pytest.mark.parametrize("selector_kind", ["roaming", "url", "reference"])
def test_cd_prepares_nonresident_home_and_reuses_it(
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
            else "https://agents.example/alice.too"
        )
        layout = AgentLayout.visiting(
            selector, "alice", toolang_root=tmp_path / "unrelated"
        )

    def fetch(ref: agent_sources.AgentRef, **_kwargs: Any) -> str:
        fetches.append(ref.render())
        return source_text

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert layout.program.read_text() == source_text
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(agent_sources, "fetch_agent_ref", fetch)
    monkeypatch.setattr(cd_command.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cd_command.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "unrelated"))
    monkeypatch.setenv("SHELL", "/custom/shell")
    monkeypatch.setattr(cd_command.subprocess, "run", run)

    assert not layout.home.exists()
    assert cli.main([selector, "cd"]) == 0
    assert cli.main([selector, "cd", "--"]) == 0

    assert (
        calls == [(["/custom/shell", "-i"], {"cwd": layout.home, "check": False})] * 2
    )
    assert not (tmp_path / "unrelated").exists()
    if selector_kind == "roaming":
        assert not layout.runtime.exists()
        assert layout.program.is_symlink()
        assert layout.program.resolve() == source
        assert layout.config.is_file()
        assert fetches == []
    else:
        assert layout.runtime_status.exists()
        assert fetches == ["https://agents.example/alice.too"]


def test_cd_reports_remote_preparation_failure_without_launching(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    layout = AgentLayout.visiting(
        "https://agents.example/alice.too", "alice", toolang_root=tmp_path / "toolang"
    )

    def fetch(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("remote source unavailable")

    monkeypatch.setattr(agent_sources, "fetch_agent_ref", fetch)
    monkeypatch.setattr(cd_command.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cd_command.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        cd_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("failed preparation started a shell"),
    )

    assert cli.main(["https://agents.example/alice.too", "cd"]) == 1
    assert "remote source unavailable" in capsys.readouterr().err
    assert not layout.program.exists()
    assert not layout.run_store.exists()


def test_cd_resident_name_is_available_through_explicit_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_home = _create_agent(tmp_path, "cd")
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr(cd_command, "_require_interactive_terminal", lambda *_: None)
    working_directories: list[Path] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        working_directories.append(kwargs["cwd"])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cd_command.subprocess, "run", run)

    assert cli.main(["agent:cd", "cd"]) == 0
    assert working_directories == [expected_home]
    group = typer.main.get_command(cli.app)
    assert isinstance(group, TyperGroup)
    assert {"home", "shell"}.isdisjoint(group.commands)


@pytest.mark.parametrize("placement", ["roaming", "visiting"])
@pytest.mark.parametrize(
    ("tail", "stdin_tty", "stdout_tty", "exit_code", "message"),
    [
        (["--help"], False, False, 0, "Enter an agent home"),
        (["-h"], True, True, 0, "Enter an agent home"),
        (["--unknown"], True, True, 2, "No such option"),
        (["extra"], True, True, 2, "unexpected extra argument"),
        ([], False, True, 1, "cd requires an interactive terminal"),
        ([], True, False, 1, "cd requires an interactive terminal"),
    ],
)
def test_cd_does_not_prepare_targets_for_help_or_invalid_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    placement: str,
    tail: list[str],
    stdin_tty: bool,
    stdout_tty: bool,
    exit_code: int,
    message: str,
) -> None:
    if placement == "roaming":
        source = tmp_path / "alice.too"
        source.write_text("agic:\n  Reply directly.\n")
        layout = AgentLayout.roaming(source)
        selector = str(source)
    else:
        layout = AgentLayout(tmp_path / "visiting", "alice", "visiting")
        selector = "https://agents.example/alice.too"
        monkeypatch.setattr(
            AgentLayout,
            "visiting",
            classmethod(lambda _cls, source, name, **kwargs: layout),
        )

    fetches: list[str] = []

    def fetch(ref: agent_sources.AgentRef, **_kwargs: Any) -> str:
        fetches.append(ref.render())
        return "agic:\n  Reply directly.\n"

    monkeypatch.setattr(agent_sources, "fetch_agent_ref", fetch)
    monkeypatch.setattr(cd_command.sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(cd_command.sys.stdout, "isatty", lambda: stdout_tty)
    monkeypatch.setattr(
        cd_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid invocation started a shell"),
    )

    assert cli.main([selector, "cd", *tail]) == exit_code
    output = capsys.readouterr()
    assert message in strip_ansi(output.out + output.err)
    assert fetches == []
    assert not layout.root.exists()


def test_cd_reports_roaming_filesystem_errors_without_launching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "alice.too"
    source.write_text("agic:\n  Reply directly.\n")
    root = tmp_path / ".toolang"
    root.write_text("existing file")
    monkeypatch.setattr(cd_command.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cd_command.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        cd_command.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("failed preparation started a shell"),
    )

    assert cli.main([str(source), "cd"]) == 1
    assert "Not a directory" in capsys.readouterr().err
    assert root.read_text() == "existing file"
