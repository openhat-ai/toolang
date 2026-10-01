"""CLI capability lookups consume materialized State layers."""

from pathlib import Path

import pytest

from toolang.cli.caps.commands import _named_entry
from toolang.common.layout import AgentLayout
from toolang.state import state as cap_state


@pytest.mark.parametrize("scope", ["root", "home"])
def test_named_cap_keeps_snapshot_content_after_source_edit(
    tmp_path, monkeypatch, scope
):
    layout = AgentLayout.resident(tmp_path, "alice")
    base = layout.home if scope == "home" else layout.root
    (base / "psyches").mkdir(parents=True)
    source = base / "psyches" / "note.md"
    source.write_text("Original body.\n")
    monkeypatch.setattr(
        cap_state,
        "list_entries",
        lambda *args, **kwargs: pytest.fail("must read State"),
    )
    entry = _named_entry(
        layout.root,
        layout.name,
        scope=scope,
        kind="psyche",
        name="note",
        source_form="authored",
    )
    source.write_text("Changed body.\n")
    assert Path(entry.path).is_absolute()
    assert entry.read_text() == "Original body.\n"
    assert entry.source.path == source.relative_to(layout.root).as_posix()
    if scope == "root":
        assert not layout.home.exists()


def test_named_configured_cap_reuses_materialized_resolution(tmp_path, monkeypatch):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.config.write_text('[prompts]\nrewrite = { ref = "acme/rewrite" }\n')
    monkeypatch.setattr(cap_state, "_github_repo_default_branch", lambda *_: "main")
    monkeypatch.setattr(cap_state, "_github_remote_exists", lambda *_: True)
    calls = []

    def materialize(*, relative_entry_path, **kwargs):
        calls.append(kwargs)
        return {str(relative_entry_path): b"Remote body.\n"}

    monkeypatch.setattr(cap_state, "_remote_materialized_files", materialize)
    monkeypatch.setattr(
        cap_state,
        "list_entries",
        lambda *args, **kwargs: pytest.fail("must read State"),
    )
    for _ in range(2):
        entry = _named_entry(
            layout.root,
            layout.name,
            scope="home",
            kind="prompt",
            name="rewrite",
            source_form="configured",
        )
        assert entry.read_text() == "Remote body.\n"
    assert len(calls) == 1


@pytest.mark.parametrize("scope", ["root", "home"])
def test_remove_unresolvable_configured_cap(tmp_path, monkeypatch, capsys, scope):
    from toolang.cli.toolang.main import main
    from toolang.catalog.config import ConfiguredCaps
    from toolang.catalog import templates

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text(
        templates.render_template("agent", name="alice", agent_name="alice")
    )
    config = layout.root_config if scope == "root" else layout.config
    config.write_text('[prompts]\nrewrite = { ref = "acme/rewrite" }\n')

    def unavailable(*args, **kwargs):
        raise ValueError("remote repository is unavailable")

    monkeypatch.setattr(cap_state, "_github_repo_default_branch", unavailable)
    target = ["alice"] if scope == "home" else []
    result = main(["--root", str(tmp_path), *target, "prompt", "remove", "rewrite"])
    output = capsys.readouterr()
    assert result == 0, output.err
    assert "Prompt rewrite removed: acme/rewrite" in output.out
    assert ConfiguredCaps(config).get("prompt", "rewrite") is None


@pytest.mark.parametrize("scope", ["root", "home"])
def test_delete_authored_cap_resolves_same_scope_conflict(
    tmp_path, monkeypatch, capsys, scope
):
    from toolang.cli.toolang.main import main
    from toolang.catalog import templates

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text(
        templates.render_template("agent", name="alice", agent_name="alice")
    )
    base = layout.root if scope == "root" else layout.home
    (base / "prompts").mkdir()
    source = base / "prompts" / "rewrite.md"
    source.write_text("Local body.\n")
    (base / "config.toml").write_text('[prompts]\nrewrite = { ref = "acme/rewrite" }\n')
    monkeypatch.setattr(cap_state, "_github_repo_default_branch", lambda *_: "main")
    monkeypatch.setattr(cap_state, "_github_remote_exists", lambda *_: True)
    monkeypatch.setattr(
        cap_state,
        "_remote_materialized_files",
        lambda *, relative_entry_path, **kwargs: {
            str(relative_entry_path): b"Remote body.\n"
        },
    )
    target = ["alice"] if scope == "home" else []
    result = main(["--root", str(tmp_path), *target, "prompt", "delete", "rewrite"])
    output = capsys.readouterr()
    assert result == 0, output.err
    assert f"Prompt rewrite deleted: {source}" in output.out
    assert not source.exists()


def test_root_add_does_not_resolve_unrelated_configured_caps(
    tmp_path, monkeypatch, capsys
):
    from toolang.catalog.config import ConfiguredCaps
    from toolang.cli.toolang.main import main

    config = tmp_path / "config.toml"
    config.write_text('[prompts]\nstale = { ref = "acme/stale" }\n')
    monkeypatch.setattr(cap_state, "_github_repo_default_branch", lambda *_: "main")
    monkeypatch.setattr(
        cap_state, "_github_remote_exists", lambda _kind, ref: "rewrite" in ref
    )

    result = main(["--root", str(tmp_path), "prompt", "add", "acme/rewrite"])
    output = capsys.readouterr()

    assert result == 0, output.err
    added = ConfiguredCaps(config).get("prompt", "rewrite")
    assert added is not None
    assert f"Prompt rewrite added: {added.ref}" in output.out
    assert ConfiguredCaps(config).get("prompt", "stale") is not None
