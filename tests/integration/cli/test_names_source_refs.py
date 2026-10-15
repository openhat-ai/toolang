"""CLI Name validation and explicit Source Ref acquisition."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolang.cli.toolang.main import app
from toolang.up import source as sources

runner = CliRunner()


@pytest.mark.parametrize("name", ["Gus", "a.b", "gus-by3", "a" * 65])
def test_new_rejects_invalid_names_without_writing(tmp_path, name):
    root = tmp_path / "root"
    result = runner.invoke(app, ["--root", str(root), "new", name])
    assert result.exit_code == 1
    assert "invalid agent name" in result.output
    assert not root.exists()


def test_new_accepts_64_characters_and_lists_valid_peers(tmp_path):
    root = tmp_path / "root"
    name = "a" * 64
    assert runner.invoke(app, ["--root", str(root), "new", name]).exit_code == 0
    legacy = root / "agents" / "old.name"
    legacy.mkdir()
    (legacy / "agent.too").write_text("flow main():\n  pass\n")
    result = runner.invoke(app, ["--root", str(root), "list"])
    assert result.exit_code == 0, result.output
    assert name in result.output


@pytest.mark.parametrize(
    "ref", ["https://example.com/中文.too", "gh:owner/repo/" + "a" * 65 + ".too@main"]
)
def test_clone_explicit_name_can_replace_an_invalid_derived_default(
    tmp_path, monkeypatch, ref
):
    calls = []

    def fetch(source, **kwargs):
        calls.append(source.render())
        return "flow main():\n  pass\n"

    monkeypatch.setattr(sources, "fetch_agent_ref", fetch)
    root = tmp_path / "root"
    rejected = runner.invoke(app, ["--root", str(root), "clone", ref])
    assert rejected.exit_code == 1, rejected.output
    assert "invalid agent name" in rejected.output
    assert calls == [] and not root.exists()
    accepted = runner.invoke(app, ["--root", str(root), "clone", ref, "daily_report"])
    assert accepted.exit_code == 0, accepted.output
    assert len(calls) == 1
    assert (root / "agents" / "daily_report" / "agent.too").is_file()


@pytest.mark.parametrize("ref", ["owner/name", "oh:owner/name", "openhat:owner/name"])
def test_clone_rejects_deferred_sources_without_network_or_writes(
    tmp_path, monkeypatch, ref
):
    monkeypatch.setattr(
        sources, "urlopen", lambda *a, **k: pytest.fail("network access")
    )
    root = tmp_path / "root"
    result = runner.invoke(app, ["--root", str(root), "clone", ref, "gus"])
    assert result.exit_code == 1, result.output
    assert "unsupported Source Ref" in result.output
    assert not root.exists()


@pytest.mark.parametrize("filename", ["Daily-Report.v2.too", " Daily Report.v2.too"])
@pytest.mark.parametrize("mode", ["direct", "run", "shebang"])
def test_script_entry_points_keep_the_filename_and_share_the_normalized_home(
    tmp_path, mode, filename
):
    import os
    import subprocess
    import sys

    source = tmp_path / filename
    source.write_text('#!/usr/bin/env too\nflow main() -> Text:\n  return "done"\n')
    source.chmod(0o755)
    cli = Path(sys.executable).with_name("too")
    command = (
        [str(source)]
        if mode == "shebang"
        else [str(cli), *(["run"] if mode == "run" else []), source.name]
    )
    env = dict(
        os.environ,
        TOOLANG_ROOT=str(tmp_path / "registry"),
        PATH=f"{cli.parent}:{os.environ.get('PATH', '')}",
    )
    result = subprocess.run(
        [*command, "main", "--quiet"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert source.is_file()
    assert (tmp_path / ".toolang/agents/daily_report_v2/agent.too").resolve() == source
