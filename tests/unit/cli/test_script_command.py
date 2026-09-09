from __future__ import annotations

import asyncio
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from rich.cells import cell_len

from typer._click.utils import strip_ansi

from toolang.base.errors import ToolangError
from toolang.base.types.model import ModelRequest
from toolang.base.types.progress import ProgressEvent
from toolang.cli.toolang.commands import script
from toolang.common.layout import AgentLayout
from toolang.execution.calls import parse_call
from toolang.execution.records import RunRecord
from toolang.execution.types import (
    ControlRef,
    ErrorMessage,
    RunOverride,
    RunStatus,
    SessionSetting,
    ThreadRef,
)
from toolang.lang.input import CallInput
from toolang.lang.ast import FlowDecl, Program, RunStmt, Span
from toolang.up import process as agents
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
            "--model",
            "openai/gpt effort=high",
            "--allow",
            "models=openai/*,deepseek/*",
            "--allow",
            "models=openai/*",
            "--allow",
            "tools=fs/*,shell/*",
            "--allow",
            "skills=reviewer",
            "--allow",
            "services=github",
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
            "--out",
            "-",
            "count=2.5",
            "enabled=true",
            "--",
            "hello",
            "world",
        ],
        prog_name="toolang",
        stdin=StringIO(),
    )

    assert result == 0
    assert captured["source_path"] == source.resolve()
    assert captured["runnable"] == "demo"
    assert captured["runnable_kind"] == "agic"
    assert captured["model_body"] == "openai/gpt effort=high"
    assert captured["allow_options"] == (
        "models=openai/*,deepseek/*",
        "models=openai/*",
        "tools=fs/*,shell/*",
        "skills=reviewer",
        "services=github",
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
    assert captured["raw_named"] == {"count": "2.5", "enabled": "true"}
    input = captured["input"]
    assert isinstance(input, CallInput)
    assert input.get("_") == "hello world"


def test_script_model_body_builds_one_invocation_session_layer() -> None:
    override = script._script_session_override(
        model_body="test/model effort=4096",
        allow_options=("models=test/*",),
        limit_options=("tokens=1000",),
    )

    assert override.model is not None
    assert override.model.identity == "test/model"
    assert override.model.effort == 4096
    assert override.allow[0].field == "models"
    assert override.limits[0].value == 1000


@pytest.mark.parametrize("child_overrides", [False, True])
def test_script_inherits_common_options_and_applies_child_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_overrides: bool
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _path, **kwargs: captured.update(kwargs) or 0
    )
    child = (
        [
            "--model",
            "child/model",
            "--sandbox",
            "host",
            "--out",
            "child.txt",
            "--dev",
            str(tmp_path / "child"),
            "--allow",
            "tools=child/*",
            "--limit",
            "tokens=200",
        ]
        if child_overrides
        else []
    )
    assert (
        script.dispatch(
            [],
            [
                str(source),
                "--model",
                "root/model",
                "--sandbox",
                "docker:python",
                "-o",
                "root.txt",
                "-q",
                "--dev",
                str(tmp_path / "root"),
                "--allow",
                "tools=root/*",
                "--limit",
                "tokens=100",
                "demo",
                *child,
                "count=2",
                "hello",
            ],
            prog_name="too",
            stdin=_UnreadableStdin(),
        )
        == 0
    )
    level = "child" if child_overrides else "root"
    assert captured["model_body"] == f"{level}/model"
    assert captured["sandbox"] == ("host" if child_overrides else "docker:python")
    assert captured["save"] == f"{level}.txt"
    assert captured["dev"] == tmp_path / level
    assert captured["quiet"] is True
    assert captured["allow_options"] == (
        ("tools=root/*", "tools=child/*") if child_overrides else ("tools=root/*",)
    )
    assert captured["limit_options"] == (
        ("tokens=100", "tokens=200") if child_overrides else ("tokens=100",)
    )
    assert captured["raw_named"] == {"count": "2"}
    assert captured["input"] == {"_": "hello"}


@pytest.mark.parametrize("value", ["-", "--", "---"])
def test_script_root_option_values_do_not_start_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _path, **kwargs: captured.update(kwargs) or 0
    )
    assert (
        script.dispatch(
            [],
            [str(source), "-o", value, "demo", "count=2", "hello"],
            prog_name="too",
            stdin=_UnreadableStdin(),
        )
        == 0
    )
    assert captured["save"] == value
    assert captured["input"] == {"_": "hello"}


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
    assert isinstance(input, CallInput)
    assert input.get("_") == "from stdin"


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
    assert captured["override"] == RunOverride(
        runnable="agic:alternate",
    )
    input = captured["input"]
    assert isinstance(input, CallInput)
    assert input.get("_") == "from stdin"
    assert captured["raw_named"] == {}


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
    assert isinstance(input, CallInput)
    assert input.get("_") == "from stdin"


class _UnreadableStdin(StringIO):
    def read(self, size: int | None = -1, /) -> str:
        raise AssertionError("this invocation must not read stdin")


@pytest.mark.parametrize("suffix", [[], ["--", "text"], ["count=2"], ["--help"]])
@pytest.mark.parametrize("stdin", ["", "body\n---\n", "unclosed"])
def test_script_rejects_fenced_marker_before_reading_or_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    suffix: list[str],
    stdin: str,
) -> None:
    source = _write_source(tmp_path)
    monkeypatch.setattr(
        script,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("invalid input must not run"),
    )
    result = script.dispatch(
        [],
        [str(source), "demo", "---", *suffix],
        prog_name="too",
        stdin=_UnreadableStdin(stdin),
    )
    output = capsys.readouterr()
    assert result == 2
    error = " ".join(strip_ansi(output.err).replace("│", " ").split())
    assert (
        "fenced input marker '---' is not supported in script mode; "
        "use '-' to read stream input from stdin"
    ) in error
    assert "Unclosed" not in error
    assert "No such option" not in error


def test_script_preserves_explicit_empty_stream_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )
    result = script.dispatch(
        [],
        [str(source), "demo", "count=2", "-"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    assert result == 0
    assert captured["input"] == CallInput({"_": ""})


@pytest.mark.parametrize("value", ("", "   "))
@pytest.mark.parametrize("explicit", [False, True])
def test_script_rejects_empty_line_input(
    tmp_path: Path,
    capsys,
    value: str,
    explicit: bool,
) -> None:
    source = _write_source(tmp_path)

    result = script.dispatch(
        [],
        [str(source), "demo", "count=2", *(["--"] if explicit else []), value],
        prog_name="toolang",
        stdin=_UnreadableStdin(),
    )

    assert result == 2
    assert "line input requires nonempty text" in capsys.readouterr().err


@pytest.mark.parametrize("explicit", [False, True])
def test_script_accepts_line_input_with_or_without_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )
    result = script.dispatch(
        [],
        [
            str(source),
            "demo",
            "count=2",
            *(["--"] if explicit else []),
            "Review",
            "this",
            "count=3",
            "--help",
            "-",
            "---",
            "--",
        ],
        prog_name="toolang",
        stdin=_UnreadableStdin(),
    )
    assert result == 0
    assert captured["input"] == CallInput({"_": "Review this count=3 --help - --- --"})
    assert captured["raw_named"] == {"count": "2"}


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
    assert captured["raw_named"] == {}
    input = captured["input"]
    assert isinstance(input, CallInput)
    assert input.get("_") == "count=2"


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
        [str(source), "demo", "count=2", "--", "@sample.png"],
        prog_name="toolang",
        stdin=StringIO(),
    )

    assert result == 0
    input = captured["input"]
    assert isinstance(input, CallInput)
    assert input.get("_") == "@sample.png"


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
        [str(source), "demo", "--", "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()

    assert result == 2
    assert "Usage:" in output.out
    assert "count=<COUNT>" in output.out
    assert "Run:" not in output.err


@pytest.mark.parametrize("color", [False, True])
def test_script_shows_runnable_help_for_missing_primary_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    color: bool,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FORCE_COLOR", "1") if color else monkeypatch.delenv(
        "FORCE_COLOR", raising=False
    )
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
    assert "* INPUT Primary input" in " ".join(strip_ansi(output.out).split())
    assert ("\x1b[" in output.out) is color
    assert "Arguments:" in strip_ansi(output.out)
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

    monkeypatch.setattr("toolang.setup.SetupWatcher.refresh", current_setup)
    override, input = parse_call(":agic missing\nInput")
    try:
        with pytest.raises(ToolangError, match="runnable query matched no items"):
            asyncio.run(
                script._execute(
                    layout=harness.setup.layout,
                    state=harness.state,
                    store=harness.store,
                    ids=harness.ids,
                    run_id="run_test",
                    sandbox="host",
                    runnable="demo",
                    override=override,
                    input=input,
                    raw_named=CallInput({"count": "1"}),
                    session_override=RunOverride(),
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
    normalized = " ".join(stdout.split())

    assert result == 0
    assert (
        "Usage: toolang demo.too demo [OPTIONS] [NAME=VALUE...] [-- <INPUT> | -]"
        in normalized
    )
    assert "Run the documented demo." in stdout
    assert "Arguments" in stdout
    assert "count=<COUNT>" in stdout
    assert "enabled=<ENABLED>" in stdout
    assert "Optional." not in stdout
    assert "* count=<COUNT>" in stdout
    assert "[required]" not in stdout
    assert "PART[]" not in stdout
    assert "Input:" not in stdout
    assert "<str>" not in stdout
    for option, metavar in (
        ("--allow", "<RESOURCE>=<QUERY>"),
        ("--limit", "<LIMIT>=<VALUE>"),
        ("--model", "<MODEL_SPEC>"),
        ("--sandbox", "<SANDBOX_SPEC>"),
        ("--out", "<PATH>"),
    ):
        row = next(line for line in stdout.splitlines() if option in line.split())
        assert metavar in row.split()
    output_row = next(
        line.split() for line in stdout.splitlines() if "--out" in line.split()
    )
    assert "-o," in output_row
    assert "--save" not in stdout
    assert "--sandbox" in stdout
    assert "--dev" in stdout
    assert "Save the Run result to PATH, or use - for stdout" in " ".join(
        stdout.replace("│", " ").split()
    )
    assert "stdout" in stdout
    assert "--verbose" not in stdout
    assert "-v" not in stdout
    _assert_common_options(stdout)
    assert "--default" not in stdout
    assert "The flow proceeds as follows:" not in stdout


def _help_panel(output: str, title: str) -> str:
    body = output.partition(f"{title}:\n")[2].split("\n\n", 1)[0]
    return " ".join(body.split())


def _assert_common_options(output: str) -> None:
    panel = _help_panel(output, "Options")
    options = (
        "--quiet",
        "--out",
        "--sandbox",
        "--allow",
        "--limit",
        "--model",
        "--dev",
        "--help",
    )
    positions = [panel.index(option) for option in options]
    assert positions == sorted(positions)
    assert "-o" in panel and "-q" in panel
    assert "--dev [PATH]" in panel
    assert "Use a local Toolang wheel [bare: .]" in panel


@pytest.mark.parametrize("child", [False, True])
def test_script_help_after_common_options_never_reads_or_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, child: bool
) -> None:
    source = _write_source(tmp_path)
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("help must not run")
    )
    assert (
        script.dispatch(
            [],
            [
                str(source),
                "--model",
                "test/model",
                *(["demo"] if child else []),
                "--help",
            ],
            prog_name="too",
            stdin=_UnreadableStdin(),
        )
        == 0
    )
    output = strip_ansi(capsys.readouterr().out)
    assert ("Arguments:" in output) is child
    assert ("Runnables:" in output) is not child
    _assert_common_options(output)


def test_script_without_public_runnables_still_shows_common_options(
    tmp_path: Path, capsys
) -> None:
    source = _write_source(tmp_path, "agic:\n  Default behavior.\n")
    assert (
        script.dispatch(
            [], [str(source), "--help"], prog_name="too", stdin=_UnreadableStdin()
        )
        == 0
    )
    output = strip_ansi(capsys.readouterr().out)
    assert "Runnables:" not in output
    _assert_common_options(output)


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize(
    ("doc", "description"),
    [
        ("", None),
        ("## Documented runnable.\n", "Documented runnable."),
        ("## Use [bold]care[/bold]\n", "Use care."),
        (
            "## Use [bold]care[/bold].\n## Keep context.\n",
            "Use care. Keep context.",
        ),
    ],
)
def test_script_runnable_description_uses_docs_or_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    kind: str,
    doc: str,
    description: str | None,
) -> None:
    body = "  Unused body text." if kind == "agic" else "  let note = observed"
    source = _write_source(tmp_path, f"{doc}{kind} demo():\n{body}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("help must not run")
    )
    assert (
        script.dispatch(
            [],
            [source.name, "demo", "--help"],
            prog_name="too",
            stdin=_UnreadableStdin(),
        )
        == 0
    )
    output = strip_ansi(capsys.readouterr().out)
    summary = f"Run {kind} demo - {description}" if description else f"Run {kind} demo."
    assert " ".join(output.split("\n\n", 1)[0].split()) == summary
    assert " ".join(output.split()).count(summary) == 1
    assert (
        output.index(f"Run {kind} demo")
        < output.index("Usage:")
        < output.index("Options:")
    )
    assert "Unused body text." not in output
    assert "Arguments:" not in output
    assert "Input:" not in output and "─ Flow" not in output
    if kind == "flow":
        assert (
            output.index(f"Run {kind} demo")
            < output.index("Options:")
            < output.index("The flow proceeds as follows:")
        )
        assert _flow_outline_lines(output) == ["[0] Set value to note"]
    else:
        assert "The flow proceeds as follows:" not in output


@pytest.mark.parametrize(
    ("kind", "prog_name", "width"),
    [
        ("agic", "too", 80),
        ("agic", "toolang", 120),
        ("flow", "toolang", 80),
        ("flow", "too", 120),
    ],
)
@pytest.mark.parametrize(
    ("signature", "input_type", "arguments"),
    [
        ("", "Part[]", []),
        ("(_)", "Part[]", []),
        ("(_: Text)", "Text", []),
        ("()", None, []),
        ("(count: Number)", None, [("count", "Number", True)]),
        ("(class: Text)", None, [("class", "Text", True)]),
        ("(enabled?: Boolean)", None, [("enabled", "Boolean", False)]),
        (
            "(items: Text[], enabled?: Boolean)",
            None,
            [("items", "Text[]", True), ("enabled", "Boolean", False)],
        ),
        (
            "(_: Boolean, input: Text, items?: Part[])",
            "Boolean",
            [("input", "Text", True), ("items", "Part[]", False)],
        ),
    ],
)
def test_script_help_groups_signature_categories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    kind: str,
    prog_name: str,
    width: int,
    signature: str,
    input_type: str | None,
    arguments: list[tuple[str, str, bool]],
) -> None:
    body = "  Describe the result." if kind == "agic" else "  let note = observed"
    source = _write_source(
        tmp_path, f"## Documented runnable.\n{kind} demo{signature}:\n{body}\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("help must not run")
    )
    result = script.dispatch(
        [],
        [source.name, "demo", "--help"],
        prog_name=prog_name,
        stdin=_UnreadableStdin(),
    )
    captured = capsys.readouterr()
    output = strip_ansi(captured.out)
    assert result == 0 and not captured.err, captured.err
    usage = next(line.strip() for line in output.splitlines() if "Usage:" in line)
    expected = f"Usage: {prog_name} demo.too demo [OPTIONS]"
    if arguments:
        expected += " [NAME=VALUE...]"
    if input_type:
        expected += " [-- <INPUT> | -]"
    assert usage == expected
    assert "---" not in output
    assert all(cell_len(line) <= width for line in output.splitlines())
    assert "Documented runnable." in output
    panel = _help_panel(output, "Arguments")
    assert bool(panel) == bool(arguments or input_type)
    assert "Input:" not in output
    positions = []
    for name, type_name, required in arguments:
        label = f"{name}=<{name.upper()}>"
        row = f"{label} Named input ({type_name})"
        assert row in panel
        assert (f"* {row}" in panel) is required
        positions.append(panel.index(label))
    assert "Optional." not in panel
    assert "Arguments may appear" not in panel
    if input_type:
        label = "INPUT"
        assert f"{label} Primary input ({input_type});" in panel
        positions.append(panel.index(f"{label} Primary input ({input_type});"))
        assert f"* {label}" in panel
        assert "reads stdin with - or when input is omitted" in panel
    else:
        assert "stdin" not in output and "TEXT..." not in output
    assert positions == sorted(positions)
    if panel:
        assert output.index("demo - Documented runnable.") < output.index("Arguments:")
        assert output.index("Arguments:") < output.index("Options:")


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize(
    ("signature", "tokens", "expected"),
    [
        ("()", [], {}),
        ("(enabled?: Boolean)", [], {}),
        ("(count: Number)", ["count=2"], {"count": "2"}),
        ("(class: Text)", ["class=report"], {"class": "report"}),
    ],
)
def test_script_without_input_runs_with_satisfied_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    signature: str,
    tokens: list[str],
    expected: dict[str, str],
) -> None:
    body = "  Describe the result." if kind == "agic" else "  let note = observed"
    source = _write_source(tmp_path, f"{kind} demo{signature}:\n{body}\n")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )
    assert (
        script.dispatch(
            [], [str(source), "demo", *tokens], prog_name="too", stdin=StringIO()
        )
        == 0
    )
    assert captured["input"] == CallInput()
    assert captured["raw_named"] == expected


@pytest.mark.parametrize(
    ("header", "save"),
    [
        (["count=2", "--quiet", "enabled=true", "--out", "---"], "---"),
        (["--out=---", "enabled=true", "-q", "count=2"], "---"),
        (["--out", "--", "enabled=true", "-q", "count=2"], "--"),
        (["count=2", "--out", "-", "-q", "enabled=true"], "-"),
        (["count=2", "-q", "enabled=true", "-o", "result.txt"], "result.txt"),
        (["count=2", "-q", "enabled=true", "-o-"], "-"),
        (["count=2", "-q", "enabled=true", "-o", "---"], "---"),
    ],
)
def test_script_options_can_follow_assignments_before_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    header: list[str],
    save: str,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )
    assert (
        script.dispatch(
            [],
            [str(source), "demo", *header, "text", "--model", "literal"],
            prog_name="too",
            stdin=_UnreadableStdin(),
        )
        == 0
    )
    assert captured["raw_named"] == {"count": "2", "enabled": "true"}
    assert captured["quiet"] is True
    assert captured["save"] == save
    assert captured["model_body"] is None
    assert captured["input"] == {"_": "text --model literal"}


@pytest.mark.parametrize("explicit", [False, True])
def test_script_line_input_preserves_includes_and_quoted_newlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )
    assert (
        script.dispatch(
            [],
            [
                str(source),
                "demo",
                "count=2",
                *(["--"] if explicit else []),
                "Review",
                "this",
                "@notes.md",
                "first\nsecond",
                "closing",
                "words",
            ],
            prog_name="too",
            stdin=_UnreadableStdin(),
        )
        == 0
    )
    assert captured["input"] == {
        "_": "Review this\n@notes.md\nfirst\nsecond\nclosing words"
    }


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["count=---", "text"], "text"),
        (["count=2", "--", "---"], "---"),
        (["count=2", "text", "---"], "text ---"),
        (["count=2", "--", "--help"], "--help"),
        (["count=2", "--", "unknown=value"], "unknown=value"),
        (["count=2", "equation a=b"], "equation a=b"),
    ],
)
def test_script_markers_and_assignments_can_be_literal_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokens: list[str],
    expected: str,
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )
    assert (
        script.dispatch(
            [],
            [str(source), "demo", *tokens],
            prog_name="too",
            stdin=_UnreadableStdin(),
        )
        == 0
    )
    assert captured["input"] == {"_": expected}
    assert captured["raw_named"] == {"count": tokens[0].partition("=")[2]}


@pytest.mark.parametrize("tokens", [["-"], []])
def test_script_stream_input_keeps_fence_lines_until_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokens: list[str],
) -> None:
    source = _write_source(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script, "_run", lambda _source_path, **kwargs: captured.update(kwargs) or 0
    )
    body = "before\n---\nafter\n"
    assert (
        script.dispatch(
            [],
            [str(source), "demo", "count=2", *tokens],
            prog_name="too",
            stdin=StringIO(body),
        )
        == 0
    )
    assert captured["input"] == {"_": body}


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        (["unknown=value"], "unknown argument: unknown; use '--' to start input"),
        (["count=1", "count=2"], "argument count was provided more than once"),
        (["----"], "No such option: ----"),
        (["--save", "result.txt"], "No such option: --save"),
        (["--"], "line input requires nonempty text"),
        (["-", "--help"], "stdin marker '-' must be the only primary input"),
        (["-", "hello"], "stdin marker '-' must be the only primary input"),
        (["-", "count=2"], "stdin marker '-' must be the only primary input"),
    ],
)
def test_script_invalid_headers_fail_before_reading_or_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tokens: list[str],
    message: str,
) -> None:
    source = _write_source(tmp_path)
    monkeypatch.setattr(
        script,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("invalid input must not run"),
    )
    assert (
        script.dispatch(
            [],
            [str(source), "demo", *tokens],
            prog_name="too",
            stdin=_UnreadableStdin(),
        )
        == 2
    )
    error = " ".join(strip_ansi(capsys.readouterr().err).replace("│", " ").split())
    assert message in error


def test_script_omitted_terminal_input_shows_help_without_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    source = _write_source(tmp_path)
    stdin = _UnreadableStdin()
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        script,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("missing input must not run"),
    )
    assert (
        script.dispatch(
            [],
            [str(source), "demo", "count=2"],
            prog_name="too",
            stdin=stdin,
        )
        == 2
    )
    assert "Arguments:" in strip_ansi(capsys.readouterr().out)


def _flow_outline_lines(output: str) -> list[str]:
    block = strip_ansi(output).partition("The flow proceeds as follows:")[2]
    return [line.rstrip() for line in block.splitlines() if line.strip()]


@pytest.mark.parametrize("explicit_help", [False, True])
def test_script_flow_help_keeps_aligned_steps_in_the_epilog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, explicit_help: bool
) -> None:
    source = _write_source(
        tmp_path,
        """## Research a topic from several sources.
flow research(_: Text):
  let topic = {{_}}
  ## Broaden [bold]the question[/bold].
  ## Keep diverse perspectives.
  let queries = scatter 3 using expand
  map using search in 4 lanes
  keep first 1

agic expand:
  Expand the question.

agic search:
  Search for evidence.
""",
    )
    monkeypatch.setenv("COLUMNS", "160")
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("help must not run")
    )
    if explicit_help:
        monkeypatch.setattr(
            script,
            "_collect_call",
            lambda *_args, **_kwargs: pytest.fail("explicit help must not read input"),
        )

    result = script.dispatch(
        [],
        [str(source), "research", *(["--help"] if explicit_help else [])],
        prog_name="too",
        stdin=StringIO(),
    )
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == (0 if explicit_help else 2), output.err
    assert output.err == ""
    assert stdout.count("Research a topic from several sources.") == 1
    assert "research - Research a topic from several sources." in stdout
    assert (
        stdout.index("Research a topic")
        < stdout.index("Usage:")
        < stdout.index("Arguments:")
        < stdout.index("Options:")
        < stdout.index("The flow proceeds as follows:")
    )
    assert stdout.count("The flow proceeds as follows:") == 1
    outline_start = stdout.partition("The flow proceeds as follows:")[2].splitlines()
    assert not outline_start[1].strip()
    assert outline_start[2].strip() == "[0] Set value to topic"
    assert "─ Flow" not in stdout
    assert _flow_outline_lines(stdout) == [
        "[0] Set value to topic",
        "[1] Broaden [bold]the question[/bold]. Keep diverse perspectives.",
        "    Scatter into 3 items with expand, save result to queries",
        "[2] Map each item with search, up to 4 at once",
        "[3] Keep the first item",
    ]
    assert stdout.endswith("[3] Keep the first item\n")


def test_script_flow_outline_expands_repeat_bodies_but_not_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    source = _write_source(
        tmp_path,
        """flow pipeline(_: Text):
  ## Refine the result.
  repeat 3 times:
    ## Revise the draft.
    run review
    repeat:
      run: Evaluate the draft.
      until: Check the draft.
    until: Check completion.
  run pipeline
  repeat 2 times:
    run review

agic review:
  Review the draft.
""",
    )
    monkeypatch.setenv("COLUMNS", "160")

    assert (
        script.dispatch(
            [], [str(source), "pipeline", "--help"], prog_name="too", stdin=StringIO()
        )
        == 0
    )

    assert _flow_outline_lines(capsys.readouterr().out) == [
        "[0] Refine the result.",
        "    Repeat up to 3 times, until <agic:9> is true",
        "  [0] Revise the draft.",
        "      Run review",
        "  [1] Repeat until <agic:8> is true",
        "    [0] Run <agic:7>",
        "[1] Run pipeline",
        "[2] Repeat 2 times",
        "  [0] Run review",
    ]


@pytest.mark.parametrize("width", [44, 80])
@pytest.mark.parametrize("tty", [False, True])
def test_script_flow_outline_truncates_each_description_and_doc_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, width: int, tty: bool
) -> None:
    source = _write_source(
        tmp_path,
        f"""flow pipeline(_: Text):
  ## {"Evidence 証拠 " * 30}DOC_END
  sort descending by score_{"x" * 100}
  keep first 1

agic score_{"x" * 100} -> Number:
  Return a numeric score.
""",
    )
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FORCE_COLOR", "1") if tty else monkeypatch.delenv(
        "FORCE_COLOR", raising=False
    )

    assert (
        script.dispatch(
            [], [str(source), "pipeline", "--help"], prog_name="too", stdin=StringIO()
        )
        == 0
    )

    rows = _flow_outline_lines(capsys.readouterr().out)
    assert len(rows) == 3
    assert rows[0].startswith("[0] Evidence 証拠 ")
    assert rows[1].startswith("    Sort items by score_")
    assert rows[0].endswith("…") and rows[1].endswith("…")
    assert all(cell_len(row) <= width - 2 for row in rows), rows
    assert rows[2] == "[1] Keep the first item"


def test_script_empty_flow_outline_has_a_placeholder() -> None:
    flow = FlowDecl(name="empty", span=Span(line=1))

    assert script._flow_outline(flow).plain == "No statements."


def test_script_flow_outline_uses_normal_style_and_separates_sibling_steps() -> None:
    program = Program.from_source("""flow work():
  ## Refine the result.
  repeat 2 times:
    run review
    run review
  run review

agic review:
  Review the draft.
""")
    outline = script._flow_outline(program.flows[0])
    assert not outline.style and not outline.spans
    assert outline.plain == (
        "[0] Refine the result.\n"
        "    Repeat 2 times\n"
        "  [0] Run review\n\n"
        "  [1] Run review\n\n"
        "[1] Run review"
    )


@pytest.mark.parametrize("doc", [None, "", "  \n\t ", "Run review"])
def test_script_flow_outline_aligns_docs_at_multi_digit_ordinals(
    doc: str | None,
) -> None:
    flow = FlowDecl(
        name="work",
        span=Span(line=1),
        stmts=(RunStmt(span=Span(line=2), runnable="review"),) * 10
        + (RunStmt(span=Span(line=3), runnable="review", doc=doc),),
    )

    lines = [line for line in script._flow_outline(flow).plain.splitlines() if line]

    assert lines[10:] == (
        ["[10] Run review", "     Run review"]
        if doc and doc.strip()
        else ["[10] Run review"]
    )


def test_script_flow_invocation_does_not_print_the_help_outline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    source = _write_source(tmp_path, "flow work(_: Text):\n  let topic = {{_}}\n")
    calls: list[str] = []

    def fake_run(_source_path: Path, **kwargs) -> int:
        calls.append(kwargs["runnable_kind"])
        return 0

    monkeypatch.setattr(script, "_run", fake_run)

    assert (
        script.dispatch(
            [],
            [str(source), "work", "--", "question"],
            prog_name="too",
            stdin=StringIO(),
        )
        == 0
    )

    assert calls == ["flow"]
    assert "The flow proceeds as follows:" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("prog_name", "width", "filename"),
    [
        ("too", 80, "demo.too"),
        ("toolang", 80, "demo.too"),
        ("too", 120, "demo.too"),
        ("toolang", 120, "demo [v1].too"),
    ],
)
def test_script_top_level_help_lists_descriptions_before_options(
    tmp_path: Path,
    monkeypatch,
    capsys,
    prog_name: str,
    width: int,
    filename: str,
) -> None:
    source = _write_source(
        tmp_path,
        """
agic:
  Default behavior.

## Run the visible command.
agic visible:
  Visible behavior.

agic undocumented:
  Undocumented behavior.

flow undocumented_flow():
  let note = observed

## Run the pipeline.
flow pipeline:
  run:
    Inline behavior.
""",
    )
    source = source.rename(tmp_path / filename)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("help must not run")
    )

    result = script.dispatch(
        [],
        [source.name, "--help"],
        prog_name=prog_name,
        stdin=_UnreadableStdin(),
    )
    output = capsys.readouterr()
    stdout = strip_ansi(output.out)

    assert result == 0
    assert f"Usage: {prog_name} {filename} [OPTIONS] <RUNNABLE>" in stdout
    assert "[NAME=VALUE...]" not in stdout
    assert f"Run runnables from {filename}" in stdout
    assert stdout.index("Runnables:") < stdout.index("Options:")
    _assert_common_options(stdout)
    assert all(cell_len(line) <= width for line in stdout.splitlines())
    assert "Commands" not in stdout
    assert "visible" in stdout
    assert "Run the visible command." in stdout
    assert "pipeline" in stdout
    assert "Run the pipeline." in stdout
    descriptions = _help_panel(stdout, "Runnables")
    assert "agic:visible Run the visible command." in descriptions
    assert "flow:pipeline Run the pipeline." in descriptions
    assert "agic:undocumented Agic undocumented" in descriptions
    assert "flow:undocumented_flow Flow undocumented_flow" in descriptions
    assert "visible -" not in descriptions
    assert "Use RUNNABLE --help" not in stdout
    assert "default" not in descriptions
    assert "<agic:" not in stdout
    assert "The flow proceeds as follows:" not in stdout


@pytest.mark.parametrize("width", [44, 80])
def test_script_long_runnable_names_keep_descriptions_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    width: int,
) -> None:
    name = "research_" + "detailed_" * 8 + "topic"
    source = _write_source(
        tmp_path,
        f"## Documented.\nagic {name}():\n  X\nagic brief():\n  X\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", str(width))

    assert (
        script.dispatch(
            [], [source.name, "--help"], prog_name="too", stdin=_UnreadableStdin()
        )
        == 0
    )
    output = strip_ansi(capsys.readouterr().out)
    panel = _help_panel(output, "Runnables")
    assert "Documented." in panel
    assert "Agic brief" in panel
    assert "…" not in panel
    assert all(cell_len(line) <= width for line in output.splitlines())


def test_script_formats_an_unknown_runnable_with_a_help_hint(
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
    assert lines[0].startswith("Error: No such command")
    assert lines[1] == ""
    assert lines[2] == f"Try 'toolang {source.name} --help' for help."
    assert "Usage:" not in stderr
    assert "╭" not in stderr
    assert lines[-1].strip()
    assert "No such command 'missing'." in stderr
    assert "\nError: No such command" not in stderr


@pytest.mark.parametrize("root_options", [[], ["--quiet"]])
@pytest.mark.parametrize(
    ("kind", "query"),
    [
        ("agic", "agic:demo"),
        ("agic", "runnable:demo"),
        ("flow", "flow:demo"),
        ("flow", "runnable:demo"),
    ],
)
def test_script_accepts_explicit_runnable_queries(
    tmp_path: Path,
    monkeypatch,
    query: str,
    kind: str,
    root_options: list[str],
) -> None:
    source = _write_source(
        tmp_path,
        _SOURCE
        if kind == "agic"
        else "flow demo(_: Part[], count: Number):\n  let note = {{_}}\n",
    )
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
        [str(source), *root_options, query, "count=2", "--", "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )

    assert result == 0
    assert captured["runnable"] == "demo"
    assert captured["runnable_kind"] == kind
    assert captured["quiet"] is bool(root_options)


@pytest.mark.parametrize("root_options", [[], ["--quiet"]])
def test_script_rejects_an_explicit_runnable_kind_mismatch(
    tmp_path: Path,
    capsys,
    root_options: list[str],
) -> None:
    source = _write_source(tmp_path)

    result = script.dispatch(
        [],
        [str(source), *root_options, "flow:demo", "count=2", "hello"],
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
        [str(source), "demo", "count=2", "--sandbox=host", "--", "hello"],
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
            thread=ThreadRef("script_remote"),
            control=ControlRef.for_run("run_remote", 0),
            state=ControlRef.for_run("run_remote", 0),
            output=None,
            status="succeeded",
            error=None,
        )

    monkeypatch.setattr(
        agents,
        "materialize_roaming_program",
        lambda _source: layout,
    )
    monkeypatch.setattr(
        "toolang.cli.common.agent_server.acquire_agent_server", agent_server_context
    )
    monkeypatch.setattr(script, "_execute_remote", execute_remote)
    monkeypatch.setattr(
        "toolang.state.prepare.prepare_agent_state",
        lambda *_args, **_kwargs: pytest.fail("remote execution prepared local state"),
    )

    result = script._run(
        source,
        runnable="demo",
        runnable_kind="agic",
        override=RunOverride(),
        input=CallInput({"_": "hello"}),
        raw_named=CallInput({"count": "2"}),
        allow_options=(),
        model_body=None,
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


def test_embedded_script_prepare_failure_uses_the_operational_failure_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _write_source(tmp_path)
    layout = AgentLayout.roaming(source)

    @contextmanager
    def embedded_server(_layout: AgentLayout, **_kwargs):
        yield None

    def fail_prepare(_layout: AgentLayout, *, progress) -> None:
        progress(
            ProgressEvent(
                id="agent:demo:home",
                kind="prepare",
                stage="materialize",
                label="Failed to prepare caps",
                status="failed",
                detail="invalid cap definition",
            )
        )
        raise ValueError("prepare failed")

    monkeypatch.setattr(
        agents,
        "materialize_roaming_program",
        lambda _source: layout,
    )
    monkeypatch.setattr(
        "toolang.cli.common.agent_server.acquire_agent_server", embedded_server
    )
    monkeypatch.setattr("toolang.state.prepare.prepare_agent_state", fail_prepare)

    result = script._run(
        source,
        runnable="demo",
        runnable_kind="agic",
        override=RunOverride(),
        input=CallInput({"_": "hello"}),
        raw_named=CallInput({"count": "2"}),
        allow_options=(),
        model_body=None,
        limit_options=(),
        sandbox="host",
        dev=None,
        save=None,
        quiet=False,
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "Failed to prepare caps" in error
    assert "Stage: prepare.materialize" in error
    assert "Reason: invalid cap definition" in error


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

    monkeypatch.setattr("toolang.execution.remote.RemoteRunClient", Client)
    monkeypatch.setattr(
        "toolang.cli.common.remote_runtime.inspect_remote_runtime", inspect
    )
    monkeypatch.setattr(script, "_create_remote_script_thread", create_thread)
    monkeypatch.setattr(
        script,
        "_remote_script_models",
        lambda *_args, **_kwargs: pytest.fail(
            "concrete remote script model must not list models"
        ),
    )
    monkeypatch.setattr(
        script,
        "_remote_script_defaults",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result=SessionSetting(
                model=ModelRequest("test/scripted"),
                runnable="agic:demo",
            ),
        ),
    )
    monkeypatch.setattr(script, "load_runtime_environ", lambda *_args, **_kwargs: {})

    async def scenario() -> None:
        task = asyncio.create_task(
            script._execute_remote(
                layout=layout,
                endpoint=Client.endpoint,
                sandbox="docker:python:3.13-slim",
                runnable="agic:demo",
                override=RunOverride(),
                input=CallInput({"_": "hello"}),
                raw_named=CallInput({"count": "2"}),
                session_override=RunOverride(),
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
    override = script._remote_script_override(
        RunOverride(runnable="default"),
        runnable="demo",
    )

    assert override == RunOverride(runnable="demo")


def test_script_materializes_input_local_runnable_refs() -> None:
    override = script._materialize_script_runnable_override(
        RunOverride(runnable="demo"),
        program=script.Program.from_source(_SOURCE),
    )

    assert override == RunOverride(runnable="agic:demo")


def test_script_materializes_input_local_runnable_queries() -> None:
    override = script._materialize_script_runnable_override(
        RunOverride(runnable="*[kind=agic;name=demo]"),
        program=script.Program.from_source(_SOURCE),
    )

    assert override == RunOverride(runnable="agic:demo")


def test_remote_script_rejects_mixed_named_input_sources() -> None:
    with pytest.raises(
        ValueError,
        match="named inputs cannot be supplied by both source and surface",
    ):
        script._remote_script_input(
            CallInput({"count": "1"}),
            raw_named=CallInput({"enabled": "true"}),
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


def test_script_rejects_removed_default_option(
    tmp_path: Path,
    capsys,
) -> None:
    source = _write_source(tmp_path)

    result = script.dispatch(
        [],
        [
            str(source),
            "demo",
            "count=2",
            "--default",
            "model=test/model effort=high",
            "hello",
        ],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()

    assert result == 2
    assert "No such option: --default" in strip_ansi(output.err)


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
            thread=ThreadRef("script_thread"),
            control=ControlRef.for_run(f"run_{status}", 0),
            state=ControlRef.for_run(f"run_{status}", 0),
            output=None,
            status=status,
            error=ErrorMessage("output is not valid Number"),
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
            thread=ThreadRef("script_thread"),
            control=ControlRef.for_run("run_failed", 0),
            state=ControlRef.for_run("run_failed", 0),
            output=None,
            status="failed",
            error=ErrorMessage("provider unavailable"),
        ),
        store_path=tmp_path / "runs.db",
        log_path=None,
        error_reported=False,
    )

    assert result == 1
    assert "provider unavailable" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("root_options", "child_options", "expected"),
    [
        ([], [], None),
        ([], ["--dev"], Path(".")),
        ([], ["--dev", "--quiet"], Path(".")),
        ([], ["--dev", "wheel directory"], Path("wheel directory")),
        ([], ["--dev=wheels"], Path("wheels")),
        ([], ["--dev=--wheel"], Path("--wheel")),
        ([], ["--dev="], Path(".")),
        ([], ["--dev", ""], Path(".")),
        ([], ["--dev", "first", "--dev"], Path(".")),
        ([], ["--dev", "--dev=last"], Path("last")),
        (["--dev", "--quiet"], [], Path(".")),
        (["--dev", "--"], [], Path(".")),
        (["--dev=."], [], Path(".")),
        (["--dev", "wheel directory"], [], Path("wheel directory")),
        (["--dev="], [], Path(".")),
        (["--dev", ""], [], Path(".")),
        (["--dev", "first", "--dev", "--quiet"], [], Path(".")),
        (["--dev=parent"], ["--dev"], Path(".")),
        (["--dev=parent"], ["--dev=child"], Path("child")),
        (["--dev", "--quiet"], ["--dev=child"], Path("child")),
    ],
)
def test_script_dev_states_preserve_inheritance_and_input(
    root_options, child_options, expected, tmp_path, monkeypatch
):
    from toolang.cli.toolang.main import main

    source = _write_source(tmp_path)
    captured = {}
    monkeypatch.setattr(
        script, "_run", lambda _source, **kwargs: captured.update(kwargs) or 0
    )
    assert (
        main(
            [
                str(source),
                *root_options,
                "demo",
                "count=2",
                *child_options,
                "--",
                "hello",
                "--dev",
                "literal",
            ]
        )
        == 0
    )
    assert captured["dev"] == expected
    assert captured["input"] == {"_": "hello --dev literal"}
    assert captured["raw_named"] == {"count": "2"}


@pytest.mark.parametrize("child", [False, True])
def test_script_bare_dev_help_never_discovers_wheels_or_runs(
    child, tmp_path, monkeypatch, capsys
):
    from toolang.cli.toolang.main import main

    source = _write_source(tmp_path)
    monkeypatch.setattr(
        script, "_run", lambda *_args, **_kwargs: pytest.fail("ran script")
    )
    assert main([str(source), *(["demo"] if child else []), "--dev", "--help"]) == 0
    _assert_common_options(strip_ansi(capsys.readouterr().out))
