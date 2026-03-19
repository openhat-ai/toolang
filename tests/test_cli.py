from __future__ import annotations

from importlib.metadata import version as package_version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolang.agent_refs import resolve_agent_ref
from toolang.cli import _remember_agent, _resolve_cli_agent, app
from toolang.errors import ToolangError
from toolang.layout import agents_db_path

runner = CliRunner()
SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_cli_has_expected_subcommands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "run" in result.output
    assert "sync" in result.output
    assert "serve" in result.output
    assert "start" in result.output
    assert "check" not in result.output
    assert "dump-ast" not in result.output


def test_cli_shows_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"toolang {package_version('toolang')}"


def test_cli_sync_resolves_resident_agent_from_toolang_root(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "source_only.too"
    (home / "alice.too").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    result = runner.invoke(app, ["sync", "alice"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "synced"


def test_cli_resolves_known_agent_by_name_from_registry(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "reviewer.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("team/reviewer", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    _remember_agent(agent, db_path=db_path)

    resolved = _resolve_cli_agent("reviewer", db_path=db_path)

    assert resolved.agent_uri == agent.agent_uri


def test_cli_resolves_known_agent_by_id_prefix(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "worker.too").write_text(
        SOURCE_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    agent = resolve_agent_ref("alice/worker", cwd=tmp_path, toolang_root=root)
    db_path = agents_db_path(root)
    _remember_agent(agent, db_path=db_path)

    resolved = _resolve_cli_agent(agent.agent_id[:8], db_path=db_path)

    assert resolved.agent_uri == agent.agent_uri


def test_cli_rejects_ambiguous_known_agent_name(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "toolang-root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    db_path = agents_db_path(root)
    for home_name in ("alice", "team"):
        home = root / "agents" / home_name
        home.mkdir(parents=True)
        (home / "reviewer.too").write_text(
            SOURCE_FIXTURE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        agent = resolve_agent_ref(f"{home_name}/reviewer", cwd=tmp_path, toolang_root=root)
        _remember_agent(agent, db_path=db_path)

    with pytest.raises(ToolangError, match="Ambiguous agent name"):
        _resolve_cli_agent("reviewer", db_path=db_path)
