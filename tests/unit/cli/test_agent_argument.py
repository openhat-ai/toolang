"""Native leaf contracts own agent prefixes, help, and invocation isolation."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from typing import Annotated

import pytest
import typer
from typer._click import Context
from typer._click.core import ParameterSource
from typer._click.exceptions import BadParameter, UsageError
from typer.core import TyperArgument, TyperGroup

from toolang.cli.common.agent_argument import (
    AgentArgument,
    AgentParameter,
    agent_parameter,
    agent_parameters,
)
from toolang.cli.common.context import CliContext, cli_context
from toolang.cli.common.help import CliCommand, CliGroup
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


@pytest.mark.parametrize("role", ["", "resident", "before", "home,after"])
def test_invalid_roles_fail_at_declaration(role):
    with pytest.raises(ValueError, match="AGENT"):
        AgentArgument(role)


@pytest.mark.parametrize("placements", ["", "resident,", "before", "resident,unknown"])
def test_invalid_placements_fail_at_declaration(placements):
    with pytest.raises(ValueError, match="AGENT"):
        AgentArgument("source", placements=placements)


def test_source_contract_cannot_require_a_runtime():
    with pytest.raises(ValueError, match="source AGENT"):
        AgentArgument("source", requires_live=True)
    with pytest.raises(ValueError, match="valid driver"):
        AgentArgument("home", required_driver="unknown")


@pytest.mark.parametrize(
    "role,placements,live,driver,description,metavar",
    [
        ("source", "resident", False, None, "Agent name", "AGENT"),
        ("home", "resident", False, None, "Agent name", "AGENT"),
        (
            "source",
            "resident,roaming",
            False,
            None,
            "Agent name or path",
            "AGENT",
        ),
        (
            "source",
            "resident,visiting,roaming",
            False,
            None,
            "Agent name, path, ref or URL",
            "AGENT",
        ),
        (
            "home",
            "resident,visiting,roaming",
            False,
            None,
            "Agent name, path, ref, URL or instance",
            "AGENT",
        ),
        (
            "home",
            "resident,visiting",
            False,
            None,
            "Agent name, ref, URL or instance",
            "AGENT",
        ),
        (
            "home",
            "resident,visiting,roaming",
            True,
            None,
            "Running agent name, path, ref, URL or instance",
            "AGENT",
        ),
        (
            "home",
            "resident",
            True,
            "chore",
            "Running agent name",
            "AGENT",
        ),
        (
            "execute",
            "resident,visiting",
            False,
            None,
            "Agent name, ref, URL or instance",
            "AGENT",
        ),
    ],
)
def test_contracts_generate_natural_descriptions(
    role, placements, live, driver, description, metavar
):
    argument = AgentArgument(
        role, placements=placements, requires_live=live, required_driver=driver
    )
    assert argument.help == description
    assert argument.metavar == metavar
    assert argument.help is not None and "(" not in argument.help


@pytest.mark.parametrize(
    "placements,selector,message",
    [
        ("resident", "https://example.test/a.too", "remote agents are not supported"),
        ("resident,visiting", "work.too", "paths are not supported"),
        ("roaming", "alice", "agent names are not supported"),
    ],
)
@pytest.mark.parametrize("agent_first", [False, True])
def test_placement_errors_are_native(placements, selector, message, agent_first):
    def invoke(
        agent: Annotated[
            str, AgentArgument("source", placements=placements, both_orders=True)
        ],
    ):
        pytest.fail("unsupported selector reached execution")

    with pytest.raises(BadParameter, match=message):
        args = [selector, "call"] if agent_first else ["call", selector]
        _app(invoke).main(args, standalone_mode=False)


@pytest.mark.parametrize(
    "role,metavar", [("source", "AGENT"), ("home", "AGENT"), ("execute", "AGENT")]
)
def test_agent_operand_is_prefix_only(role, metavar):
    seen = []

    def invoke(ctx: typer.Context, agent: Annotated[str, AgentArgument(role)]):
        seen.append((agent, ctx.get_parameter_source("agent")))

    command = _app(invoke)
    command.main(["alice", "call"], standalone_mode=False)
    assert seen == [("alice", ParameterSource.COMMANDLINE)]
    with pytest.raises(UsageError):
        command.main(["call", "alice"], standalone_mode=False)
    child = command.commands["call"]
    with Context(command, info_name="too") as parent:
        ctx = child.make_context("call", [], parent=parent, resilient_parsing=True)
        usage = ctx.get_usage()
    assert f"too <{metavar}> call" in usage
    assert usage.count(metavar) == 1
    assert child.no_args_is_help is False


@pytest.mark.parametrize("supplied", [False, True])
def test_optional_prefix_never_consumes_business_operands(supplied):
    seen = []

    def invoke(
        ctx: typer.Context,
        query: str,
        agent: Annotated[str | None, AgentArgument("source")] = None,
    ):
        seen.append((query, agent, ctx.get_parameter_source("agent")))

    command = _app(invoke)
    command.main(
        [*(["alice"] if supplied else []), "call", "hello"], standalone_mode=False
    )
    assert seen == [
        (
            "hello",
            "alice" if supplied else None,
            ParameterSource.COMMANDLINE if supplied else ParameterSource.DEFAULT,
        )
    ]


@pytest.mark.parametrize("args", [["alice", "call", "12"], ["call", "alice", "12"]])
def test_native_conversion_and_callback_run_once(args):
    seen = []

    def converted(ctx: typer.Context, value: str):
        seen.append((value, ctx.get_parameter_source("agent")))
        return value.upper()

    def invoke(
        ctx: typer.Context,
        count: int,
        agent: Annotated[
            str, AgentArgument("source", both_orders=True, callback=converted)
        ],
    ):
        return agent, count, cli_context(ctx).agent

    command = _app(invoke)
    assert command.main(args, standalone_mode=False) == (
        "ALICE",
        12,
        "alice",
    )
    child = command.commands["call"]
    parent = Context(command, info_name="too")
    ctx = Context(child, info_name="call", parent=parent)
    assert ctx.get_usage().splitlines() == [
        "Usage: too <AGENT> call [OPTIONS] <COUNT>",
        "Usage: too call [OPTIONS] <AGENT> <COUNT>",
    ]
    assert seen == [("alice", ParameterSource.COMMANDLINE)]
    with pytest.raises(UsageError, match="not a valid int"):
        command.main([*args[:-1], "wrong"], standalone_mode=False)


@pytest.mark.parametrize("agent_first", [False, True])
def test_dual_orders_preserve_option_values_and_literal_business_operands(agent_first):
    def invoke(
        agent: Annotated[str, AgentArgument("source", both_orders=True)],
        message: str,
        mode: Annotated[str, typer.Option()] = "default",
    ):
        return agent, message, mode

    command = _app(invoke)
    args = (
        ["alice", "call", "--mode", "top"]
        if agent_first
        else ["call", "--mode", "top", "alice"]
    )
    assert command.main([*args, "--", "--literal"], standalone_mode=False) == (
        "alice",
        "--literal",
        "top",
    )


def test_dual_order_prefix_does_not_consume_a_second_agent():
    def invoke(agent: Annotated[str, AgentArgument("source", both_orders=True)]):
        pytest.fail("extra agent reached execution")

    with pytest.raises(UsageError, match="unexpected extra argument"):
        _app(invoke).main(["alice", "call", "bob"], standalone_mode=False)


def test_group_carries_prefix_to_leaf_contract_without_stealing_name(tmp_path):
    app = typer.Typer(cls=_Group, add_completion=False)

    @app.callback()
    def root(ctx: typer.Context):
        ctx.obj = CliContext(tmp_path)

    group = typer.Typer(cls=CliGroup, add_completion=False)

    @group.command("list", cls=CliCommand)
    def list_items(
        ctx: typer.Context, agent: Annotated[str | None, AgentArgument("source")] = None
    ):
        return agent, cli_context(ctx).selector, ctx.get_parameter_source("agent")

    @group.command("edit", cls=CliCommand)
    def edit_item(
        ctx: typer.Context,
        name: str,
        agent: Annotated[
            str | None, AgentArgument("source", placements="resident")
        ] = None,
    ):
        return agent, name, cli_context(ctx).selector

    @group.command("history", cls=CliCommand)
    def history(agent: Annotated[str, AgentArgument("home")]):
        pytest.fail("help invoked action")

    app.add_typer(group, name="group")
    command = typer.main.get_command(app)
    assert isinstance(command, TyperGroup)
    owner = command.commands["group"]
    assert isinstance(owner, TyperGroup)
    assert agent_parameter(owner) is None
    assert len(agent_parameters(owner)) == 3
    remote = "https://example.test/a.too"
    assert command.main([remote, "group", "list"], standalone_mode=False) == (
        remote,
        remote,
        ParameterSource.COMMANDLINE,
    )
    assert command.main(["alice", "group", "edit", "draft"], standalone_mode=False) == (
        "alice",
        "draft",
        "alice",
    )
    assert command.main(["group", "edit", "draft"], standalone_mode=False) == (
        None,
        "draft",
        None,
    )
    with pytest.raises(BadParameter, match="remote agents are not supported"):
        command.main([remote, "group", "edit", "draft"], standalone_mode=False)
    parent = Context(command, info_name="too")
    group_ctx = Context(owner, info_name="group", parent=parent)
    leaf_ctx = Context(owner.commands["history"], info_name="history", parent=group_ctx)
    assert "too <AGENT> group history" in leaf_ctx.get_usage()
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("role", ["source", "home", "execute"])
def test_groups_cannot_declare_agent_parameters(role):
    argument = AgentArgument(role)
    with pytest.raises(ValueError, match="executable leaves"):
        CliGroup(
            name="group",
            params=[TyperArgument(param_decls=["agent"], type=argument.click_type)],
        )


@pytest.mark.parametrize("multiple", [False, True])
def test_invalid_leaf_parameter_shape_is_rejected(multiple):
    argument = AgentArgument("source")
    agent = TyperArgument(
        param_decls=["agent"], type=argument.click_type, nargs=1 if multiple else -1
    )
    params = [agent, AgentParameter(agent)] if multiple else [agent]
    with pytest.raises(ValueError, match="AGENT"):
        CliCommand(name="call", params=params)


def test_cached_native_parameters_isolate_concurrent_contexts(tmp_path):
    def invoke(
        ctx: typer.Context,
        agent: Annotated[str | None, AgentArgument("source", both_orders=True)] = None,
    ):
        state = cli_context(ctx)
        return agent, state.agent, state.root, ctx.command

    command = _app(invoke)
    child = command.commands["call"]
    parameter = agent_parameter(child)
    before = vars(parameter).copy()
    cases = [(f"agent{i}", tmp_path / str(i), i % 3) for i in range(24)]

    def run(case):
        name, root, position = case
        tail = (
            [name, "call"]
            if position == 1
            else ["call", name]
            if position == 2
            else ["call"]
        )
        return command.main(
            ["--root", str(root), *tail],
            standalone_mode=False,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, cases))
    for (name, root, position), (
        argument,
        selected,
        selected_root,
        selected_command,
    ) in zip(cases, results, strict=True):
        assert argument == selected == (name if position else None)
        assert selected_root == root
        assert selected_command is child
    assert vars(parameter) == before


@pytest.mark.parametrize(
    "selector", ["agent:", "gh:", "alice@r-invalid", "https://invalid.test/a.txt"]
)
@pytest.mark.parametrize("agent_first", [False, True])
def test_eager_help_precedes_selector_validation(
    selector, agent_first, monkeypatch, capsys
):
    monkeypatch.setattr("sys.argv", ["too"])
    args = [selector, "chat"] if agent_first else ["chat", selector]
    assert cli.main([*args, "--help"]) == 0
    assert "Usage: too <AGENT> chat" in capsys.readouterr().out
    assert cli.main(args) == 2
    assert "Invalid value for 'AGENT'" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name", ["list", "new", "clone", "remove", "talk", "run", "_serve"]
)
def test_catalog_and_script_commands_reject_agent_prefix(
    name, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    assert cli.main(["alice", name, "--help"]) == 2
    assert "does not accept an agent prefix" in capsys.readouterr().err
    assert not tuple(tmp_path.iterdir())


def test_command_diagnostics_do_not_read_resident_homes(tmp_path, monkeypatch, capsys):
    from toolang.catalog.agent import LocalAgents

    monkeypatch.setattr(
        LocalAgents, "get", lambda *_: pytest.fail("looked up agent while routing")
    )
    monkeypatch.setattr(
        LocalAgents, "list", lambda *_: pytest.fail("enumerated agents while routing")
    )
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    assert cli.main(["alice", "chat", "--help"]) == 0
    assert cli.main(["unknown"]) == 2
    assert "No such command 'unknown'" in capsys.readouterr().err


def test_native_completion_uses_prefix_contract_and_escapes_collisions(tmp_path):
    from toolang.catalog.agent import LocalAgents
    from typer._click.shell_completion import _resolve_context, _resolve_incomplete

    homes = LocalAgents(tmp_path / "agents")
    for name in ("alice", "top"):
        homes.create(name, content="flow main():\n  pass\n")
    command = typer.main.get_command(cli.app)
    args = ["--root", str(tmp_path)]
    ctx = _resolve_context(command, {}, "too", args)
    assert "alice" in [item.value for item in ctx.command.shell_complete(ctx, "al")]
    assert "agent:top" in [item.value for item in ctx.command.shell_complete(ctx, "to")]
    args += ["alice", "workspace", "remove"]
    ctx = _resolve_context(command, {}, "too", args)
    parameter, _ = _resolve_incomplete(ctx, args, "")
    assert parameter.name == "name"
    assert ctx.params["agent"] == "alice"


def test_root_command_completion_survives_an_unreadable_agent_inventory(monkeypatch):
    from toolang.catalog.agent import LocalAgents
    from typer._click.shell_completion import _resolve_context

    def unavailable(_):
        raise PermissionError("agent inventory unavailable")

    monkeypatch.setattr(LocalAgents, "list", unavailable)
    command = typer.main.get_command(cli.app)
    ctx = _resolve_context(command, {}, "too", [])
    assert {"start", "stop"} <= {
        item.value for item in command.shell_complete(ctx, "st")
    }


@pytest.mark.parametrize("tail", [["models"], ["inspect"], ["skill", "list"]])
def test_native_completion_does_not_offer_agent_suffixes(tail, tmp_path, monkeypatch):
    from toolang.catalog.agent import LocalAgents
    from typer._click.shell_completion import _resolve_context, _resolve_incomplete

    monkeypatch.setattr(LocalAgents, "list", lambda _: ("alice",))
    command = typer.main.get_command(cli.app)
    args = ["--root", str(tmp_path), *tail]
    ctx = _resolve_context(command, {}, "too", args)
    target, value = _resolve_incomplete(ctx, args, "al")
    assert not isinstance(target, AgentParameter)
    assert "alice" not in [item.value for item in target.shell_complete(ctx, value)]


@pytest.mark.parametrize("name", ["info", "chat", "cd", "start", "stop", "serve"])
@pytest.mark.parametrize("agent_first", [False, True])
def test_dual_order_completion_consumes_the_agent_once(
    name, agent_first, tmp_path, monkeypatch
):
    from toolang.catalog.agent import LocalAgents
    from typer._click.shell_completion import _resolve_context, _resolve_incomplete

    monkeypatch.setattr(LocalAgents, "list", lambda _: ("alice",))
    command = typer.main.get_command(cli.app)
    args = ["--root", str(tmp_path)]
    if not agent_first:
        ctx = _resolve_context(command, {}, "too", [*args, name])
        parameter, value = _resolve_incomplete(ctx, [*args, name], "al")
        assert isinstance(parameter, AgentParameter)
        assert [item.value for item in parameter.shell_complete(ctx, value)] == [
            "alice"
        ]
    args += ["alice", name] if agent_first else [name, "alice"]
    ctx = _resolve_context(command, {}, "too", args)
    parameter, _ = _resolve_incomplete(ctx, args, "")
    assert not isinstance(parameter, AgentParameter)
    assert ctx.params["agent"] == "alice"
    assert ctx.get_parameter_source("agent") is ParameterSource.COMMANDLINE


@pytest.mark.parametrize(
    "name,module,callback",
    [
        ("info", "agent", "info_agent"),
        ("chat", "chat", "chat_command"),
        ("cd", "cd", "cd"),
        ("start", "runtime", "start"),
        ("serve", "runtime", "run"),
        ("stop", "runtime", "stop"),
    ],
)
@pytest.mark.parametrize(
    "selector,kind",
    [
        ("alice", "name"),
        ("agent:top", "name"),
        ("gh:owner/alice", "remote"),
        ("gh:owner/repo/alice.too@main", "remote"),
        ("https://example.test/alice.too", "remote"),
        ("./alice.too", "path"),
        ("{root}/source with spaces/alice.too", "path"),
        ("alice@t0000000001", "visiting_instance"),
        ("alice@s00000010000000001", "roaming_instance"),
    ],
)
def test_public_dual_orders_bind_the_same_selector(
    name, module, callback, selector, kind, tmp_path, monkeypatch, capsys
):
    from functools import wraps
    from importlib import import_module

    owner = import_module(f"toolang.cli.toolang.commands.{module}")
    monkeypatch.chdir(tmp_path)
    selector = selector.format(root=tmp_path)
    seen = []

    @wraps(getattr(owner, callback))
    def invoke(**kwargs):
        ctx = kwargs["ctx"]
        seen.append(
            (
                kwargs["agent"],
                cli_context(ctx),
                ctx.get_parameter_source("agent"),
            )
        )

    monkeypatch.setattr(owner, callback, invoke)
    error = None
    if kind.endswith("instance") and name == "cd":
        error = "instances are not supported"
    elif name in {"start", "stop", "serve"}:
        if kind == "path":
            error = "paths are not supported"
        elif kind == "roaming_instance":
            error = "roaming instances are not supported"
    for args in ([selector, name], [name, selector]):
        assert cli.main(["--root", str(tmp_path), *args]) == (2 if error else 0)
        if error:
            assert error in capsys.readouterr().err
            assert not seen
    if not error:
        assert len(seen) == 2 and seen[0] == seen[1]
        argument, state, parameter_source = seen[0]
        assert argument == selector.removeprefix("agent:")
        assert parameter_source is ParameterSource.COMMANDLINE
        assert state.root == tmp_path
        assert state.agent == ("top" if selector == "agent:top" else "alice")
        assert not state.prepared and not state.instance_selected
        if kind == "path":
            assert state.source == Path(selector).resolve()
            assert state.layout is not None and state.layout.placement == "roaming"
        elif kind == "remote":
            assert state.layout is not None and state.layout.placement == "visiting"
        elif kind.endswith("instance"):
            assert state.address is not None
            assert state.address.placement == kind.removesuffix("_instance")
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    "args,expected",
    [
        (["info", "chat"], ("info", "chat")),
        (["chat", "info"], ("chat", "info")),
        (["agent:chat", "info"], ("info", "chat")),
        (["agent:info", "chat"], ("chat", "info")),
    ],
)
def test_dual_order_command_names_keep_command_precedence(
    args, expected, tmp_path, monkeypatch
):
    from functools import wraps
    from toolang.cli.toolang.commands import agent, chat

    seen = []
    for owner, callback in ((agent, "info_agent"), (chat, "chat_command")):

        @wraps(getattr(owner, callback))
        def invoke(**kwargs):
            seen.append((kwargs["ctx"].command.name, kwargs["agent"]))

        monkeypatch.setattr(owner, callback, invoke)

    assert cli.main(["--root", str(tmp_path), *args]) == 0
    assert seen == [expected]
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    "args",
    [
        ["inspect", "alice", "runs"],
        *[[name, "alice"] for name in ("top", "caps", "tools", "models", "providers")],
        *[
            [name, "alice", "run_example"]
            for name in ("cancel", "rerun", "fork", "rewind")
        ],
        ["steer", "alice", "run_example", "continue"],
        ["workspace", "list", "alice"],
        ["chore", "list", "alice"],
        *[[kind, "list", "alice"] for kind in ("psyche", "skill", "service", "prompt")],
        *[
            [kind, "new", "alice", "review"]
            for kind in ("psyche", "skill", "service", "prompt")
        ],
    ],
)
def test_scoped_commands_reject_command_first_agent_operands(args, tmp_path):
    assert cli.main(["--root", str(tmp_path), *args]) == 2
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("supplied", [False, True])
def test_optional_prefix_does_not_mask_business_completion(supplied, tmp_path):
    from typer._click.shell_completion import _resolve_context, _resolve_incomplete

    seen = []

    def complete(ctx, args, incomplete):
        seen.append(ctx.params["agent"])
        return ["task"]

    def invoke(
        agent: Annotated[str | None, AgentArgument("source")] = None,
        query: Annotated[str | None, typer.Argument(autocompletion=complete)] = None,
    ):
        pytest.fail("completion invoked the action")

    command = _app(invoke)
    args = ["--root", str(tmp_path), *(["alice"] if supplied else []), "call"]
    ctx = _resolve_context(command, {}, "too", args)
    target, value = _resolve_incomplete(ctx, args, "ta")
    assert target.name == "query"
    assert [item.value for item in target.shell_complete(ctx, value)] == ["task"]
    assert seen == ["alice" if supplied else None]
    assert ctx.get_parameter_source("agent") is (
        ParameterSource.COMMANDLINE if supplied else ParameterSource.DEFAULT
    )


@pytest.mark.parametrize("supplied", [False, True])
def test_agent_prefix_precedes_business_operands_in_help(supplied, tmp_path):
    seen = []

    def invoke(
        value: Annotated[str, typer.Argument(metavar="NAME")],
        agent: Annotated[str | None, AgentArgument("source")] = None,
    ):
        seen.append((agent, value))
        return agent, value

    command = _app(invoke)
    prefix = ["alice"] if supplied else []
    expected = "alice" if supplied else None
    assert command.main(
        ["--root", str(tmp_path), *prefix, "call", "task"], standalone_mode=False
    ) == (expected, "task")
    assert seen == [(expected, "task")]
    child = command.commands["call"]
    with Context(command, info_name="too") as parent:
        with Context(child, info_name="call", parent=parent) as ctx:
            arguments = (
                ctx.get_help().split("Arguments:\n", 1)[1].split("Options:", 1)[0]
            )
    assert arguments.index("AGENT") < arguments.index("NAME")
    assert seen == [(expected, "task")]


@pytest.mark.parametrize("tail", [[], ["value"], ["--help"], ["--unknown"]])
@pytest.mark.parametrize("rendered", [False, True])
def test_mixed_group_rejects_prefix_on_a_leaf_without_an_agent(
    tail, rendered, tmp_path, capsys
):
    from toolang.common.typer.ui import run
    from typer._click.shell_completion import _resolve_context

    app = typer.Typer(cls=_Group, add_completion=False)

    @app.callback()
    def root(ctx: typer.Context):
        ctx.obj = CliContext(tmp_path)

    group = typer.Typer(cls=CliGroup)
    calls = []

    @group.command(cls=CliCommand)
    def scoped(agent: Annotated[str, AgentArgument("source")]):
        return agent

    @group.command(cls=CliCommand, no_args_is_help=True)
    def plain(value: str):
        calls.append(value)

    app.add_typer(group, name="group")
    command = typer.main.get_command(app)
    args = ["alice", "group", "plain", *tail]
    if rendered:
        assert run(app, args=args) == 2
        assert "does not accept an agent prefix" in capsys.readouterr().err
    else:
        with pytest.raises(UsageError, match="does not accept an agent prefix"):
            command.main(args, standalone_mode=False)
    assert not calls
    assert command.main(["alice", "group", "scoped"], standalone_mode=False) == "alice"
    command.main(["group", "plain", "value"], standalone_mode=False)
    assert calls == ["value"]
    completion = _resolve_context(command, {}, "too", ["alice", "group", "plain"])
    assert completion.command.name == "group"
    assert [
        item.value for item in completion.command.shell_complete(completion, "")
    ] == ["scoped"]
    assert calls == ["value"]


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
        ctx: typer.Context,
        agent: Annotated[str, AgentArgument("source", placements="resident")],
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
    "arguments",
    [["new", "bad-name"], ["remove", "bad-name"], ["clone", "alice", "bad-name"]],
)
def test_catalog_names_are_native_operands_without_selector_side_effects(
    arguments, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    assert cli.main(arguments) == 2
    assert "Invalid value" in capsys.readouterr().err
    assert not tuple(tmp_path.iterdir())
