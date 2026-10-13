"""Independent resource acceptance, durable diagnostics, and repair."""

import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.state.cache import validate_layer_revision
from toolang.state.prepare import load_agent_state, prepare_agent_state
from toolang.state.watcher import StateWatcher


@pytest.fixture
def layout(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow chat():\n  pass\n")
    return layout


def definition(kind, name):
    if kind == "runnable":
        return f"flow {name}():\n  pass\n"
    if kind == "chore":
        return "Review the latest changes."
    if kind == "service":
        return "---\ndescription: Review\ntransport: http\ntarget: https://example.com/mcp\n---\nReview."
    return "---\ndescription: Review\n---\nReview the latest changes."


def resource_path(base, kind, name):
    return base / (
        f"runnables/{name}.too"
        if kind == "runnable"
        else f"chores/{name}.md"
        if kind == "chore"
        else f"skills/{name}/SKILL.md"
        if kind == "skill"
        else f"{kind}s/{name}.md"
        if kind != "psyche"
        else f"psyches/{name}.md"
    )


@pytest.mark.parametrize(
    "kind", ["psyche", "skill", "service", "prompt", "chore", "runnable"]
)
def test_invalid_edits_exclude_only_their_unit_and_repair_restores_it(layout, kind):
    first = resource_path(layout.home, kind, "review")
    sibling = resource_path(layout.home, kind, "other")
    for name, path in [("review", first), ("other", sibling)]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(definition(kind, name))
    if kind == "skill":
        asset = first.parent / "assets" / "review.txt"
        asset.parent.mkdir()
        asset.write_text("Bundled asset")
    watcher = StateWatcher(layout)
    original = asyncio.run(watcher.refresh())
    first.write_text("flow (" if kind == "runnable" else "---\nname: [\n---\nBroken.")
    sibling.write_text(definition(kind, "other") + "\n# Changed")
    broken = asyncio.run(watcher.refresh())
    assert broken.revision != original.revision
    records = {file.key: file for file in broken.files}
    key = first.relative_to(layout.home).as_posix()
    other = sibling.relative_to(layout.home).as_posix()
    assert records[key].digest == sha256(first.read_bytes()).hexdigest()
    assert records[key].accepted is False
    assert records[key].code and records[key].message
    assert records[other].accepted
    assert "chat" in broken.runnables
    if kind == "skill":
        assert records[asset.relative_to(layout.home).as_posix()].accepted is False
        assert "review" not in broken.skills and "other" in broken.skills
    elif kind == "runnable":
        assert "review" not in broken.runnables and "other" in broken.runnables
    elif kind == "chore":
        assert [chore.name for chore in broken.chores] == ["other"]
    else:
        assert f"{kind}:review" not in broken.caps and f"{kind}:other" in broken.caps
    assert asyncio.run(watcher.refresh(force=True)).revision == broken.revision
    assert load_agent_state(layout, broken.revision).files == broken.files
    validate_layer_revision(layout, "home", broken.home_revision)
    first.write_text(definition(kind, "review"))
    repaired = asyncio.run(watcher.refresh())
    assert all(file.accepted for file in repaired.files)
    assert watcher.diagnostics() == ()
    first.unlink()
    assert load_agent_state(layout, repaired.revision).files == repaired.files


def test_invalid_home_cap_exposes_current_valid_root_cap(layout):
    root = layout.root / "psyches" / "review.md"
    home = layout.home / "psyches" / "review.md"
    for path in (root, home):
        path.parent.mkdir()
        path.write_text(definition("psyche", "review"))
    home.write_text("---\nname: other\n---\nWrong identity.")
    state = prepare_agent_state(layout)
    assert state.psyches["review"].scope == "root"
    assert [
        file.accepted for file in state.files if file.key == "psyches/review.md"
    ] == [False, True]


def test_chore_directories_are_lazy_and_root_definitions_are_ignored(layout):
    watcher = StateWatcher(layout)
    initial = asyncio.run(watcher.refresh())
    assert initial.chores == ()
    assert not (layout.root / "chores").exists()
    assert not (layout.home / "chores").exists()
    root = layout.root / "chores" / "root.md"
    root.parent.mkdir()
    root.write_text("Root chore must be ignored")
    assert asyncio.run(watcher.refresh()).revision == initial.revision
    home = layout.home / "chores" / "review.md"
    home.parent.mkdir()
    home.write_text("Current chore")
    added = asyncio.run(watcher.refresh())
    assert [chore.name for chore in added.chores] == ["review"]
    home.unlink()
    home.parent.rmdir()
    assert asyncio.run(watcher.refresh()).chores == ()
    assert not home.parent.exists()
    home.parent.mkdir()
    home.write_text("Recreated chore")
    assert asyncio.run(watcher.refresh()).chores[0].body == "Recreated chore"


def test_unreadable_file_is_diagnosed_and_valid_sibling_publishes(layout, monkeypatch):
    bad = layout.home / "psyches" / "bad.md"
    good = bad.with_name("good.md")
    bad.parent.mkdir()
    bad.write_text(definition("psyche", "bad"))
    good.write_text(definition("psyche", "good"))
    read = Path.read_bytes

    def fail_one(path):
        if path == bad:
            raise PermissionError("Cannot read resource")
        return read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", fail_one)
        state = prepare_agent_state(layout)
    assert set(state.psyches) == {"good"}
    record = next(
        file.to_data() for file in state.files if file.key == "psyches/bad.md"
    )
    assert record["observed_digest"] is record["accepted_digest"] is None
    assert record["code"] == "source-read"
    assert set(prepare_agent_state(layout).psyches) == {"bad", "good"}


def test_unreadable_directory_excludes_its_units_without_hiding_other_kinds(
    layout, monkeypatch
):
    psyche = layout.home / "psyches" / "review.md"
    prompt = layout.home / "prompts" / "review.md"
    for path in (psyche, prompt):
        path.parent.mkdir()
        path.write_text(definition("psyche", "review"))
    listing = Path.iterdir

    def fail_listing(path):
        if path == psyche.parent:
            raise PermissionError("Cannot list resources")
        return listing(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "iterdir", fail_listing)
        state = prepare_agent_state(layout)
    assert not state.psyches and "review" in state.prompts
    assert any(
        file.key == "psyches" and not file.accepted and file.code == "source-read"
        for file in state.files
    )
    assert "review" in prepare_agent_state(layout).psyches


def test_invalid_agent_program_does_not_hide_independent_runnable(layout):
    worker = layout.home / "runnables" / "worker.too"
    worker.parent.mkdir()
    worker.write_text("flow():\n  pass\n")
    layout.program.write_text("chore invalid:\n  schedule = invalid\n  Broken.\n")
    state = prepare_agent_state(layout)
    assert set(state.runnables) == {"worker"}
    assert state.modules["agent"].chores == ()
    assert any(file.key == "agent.too" and not file.accepted for file in state.files)


@pytest.mark.parametrize("tamper", ["exclude", "remove-record", "accepted-digest"])
def test_integrity_validation_rejects_inconsistent_source_acceptance(layout, tamper):
    import json
    import shutil
    from toolang.state.cache import canonical_json, layer_revision_dir

    state = prepare_agent_state(layout)
    directory = layer_revision_dir(layout, "home", state.home_revision)
    document = json.loads((directory / "layer.json").read_bytes())
    record = document["sources"][0]
    if tamper == "exclude":
        record.update(
            accepted_digest=None, code="invalid-source", message="Excluded source"
        )
    elif tamper == "remove-record":
        document["sources"] = []
    else:
        record["accepted_digest"] = "0" * 64
    encoded = canonical_json(document)
    revision = sha256(encoded).hexdigest()
    forged = layer_revision_dir(layout, "home", revision)
    shutil.copytree(directory, forged)
    (forged / "layer.json").write_bytes(encoded)
    with pytest.raises(ValueError):
        validate_layer_revision(layout, "home", revision)


def test_offline_cap_inspection_isolates_bad_files_without_preparing_handlers(layout):
    from toolang.state.prepare import inspect_agent_caps

    bad = layout.home / "psyches/bad.md"
    bad.parent.mkdir()
    bad.write_text("---\nname: [\n---\nBroken.")
    good = bad.with_name("good.md")
    good.write_text("Useful guidance.")
    layout.program.write_text("flow broken():\n  run unavailable\n")
    entries, allowed = inspect_agent_caps(layout, kinds={"psyche"})
    assert [entry.name for entry in entries] == ["good"]
    assert entries == allowed
    assert not layout.home_state.exists()
