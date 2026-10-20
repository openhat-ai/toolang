"""Agent selection extends native Typer groups without parsing command tails."""

from __future__ import annotations

from functools import wraps

import pytest
import typer
from typer._click.utils import strip_ansi
from typer.core import TyperGroup

from toolang.cli.common.context import CliContext, cli_context
from toolang.cli.common.lazy import LazyCommand, lazy_typer_command
from toolang.cli.toolang import main as cli
from toolang.cli.toolang import routing
from toolang.up import source


def _no_preparation(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("argument parsing must not prepare an agent")

    monkeypatch.setattr(source, "resolve_visiting_layout", unexpected)
    monkeypatch.setattr(source, "materialize_roaming_program", unexpected)


@pytest.mark.parametrize(
    "target",
    [
        "alice",
        "agent:alice",
        "gh:owner/alice",
        "https://example.test/alice.too",
        "./alice.too",
    ],
)
@pytest.mark.parametrize(
    "tail,expected",
    [
        (["caht"], "No such command 'caht'."),
        (["list"], "Command 'list' is only available at the top level."),
        (["run"], "Command 'run' is only available at the top level."),
        (["new", "bob"], "Command 'new' is only available at the top level."),
        (["chat", "--unknown"], "No such option: --unknown"),
        (["workspace", "unknown"], "No such command 'unknown'."),
        (["start", "bob"], "Command 'start' is only available at the top level."),
    ],
)
def test_agent_command_errors_are_native_and_do_not_prepare(
    tmp_path, monkeypatch, capsys, target, tail, expected
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["too"])
    _no_preparation(monkeypatch)
    if target == "./alice.too" and tail == ["caht"]:
        (tmp_path / "alice.too").write_text("flow main():\n  pass\n")
    assert cli.main([target, *tail]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = strip_ansi(captured.err)
    assert expected.lower() in error.lower()
    assert "invalid agent name" not in error
    assert "Traceback" not in error
    if tail[0] in {"list", "run", "new", "start"}:
        assert "Did you mean" not in error
    assert not (tmp_path / ".toolang").exists()
    assert not (tmp_path / "toolang").exists()


@pytest.mark.parametrize(
    "target", ["missing", "agent:chat", "gh:owner/alice", "./missing.too"]
)
@pytest.mark.parametrize(
    "tail",
    [
        ["chat", "--help"],
        ["home", "--help"],
        ["workspace", "add", "--help"],
        ["workspace", "add"],
    ],
)
def test_selected_command_help_and_incomplete_input_do_not_prepare(
    tmp_path, monkeypatch, capsys, target, tail
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["too"])
    _no_preparation(monkeypatch)
    assert cli.main([target, *tail]) in {0, 2}
    output = capsys.readouterr()
    text = strip_ansi(output.out + output.err)
    assert f"Usage: too {target} {tail[0]}" in text
    assert "<AGENT>" not in text
    assert "agent not found" not in text.lower()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "target,expected",
    [
        ("gh:owner", "invalid GitHub Source Ref"),
        ("owner/alice", "unsupported Source Ref"),
        ("https://example.test/alice.txt", "must point to a .too program"),
        ("agent:Alice", "invalid agent name"),
        ("./alice.py", "must point to a .too file"),
    ],
)
def test_invalid_refs_keep_source_errors(
    tmp_path, monkeypatch, capsys, target, expected
):
    monkeypatch.chdir(tmp_path)
    _no_preparation(monkeypatch)
    assert cli.main([target, "chat"]) == 2
    error = strip_ansi(capsys.readouterr().err)
    assert "Invalid value for AGENT" in error
    assert expected in error
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("flag", ["--help", "-h", "--version", "-V"])
@pytest.mark.parametrize("tail", [["chat"], ["list"], ["start", "alice"]])
def test_root_eager_options_run_before_agent_recognition(
    monkeypatch, capsys, flag, tail
):
    _no_preparation(monkeypatch)
    assert cli.main([flag, *tail]) == 0
    output = capsys.readouterr()
    assert output.out
    assert output.err == ""


@pytest.fixture
def probe(monkeypatch):
    seen = []

    def callback(ctx: typer.Context, value: str):
        state = cli_context(ctx)
        seen.append((state.root, state.agent, value))

    monkeypatch.setattr(cli, "_agent_probe", callback, raising=False)
    monkeypatch.setitem(
        cli._REGISTERED_COMMANDS,
        "probe",
        lambda: lazy_typer_command(
            "probe",
            "toolang.cli.toolang.main:_agent_probe",
            help="Inspect native argument ownership",
            agent_placements=routing.RESIDENT,
        ),
    )
    return seen


@pytest.mark.parametrize("spelling", ["--root", "--root=", "-r", "-rATTACHED"])
def test_root_option_spellings_select_the_bound_context(tmp_path, probe, spelling):
    root = tmp_path / "custom"
    options = (
        [spelling, str(root)]
        if spelling in {"--root", "-r"}
        else [spelling.replace("ATTACHED", "") + str(root)]
    )
    assert cli.main([*options, "alice", "probe", "literal"]) == 0
    assert probe == [(root, "alice", "literal")]


def test_registered_agent_command_uses_native_lookup_help_and_completion(
    tmp_path, monkeypatch, capsys, probe
):
    monkeypatch.setattr("sys.argv", ["too"])
    assert cli.main(["alice", "probe", "hello"]) == 0
    assert probe[0][1:] == ("alice", "hello")
    assert cli.main(["alice", "probe", "--help"]) == 0
    assert "Usage: too alice probe [OPTIONS] <VALUE>" in capsys.readouterr().out
    assert cli.main(["alice", "--help"]) == 0
    assert "probe" in capsys.readouterr().out
    root = typer.main.get_command(cli.app)
    assert isinstance(root, TyperGroup)
    ctx = typer.Context(root, obj=CliContext(root=tmp_path))
    agent = root.get_command(ctx, "alice")
    assert isinstance(agent, TyperGroup)
    child = typer.Context(agent, parent=ctx)
    assert "probe" in [item.value for item in agent.shell_complete(child, "pro")]
    assert "list" not in [item.value for item in agent.shell_complete(child, "li")]


def test_native_root_position_and_literal_boundaries(tmp_path, capsys, probe):
    assert cli.main(["alice", "probe", "literal", "--root", str(tmp_path)]) == 2
    assert "No such option: --root" in capsys.readouterr().err
    assert probe == []
    assert cli.main(["alice", "probe", "--", "--root"]) == 0
    assert probe[0][1:] == ("alice", "--root")


@pytest.mark.parametrize("command", ["info", "start", "stop", "serve"])
@pytest.mark.parametrize("tail", [[], ["bob"], ["--help"]])
def test_lifecycle_commands_are_only_available_at_the_top_level(
    command, tail, monkeypatch, capsys
):
    _no_preparation(monkeypatch)
    assert cli.main(["alice", command, *tail]) == 2
    output = capsys.readouterr()
    assert f"Command '{command}' is only available at the top level." in output.err
    assert "Did you mean" not in output.err
    assert not output.out


@pytest.mark.parametrize("command", ["info", "start", "stop", "serve"])
def test_root_lifecycle_commands_use_native_options_and_target_arguments(
    tmp_path, monkeypatch, command
):
    from toolang.cli.common.context import require_runtime_agent
    from toolang.cli.toolang.commands import agent, runtime

    home = tmp_path / "agents/alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("flow main():\n  pass\n")
    owner, name = (
        (agent, "info_agent")
        if command == "info"
        else (runtime, "run" if command == "serve" else command)
    )
    seen = []

    @wraps(getattr(owner, name))
    def callback(ctx, agent, **kwargs):
        require_runtime_agent(ctx, agent)
        state = cli_context(ctx)
        seen.append((state.root, state.agent, kwargs))

    monkeypatch.setattr(owner, name, callback)
    options = ["--catalog", "catalog.json"] if command != "stop" else ["--force"]
    assert cli.main(["--root", str(tmp_path), command, *options, "alice"]) == 0
    assert len(seen) == 1
    assert seen[0][:2] == (tmp_path, "alice")
    assert seen[0][2]["force" if command == "stop" else "model_catalog"]


@pytest.mark.parametrize(
    "target,placement",
    [("alice", "resident"), ("gh:owner/alice", "visiting"), ("./alice.too", "roaming")],
)
@pytest.mark.parametrize(
    "command,placements",
    [
        ("list", set()),
        ("remove", set()),
        ("talk", set()),
        ("chat", {"resident", "visiting", "roaming"}),
        ("info", set()),
        ("serve", set()),
        ("start", set()),
        ("stop", set()),
        ("top", {"resident"}),
        ("chore", {"resident"}),
        ("skill", {"resident", "visiting", "roaming"}),
    ],
)
def test_command_availability_follows_the_selected_placement(
    tmp_path, monkeypatch, capsys, target, placement, command, placements
):
    monkeypatch.chdir(tmp_path)
    _no_preparation(monkeypatch)
    status = cli.main([target, command, "--help"])
    output = capsys.readouterr()
    if placement in placements:
        assert status == 0
        assert "Usage:" in output.out
        assert output.err == ""
    else:
        assert status == 2
        assert "Command " in output.err
        assert "only available" in output.err or "not available" in output.err
    assert not list(tmp_path.iterdir())


def test_reused_typer_tree_keeps_agent_selection_in_each_context(tmp_path, probe):
    root = typer.main.get_command(cli.app)
    for prefix in (["alice"], ["bob"], []):
        root.main(
            args=["--root", str(tmp_path), *prefix, "probe", "value"],
            standalone_mode=False,
        )
    assert probe == [(tmp_path, name, "value") for name in ("alice", "bob", None)]


def test_agent_groups_reuse_the_registered_command_objects(tmp_path, probe):
    root = typer.main.get_command(cli.app)
    assert isinstance(root, TyperGroup)
    ctx = typer.Context(root, obj=CliContext(root=tmp_path))
    for selector in ("alice", "bob"):
        group = root.get_command(ctx, selector)
        assert isinstance(group, TyperGroup)
        for name, command in group.commands.items():
            assert command is root.commands[name]
    command = root.commands["probe"]
    assert isinstance(command, LazyCommand)
    assert command._loaded is None


def test_local_target_inspection_preserves_the_selected_root(tmp_path, monkeypatch):
    from toolang.cli.common.context import context_layout, require_runtime_agent
    from toolang.cli.toolang.commands import agent

    root = tmp_path / "selected-root"
    source = tmp_path / "alice.too"
    source.write_text("flow main():\n  pass\n")
    seen = []

    @wraps(agent.info_agent)
    def callback(ctx, agent, **kwargs):
        require_runtime_agent(ctx, agent)
        seen.append(context_layout(ctx).toolang_root)

    monkeypatch.setattr(agent, "info_agent", callback)
    assert cli.main(["--root", str(root), "info", str(source)]) == 0
    assert seen == [root]
    assert not root.exists()
    assert not (tmp_path / ".toolang").exists()


@pytest.mark.parametrize("target", ["github:", "gh:", "agent:Chat"])
def test_invalid_agent_refs_are_not_treated_as_command_typos(
    target, monkeypatch, capsys
):
    _no_preparation(monkeypatch)
    assert cli.main([target, "chat", "--help"]) == 2
    error = capsys.readouterr().err
    assert "Invalid value for AGENT" in error
    assert "Did you mean" not in error


@pytest.mark.parametrize("tail", [["--help"], ["chat"], ["chat", "--help"]])
def test_directory_targets_report_the_actual_file_kind(
    tmp_path, monkeypatch, capsys, tail
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "folder.too").mkdir()
    _no_preparation(monkeypatch)
    assert cli.main(["./folder.too", *tail]) == 2
    error = capsys.readouterr().err
    assert "directory" in error
    assert "not found" not in error
    assert list(tmp_path.iterdir()) == [tmp_path / "folder.too"]


@pytest.mark.parametrize("tail", [["add", "."], ["remove", "example"]])
def test_remote_workspace_mutations_fail_before_source_acquisition(
    tmp_path, monkeypatch, capsys, tail
):
    monkeypatch.chdir(tmp_path)
    _no_preparation(monkeypatch)
    assert cli.main(["gh:owner/alice", "workspace", *tail]) == 2
    assert "no durable workspace configuration" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
