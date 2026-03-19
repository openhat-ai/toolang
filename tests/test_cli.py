from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from toolang.cli import app

runner = CliRunner()


def test_cli_has_expected_subcommands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "run" in result.output
    assert "check" in result.output
    assert "dump-ast" in result.output


def test_cli_check_resolves_resident_agent_from_toolang_root(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "sample.too"
    (home / "alice.too").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    result = runner.invoke(app, ["check", "alice"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
