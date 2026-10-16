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
    record = document["source"]["agent.too"]
    if tamper == "exclude":
        record["error"] = {"code": "invalid-source", "message": "Excluded source"}
    elif tamper == "remove-record":
        document["source"] = {}
    else:
        record["sha256"] = "0" * 64
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


def test_chore_identity_collision_excludes_all_owners_and_keeps_unrelated_sources(
    layout, monkeypatch
):
    from toolang.catalog import chores

    directory = layout.home / "chores"
    directory.mkdir()
    for name in ("first", "second"):
        (directory / f"{name}.md").write_text("Chore body.")
    monkeypatch.setattr(chores, "chore_id", lambda _, **kwargs: "00000000")
    state = prepare_agent_state(layout)
    assert state.chores == ()
    assert "chat" in state.runnables
    rejected = [file for file in state.files if not file.accepted]
    assert [file.key for file in rejected] == ["chores/first.md", "chores/second.md"]
    assert all(file.code == "chore-identity-conflict" for file in rejected)


def test_timeout_check_retries_unreadable_source_without_metadata_change(
    layout, monkeypatch
):
    source = layout.home / "psyches" / "review.md"
    source.parent.mkdir()
    source.write_text("Current guidance.")
    blocked = True
    read = Path.read_bytes

    def guarded(path):
        if path == source and blocked:
            raise PermissionError("Temporarily unreadable")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    watcher = StateWatcher(layout)

    async def scenario():
        nonlocal blocked
        initial = await watcher.refresh()
        assert not initial.psyches
        metadata = source.stat()
        blocked = False
        assert source.stat() == metadata
        result = await watcher._request_check(requested=False)
        assert "review" in result.state.psyches
        assert result.diagnostics == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["parse", "materialize"])
@pytest.mark.parametrize("error", [RuntimeError, TypeError])
def test_internal_program_failures_reject_candidate_and_preserve_state(
    layout, monkeypatch, stage, error
):
    from toolang.state import acceptance

    watcher = StateWatcher(layout)
    original = asyncio.run(watcher.refresh())
    layout.program.write_text("flow chat():\n  pass\n# Changed source.\n")

    def fail(*args, **kwargs):
        raise error("Internal preparation invariant failed")

    with monkeypatch.context() as patch:
        if stage == "parse":
            patch.setattr(acceptance.ProgramSource, "parse", fail)
        else:
            patch.setattr(acceptance, "materialize_program_caps", fail)
        receipt = asyncio.run(watcher.sync())
    assert receipt.error == "state_rejected"
    assert receipt.revision == original.revision
    assert receipt.files == ()
    assert watcher.current() is original
    assert "chat" in watcher.current().runnables
    assert asyncio.run(watcher.sync()).error is None


def test_historical_chore_definitions_load_without_parsing_markdown(
    layout, monkeypatch
):
    from toolang.catalog.chore import ChoreFile

    source = layout.home / "chores" / "good.md"
    source.parent.mkdir()
    source.write_text(
        "---\ntitle: Accepted title\nschedule: FREQ=DAILY\n---\nAccepted body."
    )
    state = prepare_agent_state(layout)
    source.unlink()

    def reject_parser(*args, **kwargs):
        pytest.fail("historical chore loading must restore accepted terms")

    monkeypatch.setattr(ChoreFile, "parse", reject_parser)
    loaded = load_agent_state(layout, state.revision)
    assert loaded.chores == state.chores


def test_cached_chore_terms_bind_to_the_loading_agent(tmp_path):
    ids = []
    for name in ("alice", "bob"):
        layout = AgentLayout.resident(tmp_path, name)
        directory = layout.home / "chores"
        directory.mkdir(parents=True)
        (directory / "review.md").write_text("Review changes.")
        state = prepare_agent_state(layout)
        (chore,) = state.chores
        assert chore.agent_name == name
        loaded = load_agent_state(layout, state.revision)
        assert loaded.chores == state.chores
        ids.append(chore.id)
    assert ids[0] != ids[1]


@pytest.mark.parametrize("tamper", ["body", "missing", "duplicate"])
def test_chore_term_integrity_matches_accepted_source(layout, tamper):
    import json
    import shutil
    from toolang.state.cache import canonical_json, layer_revision_dir

    source = layout.home / "chores" / "good.md"
    source.parent.mkdir()
    source.write_text("Accepted body.")
    state = prepare_agent_state(layout)
    directory = layer_revision_dir(layout, "home", state.home_revision)
    document = json.loads((directory / "layer.json").read_text())
    if tamper == "body":
        document["chores"][0]["value"]["body"] = "Unaccepted body."
    elif tamper == "missing":
        document["chores"] = []
    else:
        document["chores"].append(document["chores"][0])
    encoded = canonical_json(document)
    revision = (
        sha256(encoded).hexdigest() if tamper == "duplicate" else state.home_revision
    )
    forged = layer_revision_dir(layout, "home", revision)
    if directory != forged:
        shutil.copytree(directory, forged)
    (forged / "layer.json").write_bytes(encoded)
    with pytest.raises(ValueError):
        validate_layer_revision(layout, "home", revision)
