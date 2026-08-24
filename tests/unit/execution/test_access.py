"""Tests for immutable runspace access capture."""

from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.execution.access import MAX_MEMO_CHARS, capture_run_access
from toolang.setup import AgentSetup


def _setup(tmp_path: Path) -> AgentSetup:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    workspace = tmp_path / "project"
    workspace.mkdir()
    return AgentSetup(
        layout=layout,
        providers={},
        adapters={},
        models=(),
        tools={},
        envs={},
        workspaces={"project": workspace},
    )


def test_capture_run_access_selects_notes_and_collab_workspaces(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    setup.layout.collab.mkdir()
    setup.layout.collab_memo.write_text("collaboration notes\n", encoding="utf-8")

    access = capture_run_access(setup, "collab")

    assert access.space == "collab"
    assert access.working_directory == setup.layout.collab.resolve()
    assert access.memo == "collaboration notes\n"
    assert access.memo_truncated is False
    assert [(item.name, item.path) for item in access.workspaces] == [
        ("project", setup.workspaces["project"])
    ]
    assert access.writable_directories == (
        setup.layout.collab.resolve(),
        setup.workspaces["project"],
    )


def test_capture_lab_access_excludes_workspaces_and_bounds_notes(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    setup.layout.lab.mkdir()
    setup.layout.lab_memo.write_text("x" * (MAX_MEMO_CHARS + 1), encoding="utf-8")

    access = capture_run_access(setup, "lab")

    assert access.space == "lab"
    assert access.workspaces == ()
    assert access.memo_truncated is True
    assert len(access.memo) == MAX_MEMO_CHARS
    assert access.memo.endswith("[Runspace notes truncated by Toolang.]")


def test_capture_run_access_rejects_invalid_utf8_notes(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    setup.layout.collab.mkdir()
    setup.layout.collab_memo.write_bytes(b"\xff")

    with pytest.raises(UnicodeDecodeError):
        capture_run_access(setup, "collab")
