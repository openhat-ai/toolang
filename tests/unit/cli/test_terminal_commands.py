"""Command lifecycle hooks pass normal identities without coupling publishers."""

from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest
import typer

from toolang.cli.common.context import CliContext
from toolang.cli.common.terminal_session import TerminalSession
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import hub, script, thread, top
from toolang.common.layout import AgentLayout
from toolang.execution.types import RunOverride
from toolang.lang.input import CallInput
from toolang.up.types import AgentServerRef


@pytest.mark.parametrize(
    "command",
    [
        ["chat"],
        ["talk"],
        ["top"],
        ["serve"],
        ["hub", "serve"],
        ["rerun"],
        ["rerun"],
        ["agent:sample", "chat"],
        ["agent:sample", "top"],
    ],
)
def test_terminal_commands_remove_title_prefix_option(tmp_path, capsys, command):
    args = ["--root", str(tmp_path), *command]
    assert cli.main([*args, "--help"]) == 0
    output = capsys.readouterr()
    assert "--title-prefix" not in output.out
    assert not output.err

    assert cli.main([*args, "--title-prefix", "[too] "]) == 2
    assert "No such option: --title-prefix" in capsys.readouterr().err


@pytest.fixture
def publication(monkeypatch):
    entries, events = [], []

    class Terminal(TerminalSession):
        def start(self, *args, **kwargs):
            events.append("start")

        def set_run(self, run_id):
            events.append(("run", run_id))

        def close(self):
            events.append("close")

    def install(module):
        def prepare(kind, **kwargs):
            entries.append((kind, kwargs))
            return Terminal()

        monkeypatch.setattr(module, "prepare_terminal", prepare)
        return entries, events

    return install


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("runnable", ["build", "<entry:0>"])
def test_script_publishes_basename_and_actual_allocated_run(
    tmp_path, monkeypatch, publication, remote, runnable
):
    source = tmp_path / "file.too"
    source.write_text("agic build():\n  Hello.\n")
    layout = AgentLayout.roaming(source)
    layout.home.mkdir(parents=True)
    entries, events = publication(script)
    allocated = []

    @contextmanager
    def acquire(*_args, **_kwargs):
        yield AgentServerRef(sandbox="host", endpoint="http://test") if remote else None

    async def execute(**kwargs):
        run_id = "run_remote" if remote else kwargs["run_id"]
        allocated.append(run_id)
        if remote:
            kwargs["on_accept"](run_id)
        assert events == ["start", ("run", run_id)]
        return SimpleNamespace(id=run_id)

    monkeypatch.setattr(
        "toolang.up.source.materialize_roaming_program", lambda _: layout
    )
    monkeypatch.setattr("toolang.cli.common.agent_server.acquire_agent_server", acquire)
    monkeypatch.setattr(
        "toolang.state.prepare.prepare_agent_state", lambda *a, **kw: None
    )
    monkeypatch.setattr("toolang.up.logging.configure_logging_plan", lambda _: None)
    monkeypatch.setattr(script, "_execute_remote" if remote else "_execute", execute)
    monkeypatch.setattr(script, "_emit_result", lambda *a, **kw: 0)
    assert (
        script._run(
            source,
            runnable=runnable,
            runnable_kind="agic",
            override=RunOverride(),
            input=CallInput({}),
            raw_named=CallInput({}),
            allow_options=(),
            model_body=None,
            limit_options=(),
            sandbox=None,
            dev=None,
            save=None,
            quiet=False,
        )
        == 0
    )
    assert entries[0][0] == "run"
    assert entries[0][1]["label"] == "run file.too" + (
        " build" if runnable == "build" else ""
    )
    assert events == ["start", ("run", allocated[0]), "close"]


@pytest.mark.parametrize("kind", ["rerun"])
def test_restart_keeps_source_title_and_marks_accepted_execution(
    tmp_path, monkeypatch, publication, kind
):
    entries, events = publication(thread)
    layout = AgentLayout.resident(tmp_path, "alice")
    result = SimpleNamespace(id="run_actual")

    class Handle:
        run_id = "run_actual"

        async def wait(self):
            assert events == ["start", ("run", "run_actual")]
            return result

    class Client:
        async def rerun(self, request, **kwargs):
            assert request.source == "run_source"
            return Handle()

    @contextmanager
    def acquire(*_args, **_kwargs):
        yield AgentServerRef(sandbox="host", endpoint="http://test")

    @asynccontextmanager
    async def client(*_args, **_kwargs):
        yield Client()

    monkeypatch.setattr(thread, "acquire_agent_server", acquire)
    monkeypatch.setattr(thread, "acquire_run_client", client)
    assert (
        thread._run_rerun(
            layout=layout,
            source="run_source",
            sandbox=None,
            dev=None,
            commands=(),
            model_override=None,
            show_progress=False,
        )
        is result
    )
    assert entries[0][0] == kind
    assert entries[0][1]["label"] == f"{kind} run_source"
    assert events == ["start", ("run", "run_actual"), "close"]


def test_hub_serve_has_one_outer_lifecycle(tmp_path, monkeypatch, publication):
    entries, events = publication(hub)
    monkeypatch.setattr(hub, "_settings", lambda *_: (object(), 7000))

    def serve(*_args, **_kwargs):
        assert events == ["start"]
        return 0

    monkeypatch.setattr(hub, "serve_hub", serve)
    context = typer.Context(typer.main.get_command(cli.app), obj=CliContext(tmp_path))
    with pytest.raises(typer.Exit) as error:
        hub.serve(context)
    assert error.value.exit_code == 0
    assert entries[0][0] == "serve" and entries[0][1]["label"] == "serve hub"
    assert events == ["start", "close"]


@pytest.mark.parametrize("agent", [None, "alice"])
def test_interactive_top_publishes_scoped_title_and_closes_on_exit(
    tmp_path, monkeypatch, publication, agent
):
    import sys

    entries, events = publication(top)
    layout = AgentLayout.resident(tmp_path, agent) if agent else None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    class Observer:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            events.append("observer closed")

    def run(*_args):
        assert events == ["start"]

    monkeypatch.setattr(top, "TopObserver", Observer)
    monkeypatch.setattr("toolang.cli.common.top.run_top", run)
    context = typer.Context(
        typer.main.get_command(cli.app), obj=CliContext(tmp_path, agent, layout)
    )
    top.top(context)
    assert entries[0][0] == "top"
    assert entries[0][1]["label"] == (f"{agent} top" if agent else "top")
    assert entries[0][1]["layout"] == layout
    assert entries[0][1]["context"] == ("" if agent else str(tmp_path.resolve()))
    assert events == ["start", "close", "observer closed"]


def test_agent_serve_publishes_once_at_outer_cli(tmp_path, monkeypatch, publication):
    from toolang.cli.toolang.commands import runtime
    from toolang.up import sandbox

    entries, events = publication(runtime)
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(runtime, "require_runtime_agent", lambda *_: "alice")
    monkeypatch.setattr(
        runtime,
        "resolve_startup",
        lambda *a, **kw: SimpleNamespace(
            startup=SimpleNamespace(sandbox="fake"), target=layout
        ),
    )
    monkeypatch.setattr(runtime, "warn_development_package_source", lambda _: None)

    async def run(*_args, **_kwargs):
        assert events == ["start"]
        return 0

    monkeypatch.setattr(sandbox, "run", run)
    context = typer.Context(
        typer.main.get_command(cli.app), obj=CliContext(tmp_path, layout=layout)
    )
    with pytest.raises(typer.Exit) as error:
        runtime.run(context, "alice")
    assert error.value.exit_code == 0
    assert entries[0][0] == "serve" and entries[0][1]["label"] == "serve alice"
    assert entries[0][1]["layout"] == layout
    assert events == ["start", "close"]
