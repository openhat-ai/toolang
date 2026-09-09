from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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

import toolang.cli.caps.main as caps_cli
import toolang.cli.toolang.main as cli
from toolang.base.types.progress import ProgressEvent
from toolang.common.layout import AgentLayout
from toolang.cli.toolang.commands import script
from toolang.cli.toolang.commands.chat import main as chat_commands
from toolang.cli.toolang.commands.workspace import workspace_app
from toolang.execution.types import SessionSetting
from toolang.cli.toolang.routing import (
    COMMAND_SPECS,
    RoutingError,
    TargetHelp,
    dispatch_roaming,
    dispatch_visiting,
    normalize,
    select_target_help,
)
from toolang.cli.common.routing import extract_root_args
from toolang.cli.common.lazy import LazyCommand
from toolang.up import process as agents


def _call_main(arguments: list[str]) -> int:
    try:
        return cli.main(arguments)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def test_extract_root_args_supports_short_option_and_stops_at_separator() -> None:
    root_args, body = extract_root_args(
        ("-r", "/tmp/root", "alice", "chat", "--", "--root", "message")
    )

    assert root_args == ["-r", "/tmp/root"]
    assert body == ["alice", "chat", "--", "--root", "message"]


def test_cli_command_registry_matches_the_typer_surface() -> None:
    group = typer.main.get_command(cli.app)

    assert isinstance(group, TyperGroup)
    assert set(group.commands) == set(COMMAND_SPECS)


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
    ("command", "targets", "placements"),
    (
        ("new", {"none"}, set()),
        ("remove", {"after"}, {"resident"}),
        ("info", {"before", "after"}, {"resident", "roaming", "visiting"}),
        ("retry", {"before"}, {"resident", "roaming", "visiting"}),
        ("compact", {"before"}, {"resident", "roaming", "visiting"}),
        ("task", {"before"}, {"resident"}),
        ("workspace", {"before"}, {"resident"}),
        ("skill", {"none", "before"}, {"resident"}),
        ("models", {"none", "before"}, {"resident"}),
        ("catalogs", {"none"}, set()),
        ("toolsets", {"none"}, set()),
    ),
)
def test_cli_command_registry_declares_target_grammar(
    command: str,
    targets: set[str],
    placements: set[str],
) -> None:
    spec = COMMAND_SPECS[command]

    assert spec.targets == targets
    assert spec.placements == placements


def test_cli_normalize_routes_resident_target_before_command() -> None:
    args, agent = normalize(
        ["-r", "/tmp/root", "alice", "retry", "run_1"],
    )

    assert args == ["-r", "/tmp/root", "retry", "run_1"]
    assert agent == "alice"


def test_cli_normalize_preserves_a_command_models_catalog_override() -> None:
    args, agent = normalize(
        ["alice", "retry", "run_1", "--catalog", "/tmp/models.json"],
    )

    assert args == ["retry", "run_1", "--catalog", "/tmp/models.json"]
    assert agent == "alice"


@pytest.mark.parametrize("target", ["alice", "agent:alice", "agent:models"])
def test_cli_normalize_routes_resident_models(target: str) -> None:
    args, agent = normalize(
        ["-r", "/tmp/root", target, "models", "--catalog", "/tmp/catalog.json"],
    )

    assert args == ["-r", "/tmp/root", "models", "--catalog", "/tmp/catalog.json"]
    assert agent == target.removeprefix("agent:")


@pytest.mark.parametrize(
    "arguments",
    (
        ["models", "alice"],
        ["models", "agent:alice"],
        ["alice.too", "models"],
        ["briceyan/dev", "models"],
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


def test_cli_normalize_allows_both_orders_for_agent_self_commands() -> None:
    prefix_args, prefix_agent = normalize(["alice", "info"])
    postfix_args, postfix_agent = normalize(["info", "alice"])

    assert (prefix_args, prefix_agent) == (["info", "alice"], None)
    assert (postfix_args, postfix_agent) == (["info", "alice"], None)


def test_cli_normalize_defers_missing_target_to_command_help() -> None:
    assert normalize(["retry", "alice", "run_1"]) == (
        ["retry", "alice", "run_1"],
        None,
    )


def test_cli_normalize_rejects_an_invalid_target_order() -> None:
    with pytest.raises(RoutingError, match="remove requires TARGET after"):
        normalize(["alice", "remove"])


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
    assert "remove requires TARGET after the command" in stderr


def test_cli_no_args_still_shows_root_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main([])
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert "Usage: pytest [OPTIONS] COMMAND [ARGS]" in stdout.splitlines()
    assert "Run and manage Toolang agents" in stdout
    assert output.err == ""


def test_cli_control_commands_have_consistent_order_and_descriptions() -> None:
    group = typer.main.get_command(cli.app)
    expected = {
        "chat": "Start an interactive TUI",
        "steer": "Steer an active run",
        "cancel": "Cancel an active run",
        "retry": "Retry a run from a failed step",
        "rerun": "Rerun an earlier run as a new one",
        "fork": "Fork a thread from an earlier run",
        "rewind": "Rewind a thread to an earlier run",
        "compact": "Compact a thread",
    }

    assert isinstance(group, TyperGroup)
    context = typer.Context(group)
    order = tuple(name for name in group.list_commands(context) if name in expected)

    assert order == tuple(expected)
    assert {name: group.commands[name].help for name in expected} == expected


@pytest.mark.parametrize("extended", [False, True])
def test_compact_help_lists_the_public_runnable_signature(
    monkeypatch, capsys, extended
):
    from toolang.cli.toolang.commands import compact
    from toolang.cli.common.runnable_parameters import RunnableArgument

    runnable = compact.compact_runnable()
    if extended:
        runnable = replace(
            runnable,
            params=(
                *runnable.params,
                replace(
                    runnable.params[0],
                    name="page_size",
                    type_name="Number",
                    optional=True,
                    doc="History page size.",
                ),
            ),
        )
        monkeypatch.setattr(compact, "compact_runnable", lambda: runnable)
    monkeypatch.setattr(
        compact, "SetupWatcher", lambda *a, **kw: pytest.fail("help must not prepare")
    )
    assert _call_main(["compact", "--help"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    output = strip_ansi(captured.out)
    assert "Compact a thread" in output and "NAME=VALUE" not in output
    assert "previous" not in output
    positions = [output.index(f"{param.name}=ARGUMENT") for param in runnable.params]
    assert positions == sorted(positions)
    if extended:
        assert "History page size." in output

    group = typer.main.get_command(cli.app)
    assert isinstance(group, TyperGroup)
    command = group.commands["compact"]
    assert isinstance(command, LazyCommand)
    arguments = [
        param for param in command.load().params if isinstance(param, RunnableArgument)
    ]
    assert [(param.name, param.required) for param in arguments] == [
        (param.name, not param.optional) for param in runnable.params
    ]


def test_cli_visible_commands_follow_the_public_panel_order() -> None:
    group = typer.main.get_command(cli.app)
    expected = {
        "Agent Commands": (
            "new",
            "clone",
            "remove",
            "list",
            "info",
            "run",
            "start",
            "stop",
        ),
        "Cap Commands": ("psyche", "skill", "service", "prompt"),
        "Work Commands": ("chore", "task", "workspace"),
        "Control Commands": (
            "chat",
            "steer",
            "cancel",
            "retry",
            "rerun",
            "fork",
            "rewind",
            "compact",
        ),
        "Inspection Commands": (
            "inspect",
            "caps",
            "tools",
            "models",
            "providers",
            "catalogs",
            "adapters",
            "toolsets",
            "sandboxes",
        ),
    }

    assert isinstance(group, TyperGroup)
    context = typer.Context(group)
    visible = tuple(
        name for name in group.list_commands(context) if not group.commands[name].hidden
    )

    assert visible == tuple(name for names in expected.values() for name in names)
    for panel, names in expected.items():
        assert {
            getattr(group.commands[name], "rich_help_panel", None) for name in names
        } == {panel}


def test_workspace_commands_follow_the_public_order() -> None:
    group = typer.main.get_command(workspace_app())

    assert isinstance(group, TyperGroup)
    assert tuple(group.list_commands(typer.Context(group))) == (
        "list",
        "add",
        "remove",
    )


def test_cli_exposes_plural_list_resources_and_hides_channels() -> None:
    group = typer.main.get_command(cli.app)
    expected_help = {
        "inspect": "Inspect agent run history",
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
    assert group.commands["channel"].hidden


@pytest.mark.parametrize(
    ("arguments", "usage", "argument", "argument_type", "syntax_metavar"),
    (
        (
            ["clone"],
            "Usage: pytest clone [OPTIONS] SOURCE [TARGET]",
            "TARGET",
            "TEXT",
            "[TARGET]",
        ),
        (
            ["chat"],
            "Usage: pytest AGENT chat [OPTIONS]",
            "AGENT",
            "TEXT",
            "{AGENT}",
        ),
        (
            ["fmt"],
            "Usage: pytest fmt [OPTIONS] [PATH...]",
            "PATH",
            "PATH",
            "[PATH]...",
        ),
        (
            ["inspect"],
            "Usage: pytest AGENT inspect [OPTIONS] SUBJECT...",
            "SUBJECT",
            "TEXT",
            "SUBJECT...",
        ),
        (
            ["rewind"],
            "Usage: pytest AGENT rewind [OPTIONS] RUN",
            "RUN",
            "TEXT",
            "{RUN}",
        ),
        (
            ["fork"],
            "Usage: pytest AGENT fork [OPTIONS] RUN",
            "RUN",
            "TEXT",
            "{RUN}",
        ),
    ),
)
def test_cli_argument_panels_separate_names_types_and_usage_syntax(
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    usage: str,
    argument: str,
    argument_type: str,
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
    assert argument_type in row
    assert syntax_metavar not in row


def test_cli_explicit_agent_prefix_resolves_command_name_collision() -> None:
    args, agent = normalize(["agent:retry", "info"])

    assert args == ["info", "retry"]
    assert agent is None


def test_cli_command_name_wins_without_explicit_agent_prefix() -> None:
    assert normalize(["retry", "info"]) == (["retry", "info"], None)


@pytest.mark.parametrize(
    ("arguments", "residents", "expected"),
    (
        (
            ["alice"],
            {"alice"},
            TargetHelp(selector="alice", label="alice", placement="resident"),
        ),
        (
            ["alice", "--help"],
            {"alice"},
            TargetHelp(selector="alice", label="alice", placement="resident"),
        ),
        (
            ["agent:missing"],
            set(),
            TargetHelp(
                selector="agent:missing",
                label="missing",
                placement="resident",
            ),
        ),
        (
            ["agent:alice.too"],
            set(),
            TargetHelp(
                selector="agent:alice.too",
                label="alice.too",
                placement="resident",
            ),
        ),
        (
            ["briceyan/dev"],
            set(),
            TargetHelp(
                selector="briceyan/dev",
                label="briceyan/dev",
                placement="visiting",
            ),
        ),
        (
            ["https://toolang.ai/dev.too"],
            set(),
            TargetHelp(
                selector="https://toolang.ai/dev.too",
                label="https://toolang.ai/dev.too",
                placement="visiting",
            ),
        ),
        (["retry"], {"retry"}, None),
        (
            ["threads"],
            {"threads"},
            TargetHelp(selector="threads", label="threads", placement="resident"),
        ),
        (["unknown"], set(), None),
    ),
)
def test_cli_selects_only_unambiguous_targets_without_a_command(
    arguments: list[str],
    residents: set[str],
    expected: TargetHelp | None,
) -> None:
    assert select_target_help(arguments, residents=residents) == expected


def test_caps_cli_uses_command_priority_and_explicit_agent_prefix() -> None:
    global_args, global_agent = caps_cli._rewrite_agent_shortcuts(
        ["skill", "list"],
    )
    agent_args, agent = caps_cli._rewrite_agent_shortcuts(
        ["agent:skill", "skill", "list"],
    )

    assert (global_args, global_agent) == (["skill", "list"], None)
    assert (agent_args, agent) == (["skill", "list"], "skill")


def test_caps_cli_formats_a_pre_dispatch_error_without_a_panel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = caps_cli.main(["agent:", "skill"])
    output = capsys.readouterr()
    stderr = strip_ansi(output.err)

    assert result == 2
    assert stderr.startswith("Error: ")
    assert "╭" not in stderr
    assert "invalid resident agent target: agent:" in stderr


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
        "Agent Commands",
        "Cap Commands",
        "Work Commands",
        "Control Commands",
        "Inspection Commands",
    )

    assert result == 0
    assert stdout.startswith("Run and manage agent alice.\n")
    assert "steer" in stdout
    assert "models" in stdout
    assert tuple(stdout.index(panel) for panel in panels) == tuple(
        sorted(stdout.index(panel) for panel in panels)
    )
    assert "channel" not in stdout
    assert "No such command" not in output.err


def test_cli_explicit_resident_target_preserves_selector_but_labels_the_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main(["--root", str(tmp_path), "agent:alice"])
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert "Usage: pytest agent:alice" in stdout
    assert stdout.startswith("Run and manage agent alice.\n")
    assert "agent agent:alice" not in stdout


def test_cli_bare_visiting_target_shows_help_without_resolving_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        agents,
        "resolve_visiting_layout",
        lambda *_args, **_kwargs: pytest.fail("target help must not resolve the agent"),
    )

    result = _call_main(["briceyan/dev"])
    output = capsys.readouterr()

    assert result == 0
    assert strip_ansi(output.out).startswith("Run and manage agent briceyan/dev.\n")
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


def test_cli_prefix_agent_context_is_isolated_between_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = Barrier(2)
    seen: list[str | None] = []

    def fake_run(_app: typer.Typer, **_kwargs: Any) -> int:
        barrier.wait()
        seen.append(cli._PREFIX_AGENT.get())
        return 0

    monkeypatch.setattr(cli, "run", fake_run)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.submit(
                cli._run_app,
                [],
                agent,
                prog_name="toolang",
            )
            for agent in ("alice", "bob")
        )

    assert [result.result() for result in results] == [0, 0]
    assert set(seen) == {"alice", "bob"}
    assert cli._PREFIX_AGENT.get() is None


@pytest.mark.parametrize("runnable", ("demo", "threads", "runs"))
def test_cli_routes_local_script_to_script_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runnable: str,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text(f"agic {runnable}:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_dispatch(
        global_args: list[str],
        argv: list[str],
        *,
        prog_name: str,
    ) -> int:
        captured.update(
            global_args=global_args,
            argv=argv,
            prog_name=prog_name,
        )
        return 7

    monkeypatch.setattr(script, "dispatch", fake_dispatch)

    result = dispatch_roaming(
        [str(source), runnable, "--help"],
        prog_name="too",
        run_app=lambda *_args: pytest.fail("Typer app should not run"),
    )

    assert result == 7
    assert captured == {
        "global_args": [],
        "argv": [str(source), runnable, "--help"],
        "prog_name": "too",
    }


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

    expected_code = 1 if before_source else 2
    expected_error = "global CLI options" if before_source else "No such option: --root"
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
    "arguments",
    (
        ["info"],
        ["chat"],
        ["chat", "--thread"],
        ["chat", "--thread", "term_1"],
        ["chat", "-t", "term_1"],
        ["inspect", "run_1"],
        ["steer", "run_1", "change direction"],
        ["retry", "run_1"],
        ["rerun", "run_1"],
    ),
)
def test_cli_routes_roaming_agent_command_to_its_exact_layout(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_app(args: list[str], layout: AgentLayout) -> int:
        captured.update(args=args, layout=layout)
        return 9

    result = dispatch_roaming(
        [str(source), *arguments],
        prog_name="too",
        run_app=fake_run_app,
    )

    assert result == 9
    assert captured == {
        "args": ["info", source.stem] if arguments == ["info"] else arguments,
        "layout": AgentLayout.roaming(source),
    }


def test_cli_opens_roaming_chat_with_its_exact_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic chat:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class Session:
        def __init__(self, layout: AgentLayout, **_kwargs: object) -> None:
            captured["layout"] = layout

        def close(self) -> None:
            captured["closed"] = True

        def initial_setting(self) -> SessionSetting:
            return SessionSetting(model=None, runnable=None)

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(chat_commands, "LocalChatSession", Session)
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


def test_cli_routes_visiting_chat_through_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}

    def resolve(source: str, *, progress: object) -> AgentLayout:
        captured.update(source=source, progress=progress)
        return layout

    def run_app(args: list[str], selected: AgentLayout) -> int:
        captured.update(args=args, layout=selected)
        return 12

    monkeypatch.setattr(agents, "resolve_visiting_layout", resolve)

    result = dispatch_visiting(
        [selector, "chat", "--thread", "term_1"],
        run_app=run_app,
    )

    assert result == 12
    assert captured["source"] == selector
    assert captured["args"] == ["chat", "--thread", "term_1"]
    assert captured["layout"] == layout


def test_cli_visiting_failure_uses_the_operational_failure_block(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_source: str, *, progress: Any) -> AgentLayout:
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

    monkeypatch.setattr(agents, "resolve_visiting_layout", fail)

    result = dispatch_visiting(
        ["missing/agent", "chat"],
        run_app=lambda _args, _layout: pytest.fail("failed resolution must not run"),
    )

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

    monkeypatch.setattr(caps_cli.commands.cap_state, "resolve_remote_ref", fail)

    result = caps_cli.main(
        ["--root", str(tmp_path / "toolang"), "skill", "add", "missing"]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "Failed to resolve skill" in error
    assert "Stage: prepare.resolve" in error
    assert "Reason: remote skill not found" in error


def test_cli_routes_visiting_inspect_without_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(agents, "visiting_layout", lambda source: layout)
    monkeypatch.setattr(
        agents,
        "resolve_visiting_layout",
        lambda *_args, **_kwargs: pytest.fail("inspect must not materialize"),
    )

    result = dispatch_visiting(
        [selector, "inspect", "run_1"],
        run_app=lambda args, selected: (
            captured.update(
                args=args,
                layout=selected,
            )
            or 13
        ),
    )

    assert result == 13
    assert captured == {
        "args": ["inspect", "run_1"],
        "layout": layout,
    }


@pytest.mark.parametrize("command", ("threads", "runs"))
def test_cli_does_not_route_removed_history_commands_for_visiting(
    command: str,
) -> None:
    result = dispatch_visiting(
        ["brice/researcher", command],
        run_app=lambda *_args: pytest.fail("removed command must not be routed"),
    )

    assert result is None


def test_cli_routes_command_before_visiting_info_through_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        agents,
        "resolve_visiting_layout",
        lambda source, *, progress: (
            captured.update(
                source=source,
                progress=progress,
            )
            or layout
        ),
    )

    result = dispatch_visiting(
        ["info", selector],
        run_app=lambda args, selected: (
            captured.update(
                args=args,
                layout=selected,
            )
            or 14
        ),
    )

    assert result == 14
    assert captured["source"] == selector
    assert captured["args"] == ["info", "researcher"]
    assert captured["layout"] == layout


def test_cli_opens_visiting_chat_with_its_exact_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "brice/researcher"
    layout = AgentLayout(
        root=tmp_path / "visiting",
        name="researcher",
        placement="visiting",
    )
    captured: dict[str, object] = {}

    class Session:
        def __init__(self, selected: AgentLayout, **_kwargs: object) -> None:
            captured["layout"] = selected

        def close(self) -> None:
            captured["closed"] = True

        def initial_setting(self) -> SessionSetting:
            return SessionSetting(model=None, runnable=None)

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(
        agents, "resolve_visiting_layout", lambda *_args, **_kwargs: layout
    )
    monkeypatch.setattr(chat_commands, "LocalChatSession", Session)
    monkeypatch.setattr("builtins.input", end_input)
    monkeypatch.setattr(chat_commands.sys.stdin, "isatty", lambda: False)

    assert cli.main([selector, "chat"]) == 0
    assert captured == {
        "layout": layout,
        "closed": True,
    }


def test_cli_typed_runnable_prefix_escapes_a_roaming_command_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demo.too"
    source.write_text("agic demo:\n  Reply directly.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_dispatch(
        global_args: list[str],
        argv: list[str],
        *,
        prog_name: str,
    ) -> int:
        captured.update(
            global_args=global_args,
            argv=argv,
            prog_name=prog_name,
        )
        return 11

    monkeypatch.setattr(script, "dispatch", fake_dispatch)

    result = dispatch_roaming(
        [str(source), "runnable:steer", "change direction"],
        prog_name="too",
        run_app=lambda *_args: pytest.fail("typed runnable should not be a command"),
    )

    assert result == 11
    assert captured["argv"] == [
        str(source),
        "runnable:steer",
        "change direction",
    ]


@pytest.mark.parametrize(
    ("command", "module", "callback", "operands"),
    [
        ("run", "runtime", "run", []),
        ("start", "runtime", "start", []),
        ("chat", "chat", "chat_command", []),
        ("retry", "thread", "retry_command", ["run_example"]),
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
    assert (
        cli.main(["--root", str(tmp_path), "alice", command, *operands, *options]) == 0
    )
    assert captured["dev"] == expected
    if expected is not None:
        assert isinstance(captured["dev"], Path)


@pytest.mark.parametrize("command", ["run", "start", "chat", "retry", "rerun"])
def test_cli_bare_dev_keeps_unknown_option_errors(command, tmp_path, capsys):
    assert (
        cli.main(["--root", str(tmp_path), "alice", command, "--dev", "--unknown"]) == 2
    )
    assert "No such option: --unknown" in strip_ansi(capsys.readouterr().err)
