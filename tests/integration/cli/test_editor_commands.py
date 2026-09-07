from __future__ import annotations

from pathlib import Path
import shlex
import sys
from typing import Literal

from click import unstyle
import pytest

from toolang.catalog import templates
from toolang.catalog.agent import LocalAgents
from toolang.catalog.cap import AuthoredCaps, CapFile
from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.cli.caps.main import main as caps_main
from toolang.cli.toolang.main import main as toolang_main


@pytest.mark.parametrize(
    ("entry_point", "kind"),
    [
        ("caps", "skill"),
        ("toolang", "skill"),
        ("toolang", "task"),
        ("toolang", "chore"),
    ],
)
@pytest.mark.parametrize("action", ["new", "edit"])
@pytest.mark.parametrize("editor_failure", ["missing", "nonzero"])
def test_editor_failures_return_cli_errors_without_changing_authored_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry_point: str,
    kind: Literal["skill", "task", "chore"],
    action: str,
    editor_failure: str,
) -> None:
    root = tmp_path / "toolang"
    if kind == "skill":
        arguments = [kind, action, "demo"]
        if action == "edit":
            AuthoredCaps(root).create(
                CapFile.parse(
                    templates.render_template("skill", name="demo"),
                    kind="skill",
                    name="demo",
                )
            )
    else:
        home = LocalAgents(root / "agents").create(
            "alice",
            content=templates.render_template(
                "agent", name="alice", agent_name="alice"
            ),
        )
        arguments = ["alice", kind, action]
        if action == "edit":
            AuthoredJobs(home).create(
                JobFile.parse(
                    "---\nid: demo\ntitle: Demo\n---\nDo the work.\n", kind=kind
                )
            )
            arguments.append("demo")
    authored_files = {path: path.read_bytes() for path in root.rglob("*.md")}
    editor = (
        shlex.quote(str(tmp_path / "missing-editor"))
        if editor_failure == "missing"
        else shlex.join([sys.executable, "-c", "raise SystemExit(1)"])
    )
    monkeypatch.setenv("VISUAL", editor)
    monkeypatch.setenv("EDITOR", editor)

    main = caps_main if entry_point == "caps" else toolang_main
    result = main(["--root", str(root), *arguments])
    output = capsys.readouterr()
    error = " ".join(unstyle(output.err).replace("│", "").split())

    assert result == 1
    assert "Editing failed" in error
    assert "Traceback" not in output.err
    assert {path: path.read_bytes() for path in root.rglob("*.md")} == authored_files
