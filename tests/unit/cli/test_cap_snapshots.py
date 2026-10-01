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
