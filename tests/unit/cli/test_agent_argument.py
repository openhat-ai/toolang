"""Agent parameters preserve native parsing and invocation isolation."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from io import StringIO
from typing import Annotated

import pytest
import typer
from typer._click import Context
from typer._click.core import ParameterSource
from typer._click.exceptions import BadParameter, NoArgsIsHelpError, UsageError
from typer._click.globals import get_current_context
from typer.core import TyperArgument, TyperGroup

from toolang.cli.common.agent_argument import AgentArgument, agent_parameter
from toolang.cli.common.context import CliContext, cli_context
from toolang.cli.common.help import CliCommand, CliGroup
from toolang.cli.common.lazy import LazyCommand
from toolang.cli.common.parameters import RootOption
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.routing import resolve_command


class _Group(CliGroup):
    def resolve_command(self, ctx, args):
        return resolve_command(self, ctx, args)


def _app(callback):
    app = typer.Typer(cls=_Group, add_completion=False)

    @app.callback()
    def root(ctx: typer.Context, toolang_root: RootOption = None):
        ctx.obj = CliContext(root=toolang_root or Path("/unused"))

    app.command("call", cls=CliCommand)(callback)
    return typer.main.get_command(app)


@pytest.mark.parametrize(
    "tags", ["", "resident", "before", "resident,invalid,before", "resident,,before"]
)
def test_invalid_contracts_fail_at_declaration(tags):
    with pytest.raises(ValueError, match="AGENT"):
        AgentArgument(tags)


@pytest.mark.parametrize(
    "tags,description",
    [
        ("resident,before,after", "Agent name"),
        ("resident,visiting,after,before", "Agent name, ref, URL"),
        ("resident,visiting,roaming,before", "Agent name, path, ref, URL"),
    ],
)
def test_agent_descriptions_are_concise(tags, description):
    assert AgentArgument(tags).help == description


@pytest.mark.parametrize(
    "tags,selector,message",
    [
        (
            "resident,before",
            "https://example.test/a.too",
            "remote agents are not supported",
        ),
        ("resident,visiting,before", "work.too", "paths are not supported"),
        ("roaming,before", "alice", "agent names are not supported"),
    ],
)
def test_placement_errors_use_concise_parameter_diagnostics(tags, selector, message):
    def invoke(agent: Annotated[str, AgentArgument(tags)]):
        pytest.fail("unsupported selector reached execution")

    command = _app(invoke)
    with pytest.raises(BadParameter) as error:
        command.main(args=[selector, "call"], standalone_mode=False)
    assert error.value.format_message() == f"Invalid value for 'AGENT': {message}"


@pytest.mark.parametrize("accepts_after", [False, True])
def test_rejected_prefix_uses_native_command_help(accepts_after):
    def plain():
        pytest.fail("unsupported prefix invoked a callback")

    def after(agent: Annotated[str, AgentArgument("resident,after")]):
        pytest.fail("unsupported prefix invoked a callback")

    command = _app(after if accepts_after else plain)
    with pytest.raises(NoArgsIsHelpError) as error:
        command.main(args=["alice", "call"], prog_name="too", standalone_mode=False)
    assert error.value.exit_code == 2
    assert error.value.ctx.command is command.commands["call"]
    assert "Usage: too call" in error.value.ctx.get_help()


@pytest.mark.parametrize(
    "name",
    [
        "cd",
        "info",
        "start",
        "stop",
        "chat",
        "top",
    ],
)
def test_unambiguous_agent_commands_accept_both_positions(name, tmp_path):
    root = typer.main.get_command(cli.app)
    assert isinstance(root, TyperGroup)
    proxy = root.commands[name]
    assert isinstance(proxy, LazyCommand)
    command = proxy.load()
    parameter = agent_parameter(command)
    assert parameter is not None
    assert parameter.contract.positions == {"before", "after"}
    seen = []

    def capture(**params):
        ctx = get_current_context()
        seen.append((params["agent"], cli_context(ctx).selector, ctx.command))

    command.callback = capture
    for selector in (
        "alice",
        "agent:alice",
        "https://example.test/alice.too",
        "work.too",
    ):
        placement = (
            "visiting"
            if selector.startswith("https:")
            else "roaming"
            if selector.endswith(".too")
            else "resident"
        )
        if placement not in parameter.contract.placements:
            continue
        for words in ([selector, name], [name, selector]):
            root.main(
                args=["--root", str(tmp_path), *words],
                prog_name="too",
                standalone_mode=False,
            )
        assert seen[-2] == seen[-1]
        assert seen[-1][0] == ("alice" if selector == "agent:alice" else selector)
        assert seen[-1][2] is command


@pytest.mark.parametrize(
    "name", ["list", "new", "clone", "run", "serve", "remove", "_serve"]
)
@pytest.mark.parametrize("tail", [[], ["--help"], ["--unknown"], ["bob"]])
def test_rejected_agent_forms_show_usage_without_side_effects(
    name, tail, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["too"])
    assert cli.main(["alice", name, *tail]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert f"Usage: too {name} " in output.err
    assert "does not accept AGENT" not in output.err
    assert "AGENT must follow" not in output.err
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["caps", "models", "providers", "tools"])
def test_inspection_commands_keep_agent_before_the_command(
    name, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["too"])
    assert cli.main([name, "alice"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "Got unexpected extra argument" in output.err
    assert f"Usage: too [AGENT] {name} " in output.err
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    "tags,expected",
    [
        ("resident,before,after", "before"),
        ("resident,after,before", "after"),
        ("resident, before, after, before", "before"),
    ],
)
def test_usage_uses_first_position_while_accepting_both(tags, expected):
    seen = []

    def invoke(ctx: typer.Context, agent: Annotated[str, AgentArgument(tags)]):
        seen.append((agent, ctx.get_parameter_source("agent")))

    command = _app(invoke)
    for args in (["alice", "call"], ["call", "alice"]):
        command.main(args=args, prog_name="too", standalone_mode=False)
    assert seen == [("alice", ParameterSource.COMMANDLINE)] * 2
    child = command.commands["call"]
    ctx = child.make_context(
        "call", [], parent=Context(command, info_name="too"), resilient_parsing=True
    )
    usage = ctx.get_usage()
    assert ("too <AGENT> call" in usage) == (expected == "before")
    assert ("call [OPTIONS] <AGENT>" in usage) == (expected == "after")
    assert usage.count("AGENT") == 1


@pytest.mark.parametrize("supplied", [False, True])
def test_optional_prefix_never_consumes_business_operand(supplied):
    seen = []

    def invoke(
        ctx: typer.Context,
        query: str,
        agent: Annotated[str | None, AgentArgument("resident,before")] = None,
    ):
        seen.append((query, agent, ctx.get_parameter_source("agent")))

    command = _app(invoke)
    command.main(
        args=[*(["alice"] if supplied else []), "call", "hello"], standalone_mode=False
    )
    assert seen == [
        (
            "hello",
            "alice" if supplied else None,
            ParameterSource.COMMANDLINE if supplied else ParameterSource.DEFAULT,
        )
    ]


def test_both_optional_positions_leave_other_operands_native():
    seen = []

    def invoke(
        ctx: typer.Context,
        query: str,
        agent: Annotated[str | None, AgentArgument("resident,before,after")] = None,
    ):
        seen.append((query, agent))

    command = _app(invoke)
    for args in (
        ["alice", "call", "hello"],
        ["call", "hello", "alice"],
        ["call", "hello"],
    ):
        command.main(args=args, standalone_mode=False)
    assert seen == [("hello", "alice"), ("hello", "alice"), ("hello", None)]


def test_agent_callbacks_and_business_conversion_remain_native():
    seen = []

    def convert(ctx: typer.Context, value: str):
        seen.append(("convert", value, ctx.get_parameter_source("agent")))
        return value.upper()

    def invoke(
        ctx: typer.Context,
        agent: Annotated[str, AgentArgument("resident,before", callback=convert)],
        count: int,
    ):
        seen.append((agent, count, cli_context(ctx).agent))

    command = _app(invoke)
    command.main(args=["alice", "call", "12"], standalone_mode=False)
    assert seen == [
        ("convert", "alice", ParameterSource.COMMANDLINE),
        ("ALICE", 12, "alice"),
    ]
    with pytest.raises(UsageError, match="not a valid int"):
        command.main(args=["alice", "call", "wrong"], standalone_mode=False)


def test_command_and_parameter_objects_stay_shared_across_concurrent_invocations(
    tmp_path,
):
    def invoke(
        ctx: typer.Context,
        agent: Annotated[str | None, AgentArgument("resident,after,before")] = None,
    ):
        state = cli_context(ctx)
        return agent, state.agent, state.root, ctx.command

    command = _app(invoke)
    child = command.commands["call"]
    parameter = agent_parameter(child)
    before = dict(vars(parameter))
    cases = [(f"agent{index}", tmp_path / str(index), index % 3) for index in range(24)]

    def run(case):
        name, root, form = case
        tail = (
            [name, "call"] if form == 0 else ["call", name] if form == 1 else ["call"]
        )
        return command.main(args=["--root", str(root), *tail], standalone_mode=False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, cases))
    for (name, root, form), (
        argument,
        selected,
        selected_root,
        selected_command,
    ) in zip(cases, results):
        assert argument == selected == (name if form != 2 else None)
        assert selected_root == root
        assert selected_command is child
    assert agent_parameter(child) is parameter
    assert vars(parameter) == before


@pytest.mark.parametrize(
    "kind", ["multiple", "optional_first", "variadic", "multiple_values"]
)
def test_ambiguous_agent_declarations_fail_at_command_construction(kind):
    def invoke():
        pass

    # Native argument metadata is also used by dynamically assembled commands.
    from toolang.cli.common.agent_argument import AgentParameter

    info = AgentArgument("resident,after")
    agent = TyperArgument(
        param_decls=["agent"],
        type=info.click_type,
        required=False,
        nargs=-1 if kind == "multiple_values" else 1,
    )
    other = TyperArgument(
        param_decls=["query"], type=str, nargs=-1 if kind == "variadic" else 1
    )
    params = (
        [agent, AgentParameter(agent)]
        if kind == "multiple"
        else [other, agent]
        if kind in {"variadic", "multiple_values"}
        else [agent, other]
    )
    with pytest.raises(ValueError, match="AGENT"):
        CliCommand(name="call", callback=invoke, params=params)


@pytest.mark.parametrize("positions", ["after", "before,after", "after,before"])
def test_optional_after_agent_is_rejected_on_groups(positions):
    info = AgentArgument(f"resident,{positions}")
    with pytest.raises(ValueError, match="optional after AGENT.*group"):
        CliGroup(
            name="group",
            params=[
                TyperArgument(
                    param_decls=["agent"], type=info.click_type, required=False
                )
            ],
            commands={"leaf": CliCommand(name="leaf")},
        )


@pytest.mark.parametrize(
    "selector", ["agent:", "gh:", "alice@r-invalid", "https://invalid.test/a.txt"]
)
def test_help_precedes_selector_validation(selector, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["too"])
    assert cli.main([selector, "chat", "--help"]) == 0
    assert "Usage: too <AGENT> chat" in capsys.readouterr().out
    assert cli.main([selector, "chat"]) == 2
    error = capsys.readouterr().err
    assert "Invalid value for 'AGENT'" in error
    assert "Did you mean" not in error


@pytest.mark.parametrize(
    "args,diagnostic",
    [
        (["alice", "list"], "Usage:"),
        (["alice", "_serve"], "Usage:"),
        (
            ["https://example.test/a.too", "top", "--once"],
            "remote agents are not supported",
        ),
        (["agent:alice", "missing"], "No such command 'missing'"),
        (["unknown", "missing"], "No such command 'unknown'"),
    ],
)
def test_command_and_agent_errors_remain_distinct(
    args, diagnostic, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    assert cli.main(args) == 2
    assert diagnostic in capsys.readouterr().err


def test_resident_fallback_uses_a_single_name_lookup_only(
    tmp_path, monkeypatch, capsys
):
    from toolang.catalog.agent import LocalAgents

    looked_up = []
    get = LocalAgents.get

    def lookup(self, name):
        looked_up.append(name)
        return get(self, name)

    monkeypatch.setattr(LocalAgents, "get", lookup)
    monkeypatch.setattr(
        LocalAgents, "list", lambda _: pytest.fail("enumerated residents")
    )
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    assert cli.main(["chat", "--help"]) == 0
    assert cli.main(["alice", "chat", "--help"]) == 0
    assert looked_up == []
    assert cli.main(["unknown"]) == 2
    assert looked_up == ["unknown"]
    capsys.readouterr()


@pytest.mark.parametrize("tail", [["--help"], ["research", "--help"], ["research"]])
@pytest.mark.parametrize("explicit", [False, True])
def test_script_reads_program_once_and_builds_only_selected_signature(
    tmp_path, monkeypatch, tail, explicit
):
    from toolang.cli.toolang.commands import script
    from toolang.lang.ast import Program

    source = tmp_path / "work.too"
    source.write_text("flow research():\n  pass\nflow other():\n  pass\n")
    parses = []
    signatures = []
    parse = Program.from_source
    build = script.runnable_parameters

    def from_source(text):
        parses.append(text)
        return parse(text)

    def parameters(runnable, **kwargs):
        signatures.append(runnable.name)
        return build(runnable, **kwargs)

    monkeypatch.setattr(Program, "from_source", from_source)
    monkeypatch.setattr(script, "runnable_parameters", parameters)
    monkeypatch.setattr("sys.stdin", StringIO())
    monkeypatch.setattr(script, "_run", lambda *_args, **_kwargs: 0)
    assert cli.main([*(["run"] if explicit else []), str(source), *tail]) == 0
    assert len(parses) == 1
    assert signatures == ([] if tail == ["--help"] else ["research"])


def test_builtin_precedence_does_not_parse_a_script_file(tmp_path, monkeypatch, capsys):
    from toolang.lang.ast import Program

    source = tmp_path / "work.too"
    source.write_text("not a valid program")
    monkeypatch.setattr(
        Program, "from_source", lambda _: pytest.fail("parsed builtin operand")
    )
    assert cli.main([str(source), "chat", "--help"]) == 0
    assert cli.main([str(source), "list"]) == 2
    assert "Usage:" in capsys.readouterr().err


@pytest.mark.parametrize("explicit", [False, True])
def test_script_failure_status_is_preserved(explicit, tmp_path, monkeypatch):
    from toolang.cli.toolang.commands import script

    source = tmp_path / "work.too"
    source.write_text("flow research():\n  pass\n")
    monkeypatch.setattr("sys.stdin", StringIO())
    monkeypatch.setattr(script, "_run", lambda *_args, **_kwargs: 7)
    assert cli.main([*(["run"] if explicit else []), str(source), "research"]) == 7


def test_native_completion_keeps_prefix_binding_and_uses_agent_metadata(tmp_path):
    from typer._click.shell_completion import _resolve_context, _resolve_incomplete
    from toolang.catalog.agent import LocalAgents

    LocalAgents(tmp_path / "agents").create("alice", content="flow main():\n  pass\n")

    def invoke(
        ctx: typer.Context,
        agent: Annotated[str, AgentArgument("resident,after,before")],
        query: str,
    ):
        pytest.fail("completion invoked callback")

    command = _app(invoke)
    args = ["--root", str(tmp_path), "alice", "call"]
    ctx = _resolve_context(command, {}, "too", args)
    parameter, _ = _resolve_incomplete(ctx, args, "")
    assert parameter.name == "query"
    assert ctx.params["agent"] == "alice"
    args = ["--root", str(tmp_path), "call"]
    ctx = _resolve_context(command, {}, "too", args)
    parameter, _ = _resolve_incomplete(ctx, args, "al")
    assert [item.value for item in parameter.shell_complete(ctx, "al")] == ["alice"]
    ctx = _resolve_context(command, {}, "too", ["--root", str(tmp_path), "agent:alice"])
    assert [item.value for item in ctx.command.shell_complete(ctx, "ca")] == ["call"]


@pytest.mark.parametrize(
    "tail",
    [
        ["agent:alice", "list"],
        ["agent:alice", "_serve"],
        ["agent:alice", "--help"],
        ["agent:alice", "-h"],
    ],
)
def test_completion_suppresses_routing_errors_and_help(tail, tmp_path, capsys):
    from typer._click.shell_completion import _resolve_context

    command = typer.main.get_command(cli.app)
    ctx = _resolve_context(command, {}, "too", ["--root", str(tmp_path), *tail])
    assert ctx.command is command
    assert ctx.resilient_parsing
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_concurrent_first_use_loads_one_shared_lazy_command(tmp_path):
    from threading import Barrier
    from toolang.cli.common.lazy import LazyCommand

    def invoke(
        ctx: typer.Context, agent: Annotated[str, AgentArgument("resident,before")]
    ):
        return agent, cli_context(ctx).agent, ctx.command

    command = _app(invoke)
    original = command.commands["call"]
    loads = []

    def load():
        loads.append(1)
        # Construct real Typer parameters through the same factory as registration.
        return _app(invoke).commands["call"]

    proxy = LazyCommand("call", loader=load, help="Call an agent")
    command.commands["call"] = proxy
    barrier = Barrier(8)

    def run(index):
        barrier.wait()
        return command.main(
            args=["--root", str(tmp_path), f"agent{index}", "call"],
            standalone_mode=False,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))
    assert loads == [1]
    for index, (argument, selected, child) in enumerate(results):
        assert argument == selected == f"agent{index}"
        assert child is proxy.load()
        assert child is not original


@pytest.mark.parametrize(
    "tags,expected",
    [
        ("resident,before,after", "too <AGENT> group <QUERY> leaf"),
        ("resident,after,before", "too group <QUERY> <AGENT> leaf"),
    ],
)
def test_descendant_usage_preserves_native_group_operands(tags, expected):
    info = AgentArgument(tags)
    group = CliGroup(
        name="group",
        params=[
            TyperArgument(param_decls=["query"], type=str, required=True),
            TyperArgument(
                param_decls=["agent"],
                type=info.click_type,
                metavar="AGENT",
                required=True,
            ),
        ],
    )
    root = CliGroup(name="too")
    leaf = CliCommand(name="leaf")
    parent = Context(root, info_name="too")
    owner = Context(group, info_name="group", parent=parent)
    child = Context(leaf, info_name="leaf", parent=owner)
    assert expected in child.get_usage()
    assert child.get_usage().count("AGENT") == 1
