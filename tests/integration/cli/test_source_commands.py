"""Source commands operate offline without agent preparation."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from typer.testing import CliRunner
from typer._click.utils import strip_ansi

from toolang.cli.toolang.main import app
from toolang.lang.ast import Program, to_data
from toolang.lang.format import format_source

runner = CliRunner()
SOURCE = "## @param _ Input.\nagic echo( _ ):\n    user: {{_}}\n"


@pytest.fixture(autouse=True)
def plain_environment(monkeypatch):
    for key in ("NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("tree", ["--ast", "--cst"])
@pytest.mark.parametrize(
    "representation", [[], ["--json"], ["--compact"], ["--json", "--compact"]]
)
def test_tree_and_representation_are_independent(tree, representation):
    result = runner.invoke(app, ["parse", "-", tree, *representation], input=SOURCE)
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if representation:
        payload = json.loads(result.stdout)
        if tree == "--ast":
            assert payload == to_data(Program.from_source(SOURCE))
        else:
            assert payload["source"] == SOURCE
            assert payload["root"]["type"] == "source_file"
        if "--compact" in representation:
            assert result.stdout.count("\n") == 1
    else:
        assert result.stdout.startswith(
            "(program" if tree == "--ast" else "(source_file"
        )


def test_default_ast_and_invalid_tree_behavior():
    default = runner.invoke(app, ["parse", "-"], input=SOURCE)
    assert default.stdout.startswith("(program\n")
    bad = "flow broken:\n  run\n"
    ast = runner.invoke(app, ["parse", "-"], input=bad)
    assert ast.exit_code == 1 and not ast.stdout
    cst = runner.invoke(
        app,
        ["parse", "-", "--cst", "--json", "--stdin-filepath", "work.too"],
        input=bad,
    )
    assert cst.exit_code == 1
    assert json.loads(cst.stdout)["diagnostics"][0]["kind"] == "invalid"
    assert "work.too:2:" in cst.stderr
    assert runner.invoke(app, ["highlight", "-"], input=bad).exit_code == 0


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--stdout"],
        ["--highlight"],
        ["--stdout", "--highlight"],
        ["--highlight", "--color", "always"],
        ["--highlight", "--html"],
    ],
)
def test_format_output_modes_and_shared_highlighting(tmp_path, options):
    source = tmp_path / "with spaces.too"
    source.write_text(SOURCE)
    result = runner.invoke(app, ["fmt", str(source), *options])
    assert result.exit_code == 0, result.output
    formatted = format_source(SOURCE)
    if not options:
        assert source.read_text() == formatted
        assert "formatted " in result.stdout
    else:
        assert source.read_text() == SOURCE
        if "--html" not in options:
            assert strip_ansi(result.stdout) == formatted
        if "--highlight" in options:
            highlight_options = options[options.index("--highlight") + 1 :]
            direct = runner.invoke(
                app, ["highlight", "-", *highlight_options], input=formatted
            )
            assert direct.stdout == result.stdout


def test_check_keeps_files_and_returns_change_status(tmp_path):
    source = tmp_path / "work.too"
    source.write_text(SOURCE)
    check = runner.invoke(app, ["fmt", str(source), str(tmp_path), "--check"])
    assert check.exit_code == 1
    assert check.stdout.count("would reformat") == 1
    assert source.read_text() == SOURCE
    assert runner.invoke(app, ["fmt", str(tmp_path)]).exit_code == 0
    assert runner.invoke(app, ["fmt", str(source), "--check"]).exit_code == 0


@pytest.mark.parametrize(
    "options",
    [
        ["--check", "--stdout"],
        ["--check", "--highlight"],
        ["--color", "never"],
        ["--html"],
        ["--highlight", "--color", "invalid"],
    ],
)
def test_invalid_output_options_never_modify_files(tmp_path, options):
    source = tmp_path / "work.too"
    source.write_text(SOURCE)
    result = runner.invoke(app, ["fmt", str(source), *options])
    assert result.exit_code == 2
    assert not result.stdout
    assert source.read_text() == SOURCE


def test_input_validation_and_stdin_compatibility(tmp_path):
    assert (
        runner.invoke(app, ["parse", "-", "--ast", "--cst"], input=SOURCE).exit_code
        == 2
    )
    assert runner.invoke(
        app, ["fmt", "--stdin-filepath", "label.too"], input=SOURCE
    ).stdout == format_source(SOURCE)
    for command in ("parse", "highlight"):
        assert runner.invoke(app, [command, str(tmp_path)]).exit_code == 2
        assert (
            runner.invoke(app, [command, str(tmp_path / "missing.too")]).exit_code == 1
        )
        assert runner.invoke(app, [command, str(tmp_path / "wrong.txt")]).exit_code == 1
    assert runner.invoke(app, ["fmt", str(tmp_path), "--stdout"]).exit_code == 2
    source = tmp_path / "bad.too"
    source.write_bytes(b"\xff")
    for command in ("parse", "fmt", "highlight"):
        result = runner.invoke(app, [command, str(source)])
        assert result.exit_code == 1 and not result.stdout
    source.write_text("flow broken:\n  run\n")
    result = runner.invoke(app, ["fmt", str(source), "--highlight", "--html"])
    assert result.exit_code == 1 and not result.stdout


@pytest.mark.parametrize("executable", ["too", "toolang"])
def test_real_entry_points_keep_raw_bytes_without_setup(tmp_path, executable):
    command = Path(sys.executable).parent / executable
    source = b"# docs\r\nagic echo:\r\n\tuser: [red] <>&  "
    env = {**os.environ, "NO_COLOR": "1"}
    for args in (["highlight", "-"], ["highlight", "-", "--color", "always"]):
        result = subprocess.run(
            [str(command), "--root", str(tmp_path / "toolang-root"), *args],
            input=source,
            capture_output=True,
            cwd=tmp_path,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert strip_ansi(result.stdout.decode()).encode() == source
    result = subprocess.run(
        [
            str(command),
            "--root",
            str(tmp_path / "toolang-root"),
            "parse",
            "-",
            "--cst",
            "--json",
        ],
        input=source,
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    assert json.loads(result.stdout)["source"].encode() == source
    assert list(tmp_path.iterdir()) == []


def test_dash_paths_and_multiple_stdout_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "-example.too"
    source.write_text(SOURCE)
    for command in ("parse", "highlight"):
        result = runner.invoke(app, [command, "--", source.name])
        assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["fmt", "--stdout", "--", source.name])
    assert result.stdout == format_source(SOURCE)
    result = runner.invoke(app, ["fmt", str(source), str(source), "--highlight"])
    assert result.exit_code == 2
    assert source.read_text() == SOURCE
    for options in (["--check"], ["--check", "--highlight"]):
        assert runner.invoke(app, ["fmt", "-", *options], input=SOURCE).exit_code != 0


def test_semantic_validation_stays_out_of_formatting_and_highlighting(tmp_path):
    source = "## @param missing Details.\nagic echo(_):\n  {{_}}\n"
    path = tmp_path / "work.too"
    path.write_text(format_source(source))
    assert runner.invoke(app, ["fmt", str(path), "--check"]).exit_code == 0
    for command in (["highlight", "-"], ["parse", "-", "--cst"]):
        assert runner.invoke(app, command, input=source).exit_code == 0
    result = runner.invoke(app, ["parse", "-", "--json"], input=source)
    assert result.exit_code == 1 and not result.stdout


def test_query_failure_does_not_emit_partial_code_or_html(monkeypatch):
    from toolang.lang import highlight

    def fail(source):
        raise ValueError("query could not compile")

    monkeypatch.setattr(highlight, "captures", fail)
    for command in (
        ["highlight", "-", "--color", "always"],
        ["fmt", "-", "--highlight", "--html"],
    ):
        result = runner.invoke(app, command, input=SOURCE)
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "query could not compile" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["-"],
        ["--stdin-filepath", "work.too"],
        ["-", "--stdout"],
        ["-", "--highlight", "--color", "never"],
    ],
)
def test_formatter_stdin_uses_utf8_independently_of_python_io_encoding(
    tmp_path, arguments
):
    source = "## 中文文档\nagic echo:\n    中文 🧭\n".encode()
    command = Path(sys.executable).parent / "too"
    result = subprocess.run(
        [str(command), "--root", str(tmp_path / "root"), "fmt", *arguments],
        input=source,
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "latin-1"},
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == format_source(source.decode()).encode()
    assert result.stderr == b""


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_ast_and_formatter_modes_preserve_universal_newline_compatibility(
    tmp_path, newline
):
    canonical = "agic echo:\n  Hello.\n"
    original = canonical.replace("\n", newline).encode()
    path = tmp_path / "work.too"
    path.write_bytes(original)
    parsed = runner.invoke(app, ["parse", str(path), "--json"])
    assert parsed.exit_code == 0, parsed.output
    assert json.loads(parsed.stdout) == to_data(Program.from_source(canonical))
    assert runner.invoke(app, ["fmt", str(path), "--check"]).exit_code == 0
    for arguments in (["--stdout"], ["--highlight", "--color", "never"]):
        formatted = runner.invoke(app, ["fmt", str(path), *arguments])
        assert formatted.exit_code == 0, formatted.output
        assert formatted.stdout == canonical
    concrete = runner.invoke(app, ["parse", str(path), "--cst", "--json"])
    assert json.loads(concrete.stdout)["source"].encode() == original
    highlighted = runner.invoke(app, ["highlight", str(path), "--color", "never"])
    assert highlighted.stdout_bytes == original
    assert path.read_bytes() == original
