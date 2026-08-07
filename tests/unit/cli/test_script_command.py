from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

import pytest
from click.utils import strip_ansi

from toolang.base.errors import ToolangError
from toolang.cli.toolang.commands import script
from toolang.execution.executor import CeilingSpec
from toolang.execution.records import RunInputRef, RunRecord
from toolang.lang.submission import RunnableCall, parse_runnable_call
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
            "--model",
            "openai/gpt",
            "--models",
            "openai/*,deepseek/*",
            "--models",
            "openai/*",
            "--tools",
            "filesystem/*,shell/*",
            "--caps",
            "skill/reviewer,service/github",
            "--limit",
            "tokens=1000,cost=2.5",
            "--limit",
            "time=60",
            "-vv",
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
    assert captured["model"] == "openai/gpt"
    assert captured["ceiling"] == CeilingSpec(
        models=("openai/*", "deepseek/*"),
        tools=("filesystem/*", "shell/*"),
        caps=("skill/reviewer", "service/github"),
    )
    assert captured["verbosity"] == 2
    assert captured["limit_options"] == (
        "tokens=1000,cost=2.5",
        "time=60",
    )
    assert captured["raw_args"] == (("count", "2.5"), ("enabled", "true"))
    call = captured["call"]
    assert isinstance(call, RunnableCall)
    assert call.content == "hello world"


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
    call = captured["call"]
    assert isinstance(call, RunnableCall)
    assert call.content == "from stdin"


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
    call = captured["call"]
    assert isinstance(call, RunnableCall)
    assert call.overrides[0].selector == "alternate"
    assert call.content == "from stdin"
    assert captured["raw_args"] == ()


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
    call = captured["call"]
    assert isinstance(call, RunnableCall)
    assert call.content == "from stdin"


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
    assert captured["raw_args"] == ()
    call = captured["call"]
    assert isinstance(call, RunnableCall)
    assert call.content == "count=2"


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
    call = captured["call"]
    assert isinstance(call, RunnableCall)
    assert call.content == "@sample.png"


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
    try:
        with pytest.raises(ToolangError, match="Runnable not found: missing"):
            asyncio.run(
                script._execute(
                    layout=harness.setup.layout,
                    state=harness.state,
                    store=harness.store,
                    ids=harness.ids,
                    run_id="run_test",
                    runnable="demo",
                    call=parse_runnable_call(":agic missing\nInput"),
                    raw_args=(("count", "1"),),
                    model=None,
                    ceiling=CeilingSpec(),
                    quiet=True,
                    verbosity=0,
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
        "count=Number [enabled=Boolean] INPUT..."
        in stdout
    )
    assert "Run the documented demo." in stdout
    assert "Arguments" in stdout
    assert "count=Number" in stdout
    assert "enabled=Boolean" in stdout
    assert "Primary Part[] input." in stdout


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
        lambda source_path, **kwargs: captured.update(
            source_path=source_path,
            **kwargs,
        )
        or 0,
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


def test_script_rejects_sandbox_option(
    tmp_path: Path,
    capsys,
) -> None:
    source = _write_source(tmp_path)

    result = script.dispatch(
        [],
        [str(source), "demo", "count=2", "--sandbox=none", "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()

    assert result == 2
    assert "No such option: --sandbox" in strip_ansi(output.err)


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


def test_script_does_not_repeat_a_failure_reported_by_the_tracer(
    tmp_path: Path,
    capsys,
) -> None:
    result = script._emit_result(
        RunRecord(
            id="run_failed",
            parent=None,
            thread="script_thread",
            input=RunInputRef(),
            output=None,
            status="failed",
            error="output is not valid Number",
        ),
        store_path=tmp_path / "runs.db",
        log_path=None,
        error_reported=True,
    )
    output = capsys.readouterr()

    assert result == 1
    assert output.err == "Run: run_failed\n"
