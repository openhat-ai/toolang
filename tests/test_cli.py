from __future__ import annotations

from typer.testing import CliRunner

from toolang.cli import app

runner = CliRunner()


def test_cli_has_expected_subcommands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "run" in result.output
    assert "check" not in result.output
    assert "dump-ast" not in result.output
