from __future__ import annotations

from pathlib import Path
import shlex
import sys
from typing import Literal

import pytest
from typer._click.utils import strip_ansi

from toolang.catalog import templates
from toolang.catalog.agent import LocalAgents
from toolang.catalog.cap import AuthoredCaps, CapFile
from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.cli.caps.main import main as caps_main
from toolang.cli.toolang.main import main as toolang_main
from toolang.common.layout import home_edit_lock_path, root_edit_lock_path


@pytest.mark.parametrize(
    ("entry_point", "kind"),
    [
        ("caps", "skill"),
        ("toolang", "skill"),
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
    kind: Literal["skill", "chore"],
    action: str,
    editor_failure: str,
) -> None:
    root = tmp_path / "toolang"
    if kind == "skill":
        arguments = [kind, action, "demo"]
        if action == "edit":
            AuthoredCaps(root, lock_path=root_edit_lock_path(root)).create(
                CapFile.parse(
                    templates.load_template("skill").raw_text,
                    kind="skill",
                    name="demo",
                )
            )
    else:
        home = LocalAgents(root / "agents").create(
            "alice",
            content=templates.load_template("agent").raw_text,
        )
        arguments = ["alice", kind, action]
        if action == "edit":
            AuthoredChores(home, lock_path=home_edit_lock_path(home)).create(
                ChoreFile.parse(
                    "---\nid: demo\ntitle: Demo\n---\nDo the work.\n", name="demo"
                )
            )
            arguments.append("demo")
    authored_files = {
        path: path.read_bytes()
        for path in root.rglob("*.md")
        if ".state" not in path.relative_to(root).parts
    }
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
    error = " ".join(strip_ansi(output.err).replace("│", "").split())

    assert result == 1
    assert "Editing failed" in error
    assert "Traceback" not in output.err
    assert {
        path: path.read_bytes()
        for path in root.rglob("*.md")
        if ".state" not in path.relative_to(root).parts
    } == authored_files


@pytest.mark.parametrize("main", [toolang_main, caps_main])
@pytest.mark.parametrize("kind", ["psyche", "skill", "service", "prompt"])
def test_new_cap_edits_and_saves_the_default_template(
    main, kind, tmp_path, monkeypatch, capsys
):
    edited = templates.load_template(kind).raw_text + "\nCreated here.\n"
    seeds = []

    def edit(text):
        seeds.append(text)
        return edited

    monkeypatch.setattr("toolang.cli.caps.commands.edit_markdown", edit)
    assert main(["--root", str(tmp_path), kind, "new", "demo"]) == 0
    assert seeds == [templates.load_template(kind).raw_text]
    saved = AuthoredCaps(tmp_path, lock_path=root_edit_lock_path(tmp_path)).get(
        kind, "demo"
    )
    assert saved is not None and saved.path is not None
    assert saved.path.read_text() == edited
    assert "created:" in capsys.readouterr().out


def test_new_agent_uses_the_default_template(tmp_path, capsys):
    assert toolang_main(["--root", str(tmp_path), "new", "demo"]) == 0
    assert (
        tmp_path / "agents" / "demo" / "agent.too"
    ).read_text() == templates.load_template("agent").raw_text
    assert "Agent demo created:" in capsys.readouterr().out
