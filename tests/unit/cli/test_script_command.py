from __future__ import annotations

from io import StringIO
from pathlib import Path

from typer.testing import CliRunner

from toolang.base.types.message import ImagePart, TextPart
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import script
from toolang.execution.executor import CeilingSpec


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
    assert captured["args"] == {"count": 2.5, "enabled": True}
    assert captured["input_value"] == (TextPart("hello world"),)


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
    assert captured["input_value"] == (TextPart("from stdin"),)


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
    assert captured["input_value"] == (TextPart("from stdin"),)


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
    assert captured["args"] == {}
    assert captured["input_value"] == (TextPart("count=2"),)


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
    parts = captured["input_value"]
    assert isinstance(parts, tuple)
    assert len(parts) == 1
    part = parts[0]
    assert isinstance(part, ImagePart)
    assert part.image_url == "data:image/png;base64,iVBORw0K"
    assert part.filename == "sample.png"


def test_script_rejects_missing_required_parameter(
    tmp_path: Path,
    capsys,
) -> None:
    source = _write_source(tmp_path)

    result = script.dispatch(
        [],
        [str(source), "demo", "hello"],
        prog_name="toolang",
        stdin=StringIO(),
    )
    output = capsys.readouterr()

    assert result == 2
    assert "missing required arguments: count=..." in output.err


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

    assert result == 0
    assert (
        "Usage: toolang demo.too demo [OPTIONS] "
        "count=Number [enabled=Boolean] INPUT..."
        in output.out
    )
    assert "Run the documented demo." in output.out
    assert "Arguments" in output.out
    assert "count=Number" in output.out
    assert "enabled=Boolean" in output.out
    assert "Primary Part[] input." in output.out


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

    assert result == 0
    assert "visible" in output.out
    assert "Run the visible command." in output.out
    assert "pipeline" in output.out
    assert "Run the pipeline." in output.out
    assert "default" not in output.out
    assert "<agic:" not in output.out


def test_hidden_script_command_exposes_generic_usage() -> None:
    result = CliRunner().invoke(cli.app, ["script"])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "SCRIPT [RUNNABLE [ARGUMENTS]...]" in result.stdout
    assert "RUNNABLE [ARGUMENTS]..." in result.stdout


def test_hidden_script_command_rejects_a_script_path(tmp_path: Path) -> None:
    source = _write_source(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["script", str(source), "demo", "--help"],
    )

    assert result.exit_code == 2
    assert "Got unexpected extra arguments" in result.stderr
    assert source.name in result.stderr


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
    assert "No such option: --sandbox" in output.err


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
