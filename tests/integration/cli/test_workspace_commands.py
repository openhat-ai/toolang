"""Workspace grant command scenarios."""

from pathlib import Path

import click
import pytest

import toolang.cli.toolang.main as cli
from toolang.cli.toolang.commands import workspace as workspace_commands


def _run(root: Path, *arguments: str) -> int:
    return cli.main(["--root", str(root), *arguments])


def test_workspace_commands_add_list_and_remove(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "toolang"
    project = tmp_path / "project"
    project.mkdir()
    assert _run(root, "new", "alice") == 0
    capsys.readouterr()
    memo = root / "agents" / "alice" / "collab" / "MEMO.md"
    memo.write_text("keep these notes\n", encoding="utf-8")

    assert (
        _run(
            root,
            "alice",
            "workspace",
            "add",
            str(project),
            "--name",
            "code",
        )
        == 0
    )
    added = click.unstyle(capsys.readouterr().out)
    assert f"Added workspace code: {project}" in added
    assert memo.read_text(encoding="utf-8") == "keep these notes\n"

    assert _run(root, "alice", "workspace", "list") == 0
    listed = click.unstyle(capsys.readouterr().out)
    assert "NAME" in listed
    assert "code" in listed
    assert str(project) in listed
    assert "active" in listed

    assert _run(root, "alice", "workspace", "remove", "code") == 0
    removed = click.unstyle(capsys.readouterr().out)
    assert f"Removed workspace code: {project}" in removed
    assert memo.read_text(encoding="utf-8") == "keep these notes\n"

    assert _run(root, "alice", "workspace", "list") == 0
    assert "No workspaces configured." in capsys.readouterr().out


def test_workspace_commands_require_resident_agent_and_valid_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "toolang"
    assert _run(root, "new", "alice") == 0
    capsys.readouterr()

    assert _run(root, "alice", "workspace", "add", str(tmp_path / "missing")) == 1
    assert "workspace directory not found" in click.unstyle(capsys.readouterr().err)

    assert _run(root, "missing", "workspace", "list") == 1
    assert "Agent missing not found" in click.unstyle(capsys.readouterr().err)


def test_workspace_commands_report_running_docker_restart(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    project = tmp_path / "project"
    project.mkdir()
    assert _run(root, "new", "alice") == 0
    capsys.readouterr()
    monkeypatch.setattr(
        workspace_commands,
        "_active_docker_workspaces",
        lambda _layout: {},
    )

    assert _run(root, "alice", "workspace", "add", str(project)) == 0
    output = click.unstyle(capsys.readouterr().out)

    assert "Restart required for the running Docker agent." in output
    assert _run(root, "alice", "workspace", "list") == 0
    assert "restart required" in click.unstyle(capsys.readouterr().out)
