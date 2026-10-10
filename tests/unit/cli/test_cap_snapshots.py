"""Cap authoring uses its catalog before publishing the resulting State."""

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import state as cap_state


@pytest.mark.parametrize("scope", ["root", "home"])
@pytest.mark.parametrize("command", ["new", "edit"])
def test_authoring_does_not_resolve_unrelated_caps(
    tmp_path, monkeypatch, capsys, scope, command
):
    from toolang.cli.toolang.main import main

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("invalid Toolang!!!")
    base = layout.home if scope == "home" else layout.root
    (base / "prompts").mkdir()
    source = base / "prompts" / "note.md"
    source.write_text("Original body.\n")
    (base / "config.toml").write_text('[prompts]\nremote = { ref = "acme/remote" }\n')
    monkeypatch.setattr(
        cap_state,
        "_github_repo_default_branch",
        lambda *_: pytest.fail("authoring must not fetch unrelated sources"),
    )
    opened = []
    monkeypatch.setattr(
        "toolang.cli.caps.commands.edit_markdown", lambda text: opened.append(text)
    )
    target = ["alice"] if scope == "home" else []
    name = "note" if command == "edit" else "another"
    assert main(["--root", str(tmp_path), *target, "prompt", command, name]) == 0
    assert "No changes" in capsys.readouterr().out
    assert len(opened) == 1
    if command == "edit":
        assert opened == ["Original body.\n"]
    assert not layout.agent_state.exists()


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


@pytest.mark.parametrize("command", ["new", "edit"])
def test_authored_change_is_saved_before_state_publication_failure(
    tmp_path, monkeypatch, capsys, command
):
    from toolang.cli.toolang.main import main

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("invalid Toolang!!!")
    (layout.home / "prompts").mkdir()
    source = layout.home / "prompts" / "note.md"
    if command == "edit":
        source.write_text("Original body.\n")
    monkeypatch.setattr(
        "toolang.cli.caps.commands.edit_markdown", lambda _: "Saved body.\n"
    )
    result = main(["--root", str(tmp_path), "alice", "prompt", command, "note"])
    output = capsys.readouterr()
    assert result == 1
    assert source.read_text() == "Saved body.\n"
    assert "change was saved" in output.err
    assert "State" in output.err
