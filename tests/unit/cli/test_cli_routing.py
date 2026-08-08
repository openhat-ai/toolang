from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import click
import pytest
import typer

import toolang.cli.caps.main as caps_cli
import toolang.cli.toolang.main as cli
from toolang.common.layout import AgentLayout
from toolang.cli.toolang.commands import script
from toolang.cli.toolang.commands.chat import main as chat_commands
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

    assert isinstance(group, click.Group)
    assert set(group.commands) == set(COMMAND_SPECS)


@pytest.mark.parametrize(
    ("command", "targets", "placements"),
    (
        ("new", {"none"}, set()),
        ("remove", {"after"}, {"resident"}),
        ("info", {"before", "after"}, {"resident", "roaming", "visiting"}),
        ("retry", {"before"}, {"resident", "roaming", "visiting"}),
        ("task", {"before"}, {"resident"}),
        ("skill", {"none", "before"}, {"resident"}),
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


def test_cli_formats_a_routing_error_as_a_rich_panel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main(["alice", "remove"])
    output = capsys.readouterr()
    stderr = click.unstyle(output.err)
    lines = stderr.splitlines()

    assert result == 2
    assert lines[0].strip() == ""
    assert lines[1].startswith("╭─ Error ")
    assert lines[-1].strip() == ""
    assert "remove requires TARGET after the command" in stderr


def test_cli_no_args_still_shows_root_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main([])
    output = capsys.readouterr()
    stdout = click.unstyle(output.out)

    assert result == 2
    assert "Usage: pytest [OPTIONS] COMMAND [ARGS]..." in stdout
    assert "Run and manage Toolang agents." in stdout
    assert output.err == ""


def test_cli_thread_commands_have_consistent_order_and_descriptions() -> None:
    group = typer.main.get_command(cli.app)
    expected = {
        "chat": "Open or continue a terminal chat.",
        "steer": "Steer an active run.",
        "cancel": "Cancel an active run.",
        "retry": "Retry a run from a failed step.",
        "rerun": "Rerun a prior invocation.",
        "rewind": "Rewind a thread to an earlier run.",
        "fork": "Fork a thread from an earlier run.",
        "inspect": "Inspect a thread or run.",
        "runs": "List runs.",
        "threads": "List threads.",
    }

    assert isinstance(group, click.Group)
    context = click.Context(group)
    order = tuple(name for name in group.list_commands(context) if name in expected)

    assert order == tuple(expected)
    assert {name: group.commands[name].help for name in expected} == expected


@pytest.mark.parametrize(
    ("arguments", "usage", "argument", "argument_type", "syntax_metavar"),
    (
        (
            ["clone"],
            "Usage: pytest clone [OPTIONS] SOURCE [TARGET]",
            "target",
            "TEXT",
            "[TARGET]",
        ),
        (
            ["chat"],
            "Usage: pytest AGENT chat [OPTIONS] [THREAD]",
            "thread",
            "TEXT",
            "[THREAD]",
        ),
        (
            ["fmt"],
            "Usage: pytest fmt [OPTIONS] [PATHS]...",
            "paths",
            "PATH...",
            "[PATHS]...",
        ),
    ),
)
def test_cli_argument_panels_show_types_without_changing_usage(
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    usage: str,
    argument: str,
    argument_type: str,
    syntax_metavar: str,
) -> None:
    result = _call_main([*arguments, "--help"])
    stdout = click.unstyle(capsys.readouterr().out)
    row = next(
        line
        for line in stdout.splitlines()
        if "│" in line and argument in line.split()
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


def test_caps_cli_formats_a_pre_dispatch_error_as_a_rich_panel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = caps_cli.main(["agent:", "skill"])
    output = capsys.readouterr()
    stderr = click.unstyle(output.err)

    assert result == 2
    assert "╭─ Error " in stderr
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

    assert "Usage:" in output.out
    assert "Steer an active run." in output.out
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

    assert result == 0
    assert "Commands for resident agent alice." in output.out
    assert "steer" in output.out
    assert "No such command" not in output.err


def test_cli_explicit_resident_target_preserves_selector_but_labels_the_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _call_main(["--root", str(tmp_path), "agent:alice"])
    output = capsys.readouterr()
    stdout = click.unstyle(output.out)

    assert result == 0
    assert "Usage: pytest agent:alice" in stdout
    assert "Commands for resident agent alice." in stdout
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
    assert "Commands for visiting agent briceyan/dev." in output.out
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
    stderr = click.unstyle(output.err)

    assert result == 1
    assert f"script not found: {target}" in stderr
    assert "No such command" not in stderr
    assert "Commands for visiting agent" not in output.out


def test_cli_prefix_agent_context_is_isolated_between_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = Barrier(2)
    seen: list[str | None] = []

    def fake_app(**_kwargs: Any) -> None:
        barrier.wait()
        seen.append(cli._PREFIX_AGENT.get())

    monkeypatch.setattr(cli, "app", cast(Any, fake_app))

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


def test_cli_routes_local_script_to_script_command(
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
        return 7

    monkeypatch.setattr(script, "dispatch", fake_dispatch)

    result = dispatch_roaming(
        [str(source), "demo", "--help"],
        prog_name="too",
        run_app=lambda *_args: pytest.fail("Typer app should not run"),
    )

    assert result == 7
    assert captured == {
        "global_args": [],
        "argv": [str(source), "demo", "--help"],
        "prog_name": "too",
    }


@pytest.mark.parametrize(
    "arguments",
    (
        ["info"],
        ["chat"],
        ["chat", "term_1"],
        ["threads"],
        ["runs", "--thread", "script_1"],
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
        [selector, "chat", "term_1"],
        run_app=run_app,
    )

    assert result == 12
    assert captured["source"] == selector
    assert captured["args"] == ["chat", "term_1"]
    assert captured["layout"] == layout


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
        run_app=lambda args, selected: captured.update(
            args=args,
            layout=selected,
        )
        or 13,
    )

    assert result == 13
    assert captured == {
        "args": ["inspect", "run_1"],
        "layout": layout,
    }


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
        lambda source, *, progress: captured.update(
            source=source,
            progress=progress,
        )
        or layout,
    )

    result = dispatch_visiting(
        ["info", selector],
        run_app=lambda args, selected: captured.update(
            args=args,
            layout=selected,
        )
        or 14,
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

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(agents, "resolve_visiting_layout", lambda *_args, **_kwargs: layout)
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
