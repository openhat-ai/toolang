from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from importlib import import_module
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from typing import Any

import pytest
import typer
from typer._click.utils import strip_ansi
from typer.core import TyperGroup

import toolang.cli.toolang.main as cli
from toolang.up import source as agent_sources

from toolang.base.types.progress import ProgressEvent
from toolang.common.layout import AgentLayout
from toolang.cli.toolang.commands import script
from toolang.cli.toolang.commands.chat import main as chat_commands
from toolang.cli.toolang.commands.workspace import workspace_app
from toolang.execution.types import SessionSetting
from toolang.cli.common.agent_server import AgentServerRef
from toolang.cli.common.lazy import LazyCommand


def _call_main(arguments: list[str]) -> int:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "argv", ["toolang", *arguments])
        try:
            return cli.main(arguments)
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1


def test_cli_command_registry_matches_the_typer_surface() -> None:
    group = typer.main.get_command(cli.app)

    assert isinstance(group, TyperGroup)
    assert set(group.commands) == set(cli._REGISTERED_COMMANDS)


@pytest.mark.parametrize("target", ["resident", "url", "github", "source"])
@pytest.mark.parametrize(
    "options", [[], ["--all"], ["--query", "psyche/english-coach"]]
)
@pytest.mark.parametrize(
    "command",
    [["caps"], *[[kind, "list"] for kind in ("psyche", "skill", "service", "prompt")]],
)
def test_caps_inspects_the_selected_agent_layout(
    tmp_path, monkeypatch, capsys, target, options, command
):
    from toolang.cli.caps import commands

    source = tmp_path / "lex.too"
    source.write_text(
        "psyche english-coach:\n  Help the user practice English.\n\nagic:\n  {{_}}\n"
    )
    root = tmp_path / "root"
    if target == "source":
        layout = AgentLayout.roaming(source)
        selector = str(source)
    else:
        placement = "resident" if target == "resident" else "visiting"
        layout = AgentLayout(root, "lex", placement)
        layout.home.mkdir(parents=True)
        layout.program.write_text(source.read_text())
        selector = {
            "resident": "lex",
            "url": "https://example.com/lex.too",
            "github": "gh:example/lex",
        }[target]
        monkeypatch.setattr(
            agent_sources, "resolve_visiting_layout", lambda *_args, **_kwargs: layout
        )

    inspect_caps = commands.inspect_agent_caps
    prepared = []

    def inspect(selected, **kwargs):
        prepared.append(selected)
        return inspect_caps(selected, **kwargs)

    monkeypatch.setattr(commands, "inspect_agent_caps", inspect)
    args = [selector, *command, *options]
    if target != "source":
        args = ["--root", str(root), *args]
    assert _call_main(args) == 0
    output = capsys.readouterr()
    if command[0] in {"caps", "psyche"}:
        assert "english-coach" in output.out
    else:
        assert f"0 {command[0]}s" in output.out
    assert prepared == [layout]


@pytest.mark.parametrize("target", ["url", "github", "source"])
@pytest.mark.parametrize("kind", ["psyche", "skill", "service", "prompt"])
@pytest.mark.parametrize("operation", ["new", "edit", "add", "remove", "delete"])
def test_nonresident_cap_mutations_remain_rejected(
    target, kind, operation, tmp_path, monkeypatch, capsys
):
    from toolang.cli.caps import commands

    source = tmp_path / "lex.too"
    source.write_text("agic:\n  Reply directly.\n")
    layout = AgentLayout(tmp_path / "cache", "lex", "visiting")
    selectors = {
        "url": "https://example.com/lex.too",
        "github": "gh:example/lex",
        "source": str(source),
    }
    monkeypatch.setattr(
        agent_sources, "resolve_visiting_layout", lambda *_args, **_kwargs: layout
    )
    monkeypatch.setattr(
        commands, "edit_markdown", lambda *_args: pytest.fail("must not open editor")
    )
    monkeypatch.setattr(
        commands.cap_source,
        "resolve_remote_ref",
        lambda *_args, **_kwargs: pytest.fail("must not fetch a cap"),
    )
    assert _call_main([selectors[target], kind, operation, "example"]) == 1
    assert (
        "cap changes require a resident agent; clone the source first"
        in capsys.readouterr().err
    )
    assert source.read_text() == "agic:\n  Reply directly.\n"
    assert not layout.root.exists()
    assert not (tmp_path / "toolang.toml").exists()


def test_lazy_command_completes_options_using_typer_parameters() -> None:
    group = typer.main.get_command(cli.app)
    assert isinstance(group, TyperGroup)
    command = group.commands["fmt"]
    assert isinstance(command, LazyCommand)

    with command.make_context("fmt", [], resilient_parsing=True) as ctx:
        completions = command.shell_complete(ctx, "--tab")

    assert [item.value for item in completions] == ["--tab-size"]
    assert completions[0].help == "Number of spaces per indentation level"


def test_thread_option_registration_keeps_chat_runtime_imports_lazy() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import toolang.cli.toolang.main; "
            "assert 'toolang.cli.toolang.commands.chat' not in sys.modules; "
            "assert 'toolang.cli.common.agent_server' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "arguments",
    (
        ["models", "alice"],
        ["models", "agent:alice"],
    ),
)
def test_cli_models_rejects_unsupported_target_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = _call_main(arguments)
    output = capsys.readouterr()

    assert result == 2
    assert output.err
    assert "Traceback" not in output.err
    assert not tuple(tmp_path.iterdir())


def test_cli_formats_a_routing_error_without_a_panel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main(["alice", "remove"])
    output = capsys.readouterr()
    stderr = strip_ansi(output.err)
    lines = stderr.splitlines()

    assert result == 2
    assert lines[0].startswith("Error: ")
    assert lines[-1].strip()
    assert "╭" not in stderr
    assert "Command 'remove' is only available at the top level." in stderr


def test_cli_no_args_still_shows_root_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main([])
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert "Usage: toolang [OPTIONS] <COMMAND> [ARGUMENTS]" in stdout.splitlines()
    assert "Toolang — a language and runtime for agents and humans." in stdout
    assert output.err == ""


def test_cli_control_commands_have_consistent_order_and_descriptions() -> None:
    group = typer.main.get_command(cli.app)
    expected = {
        "chat": "Chat directly with an agent",
        "steer": "Steer an active run",
        "cancel": "Cancel an active run",
        "rerun": "Rerun an earlier run as a new one",
        "fork": "Fork a thread from an earlier run",
        "rewind": "Rewind a thread to an earlier run",
    }

    assert isinstance(group, TyperGroup)
    context = typer.Context(group)
    selected = group.get_command(context, "alice")
    assert isinstance(selected, TyperGroup)
    order = tuple(name for name in selected.list_commands(context) if name in expected)

    assert order == tuple(expected)
    assert {name: group.commands[name].help for name in expected} == expected


@pytest.mark.parametrize("arguments", [[], ["--help"], ["-h"]])
def test_cli_visible_commands_follow_the_public_panel_order(arguments, capsys) -> None:
    group = typer.main.get_command(cli.app)
    expected = {
        "Agent commands": (
            "new",
            "clone",
            "remove",
            "list",
            "info",
            "home",
        ),
        "Cap commands": ("psyche", "skill", "service", "prompt"),
        "Runtime commands": ("serve", "start", "stop", "hub"),
        "Work commands": ("chat", "talk", "chore", "convo", "workspace"),
        "Inspection commands": (
            "caps",
            "tools",
            "models",
            "providers",
            "inspect",
            "top",
        ),
        "Script commands": ("init", "run"),
    }

    assert isinstance(group, TyperGroup)
    context = typer.Context(group)
    visible = tuple(
        name for name in group.list_commands(context) if not group.commands[name].hidden
    )

    assert visible == tuple(name for names in expected.values() for name in names)
    assert "text" not in group.commands
    for panel, names in expected.items():
        assert {
            getattr(group.commands[name], "rich_help_panel", None) for name in names
        } == {panel}

    assert _call_main(arguments) == 0
    output = capsys.readouterr()
    assert not output.err
    help_text = strip_ansi(output.out)
    panels = [*expected, "Options"]
    positions = [help_text.index(f"{panel}:") for panel in panels]
    assert positions == sorted(positions)
    for (panel, names), next_panel in zip(expected.items(), panels[1:]):
        section = help_text.split(f"{panel}:\n", 1)[1].split(f"{next_panel}:", 1)[0]
        assert (
            tuple(line.split()[0] for line in section.splitlines() if line.strip())
            == names
        )


def test_workspace_commands_follow_the_public_order() -> None:
    group = typer.main.get_command(workspace_app())

    assert isinstance(group, TyperGroup)
    assert tuple(group.list_commands(typer.Context(group))) == (
        "list",
        "add",
        "remove",
    )


def test_cli_exposes_plural_list_resources_and_removes_channels() -> None:
    group = typer.main.get_command(cli.app)
    expected_help = {
        "inspect": "Inspect agent runs",
        "home": "Open a shell in agent home",
        "caps": "List available caps",
        "models": "List available models",
        "providers": "List available model providers",
        "tools": "List available tools",
        "catalogs": "List installed model catalogs",
        "adapters": "List installed model adapters",
        "toolsets": "List installed toolsets",
        "sandboxes": "List installed sandboxes",
    }

    assert isinstance(group, TyperGroup)
    removed = {"threads", "runs", "model", "tool", "catalog", "toolset", "sandbox"}
    assert removed.isdisjoint(group.commands)
    assert expected_help.keys() <= group.commands.keys()
    assert {name: group.commands[name].help for name in expected_help} == expected_help
    assert not group.commands["tools"].hidden
    assert {"catalogs", "adapters", "toolsets", "sandboxes"}.isdisjoint(
        group.list_commands(typer.Context(group))
    )
    assert "channel" not in group.commands


@pytest.mark.parametrize(
    ("arguments", "usage", "argument", "syntax_metavar"),
    (
        (
            ["clone"],
            "Usage: toolang clone [OPTIONS] <SOURCE> [TARGET]",
            "TARGET",
            "[TARGET]",
        ),
        (
            ["chat"],
            "Usage: toolang <AGENT> chat [OPTIONS]",
            "AGENT",
            "{AGENT}",
        ),
        (
            ["parse"],
            "Usage: toolang parse [OPTIONS] <SOURCE>",
            "SOURCE",
            "{SOURCE}",
        ),
        (
            ["highlight"],
            "Usage: toolang highlight [OPTIONS] <SOURCE>",
            "SOURCE",
            "{SOURCE}",
        ),
        (
            ["fmt"],
            "Usage: toolang fmt [OPTIONS] [PATH...]",
            "PATH",
            "[PATH]...",
        ),
        (
            ["inspect"],
            "Usage: toolang <AGENT> inspect [OPTIONS] <SUBJECT>...",
            "SUBJECT",
            "SUBJECT...",
        ),
        (
            ["rewind"],
            "Usage: toolang <AGENT> rewind [OPTIONS] <RUN>",
            "RUN",
            "{RUN}",
        ),
        (
            ["fork"],
            "Usage: toolang <AGENT> fork [OPTIONS] <RUN>",
            "RUN",
            "{RUN}",
        ),
    ),
)
def test_cli_argument_panels_show_only_metavars_without_type_or_usage_syntax(
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    usage: str,
    argument: str,
    syntax_metavar: str,
) -> None:
    result = _call_main([*arguments, "--help"])
    stdout = strip_ansi(capsys.readouterr().out)
    row = next(
        line
        for line in stdout.splitlines()
        if line.startswith("  ") and argument in line.split()
    )

    assert result == 0
    assert usage in stdout
    assert row.strip().removeprefix("* ").split("  ", 1)[0] == argument
    assert syntax_metavar not in row


@pytest.mark.parametrize(
    "arguments",
    (
        ["steer"],
        ["alice", "steer"],
        ["agent:alice", "steer"],
        ["alice", "steer", "run_1"],
    ),
)
def test_cli_incomplete_command_shows_help_before_target_validation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    _call_main(["--root", str(tmp_path), *arguments])
    output = capsys.readouterr()

    assert "Usage:" in output.out + output.err
    assert "Steer an active run" in output.out + output.err
    assert "Agent alice not found" not in output.err


def test_cli_complete_command_validates_the_resident_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main(
        [
            "--root",
            str(tmp_path),
            "alice",
            "steer",
            "run_1",
            "Change direction",
        ]
    )
    output = capsys.readouterr()

    assert result == 1
    assert "Agent alice not found" in output.err


def test_cli_bare_resident_target_shows_its_command_help(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    program = tmp_path / "agents" / "alice" / "agent.too"
    program.parent.mkdir(parents=True)
    program.write_text("agic:\n  Reply directly.\n", encoding="utf-8")

    result = _call_main(["--root", str(tmp_path), "alice"])
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)
    panels = (
        "Cap commands",
        "Work commands",
        "Inspection commands",
        "Run commands",
        "Thread commands",
    )

    assert result == 0
    assert stdout.startswith("Run and manage agent alice.\n")
    for control in ("steer", "cancel", "rerun", "fork", "rewind"):
        assert control in stdout
    assert "models" in stdout
    assert "Teaming commands" not in stdout
    sections = {
        panel: stdout.split(f"{panel}:\n", 1)[1].split("\n\n", 1)[0]
        for panel in ("Work commands", "Inspection commands")
    }
    assert "workspace" in sections["Work commands"]
    for command in ("info", "serve", "start", "stop"):
        assert command not in stdout
    assert "top" in sections["Inspection commands"]
    assert "talk" not in stdout
    assert "hub" not in stdout
    assert tuple(stdout.index(panel) for panel in panels) == tuple(
        sorted(stdout.index(panel) for panel in panels)
    )
    assert "channel" not in stdout
    assert "compact" not in stdout
    assert "No such command" not in output.err


def test_cli_explicit_resident_target_preserves_selector_but_labels_the_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main(["--root", str(tmp_path), "agent:alice"])
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert "Usage: toolang agent:alice" in stdout
    assert stdout.startswith("Run and manage agent alice.\n")
    assert "agent agent:alice" not in stdout


def test_cli_bare_visiting_target_shows_help_without_resolving_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        agent_sources,
        "resolve_visiting_layout",
        lambda *_args, **_kwargs: pytest.fail("target help must not resolve the agent"),
    )

    result = _call_main(["gh:briceyan/dev"])
    output = capsys.readouterr()

    assert result == 0
    assert strip_ansi(output.out).startswith("Run and manage agent gh:briceyan/dev.\n")
    assert "chat" in output.out
    assert "No such command" not in output.err


@pytest.mark.parametrize("target", ("missing.too", "./missing.too"))
def test_cli_missing_local_source_syntax_reports_a_script_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = _call_main([target])
    output = capsys.readouterr()
    stderr = strip_ansi(output.err)

    assert result == 1
    assert f"script not found: {target}" in stderr
    assert "No such command" not in stderr
    assert "Run and manage agent" not in output.out


def test_cli_agent_context_is_isolated_between_threads(monkeypatch):
    from toolang.cli.common.context import cli_context
    from toolang.cli.toolang.commands import chat

    barrier = Barrier(2)
    seen = []

    @wraps(chat.chat_command)
    def invoke(ctx, **kwargs):
        barrier.wait(timeout=10)
        seen.append(cli_context(ctx).agent)

    monkeypatch.setattr(chat, "chat_command", invoke)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(cli.main, [name, "chat"]) for name in ("alice", "bob")]
        assert [result.result() for result in results] == [0, 0]
    assert set(seen) == {"alice", "bob"}


@pytest.mark.parametrize(
    ("options", "input_tokens", "expected_input"),
    [
        (["--quiet"], ["--", "text", "--inbox", "literal"], "text --inbox literal"),
        (["--sandbox", "host"], ["text", "--inbox=literal"], "text --inbox=literal"),
        (["--out", "--inbox"], ["text"], "text"),
        (["--dev=--inbox"], ["text"], "text"),
    ],
)
def test_cli_script_root_options_preserve_literal_inbox_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    input_tokens: list[str],
    expected_input: str,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )

    assert cli.main([str(source), *options, "agic:demo", *input_tokens]) == 0
    assert captured["input"] == {"_": expected_input}
    if options[0] == "--out":
        assert captured["save"] == "--inbox"
    if options[0].startswith("--dev="):
        assert captured["dev"] == Path("--inbox")


@pytest.mark.parametrize("literal", ["--root", "-r", "--root=literal"])
@pytest.mark.parametrize("root_option", [False, True])
def test_cli_script_preserves_global_option_names_as_option_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
    root_option: bool,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )
    header = (
        ["--out", literal, "agic:demo"]
        if root_option
        else ["agic:demo", "--out", literal]
    )

    assert cli.main([str(source), *header, "text"]) == 0
    assert captured["save"] == literal
    assert captured["input"] == {"_": "text"}


@pytest.mark.parametrize("literal", ["--root", "-r", "--root=literal"])
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_script_preserves_global_option_names_in_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
    explicit: bool,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )

    assert (
        cli.main(
            [
                str(source),
                "--quiet",
                "agic:demo",
                *(["--"] if explicit else []),
                "text",
                literal,
                "body",
            ]
        )
        == 0
    )
    assert captured["input"] == {"_": f"text {literal} body"}


@pytest.mark.parametrize("before_source", [False, True])
def test_cli_script_rejects_actual_global_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before_source: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    monkeypatch.setattr(
        script,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("invalid input must not run"),
    )
    header = (
        ["--root", str(tmp_path), str(source)]
        if before_source
        else [str(source), "--root", str(tmp_path)]
    )

    expected_code = 2
    expected_error = "global --root / -r" if before_source else "No such option: --root"
    assert cli.main([*header, "agic:demo", "text"]) == expected_code
    error = strip_ansi(capsys.readouterr().err)
    assert expected_error in error


@pytest.mark.parametrize("selector", [[], ["agic:demo"]])
@pytest.mark.parametrize("option", [["--inbox", "requests"], ["--inbox=requests"]])
def test_cli_script_rejects_inbox_option(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    selector: list[str],
    option: list[str],
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")

    assert cli.main([str(source), *selector, *option]) == 2
    assert "No such option: --inbox" in strip_ansi(capsys.readouterr().err)


@pytest.mark.parametrize(
    "command,operands",
    [
        ("chat", []),
        ("chat", ["--thread", "chat_1"]),
        ("inspect", ["runs"]),
        ("steer", ["run_1", "change direction"]),
        ("rerun", ["run_1"]),
    ],
)
def test_cli_routes_roaming_agent_command_to_its_exact_layout(
    tmp_path, monkeypatch, command, operands
):
    from toolang.cli.common.context import cli_context

    module, callback = {
        "chat": ("chat", "chat_command"),
        "inspect": ("inspect", "inspect_command"),
        "steer": ("thread", "steer_command"),
        "rerun": ("thread", "rerun_command"),
    }[command]
    source = tmp_path / "demo.too"
    source.write_text("flow main():\n  pass\n")
    seen = []
    owner = import_module(f"toolang.cli.toolang.commands.{module}")

    @wraps(getattr(owner, callback))
    def invoke(ctx, **kwargs):
        seen.append(cli_context(ctx))

    monkeypatch.setattr(owner, callback, invoke)
    assert cli.main([str(source), command, *operands]) == 0
    assert len(seen) == 1
    assert seen[0].source == source
    assert seen[0].layout == AgentLayout.roaming(source)
    assert not (tmp_path / ".toolang").exists()


def test_cli_opens_roaming_chat_with_its_exact_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic chat:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class Session:
        def __init__(self, endpoint: str, **_kwargs: object) -> None:
            assert endpoint == "http://runtime.test:7001"

        def close(self) -> None:
            captured["closed"] = True

        def thread_title(self, thread_id: str) -> str | None:
            return None

        def initial_setting(self) -> SessionSetting:
            return SessionSetting(model=None, runnable=None)

    def end_input(_prompt: str) -> str:
        raise EOFError

    @contextmanager
    def acquire(selected: AgentLayout, **_kwargs: object) -> Iterator[AgentServerRef]:
        captured["layout"] = selected
        yield AgentServerRef(sandbox="host", endpoint="http://runtime.test:7001")

    monkeypatch.setattr(chat_commands, "acquire_agent_server", acquire)
    monkeypatch.setattr(chat_commands, "RemoteChatSession", Session)
    monkeypatch.setattr("builtins.input", end_input)
    monkeypatch.setattr(chat_commands.sys.stdin, "isatty", lambda: False)

    assert cli.main([str(source), "chat"]) == 0
    assert captured == {
        "layout": AgentLayout.roaming(source),
        "closed": True,
    }


def test_cli_routes_chat_development_artifact_to_the_exact_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic chat:\n  Reply directly.\n", encoding="utf-8")
    development = tmp_path / "dist"
    captured: dict[str, object] = {}

    def interactive(
        ctx: typer.Context,
        *,
        thread_id: str | None,
        selector_payload: dict[str, object] | None = None,
        sandbox: str | None = None,
        dev: Path | None = None,
        **_kwargs: object,
    ) -> None:
        captured.update(
            layout=chat_commands.context_layout(ctx),
            thread=thread_id,
            selectors=selector_payload,
            sandbox=sandbox,
            dev=dev,
        )

    monkeypatch.setattr(chat_commands, "_chat_interactive", interactive)

    assert (
        cli.main(
            [
                str(source),
                "chat",
                "--sandbox",
                "docker",
                "--dev",
                str(development),
            ]
        )
        == 0
    )
    assert captured == {
        "layout": AgentLayout.roaming(source),
        "thread": None,
        "selectors": None,
        "sandbox": "docker",
        "dev": development,
    }


def test_cli_visiting_failure_uses_the_operational_failure_block(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(
        _source: str, *, progress: Any, toolang_root: Path | None = None
    ) -> AgentLayout:
        progress(
            ProgressEvent(
                id="agent:missing",
                kind="prepare",
                stage="fetch",
                label="Failed to fetch agent",
                status="failed",
                detail="remote agent not found",
            )
        )
        raise ValueError("fetch failed")

    monkeypatch.setattr(agent_sources, "resolve_visiting_layout", fail)

    result = cli.main(["gh:missing/agent", "chat"])

    assert result == 1
    error = capsys.readouterr().err
    assert "Failed to fetch agent" in error
    assert "Stage: prepare.fetch" in error
    assert "Reason: remote agent not found" in error


def test_caps_add_failure_uses_the_operational_failure_block(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail(
        _kind: str,
        _ref: str,
        *,
        progress: Any,
    ) -> str:
        progress(
            ProgressEvent(
                id="cap:skill:missing",
                kind="prepare",
                stage="resolve",
                label="Failed to resolve skill",
                status="failed",
                detail="remote skill not found",
            )
        )
        raise ValueError("resolve failed")

    from toolang.cli.caps import commands

    monkeypatch.setattr(commands.cap_source, "resolve_remote_ref", fail)

    result = cli.main(["--root", str(tmp_path / "toolang"), "skill", "add", "missing"])

    assert result == 1
    error = capsys.readouterr().err
    assert "Failed to resolve skill" in error
    assert "Stage: prepare.resolve" in error
    assert "Reason: remote skill not found" in error


def test_cli_visiting_inspect_does_not_materialize(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        agent_sources,
        "resolve_visiting_layout",
        lambda *a, **kw: pytest.fail("inspect must not fetch"),
    )
    assert (
        cli.main(["--root", str(tmp_path), "gh:brice/researcher", "inspect", "runs"])
        == 1
    )
    assert "execution history not found: researcher" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ("threads", "runs"))
def test_cli_unknown_visiting_command_uses_agent_help(command, monkeypatch, capsys):
    monkeypatch.setattr(
        agent_sources,
        "resolve_visiting_layout",
        lambda *a, **kw: pytest.fail("must not fetch"),
    )
    assert cli.main(["gh:brice/researcher", command]) == 2
    error = capsys.readouterr().err
    assert f"No such command '{command}'." in error
    assert "gh:brice/researcher --help" in error


def test_cli_opens_visiting_chat_with_its_exact_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "gh:brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}

    class Session:
        def __init__(self, endpoint: str, **_kwargs: object) -> None:
            assert endpoint == "http://runtime.test:7001"

        def close(self) -> None:
            captured["closed"] = True

        def thread_title(self, thread_id: str) -> str | None:
            return None

        def initial_setting(self) -> SessionSetting:
            return SessionSetting(model=None, runnable=None)

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(
        agent_sources, "resolve_visiting_layout", lambda *_args, **_kwargs: layout
    )

    @contextmanager
    def acquire(selected: AgentLayout, **_kwargs: object) -> Iterator[AgentServerRef]:
        captured["layout"] = selected
        yield AgentServerRef(sandbox="host", endpoint="http://runtime.test:7001")

    monkeypatch.setattr(chat_commands, "acquire_agent_server", acquire)
    monkeypatch.setattr(chat_commands, "RemoteChatSession", Session)
    monkeypatch.setattr("builtins.input", end_input)
    monkeypatch.setattr(chat_commands.sys.stdin, "isatty", lambda: False)

    assert cli.main([selector, "chat"]) == 0
    assert captured == {
        "layout": layout,
        "closed": True,
    }


@pytest.mark.parametrize(
    ("command", "module", "callback", "operands"),
    [
        ("serve", "runtime", "run", []),
        ("start", "runtime", "start", []),
        ("chat", "chat", "chat_command", []),
        ("rerun", "thread", "rerun_command", ["run_example"]),
    ],
)
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ([], None),
        (["--dev"], Path(".")),
        (["--dev", "wheels"], Path("wheels")),
        (["--dev=wheels"], Path("wheels")),
        (
            ["--dev", "wheel directory/toolang-1.whl"],
            Path("wheel directory/toolang-1.whl"),
        ),
        (["--dev=--wheel"], Path("--wheel")),
        (["--dev", "./--wheel"], Path("./--wheel")),
        (["--dev="], Path(".")),
        (["--dev", ""], Path(".")),
        (["--dev", "first", "--dev"], Path(".")),
        (["--dev", "--dev=last"], Path("last")),
        (["--dev", "--allow", "tools=fs/*"], Path(".")),
        (["--dev", "--"], Path(".")),
    ],
)
def test_cli_dev_states_reach_each_lazy_command(
    command, module, callback, operands, options, expected, tmp_path, monkeypatch
):
    owner = import_module(f"toolang.cli.toolang.commands.{module}")
    captured = {}

    @wraps(getattr(owner, callback))
    def invoke(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(owner, callback, invoke)
    prefix = [command, "alice"] if command in {"serve", "start"} else ["alice", command]
    assert cli.main(["--root", str(tmp_path), *prefix, *operands, *options]) == 0
    assert captured["dev"] == expected
    if expected is not None:
        assert isinstance(captured["dev"], Path)


@pytest.mark.parametrize("command", ["serve", "start", "chat", "rerun"])
def test_cli_bare_dev_keeps_unknown_option_errors(command, tmp_path, capsys):
    prefix = [command, "alice"] if command in {"serve", "start"} else ["alice", command]
    assert cli.main(["--root", str(tmp_path), *prefix, "--dev", "--unknown"]) == 2
    assert "No such option: --unknown" in strip_ansi(capsys.readouterr().err)


def test_remote_agent_parse_failure_prevents_launch(tmp_path, monkeypatch, capsys):
    from toolang.cli.toolang.commands import runtime

    monkeypatch.setattr(agent_sources, "_fetch_http_text", lambda *_: "flow broken(:\n")
    monkeypatch.setattr(
        runtime,
        "resolve_startup",
        lambda *a, **kw: pytest.fail("invalid agent must not launch"),
    )
    assert (
        cli.main(["--root", str(tmp_path), "serve", "https://example.test/broken.too"])
        == 1
    )
    assert "Failed to parse agent" in capsys.readouterr().err
