from __future__ import annotations

import asyncio
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from click.utils import strip_ansi

from toolang.base.errors import ToolangError
from toolang.cli.toolang.commands import script
from toolang.common.layout import AgentLayout
from toolang.execution.calls import parse_call
from toolang.execution.records import RunRecord
from toolang.execution.types import ControlRef, RunOverride, RunStatus
from toolang.lang.input import RunnableInputRaw
from toolang.up.types import AgentServerRef
from tests.support.execution_harness import ExecutionHarness


_SOURCE = """
## Run the documented demo.
agic demo(_: Part[], count: Number, enabled?: Boolean):
  {{_}}
"""


def _write_source(tmp_path: Path, source: str = _SOURCE) -> Path:
    path = tmp_path / "demo.too"
    path.write_text(source, encoding="utf-8")
    return path


def test_script_binds_options_arguments_and_primary_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(source_path: Path, **kwargs) -> int:
        captured.update(source_path=source_path, **kwargs)
        return 0

    monkeypatch.setattr(script, "_run", fake_run)

    result = script.dispatch(
        [],
        [
            str(source),
            "demo",
            "--default",
            "model=openai/gpt",
            "--allow",
            "models=openai/*,deepseek/*",
            "--allow",
            "models=openai/*",
            "--allow",
            "tools=fs/*,shell/*",
            "--allow",
            "caps=skill/reviewer,service/github",
            "--limit",
            "tokens=1000",
            "--limit",
            "cost=2.5",
            "--limit",
            "time=60",
            "--sandbox",
            "docker:python:3.13-slim",
            "--dev",
            str(tmp_path / "dist"),
            "--save",
            "-",
            "count=2.5",
            "enabled=true",
            "hello",
            "world",
        ],
        prog_name="toolang",
        stdin=StringIO(),
    )

    assert result == 0
    assert captured["source_path"] == source.resolve()
    assert captured["runnable"] == "demo"
    assert captured["default_options"] == ("model=openai/gpt",)
    assert captured["allow_options"] == (
        "models=openai/*,deepseek/*",
        "models=openai/*",
        "tools=fs/*,shell/*",
        "caps=skill/reviewer,service/github",
    )
    assert "verbosity" not in captured
    assert captured["save"] == "-"
    assert captured["sandbox"] == "docker:python:3.13-slim"
    assert captured["dev"] == tmp_path / "dist"
    assert captured["limit_options"] == (
        "tokens=1000",
        "cost=2.5",
        "time=60",
    )
    assert captured["raw_named"] == (("count", "2.5"), ("enabled", "true"))
    input = captured["input"]
    assert isinstance(input, RunnableInputRaw)
    assert input.primary == "hello world"


def test_script_reads_primary_input_from_stdin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(_source_path: Path, **kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(script, "_run", fake_run)

    result = script.dispatch(
        [],
        [str(source), "demo", "count=2"],
        prog_name="toolang",
        stdin=StringIO("from stdin"),
    )

    assert result == 0
    input = captured["input"]
    assert isinstance(input, RunnableInputRaw)
    assert input.primary == "from stdin"


def test_script_stdin_can_override_the_cli_runnable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _write_source(
        tmp_path,
        """
agic demo(_: Part[], count: Number):
  {{_}}

agic alternate(_: Part[]):
  {{_}}
""",
    )
    captured: dict[str, object] = {}

    def fake_run(_source_path: Path, **kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(script, "_run", fake_run)

    result = script.dispatch(
        [],
        [str(source), "demo"],
        prog_name="toolang",
        stdin=StringIO(":agic alternate\n\nfrom stdin"),
    )

    assert result == 0
    assert captured["commands"] == (
        RunOverride("default", "runnable", "agic:alternate"),
    )
    input = captured["input"]
    assert isinstance(input, RunnableInputRaw)
    assert input.primary == "from stdin"
    assert captured["raw_named"] == ()


def test_script_supports_explicit_stdin_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(_source_path: Path, **kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(script, "_run", fake_run)

    result = script.dispatch(
        [],
        [str(source), "demo", "count=2", "-"],
        prog_name="toolang",
        stdin=StringIO("from stdin"),
    )

    assert result == 0
    input = captured["input"]
    assert isinstance(input, RunnableInputRaw)
    assert input.primary == "from stdin"


def test_script_keeps_assignments_after_separator_as_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _write_source(
        tmp_path,
        """
agic demo(_: Part[], count?: Number):
  {{_}}
""",
    )
    captured: dict[str, object] = {}

    def fake_run(_source_path: Path, **kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(script, "_run", fake_run)

    result = script.dispatch(
        [],
        [str(source), "demo", "--", "count=2"],
        prog_name="toolang",
        stdin=StringIO(),
    )

    assert result == 0
    assert captured["raw_named"] == ()
    input = captured["input"]
    assert isinstance(input, RunnableInputRaw)
    assert input.primary == "count=2"


def test_script_includes_an_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _write_source(tmp_path)
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n")
    captured: dict[str, object] = {}

    def fake_run(_source_path: Path, **kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(script, "_run", fake_run)
    monkeypatch.chdir(tmp_path)

    result = script.dispatch(
        [],
        [str(source), "demo", "count=2", "@sample.png"],
        prog_name="toolang",
        stdin=StringIO(),
    )

    assert result == 0
    input = captured["input"]
    assert isinstance(input, RunnableInputRaw)
    assert input.primary == "@sample.png"


def test_script_shows_runnable_help_for_a_missing_required_parameter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = _write_source(tmp_path)
    monkeypatch.setattr(
        script,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("an incomplete call must not run"),
    )

    result = script.dispatch(
        [],
        [str(source), "demo", "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()

    assert result == 2
    assert "Usage:" in output.out
    assert "count=Number" in output.out
    assert "Run:" not in output.err


def test_script_shows_runnable_help_for_missing_primary_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = _write_source(
        tmp_path,
        """
agic demo(_: Part[]):
  {{_}}
""",
    )
    monkeypatch.setattr(
        script,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("an incomplete call must not run"),
    )

    result = script.dispatch(
        [],
        [str(source), "demo"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()

    assert result == 2
    assert "Usage:" in output.out
    assert "Primary Part[] input." in output.out
    assert "requires primary input" not in output.err
    assert "Run:" not in output.err


def test_script_validates_before_creating_a_thread(tmp_path, monkeypatch) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=_SOURCE,
        responses=[],
    )

    async def current_setup(_watcher):
        return harness.setup

    monkeypatch.setattr(script.SetupWatcher, "refresh", current_setup)
    commands, input = parse_call(":agic missing\nInput")
    try:
        with pytest.raises(ToolangError, match="Runnable not found: missing"):
            asyncio.run(
                script._execute(
                    layout=harness.setup.layout,
                    state=harness.state,
                    store=harness.store,
                    ids=harness.ids,
                    run_id="run_test",
                    sandbox="host",
                    runnable="demo",
                    commands=commands,
                    input=input,
                    raw_named=(("count", "1"),),
                    allow_options=(),
                    default_options=(),
                    quiet=True,
                )
            )

        assert not harness.store.list_threads()
        assert not harness.store.list_runs(limit=None)
    finally:
        harness.store.close()


def test_script_uses_typer_help_and_authored_docs(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = _write_source(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = script.dispatch(
        [],
        [source.name, "demo", "--help"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert (
        "Usage: toolang demo.too demo [OPTIONS] "
        "count=Number [enabled=Boolean] INPUT..." in stdout
    )
    assert "Run the documented demo." in stdout
    assert "Arguments" in stdout
    assert "count=Number" in stdout
    assert "enabled=Boolean" in stdout
    assert "[enabled=Boolean]" in stdout.partition("Arguments")[2]
    assert "Primary Part[] input." in stdout
    assert "--save" in stdout
    assert "--sandbox" in stdout
    assert "--dev" in stdout
    assert "Save the Run result to PATH, or use - for" in stdout
    assert "stdout." in stdout
    assert "--verbose" not in stdout
    assert "-v" not in stdout
    positions = tuple(
        stdout.index(option) for option in ("--allow", "--limit", "--default")
    )
    assert positions == tuple(sorted(positions))


def test_script_hides_default_and_generated_agics(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = _write_source(
        tmp_path,
        """
agic:
  Default behavior.

## Run the visible command.
agic visible:
  Visible behavior.

## Run the pipeline.
flow pipeline:
  run:
    Inline behavior.
""",
    )
    monkeypatch.chdir(tmp_path)

    result = script.dispatch(
        [],
        [source.name, "--help"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert "Usage: toolang demo.too [OPTIONS] RUNNABLE [ARGS]..." in stdout
    assert "Runnables" in stdout
    assert "Commands" not in stdout
    assert "visible" in stdout
    assert "Run the visible command." in stdout
    assert "pipeline" in stdout
    assert "Run the pipeline." in stdout
    assert "default" not in stdout
    assert "<agic:" not in stdout


def test_script_formats_an_unknown_runnable_as_a_rich_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = _write_source(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = script.dispatch(
        [],
        [source.name, "missing"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()
    stderr = strip_ansi(output.err)
    lines = stderr.splitlines()

    assert result == 2
    assert lines[0].strip() == ""
    assert lines[1].startswith(" Usage: toolang ")
    assert lines[2].strip() == ""
    assert lines[3].startswith(" Try '")
    assert lines[4].strip() == ""
    assert lines[5].startswith("╭─ Error ")
    assert lines[-1].strip() == ""
    assert "No such command 'missing'." in stderr
    assert "\nError: No such command" not in stderr


@pytest.mark.parametrize("selector", ("agic:demo", "runnable:demo"))
def test_script_accepts_explicit_runnable_selectors(
    tmp_path: Path,
    monkeypatch,
    selector: str,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script,
        "_run",
        lambda source_path, **kwargs: (
            captured.update(
                source_path=source_path,
                **kwargs,
            )
            or 0
        ),
    )

    result = script.dispatch(
        [],
        [str(source), selector, "count=2", "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )

    assert result == 0
    assert captured["runnable"] == "demo"


def test_script_rejects_an_explicit_runnable_kind_mismatch(
    tmp_path: Path,
    capsys,
) -> None:
    source = _write_source(tmp_path)

    result = script.dispatch(
        [],
        [str(source), "flow:demo", "count=2", "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )

    assert result == 1
    assert "runnable is not a flow: demo" in capsys.readouterr().err


def test_script_accepts_sandbox_option_at_the_runnable_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script,
        "_run",
        lambda _source, **kwargs: captured.update(kwargs) or 0,
    )

    result = script.dispatch(
        [],
        [str(source), "demo", "count=2", "--sandbox=host", "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    assert result == 0
    assert captured["sandbox"] == "host"


def test_script_routes_quiet_execution_through_a_remote_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = _write_source(tmp_path)
    layout = AgentLayout.roaming(source)
    captured: dict[str, object] = {}

    @contextmanager
    def agent_server_context(selected: AgentLayout, **kwargs):
        assert selected == layout
        captured["runtime"] = kwargs
        yield AgentServerRef(
            sandbox="docker:python:3.13-slim",
            endpoint="http://127.0.0.1:7001",
        )

    async def execute_remote(**kwargs):
        captured["execute"] = kwargs
        kwargs["on_accept"]("run_remote")
        return RunRecord(
            id="run_remote",
            parent=None,
            thread="script_remote",
            control=ControlRef("run_remote", 0),
            state=ControlRef("run_remote", 0),
            output=None,
            status="succeeded",
            error=None,
        )

    monkeypatch.setattr(
        script.agents,
        "materialize_roaming_program",
        lambda _source: layout,
    )
    monkeypatch.setattr(script, "acquire_agent_server", agent_server_context)
    monkeypatch.setattr(script, "_execute_remote", execute_remote)
    monkeypatch.setattr(
        script,
        "prepare_agent_state",
        lambda *_args, **_kwargs: pytest.fail("remote execution prepared local state"),
    )

    result = script._run(
        source,
        runnable="demo",
        commands=(),
        input=RunnableInputRaw(primary="hello"),
        raw_named=(("count", "2"),),
        allow_options=(),
        default_options=(),
        limit_options=(),
        sandbox="docker",
        dev=tmp_path / "dist",
        save=None,
        quiet=True,
    )

    assert result == 0
    assert captured["runtime"] == {
        "sandbox": "docker",
        "dev": tmp_path / "dist",
        "show_progress": False,
    }
    execute = cast(dict[str, object], captured["execute"])
    assert execute["sandbox"] == "docker:python:3.13-slim"
    assert execute["quiet"] is True
    assert capsys.readouterr() == ("", "")


def test_remote_script_cancellation_cancels_the_accepted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    entered = asyncio.Event()
    canceled = asyncio.Event()
    calls: list[object] = []

    class Handle:
        run_id = "run_remote"

        async def wait(self):
            calls.append("wait")
            entered.set()
            await canceled.wait()
            return SimpleNamespace(id=self.run_id)

    class Client:
        endpoint = "http://runtime.test:7001"

        def __init__(self, endpoint: str, *, client: object) -> None:
            assert endpoint == self.endpoint
            del client

        async def connect(self) -> None:
            calls.append("connect")

        async def run(self, request, *, tracer=None):
            calls.append(("run", request, tracer))
            return Handle()

        async def cancel(self, run_id: str, **kwargs):
            calls.append(("cancel", run_id, kwargs))
            canceled.set()
            return object()

        async def disconnect(self) -> None:
            calls.append("disconnect")

    async def inspect(*_args, **_kwargs):
        return object()

    async def create_thread(*_args, **_kwargs):
        return "script_remote"

    monkeypatch.setattr(script, "RemoteRunClient", Client)
    monkeypatch.setattr(script, "inspect_remote_runtime", inspect)
    monkeypatch.setattr(script, "_create_remote_script_thread", create_thread)
    monkeypatch.setattr(script, "load_runtime_environ", lambda *_args, **_kwargs: {})

    async def scenario() -> None:
        task = asyncio.create_task(
            script._execute_remote(
                layout=layout,
                endpoint=Client.endpoint,
                sandbox="docker:python:3.13-slim",
                runnable="demo",
                commands=(),
                input=RunnableInputRaw(primary="hello"),
                raw_named=(("count", "2"),),
                allow_options=(),
                default_options=(),
                limit_options=(),
                quiet=True,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())

    cancel = next(
        item for item in calls if isinstance(item, tuple) and item[0] == "cancel"
    )
    assert cancel[1] == "run_remote"
    assert cast(dict[str, object], cancel[2])["reason"] == "script interrupted"
    assert calls.count("wait") == 2
    assert calls[0] == "connect"
    assert calls[-1] == "disconnect"


def test_remote_script_default_returns_to_the_dynamic_runnable() -> None:
    commands = script._remote_script_commands(
        (RunOverride("default", "runnable", None),),
        runnable="demo",
    )

    assert commands == (RunOverride("default", "runnable", "demo"),)


def test_remote_script_rejects_mixed_named_input_sources() -> None:
    with pytest.raises(
        ValueError,
        match="named inputs cannot be supplied by both source and surface",
    ):
        script._remote_script_input(
            RunnableInputRaw(named=(("count", "1"),)),
            raw_named=(("enabled", "true"),),
        )


@pytest.mark.parametrize("option", ("-v", "--verbose"))
def test_script_rejects_removed_verbose_option(
    tmp_path: Path,
    capsys,
    option: str,
) -> None:
    source = _write_source(tmp_path)

    result = script.dispatch(
        [],
        [str(source), "demo", "count=2", option, "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()

    assert result == 2
    assert f"No such option: {option}" in strip_ansi(output.err)


def test_script_rejects_stdin_marker_mixed_with_input(
    tmp_path: Path,
    capsys,
) -> None:
    source = _write_source(tmp_path)

    result = script.dispatch(
        [],
        [str(source), "demo", "count=2", "-", "hello"],
        prog_name="toolang",
        stdin=StringIO("stdin"),
    )
    output = capsys.readouterr()

    assert result == 2
    assert "stdin marker '-' must be the only primary input" in output.err


@pytest.mark.parametrize("status", ("failed", "canceled"))
def test_script_does_not_save_an_unsuccessful_run(
    tmp_path: Path,
    capsys,
    status: RunStatus,
) -> None:
    destination = tmp_path / "result.txt"
    destination.write_text("existing", encoding="utf-8")
    result = script._emit_result(
        RunRecord(
            id=f"run_{status}",
            parent=None,
            thread="script_thread",
            control=ControlRef(f"run_{status}", 0),
            state=ControlRef(f"run_{status}", 0),
            output=None,
            status=status,
            error="output is not valid Number",
        ),
        store_path=tmp_path / "runs.db",
        log_path=None,
        save=str(destination),
        error_reported=True,
    )
    output = capsys.readouterr()

    assert result == 1
    assert output.err == ""
    assert destination.read_text(encoding="utf-8") == "existing"


def test_quiet_unsuccessful_run_reports_fallback_error(
    tmp_path: Path,
    capsys,
) -> None:
    result = script._emit_result(
        RunRecord(
            id="run_failed",
            parent=None,
            thread="script_thread",
            control=ControlRef("run_failed", 0),
            state=ControlRef("run_failed", 0),
            output=None,
            status="failed",
            error="provider unavailable",
        ),
        store_path=tmp_path / "runs.db",
        log_path=None,
        error_reported=False,
    )

    assert result == 1
    assert "provider unavailable" in capsys.readouterr().err
