from __future__ import annotations

from toolang.catalog import source as cap_source

from concurrent.futures import ProcessPoolExecutor
from hashlib import sha256
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import time

import re
import pytest

from toolang.base.types.progress import ProgressEvent
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.runnables import (
    resolve_bound_runnable,
    resolve_state_runnable,
)
from toolang.lang.ast import Program
from toolang.state import prepare as state_prepare
from toolang.state.cache import (
    LayerScope,
    _agent_publish_lock,
    agent_revision_dir,
    canonical_json,
    load_home_layer,
    load_root_layer,
    layer_revision_dir,
    validate_agent_revision,
)
from toolang.state.schemas import CapDetail
from toolang.state.watcher import StateWatcher
from toolang.state.prepare import (
    compose_layer_state,
    load_agent_state,
    prepare_agent_state,
    prepare_root_home,
    refresh_agent_state,
)
from toolang.state.source import ProgramSource
from toolang.state.state import runnable_module_name


def _assert_excluded(state, match=None):
    excluded = [file for file in state.files if not file.accepted]
    assert excluded, "invalid source must be excluded from the published State"
    assert all(file.code and file.message for file in excluded)
    if match is not None:
        assert re.search(match, "; ".join(file.message or "" for file in excluded))
    return excluded


def _layout(root: Path, name: str = "alice") -> AgentLayout:
    return AgentLayout.resident(root, name)


@pytest.mark.parametrize("exported", [False, True])
def test_authored_default_flow_takes_precedence_over_synthetic_default(
    tmp_path, exported
):
    layout = _layout(tmp_path)
    layout.home.mkdir(parents=True)
    if exported:
        layout.program.write_text("agic():\n  Main.\n")
        source = layout.home / "runnables" / "default.too"
        source.parent.mkdir()
        source.write_text("flow():\n  pass\n")
    else:
        layout.program.write_text("flow default():\n  pass\n")
    state = prepare_agent_state(layout)
    module, runnable = resolve_state_runnable(state, "default", kind="flow")
    assert module == ("default" if exported else "agent")
    assert runnable.name == (None if exported else "default")
    assert state.runnables["default"].kind == "flow"


@pytest.mark.parametrize("first", ["agic", "flow"])
@pytest.mark.parametrize("second", ["agic", "flow"])
def test_state_rejects_colliding_unnamed_entries(tmp_path, first, second):
    layout = _layout(tmp_path)
    layout.home.mkdir(parents=True)
    source = f"{first}:\n  pass\n\n{second}:\n  pass\n"
    program = Program.from_source(source)
    assert any(item.name is None for item in (*program.agics, *program.flows))
    layout.program.write_text(source)
    state = prepare_agent_state(layout)
    _assert_excluded(state, match="Runnable name is not unique: entry")


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_unnamed_entry_can_coexist_with_explicit_main(tmp_path, kind):
    layout = _layout(tmp_path)
    layout.home.mkdir(parents=True)
    layout.program.write_text(f"{kind}:\n  pass\n\n{kind} main:\n  pass\n")
    state = prepare_agent_state(layout)
    assert any(name.startswith("<entry:") for name in state.runnables)
    assert "main" in state.runnables


def test_unnamed_flow_export_is_locally_entry(tmp_path):
    layout = _layout(tmp_path)
    source = layout.home / "runnables" / "research.too"
    source.parent.mkdir(parents=True)
    source.write_text("flow:\n  pass\n")
    state = prepare_agent_state(layout)
    assert state.runnables["research"].name is None
    assert state.module_runnable("research", "_") is state.runnables["research"]


def test_prepare_reuses_explicit_default_agic_cache(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    layout.home.mkdir(parents=True)
    layout.program.write_text("agic default:\n  Hello.\n")
    before = prepare_agent_state(layout)
    monkeypatch.setattr(
        ProgramSource, "parse", lambda _: pytest.fail("rebuilt explicit default")
    )
    after = prepare_agent_state(layout)
    assert after.revision == before.revision
    assert after.modules["agent"].agics[0].name == "default"


@pytest.mark.parametrize("header", ["agent alice", "agent: alice"])
def test_state_rejects_headers_rejected_by_the_language(tmp_path, header):
    layout = _layout(tmp_path)
    layout.home.mkdir(parents=True)
    source = f"#!/usr/bin/env too\n# Agent source.\n{header}\n\nagic chat:\n  Hello.\n"
    layout.program.write_text(source)
    with pytest.raises(ToolangError) as language_error:
        Program.from_source(source)
    state = prepare_agent_state(layout)
    _assert_excluded(state)
    assert state.toolang_root is not None
    state_error = StateWatcher(
        AgentLayout.resident(state.toolang_root, state.name), initial_state=state
    ).diagnostics()
    assert str(language_error.value) in "; ".join(item.message for item in state_error)


def _prepare_revisions_in_process(toolang_root: str) -> tuple[str, str, str]:
    state = prepare_agent_state(
        _layout(Path(toolang_root)),
    )
    return (
        state.revision,
        state.root_revision,
        state.home_revision,
    )


def _prepare_agent_revisions_in_process(
    request: tuple[str, str],
) -> tuple[str, str, str]:
    toolang_root, agent_name = request
    state = prepare_agent_state(
        _layout(Path(toolang_root), agent_name),
    )
    return (
        state.revision,
        state.root_revision,
        state.home_revision,
    )


def _prepare_after_signal(
    request: tuple[str, str, str],
) -> tuple[str, str, str]:
    toolang_root, agent_name, signal_path = request
    Path(signal_path).write_text("started\n", encoding="utf-8")
    return _prepare_agent_revisions_in_process((toolang_root, agent_name))


def _wait_for_path(path: Path, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.01)


def test_prepare_materialize_fails_if_layer_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    events: list[ProgressEvent] = []

    def fail_write(**_kwargs: object) -> str:
        raise OSError("layer write failed")

    monkeypatch.setattr(state_prepare, "write_layer", fail_write)

    with pytest.raises(OSError, match="layer write failed"):
        prepare_agent_state(_layout(toolang_root), progress=events.append)

    root_events = [event for event in events if event.id == "agent:alice:root"]
    assert [(event.stage, event.status) for event in root_events] == [
        ("materialize", "running"),
        ("materialize", "failed"),
    ]


def test_prepare_materialize_stays_open_across_source_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    prompt = toolang_root / "prompts" / "review.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("First version\n", encoding="utf-8")
    layout = _layout(toolang_root)
    events: list[ProgressEvent] = []
    observe_scope_source = state_prepare._observe_scope_source
    root_scans = 0

    def scan_with_one_change(
        selected: AgentLayout,
        *,
        scope: LayerScope,
    ):
        nonlocal root_scans
        if scope == "root":
            root_scans += 1
            if root_scans == 2:
                prompt.write_text("Second version\n", encoding="utf-8")
        return observe_scope_source(selected, scope=scope)

    monkeypatch.setattr(state_prepare, "_observe_scope_source", scan_with_one_change)

    state_prepare.prepare_root(layout, progress=events.append)

    root_events = [event for event in events if event.id == "agent:alice:root"]
    assert [(event.stage, event.status) for event in root_events] == [
        ("materialize", "running"),
        ("materialize", "ok"),
    ]
    assert root_scans == 5


def test_prepare_root_home_snapshot_root_and_home(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    root_prompt = toolang_root / "prompts" / "review.md"
    root_prompt.parent.mkdir(parents=True)
    (toolang_root / "config.toml").write_text(
        '[models]\ndefault = "root"\n', encoding="utf-8"
    )
    root_prompt.write_text(
        "---\ndescription: Review\n---\nReview carefully.\n", encoding="utf-8"
    )
    home = toolang_root / "agents" / "alice"
    skill = home / "skills" / "pdf"
    skill.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    (home / "config.toml").write_text('[models]\ndefault = "home"\n', encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\ndescription: PDF\n---\nUse PDF tools.\n", encoding="utf-8"
    )
    (skill / "notes.txt").write_text("asset\n", encoding="utf-8")

    root, home_layer = prepare_root_home(
        _layout(toolang_root),
    )

    assert [(entry.kind, entry.name) for entry in root.caps] == [("prompt", "review")]
    assert [(entry.kind, entry.name) for entry in home_layer.caps] == [("skill", "pdf")]
    assert not (toolang_root / ".caps").exists()
    assert not (home / ".caps").exists()
    assert Path(root.caps[0].path) == (
        root.revision_dir / "source" / "prompts" / "review.md"
    )
    assert Path(home_layer.caps[0].path) == (
        home_layer.revision_dir / "source" / "skills" / "pdf" / "SKILL.md"
    )
    assert (
        home_layer.revision_dir / "source" / "skills" / "pdf" / "notes.txt"
    ).read_text(encoding="utf-8") == "asset\n"
    assert home_layer.modules["agent"].span.line == 1
    assert root.config == {}
    assert home_layer.config == {}
    assert (
        layer_revision_dir(_layout(toolang_root), "root", root.revision)
        == root.revision_dir
    )
    state = compose_layer_state(
        root,
        home_layer,
        name="alice",
    )
    assert state.root_revision == root.revision
    assert state.home_revision == home_layer.revision
    assert len(state.revision) == 64
    assert [(entry.kind, entry.name) for entry in state.caps.values()] == [
        ("prompt", "review"),
        ("skill", "pdf"),
    ]
    assert tuple(state.caps) == ("prompt:review", "skill:pdf")
    assert tuple(state.prompts) == ("review",)
    assert tuple(state.skills) == ("pdf",)
    assert not state.psyches
    assert not state.services


def test_prepare_does_not_create_missing_agent_source(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir()

    with pytest.raises(FileNotFoundError, match="agent home not found"):
        prepare_agent_state(
            _layout(toolang_root),
        )

    assert not (toolang_root / "agents" / "alice").exists()
    assert not (toolang_root / ".state").exists()


def test_prepare_rejects_invalid_state_owned_config_queries(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (toolang_root / "config.toml").write_text(
        '[allow]\ncaps = ["*[missing=value]"]\n',
        encoding="utf-8",
    )
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")

    with pytest.raises((ToolangError, ValueError), match="unknown allow field: caps"):
        prepare_agent_state(_layout(toolang_root))


def test_prepare_does_not_create_missing_agent_program(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)

    state = prepare_agent_state(
        _layout(toolang_root),
    )

    assert state.modules["agent"].span.line == 1
    assert not (home / "agent.too").exists()


def test_flow_module_keeps_inline_caps_without_agent_program(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "runnables"
    flows.mkdir(parents=True)
    (flows / "report.too").write_text(
        "prompt style:\n  Flow style.\n\nflow:\n  pass\n",
        encoding="utf-8",
    )

    state = prepare_agent_state(_layout(toolang_root))

    cap = state.module_caps["report"][0]
    assert cap.name == "style"
    assert cap.read_content() == "Flow style."
    assert not (home / "agent.too").exists()


def test_prepare_ignores_legacy_state_cache_layout(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    (toolang_root / ".state").mkdir()
    (toolang_root / ".state" / "current").write_text("legacy\n", encoding="utf-8")
    (home / ".state").mkdir()
    (home / ".state" / "current").write_text("legacy\n", encoding="utf-8")

    state = prepare_agent_state(_layout(toolang_root))

    assert (toolang_root / ".state" / "current").read_text() == "legacy\n"
    assert (home / ".state" / "current").read_text() == "legacy\n"
    assert (toolang_root / ".state" / "root" / "current").is_file()
    assert (home / ".state" / "agent" / "current").read_text().strip() == (
        state.revision
    )


def test_prepare_root_home_reuses_unchanged_revisions(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")

    first = prepare_root_home(
        _layout(toolang_root),
    )
    second = prepare_root_home(
        _layout(toolang_root),
    )

    assert second[0].revision == first[0].revision
    assert second[1].revision == first[1].revision
    state = prepare_agent_state(
        _layout(toolang_root),
    )
    assert state.root_revision == second[0].revision
    assert state.home_revision == second[1].revision
    assert state.revision_dir == (home / ".state" / "agent" / "revs" / state.revision)
    assert state.revision_dir is not None
    assert state.revision_dir.is_dir()


def test_prepare_reuses_portable_state_after_root_and_home_remount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_root = tmp_path / "host"
    guest_root = tmp_path / "guest"
    prompt = host_root / "prompts" / "review.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Review carefully.\n", encoding="utf-8")
    home = host_root / "agents" / "alice"
    home.mkdir(parents=True)
    program = home / "agent.too"
    program.write_text(
        "prompt review:\n  Review.\n\nagic answer:\n  Ready.\n",
        encoding="utf-8",
    )
    host_layout = _layout(host_root)
    expected = prepare_agent_state(host_layout)
    shutil.copytree(host_root, guest_root, copy_function=shutil.copy2)
    guest_program = guest_root / "agents" / "alice" / "agent.too"
    guest_prompt = guest_root / "prompts" / "review.md"
    for path in (guest_program, guest_prompt):
        changed = path.stat().st_mtime_ns + 2_000_000_000
        os.utime(path, ns=(changed, changed))
    guest_layout = _layout(guest_root)
    root_current = guest_layout.root_state / "current"
    home_current = guest_layout.home_state / "current"
    agent_current = guest_layout.agent_state / "current"
    pointer_inodes = tuple(
        path.stat().st_ino for path in (root_current, home_current, agent_current)
    )

    def fail_prepare(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("remounted State must not rebuild a matching layer")

    def fail_parse(_cls: type[Program], _source: str) -> Program:
        raise AssertionError("remounted State must not parse persisted Programs")

    monkeypatch.setattr(state_prepare, "_prepare_layer", fail_prepare)
    monkeypatch.setattr(Program, "from_source", classmethod(fail_parse))

    actual = prepare_agent_state(guest_layout)

    assert actual.revision == expected.revision
    assert actual.root_revision == expected.root_revision
    assert actual.home_revision == expected.home_revision
    assert (
        tuple(
            path.stat().st_ino for path in (root_current, home_current, agent_current)
        )
        == pointer_inodes
    )
    assert all(
        Path(cap.path).is_relative_to(guest_root) for cap in actual.caps.values()
    )


def test_prepare_ignores_metadata_only_source_changes(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    program = home / "agent.too"
    program.write_text("agic answer:\n  Ready.\n", encoding="utf-8")
    layout = _layout(toolang_root)
    first = prepare_agent_state(layout)
    changed = program.stat().st_mtime_ns + 2_000_000_000
    os.utime(program, ns=(changed, changed))

    second = prepare_agent_state(layout)

    assert second.revision == first.revision
    assert second.home_revision == first.home_revision


def test_force_prepare_ignores_metadata_only_source_changes(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    program = home / "agent.too"
    program.write_text(
        "prompt review:\n  Review.\n\nagic answer:\n  Ready.\n",
        encoding="utf-8",
    )
    layout = _layout(toolang_root)
    first = prepare_agent_state(layout)
    changed = program.stat().st_mtime_ns + 2_000_000_000
    os.utime(program, ns=(changed, changed))

    second = prepare_agent_state(layout, force=True)

    assert second.revision == first.revision
    assert second.home_revision == first.home_revision


def test_prepare_detects_content_change_with_preserved_size_and_mtime(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    program = home / "agent.too"
    program.write_text("agic answer:\n  First.\n", encoding="utf-8")
    original = program.stat()
    layout = _layout(toolang_root)
    first = prepare_agent_state(layout)
    program.write_text("agic answer:\n  Other.\n", encoding="utf-8")
    os.utime(program, ns=(original.st_atime_ns, original.st_mtime_ns))

    second = prepare_agent_state(layout)

    assert program.stat().st_size == original.st_size
    assert second.revision != first.revision
    assert second.home_revision != first.home_revision


def test_setup_only_config_changes_revision_but_not_state_terms(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    config = toolang_root / "config.toml"
    config.write_text(
        '[default]\nmodel = "openai/one"\n\n[allow]\nmodels = ["openai/*"]\n',
        encoding="utf-8",
    )
    layout = _layout(toolang_root)
    first = prepare_agent_state(layout)
    first_root = load_root_layer(layout, first.root_revision)
    config.write_text(
        '[default]\nmodel = "openai/two"\n\n[allow]\nmodels = ["local/*"]\n',
        encoding="utf-8",
    )

    second = prepare_agent_state(layout)
    second_root = load_root_layer(layout, second.root_revision)

    assert second.revision != first.revision
    assert second.root_revision != first.root_revision
    assert second.home_revision == first.home_revision
    assert first_root.config == second_root.config == {}
    assert first.modules == second.modules
    assert first.caps_by_module == second.caps_by_module
    assert (
        first_root.revision_dir / "source/config.toml"
    ).read_bytes() != config.read_bytes()
    assert (
        second_root.revision_dir / "source/config.toml"
    ).read_bytes() == config.read_bytes()


def test_setup_only_config_creation_and_removal_changes_dependency_revision(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    layout = _layout(toolang_root)
    first = prepare_agent_state(layout)
    config = toolang_root / "config.toml"
    config.write_text(
        '[default]\nmodel = "openai/one"\n\n[allow]\nmodels = ["openai/*"]\n',
        encoding="utf-8",
    )

    created = prepare_agent_state(layout)
    config.unlink()
    removed = prepare_agent_state(layout)

    assert created.revision != first.revision
    assert created.root_revision != first.root_revision
    assert removed.revision != first.revision
    assert removed.root_revision != first.root_revision
    assert removed.config == first.config


def test_prepare_repairs_a_corrupt_content_addressed_layer(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    layout = _layout(toolang_root)
    first = prepare_agent_state(layout)
    revision_dir = layer_revision_dir(layout, "home", first.home_revision)
    layer_path = revision_dir / "layer.json"
    document = json.loads(layer_path.read_text(encoding="utf-8"))
    document["source"]["../escape"] = {"size": 0, "sha256": "0" * 64}
    layer_path.write_bytes(canonical_json(document))

    repaired = prepare_agent_state(layout)

    assert repaired.revision != first.revision
    assert repaired.home_revision != first.home_revision
    assert load_agent_state(layout).revision == repaired.revision


def test_prepare_repairs_corrupt_materialized_layer_content(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    program = home / "agent.too"
    program.write_text("# Agent alice\n", encoding="utf-8")
    layout = _layout(toolang_root)
    first = prepare_agent_state(layout)
    materialized = (
        layer_revision_dir(layout, "home", first.home_revision) / "source" / "agent.too"
    )
    materialized.write_text("corrupt\n", encoding="utf-8")

    repaired = prepare_agent_state(layout, force=True)

    assert repaired.revision != first.revision
    assert repaired.home_revision != first.home_revision
    restored = (
        layer_revision_dir(layout, "home", repaired.home_revision) / "source/agent.too"
    )
    assert restored.read_bytes() == program.read_bytes()
    validate_agent_revision(layout, repaired.revision)


def test_prepare_repairs_a_corrupt_agent_composition(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    layout = _layout(toolang_root)
    first = prepare_agent_state(layout)
    layers_path = agent_revision_dir(layout, first.revision) / "layers.json"
    layers_path.write_text("not JSON\n", encoding="utf-8")

    repaired = prepare_agent_state(layout)

    assert repaired.revision != first.revision
    assert load_agent_state(layout).revision == repaired.revision


def test_prepare_stores_inline_caps_directly_without_generated_files(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text(
        "# Agent alice\n\nprompt summarize:\n  Summarize this.\n",
        encoding="utf-8",
    )

    state = prepare_agent_state(
        _layout(toolang_root),
    )

    assert len(state.caps) == 1
    entry = state.caps["prompt:summarize"]
    home_revision_dir = layer_revision_dir(
        _layout(toolang_root),
        "home",
        state.home_revision,
    )
    assert Path(entry.path) == home_revision_dir / "source/agent.too"
    assert not tuple((home_revision_dir / "materialized").rglob("*.md"))
    assert entry.read_content() == "Summarize this."
    assert entry.source.path == "agents/alice/agent.too"
    layer = json.loads((home_revision_dir / "layer.json").read_text(encoding="utf-8"))
    assert layer["caps"][0]["value"]["body"] == "Summarize this."

    (home / "agent.too").write_text(
        "# Agent alice\n\nprompt summarize:\n  Changed later.\n",
        encoding="utf-8",
    )
    assert entry.read_content() == "Summarize this."


@pytest.mark.parametrize("source_kind", ["agent", "flow", "script"])
def test_inline_skill_materialization_preserves_source_and_snapshots(
    tmp_path: Path, source_kind: str
) -> None:
    layout = _layout(tmp_path / "toolang")
    module = "review" if source_kind == "flow" else "agent"
    source = layout.home / (
        "runnables/review.too" if source_kind == "flow" else "agent.too"
    )
    if source_kind == "script":
        source = tmp_path / "review.too"
        layout = AgentLayout.roaming(source)
        layout.home.mkdir(parents=True)
        layout.program.symlink_to(source)
    source.parent.mkdir(parents=True, exist_ok=True)
    body = "Read {{literal}} and report concrete bugs."
    declaration = f"skill review:\n  description = Review changes.\n\n  {body}\n"
    source.write_text(declaration + "\nflow:\n  pass\n", encoding="utf-8")

    state = prepare_agent_state(layout)

    (entry,) = state.caps_for(module)
    assert (entry.kind, entry.name, entry.shape, entry.scope) == (
        "skill",
        "review",
        "file",
        "here",
    )
    assert entry.ref == "inline://skills/review"
    assert entry.effective_ref == "skill/review"
    assert entry.meta == {"description": "Review changes."}
    assert entry.read_content() == body
    assert entry.source.form == "inline"
    assert entry.source.line == 1
    assert entry.source.path == f"agents/{layout.name}/" + (
        "runnables/review.too" if source_kind == "flow" else "agent.too"
    )
    assert Path(entry.path) == (
        layer_revision_dir(layout, "home", state.home_revision)
        / "source"
        / ("runnables/review.too" if source_kind == "flow" else "agent.too")
    )
    detail = CapDetail.from_cap(entry, agent_name=layout.name)
    assert detail.definition_file == entry.source.path
    assert detail.line == 1 and not detail.editable
    assert detail.files is None
    assert detail.content is not None and body in detail.content

    source.write_text(
        declaration.replace(body, "Changed guidance.") + "\nflow:\n  pass\n",
        encoding="utf-8",
    )
    changed = prepare_agent_state(layout)
    (updated,) = changed.caps_for(module)
    assert updated.revision != entry.revision
    assert updated.read_content() == "Changed guidance."
    (historical,) = load_agent_state(layout, state.revision).caps_for(module)
    assert historical.read_content() == body

    source.write_text("flow:\n  pass\n", encoding="utf-8")
    assert not prepare_agent_state(layout).caps_for(module)
    assert historical.read_content() == body


@pytest.mark.parametrize("scope", ["root", "home"])
def test_inline_skill_precedence_is_module_local(tmp_path: Path, scope: str) -> None:
    layout = _layout(tmp_path)
    (layout.home / "runnables").mkdir(parents=True)
    skill = (layout.root if scope == "root" else layout.home) / "skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\ndescription: Shared review\n---\nShared guidance.\n")
    for filename, body in (
        ("agent.too", "Agent guidance."),
        ("runnables/review.too", "Flow guidance."),
    ):
        (layout.home / filename).write_text(
            f"skill review:\n  description = Review changes.\n\n  {body}\n"
            "\nflow:\n  pass\n"
        )
    (layout.home / "runnables/other.too").write_text("flow:\n  pass\n")

    state = prepare_agent_state(layout)

    for module, body, expected_scope in (
        ("agent", "Agent guidance.", "here"),
        ("review", "Flow guidance.", "here"),
        ("other", "Shared guidance.", scope),
    ):
        (entry,) = state.caps_for(module)
        assert entry.read_content() == body
        assert entry.scope == expected_scope


def test_inline_skill_conflicts_with_same_name_reference(tmp_path: Path, monkeypatch):
    layout = _layout(tmp_path)
    layout.home.mkdir(parents=True)
    layout.program.write_text(
        "with skill github://acme/skills/review@main\n"
        "skill review:\n  description = Review changes.\n\n  Review carefully.\n"
    )
    monkeypatch.setattr(
        cap_source,
        "_fetch_remote_cap_files",
        lambda *, relative_entry_path, **_kwargs: {
            relative_entry_path.as_posix(): b"---\ndescription: Remote review\n---\nReview.\n"
        },
    )
    state = prepare_agent_state(layout)
    _assert_excluded(state, match="conflicting capabilities.*skill.*review")


def test_concurrent_processes_publish_one_root_and_home_revision(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")

    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        results = tuple(
            executor.map(
                _prepare_revisions_in_process,
                (str(toolang_root), str(toolang_root)),
            )
        )

    assert results[0] == results[1]
    assert len(tuple((toolang_root / ".state" / "root" / "revs").iterdir())) == 1
    assert (
        len(
            tuple(
                (
                    toolang_root / "agents" / "alice" / ".state" / "home" / "revs"
                ).iterdir()
            )
        )
        == 1
    )
    agent_revs = home / ".state" / "agent" / "revs"
    assert [path.name for path in agent_revs.iterdir()] == [results[0][0]]


@pytest.mark.parametrize(
    ("scope", "label"),
    [("root", "root state"), ("home", "home state"), (None, "agent alice state")],
)
def test_state_lock_progress_uses_sentence_case(tmp_path, scope, label):
    import fcntl
    from toolang.state import cache

    layout = _layout(tmp_path)
    events = []

    def release_on_wait(event):
        events.append(event)
        if event.status == "running":
            fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)

    if scope is None:
        path = cache._agent_publish_lock_path(layout)
        lock = cache._agent_publish_lock(layout, progress=release_on_wait)
    else:
        path = cache.layer_build_lock_path(layout, scope)
        lock = cache.layer_build_lock(layout, scope, progress=release_on_wait)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX)
        with lock:
            pass

    assert [event.label for event in events] == [
        f"Waiting for {label}...",
        f"{label.capitalize()} available",
    ]


def test_agent_publish_lock_blocks_same_agent_process(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    started = tmp_path / "same-agent-started"
    layout = _layout(toolang_root)
    prepare_agent_state(layout)

    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        with _agent_publish_lock(layout):
            future = executor.submit(
                _prepare_after_signal,
                (str(toolang_root), "alice", str(started)),
            )
            _wait_for_path(started)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.25)

        state_revision, _, _ = future.result(timeout=10)

    assert len(state_revision) == 64


def test_agent_publish_lock_does_not_block_another_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    for name in ("alice", "bob"):
        home = toolang_root / "agents" / name
        home.mkdir(parents=True)
        (home / "agent.too").write_text(f"# Agent {name}\n", encoding="utf-8")
    started = tmp_path / "other-agent-started"
    prepare_agent_state(_layout(toolang_root, "bob"))

    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        with _agent_publish_lock(_layout(toolang_root, "alice")):
            future = executor.submit(
                _prepare_after_signal,
                (str(toolang_root), "bob", str(started)),
            )
            _wait_for_path(started)
            state_revision, _, _ = future.result(timeout=10)

    assert len(state_revision) == 64


def test_different_agent_processes_share_one_root_generation(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    root_prompt = toolang_root / "prompts" / "review.md"
    root_prompt.parent.mkdir(parents=True)
    root_prompt.write_text(
        "---\ndescription: Review\n---\nReview carefully.\n",
        encoding="utf-8",
    )
    for agent_name in ("alice", "bob"):
        home = toolang_root / "agents" / agent_name
        home.mkdir(parents=True)
        (home / "agent.too").write_text(
            f"# Agent {agent_name}\n",
            encoding="utf-8",
        )

    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        alice, bob = tuple(
            executor.map(
                _prepare_agent_revisions_in_process,
                (
                    (str(toolang_root), "alice"),
                    (str(toolang_root), "bob"),
                ),
            )
        )

    assert alice[1] == bob[1]
    root_revisions = toolang_root / ".state" / "root" / "revs"
    assert [path.name for path in root_revisions.iterdir()] == [alice[1]]
    assert (
        toolang_root / "agents" / "alice" / ".state" / "agent" / "current"
    ).is_file()
    assert (toolang_root / "agents" / "bob" / ".state" / "agent" / "current").is_file()


def test_remote_refresh_changes_resolved_and_root_revision(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir()
    (toolang_root / "config.toml").write_text(
        '[prompts]\nrewrite = { ref = "gh:acme/rewrite" }\n',
        encoding="utf-8",
    )
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    version = {"sha": "1" * 40}
    monkeypatch.setattr(
        cap_source, "_resolve_github_commit", lambda ref: version["sha"]
    )
    content = {"value": b"---\ndescription: Rewrite\n---\nFirst.\n"}
    monkeypatch.setattr(
        cap_source, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_source, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_fetch_cap_files(*, relative_entry_path, kind, name, ref):
        del kind, name, ref
        return {str(relative_entry_path): content["value"]}

    monkeypatch.setattr(cap_source, "_fetch_remote_cap_files", fake_fetch_cap_files)

    first_root, _ = prepare_root_home(
        _layout(toolang_root),
    )
    content["value"] = b"---\ndescription: Rewrite\n---\nSecond.\n"
    cached_root, _ = prepare_root_home(
        _layout(toolang_root),
    )
    version["sha"] = "2" * 40
    refreshed = refresh_agent_state(
        _layout(toolang_root),
    )
    second_root = load_root_layer(
        _layout(toolang_root),
        refreshed.root_revision,
    )

    assert cached_root.revision == first_root.revision
    assert Path(cached_root.caps[0].path).read_bytes().endswith(b"First.\n")
    assert second_root.revision != first_root.revision
    assert second_root.resolutions != first_root.resolutions
    assert (
        second_root.caps[0].source.fingerprint != first_root.caps[0].source.fingerprint
    )
    assert Path(second_root.caps[0].path).read_bytes() == content["value"]
    assert Path(second_root.caps[0].path).is_relative_to(
        second_root.revision_dir / "materialized"
    )


def test_local_change_reuses_unchanged_remote_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir()
    (toolang_root / "config.toml").write_text(
        '[prompts]\nrewrite = { ref = "gh:acme/rewrite" }\n',
        encoding="utf-8",
    )
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    monkeypatch.setattr(
        cap_source, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_source, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_source,
        "_fetch_remote_cap_files",
        lambda *, relative_entry_path, **_kwargs: {
            str(relative_entry_path): (
                b"---\ndescription: Rewrite\n---\nRemote content.\n"
            )
        },
    )
    first, _ = prepare_root_home(
        _layout(toolang_root),
    )
    local = toolang_root / "prompts" / "local.md"
    local.parent.mkdir()
    local.write_text("---\ndescription: Local\n---\nLocal content.\n", encoding="utf-8")
    monkeypatch.setattr(
        cap_source,
        "_fetch_remote_cap_files",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unchanged remote cap must be reused")
        ),
    )

    events: list[ProgressEvent] = []
    second, _ = prepare_root_home(
        _layout(toolang_root),
        progress=events.append,
    )

    assert second.revision != first.revision
    assert {entry.name for entry in second.caps} == {"local", "rewrite"}
    remote = next(entry for entry in second.caps if entry.name == "rewrite")
    assert Path(remote.path).read_text(encoding="utf-8").endswith("Remote content.\n")
    cached_terminals = [
        event
        for event in events
        if event.kind == "prepare"
        and event.stage == "materialize"
        and event.status == "skipped"
    ]
    assert len(cached_terminals) == 1
    assert cached_terminals[0].label == "Skipped updating prompt rewrite"
    assert cached_terminals[0].detail == "cached"


def test_local_change_does_not_reuse_corrupt_remote_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir()
    (toolang_root / "config.toml").write_text(
        '[prompts]\nrewrite = { ref = "gh:acme/rewrite" }\n',
        encoding="utf-8",
    )
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    monkeypatch.setattr(
        cap_source, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_source, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_source,
        "_fetch_remote_cap_files",
        lambda *, relative_entry_path, **_kwargs: {
            str(relative_entry_path): b"---\ndescription: Rewrite\n---\nFirst.\n"
        },
    )
    layout = _layout(toolang_root)
    first, _ = prepare_root_home(layout)
    Path(first.caps[0].path).write_text("corrupt\n", encoding="utf-8")
    local = toolang_root / "prompts" / "local.md"
    local.parent.mkdir()
    local.write_text("---\ndescription: Local\n---\nLocal.\n", encoding="utf-8")
    calls = 0

    def rematerialize(*, relative_entry_path: Path, **_kwargs: object):
        nonlocal calls
        calls += 1
        return {
            str(relative_entry_path): b"---\ndescription: Rewrite\n---\nRepaired.\n"
        }

    monkeypatch.setattr(cap_source, "_fetch_remote_cap_files", rematerialize)

    second, _ = prepare_root_home(layout)

    remote = next(entry for entry in second.caps if entry.name == "rewrite")
    assert calls == 1
    assert Path(remote.path).read_text(encoding="utf-8").endswith("Repaired.\n")


def test_declared_ref_change_refreshes_remote_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir()
    config = toolang_root / "config.toml"
    config.write_text(
        '[prompts]\nrewrite = { ref = "gh:acme/rewrite" }\n',
        encoding="utf-8",
    )
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    monkeypatch.setattr(
        cap_source, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_source, "_github_remote_exists", lambda _kind, _ref: True)
    calls: list[str] = []

    def fake_fetch_cap_files(*, relative_entry_path, kind, name, ref):
        del kind, name
        calls.append(ref)
        return {
            str(relative_entry_path): (
                f"---\ndescription: Rewrite\n---\n{ref}\n".encode()
            )
        }

    monkeypatch.setattr(cap_source, "_fetch_remote_cap_files", fake_fetch_cap_files)

    first, _ = prepare_root_home(
        _layout(toolang_root),
    )
    config.write_text(
        '[prompts]\nrewrite = { ref = "gh:acme/rewrite-v2" }\n',
        encoding="utf-8",
    )
    second, _ = prepare_root_home(
        _layout(toolang_root),
    )

    assert len(calls) == 2
    assert first.revision != second.revision
    assert first.caps[0].source.declared_ref == "gh:acme/rewrite"
    assert second.caps[0].source.declared_ref == "gh:acme/rewrite-v2"
    assert second.resolutions[0].declared_ref == "gh:acme/rewrite-v2"


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("named", [False, True])
def test_prepare_discovers_and_reloads_runnable_exports(tmp_path, kind, named):
    layout = _layout(tmp_path)
    modules = layout.home / "runnables"
    modules.mkdir(parents=True)
    layout.program.write_text("agic chat():\n  Chat.\n")
    source = modules / "research.too"
    source.write_text(
        f"{kind}{' research' if named else ''}():\n  pass\nagic helper():\n  Private.\n"
    )

    state = prepare_agent_state(layout)
    owner, exported = resolve_state_runnable(state, "research", kind=kind)
    assert owner == "research"
    assert exported.name == ("research" if named else None)
    assert set(state.runnables) == {"chat", "research"}
    assert state.module_runnable(owner, "helper", kind="agic") is not None

    # Prepared snapshots retain the owning Program without authored files.
    source.unlink()
    loaded = load_agent_state(layout, state.revision)
    assert loaded.runnable_modules == state.runnable_modules
    assert loaded.modules == state.modules
    assert resolve_state_runnable(loaded, "research", kind=kind)[1] == exported


def test_prepare_discovers_independent_flow_module_exports(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "runnables"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    (flows / "research.too").write_text(
        "agic helper:\n  Research.\n\nflow:\n  generate 2 using helper\n  reduce using helper\n",
        encoding="utf-8",
    )

    state = prepare_agent_state(_layout(toolang_root))

    assert [(item.kind, name) for name, item in state.runnables.items()] == [
        ("flow", "research"),
    ]
    assert tuple(state.agics) == ()
    assert tuple(state.flows) == ("research",)
    program = state.modules["research"]
    assert not hasattr(state, "program")
    assert tuple(state.modules) == ("agent", "research")
    assert state.module_sources["research"] == "runnables/research.too"
    assert program.find_agic("helper") is not None
    assert "helper" not in state.runnables
    helper = state.module_runnable("research", "helper", kind="agic")
    assert helper is not None
    assert helper is program.find_agic("helper")
    exported = state.runnables["research"]
    assert state.runnable_modules["research"] == "research"
    assert exported is program.flows[0]
    assert exported.name is None
    assert state.runnable_modules["research"] == "research"


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_unnamed_runnable_export_renames_with_its_file(tmp_path: Path, kind) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "runnables"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    source = flows / "research.too"
    source.write_text(f"{kind}:\n  pass\n", encoding="utf-8")
    first = prepare_agent_state(_layout(toolang_root))

    public_module, public = resolve_state_runnable(first, "research", kind=kind)
    local = resolve_bound_runnable(first, "research", f"{kind}:research")
    assert public_module == "research"
    assert public is local
    assert public.name is local.name is None

    source.rename(flows / "report.too")
    second = prepare_agent_state(_layout(toolang_root))

    assert "research" in first.runnables
    assert "research" not in second.runnables
    assert second.runnables["report"].name is None
    assert first.revision != second.revision


@pytest.mark.parametrize(
    "authored_path",
    (
        "runnables/1report.too",
        "runnables/bad name.too",
        "runnables/con.too",
        f"runnables/{'a' * 65}.too",
        "nested/runnables/report.too",
    ),
)
def test_runnable_module_names_reject_nonportable_source_paths(
    authored_path: str,
) -> None:
    with pytest.raises(ValueError):
        runnable_module_name(authored_path)


def test_runnable_module_names_reject_casefold_collisions(tmp_path) -> None:
    from toolang.state.acceptance import accept_sources
    from toolang.state.source import SourceFile, SourceSnapshot

    content = b"flow:\n  pass\n"
    digest = sha256(content).hexdigest()
    paths = ("runnables/Report.too", "runnables/report.too")
    files = tuple(
        SourceFile(
            path=tmp_path / "agents/alice" / path,
            relative_path=f"agents/alice/{path}",
            category="program",
            layer="agent",
            content=content,
            digest=digest,
            size=len(content),
            raw_digest=digest,
        )
        for path in paths
    )
    accepted = accept_sources(SourceSnapshot(tmp_path, "alice", files), scope="home")
    assert {record.key for record in accepted.records} == set(paths)
    assert all(
        not record.accepted and record.code == "invalid-runnable-filename"
        for record in accepted.records
    )
    assert [program.name for program in accepted.programs] == ["agent"]


@pytest.mark.parametrize(
    ("source", "layer"),
    [
        ("flow research:\n  reduce using missing\n", "program"),
        ("flow other:\n  pass\n", "runnable-extension"),
        ("flow:\n  pass\n\nflow research:\n  pass\n", "runnable-extension"),
        ("agic other:\n  Help.\n", "runnable-extension"),
        ("agic:\n  Help.\n\nagic research:\n  Help.\n", "runnable-extension"),
        ("flow:\n  pass\n\nagic research:\n  Help.\n", "runnable-extension"),
        ("agic:\n  Help.\n\nflow research:\n  pass\n", "runnable-extension"),
    ],
)
def test_prepare_rejects_runnable_module_at_the_correct_layer(
    tmp_path: Path,
    source: str,
    layer: str,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "runnables"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text("# Agent alice\n", encoding="utf-8")
    (flows / "research.too").write_text(source, encoding="utf-8")

    state = prepare_agent_state(_layout(toolang_root))
    _assert_excluded(state)
    assert state.toolang_root is not None
    raised = StateWatcher(
        AgentLayout.resident(state.toolang_root, state.name), initial_state=state
    ).diagnostics()

    diagnostic = raised[0]
    assert diagnostic.layer == layer
    assert diagnostic.module_kind == "runnable"
    assert diagnostic.authored_path == "runnables/research.too"


@pytest.mark.parametrize("root_kind", ["agic", "flow"])
@pytest.mark.parametrize("module_kind", ["agic", "flow"])
def test_prepare_rejects_public_runnable_collisions(
    tmp_path: Path, root_kind, module_kind
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "runnables"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text(
        f"# Agent alice\n\n{root_kind} research:\n  pass\n",
        encoding="utf-8",
    )
    (flows / "research.too").write_text(
        f"{module_kind} research:\n  pass\n",
        encoding="utf-8",
    )

    state = prepare_agent_state(_layout(toolang_root))
    _assert_excluded(state)
    assert state.toolang_root is not None
    raised = StateWatcher(
        AgentLayout.resident(state.toolang_root, state.name), initial_state=state
    ).diagnostics()

    assert raised[0].layer == "state-composition"
    assert "agent.too:3" in "; ".join(item.message for item in raised)
    assert "runnables/research.too:1" in "; ".join(item.message for item in raised)


def test_module_here_caps_are_isolated_and_reload_from_cache(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "runnables"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text(
        (
            "# Agent alice\n\n"
            "prompt style:\n  Agent style.\n\n"
            "prompt only_agent:\n  Private to agent.\n"
        ),
        encoding="utf-8",
    )
    flow_path = flows / "research.too"
    flow_path.write_text(
        "prompt style:\n  Flow style.\n\nflow:\n  pass\n",
        encoding="utf-8",
    )
    state = prepare_agent_state(_layout(toolang_root))

    agent_cap = next(item for item in state.caps_for("agent") if item.name == "style")
    flow_cap = next(item for item in state.caps_for("research") if item.name == "style")
    assert agent_cap.read_content() == "Agent style."
    assert flow_cap.read_content() == "Flow style."
    assert agent_cap.path != flow_cap.path
    assert all(item.name != "only_agent" for item in state.caps_for("research"))

    flow_path.write_text("invalid", encoding="utf-8")
    loaded = load_home_layer(_layout(toolang_root), state.home_revision)
    assert loaded.modules["research"].flows[0].name is None


def test_flow_modules_can_reference_the_same_cap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "runnables"
    flows.mkdir(parents=True)
    source = "with skill gh:acme/reviewer\n\nflow:\n  pass\n"
    (flows / "one.too").write_text(source, encoding="utf-8")
    (flows / "two.too").write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        cap_source, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_source, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_source,
        "_fetch_remote_cap_files",
        lambda *, relative_entry_path, **_kwargs: {
            relative_entry_path.as_posix(): (
                b"---\ndescription: Review\n---\nReview carefully.\n"
            )
        },
    )

    state = prepare_agent_state(_layout(toolang_root))

    one = state.module_caps["one"][0]
    two = state.module_caps["two"][0]
    assert one.ref == two.ref
    assert one.path == two.path
    assert one.read_content() == "Review carefully."
    assert two.read_content() == "Review carefully."
    assert not (home / "agent.too").exists()


@pytest.mark.parametrize("target", ["research", "private_helper"])
def test_flow_modules_cannot_import_other_module_runnables(tmp_path, target):
    layout = _layout(tmp_path)
    (layout.home / "runnables").mkdir(parents=True)
    layout.program.write_text("agic private_helper():\n  Main helper.\n")
    (layout.home / "runnables" / "research.too").write_text("flow():\n  pass\n")
    (layout.home / "runnables" / "review.too").write_text(f"flow():\n  run {target}\n")
    state = prepare_agent_state(layout)
    _assert_excluded(state, match="unknown runnable")


def test_main_composition_does_not_import_private_helpers_or_types(tmp_path):
    layout = _layout(tmp_path)
    (layout.home / "runnables").mkdir(parents=True)
    (layout.home / "runnables" / "research.too").write_text(
        "struct Secret:\n  value: Text\nagic private_helper():\n  Help.\nflow():\n  run private_helper\n"
    )
    layout.program.write_text("flow parent():\n  run private_helper\n")
    state = prepare_agent_state(layout)
    _assert_excluded(state, match="unknown runnable")
    layout.program.write_text("flow parent(_: Secret):\n  run research\n")
    state = prepare_agent_state(layout)
    _assert_excluded(state, match="Unknown Toolang type")


def test_visiting_agent_parses_once_before_parallel_dependencies(tmp_path, monkeypatch):
    from threading import Barrier, Lock
    from toolang.state import source as source_owner

    layout = AgentLayout(root=tmp_path, name="visitor", placement="visiting")
    layout.home.mkdir(parents=True)
    text = "with prompt github://acme/caps/one.md@main\nwith prompt github://acme/caps/two.md@main\n"
    layout.program.write_text(text)
    original = Program.from_source
    parses = []
    barrier = Barrier(2)
    lock = Lock()
    running = [0, 0]

    def parse(text, **kwargs):
        parses.append(text)
        return original(text, **kwargs)

    def fetch(_ref):
        assert parses == [text]
        with lock:
            running[0] += 1
            running[1] = max(running)
        barrier.wait(timeout=5)
        with lock:
            running[0] -= 1
        return b"---\ndescription: A prompt\n---\nPrompt.\n"

    monkeypatch.setattr(Program, "from_source", parse)
    monkeypatch.setattr(cap_source, "_fetch_github_file", fetch)
    events = []
    state_prepare.parse_visiting_agent(
        layout, ref="https://example.test/agent.too", progress=events.append
    )
    state = prepare_agent_state(layout, progress=events.append)
    assert len(parses) == 1
    assert running[1] == 2
    parsed = next(
        i for i, e in enumerate(events) if e.stage == "parse" and e.status == "ok"
    )
    fetching = [
        i for i, e in enumerate(events) if e.subject == "cap" and e.stage == "fetch"
    ]
    assert fetching and min(fetching) > parsed
    assert len(state.caps_for("agent")) == 2
    # Simulate a new process: persisted State supplies the validated AST silently.
    source_owner._AGENT_PROGRAMS.clear()
    monkeypatch.setattr(
        Program, "from_source", lambda *_a, **_k: pytest.fail("warm parse")
    )
    monkeypatch.setattr(
        cap_source, "_fetch_github_file", lambda *_a: pytest.fail("warm fetch")
    )
    warm_events = []
    warm = prepare_agent_state(layout, progress=warm_events.append)
    assert warm.revision == state.revision
    assert warm_events == []


def test_invalid_remote_agent_blocks_all_dependency_preparation(tmp_path, monkeypatch):
    layout = AgentLayout(root=tmp_path, name="visitor", placement="visiting")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow broken(:\n")
    monkeypatch.setattr(
        state_prepare,
        "prepare_root",
        lambda *_a, **_k: pytest.fail("dependency work before agent parsing"),
    )
    from toolang.lang.errors import ToolangSourceError

    with pytest.raises(ToolangSourceError):
        prepare_agent_state(layout)


def test_remote_parse_reuse_requires_matching_source_bytes(tmp_path, monkeypatch):
    layout = AgentLayout(root=tmp_path, name="visitor", placement="visiting")
    layout.home.mkdir(parents=True)
    original = Program.from_source
    calls = []

    def parse(text, **kwargs):
        calls.append(text)
        return original(text, **kwargs)

    monkeypatch.setattr(Program, "from_source", parse)
    layout.program.write_bytes(b"# First\r\n")
    prepare_agent_state(layout)
    assert calls == ["# First\r\n"]
    layout.program.write_text("# Changed\n")
    prepare_agent_state(layout)
    assert calls == ["# First\r\n", "# Changed\n"]


def test_cached_agent_ast_revalidates_removed_external_runnable(tmp_path):
    resident = AgentLayout.resident(tmp_path, "visitor")
    (resident.home / "runnables").mkdir(parents=True)
    helper = resident.home / "runnables/helper.too"
    helper.write_text("flow():\n  pass\n")
    resident.program.write_text("flow():\n  run helper\n")
    prepare_agent_state(resident)
    visiting = AgentLayout(root=tmp_path, name="visitor", placement="visiting")
    prepare_agent_state(visiting)
    helper.unlink()
    changed = prepare_agent_state(visiting)
    _assert_excluded(changed, match="unknown runnable")
