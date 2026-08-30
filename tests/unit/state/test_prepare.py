from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from hashlib import sha256
import json
import multiprocessing
from pathlib import Path
import shutil
import time

import pytest

from toolang.base.types.progress import ProgressEvent
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.runnables import (
    resolve_bound_runnable,
    resolve_state_runnable,
)
from toolang.state import prepare as state_prepare
from toolang.state import state as cap_state
from toolang.state.cache import (
    LAYER_SCHEMA,
    LayerScope,
    _agent_check_lock,
    _persist_agent_revision,
    canonical_json,
    load_home_layer,
    load_root_layer,
    layer_revision_dir,
    publish_layer_current,
)
from toolang.state.errors import StatePreparationError
from toolang.state.prepare import (
    _validate_flow_source_names,
    compose_layer_state,
    load_agent_state,
    prepare_agent_state,
    prepare_root_home,
    refresh_agent_state,
)
from toolang.state.source import ProgramSource
from toolang.state.state import flow_module_name


def _layout(root: Path, name: str = "alice") -> AgentLayout:
    return AgentLayout.resident(root, name)


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
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
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
    scan_scope_source = state_prepare._scan_scope_source
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
        return scan_scope_source(selected, scope=scope)

    monkeypatch.setattr(state_prepare, "_scan_scope_source", scan_with_one_change)

    state_prepare.prepare_root(layout, progress=events.append)

    root_events = [event for event in events if event.id == "agent:alice:root"]
    assert [(event.stage, event.status) for event in root_events] == [
        ("materialize", "running"),
        ("materialize", "ok"),
    ]
    assert root_scans == 4


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
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
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
        root.revision_dir / "files" / "caps" / "authored" / "prompt" / "review.md"
    )
    assert Path(home_layer.caps[0].path) == (
        home_layer.revision_dir
        / "files"
        / "caps"
        / "authored"
        / "skill"
        / "pdf"
        / "SKILL.md"
    )
    assert (
        home_layer.revision_dir
        / "files"
        / "caps"
        / "authored"
        / "skill"
        / "pdf"
        / "notes.txt"
    ).read_text(encoding="utf-8") == "asset\n"
    assert home_layer.modules["agent"].span.line == 1
    assert root.config == {"models": {"default": "root"}}
    assert home_layer.config == {"models": {"default": "home"}}
    assert (
        layer_revision_dir(_layout(toolang_root), "root", root.revision)
        == root.revision_dir
    )
    state = compose_layer_state(
        root,
        home_layer,
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


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ('[allow]\ncaps = ["*[missing=value]"]\n', "unknown allow field: caps"),
        (
            '[default]\nrunnable = "*[missing=value]"\n',
            "unknown runnables query field",
        ),
    ],
)
def test_prepare_rejects_invalid_state_owned_config_queries(
    tmp_path: Path,
    config: str,
    message: str,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (toolang_root / "config.toml").write_text(config, encoding="utf-8")
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")

    with pytest.raises((ToolangError, ValueError), match=message):
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
    flows = home / "flows"
    flows.mkdir(parents=True)
    (flows / "report.too").write_text(
        "prompt style:\n  Flow style.\n\nflow:\n  pass\n",
        encoding="utf-8",
    )

    state = prepare_agent_state(_layout(toolang_root))

    cap = state.module_caps["_flow_report"][0]
    assert cap.name == "style"
    assert cap.read_content() == "Flow style."
    assert not (home / "agent.too").exists()


def test_prepare_ignores_legacy_state_cache_layout(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
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
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")

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


def test_prepare_rebuilds_a_current_layer_from_an_older_schema(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    layout = _layout(toolang_root)
    current_root, current_home = prepare_root_home(layout)
    current_dir = layer_revision_dir(layout, "home", current_home.revision)
    document = json.loads((current_dir / "layer.json").read_text(encoding="utf-8"))
    document["schema"] = LAYER_SCHEMA - 1
    encoded = canonical_json(document)
    stale_revision = sha256(encoded).hexdigest()
    stale_dir = layer_revision_dir(layout, "home", stale_revision)
    shutil.copytree(current_dir, stale_dir)
    (stale_dir / "layer.json").write_bytes(encoded)
    publish_layer_current(layout, "home", stale_revision)
    with _agent_check_lock(layout):
        stale_state_revision = _persist_agent_revision(
            layout,
            root_revision=current_root.revision,
            home_revision=stale_revision,
        )

    with pytest.raises(ValueError, match="outdated layer schema"):
        load_agent_state(layout)
    assert (
        load_agent_state(layout, stale_state_revision).home_revision == stale_revision
    )

    prepared_state = prepare_agent_state(layout)
    prepared_home = load_home_layer(layout, prepared_state.home_revision)

    assert prepared_home.revision != stale_revision
    prepared_document = json.loads(
        (prepared_home.revision_dir / "layer.json").read_text(encoding="utf-8")
    )
    assert prepared_document["schema"] == LAYER_SCHEMA
    assert load_agent_state(layout).revision == prepared_state.revision


def test_prepare_materializes_inline_caps_as_independent_files(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text(
        "agent alice\n\nprompt summarize:\n  Summarize this.\n",
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
    assert Path(entry.path) == (
        home_revision_dir
        / "files"
        / "caps"
        / "inline"
        / "agent"
        / "prompt"
        / "summarize.md"
    )
    assert Path(entry.path).read_text(encoding="utf-8") == "Summarize this."
    assert entry.read_content() == "Summarize this."
    assert entry.source.path == "agents/alice/agent.too"
    layer = json.loads((home_revision_dir / "layer.json").read_text(encoding="utf-8"))
    assert "content" not in layer["modules"][0]["here_caps"][0]

    (home / "agent.too").write_text(
        "agent alice\n\nprompt summarize:\n  Changed later.\n",
        encoding="utf-8",
    )
    assert entry.read_content() == "Summarize this."


def test_concurrent_processes_publish_one_root_and_home_revision(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")

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


def test_agent_check_lock_blocks_same_agent_process(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    started = tmp_path / "same-agent-started"
    layout = _layout(toolang_root)
    prepare_agent_state(layout)

    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        with _agent_check_lock(layout):
            future = executor.submit(
                _prepare_after_signal,
                (str(toolang_root), "alice", str(started)),
            )
            _wait_for_path(started)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.25)

        state_revision, _, _ = future.result(timeout=10)

    assert len(state_revision) == 64


def test_agent_check_lock_does_not_block_another_agent(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    for name in ("alice", "bob"):
        home = toolang_root / "agents" / name
        home.mkdir(parents=True)
        (home / "agent.too").write_text(f"agent {name}\n", encoding="utf-8")
    started = tmp_path / "other-agent-started"
    prepare_agent_state(_layout(toolang_root, "bob"))

    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        with _agent_check_lock(_layout(toolang_root, "alice")):
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
            f"agent {agent_name}\n",
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
        '[prompts]\nrewrite = { ref = "acme/rewrite" }\n',
        encoding="utf-8",
    )
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    content = {"value": b"---\ndescription: Rewrite\n---\nFirst.\n"}
    monkeypatch.setattr(
        cap_state, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_state, "_github_remote_exists", lambda _kind, _ref: True)

    def fake_materialized_files(*, relative_entry_path, kind, name, ref):
        del kind, name, ref
        return {str(relative_entry_path): content["value"]}

    monkeypatch.setattr(
        cap_state, "_remote_materialized_files", fake_materialized_files
    )

    first_root, _ = prepare_root_home(
        _layout(toolang_root),
    )
    content["value"] = b"---\ndescription: Rewrite\n---\nSecond.\n"
    cached_root, _ = prepare_root_home(
        _layout(toolang_root),
    )
    refreshed = refresh_agent_state(
        _layout(toolang_root),
    )
    second_root = load_root_layer(
        _layout(toolang_root),
        refreshed.root_revision,
    )

    assert cached_root.revision == first_root.revision
    assert (
        cached_root.revision_dir
        / "files"
        / "caps"
        / "configured"
        / "prompt"
        / "rewrite.md"
    ).read_bytes() == b"---\ndescription: Rewrite\n---\nFirst.\n"
    assert second_root.revision != first_root.revision
    assert second_root.resolutions != first_root.resolutions
    assert (
        second_root.caps[0].source.fingerprint != first_root.caps[0].source.fingerprint
    )
    assert (
        second_root.revision_dir
        / "files"
        / "caps"
        / "configured"
        / "prompt"
        / "rewrite.md"
    ).read_bytes() == content["value"]
    assert Path(second_root.caps[0].path) == (
        second_root.revision_dir
        / "files"
        / "caps"
        / "configured"
        / "prompt"
        / "rewrite.md"
    )


def test_local_change_reuses_unchanged_remote_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir()
    (toolang_root / "config.toml").write_text(
        '[prompts]\nrewrite = { ref = "acme/rewrite" }\n',
        encoding="utf-8",
    )
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    monkeypatch.setattr(
        cap_state, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_state, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_state,
        "_remote_materialized_files",
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
        cap_state,
        "_remote_materialized_files",
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


def test_declared_ref_change_refreshes_remote_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir()
    config = toolang_root / "config.toml"
    config.write_text(
        '[prompts]\nrewrite = { ref = "acme/rewrite" }\n',
        encoding="utf-8",
    )
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    monkeypatch.setattr(
        cap_state, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_state, "_github_remote_exists", lambda _kind, _ref: True)
    calls: list[str] = []

    def fake_materialized_files(*, relative_entry_path, kind, name, ref):
        del kind, name
        calls.append(ref)
        return {
            str(relative_entry_path): (
                f"---\ndescription: Rewrite\n---\n{ref}\n".encode()
            )
        }

    monkeypatch.setattr(
        cap_state, "_remote_materialized_files", fake_materialized_files
    )

    first, _ = prepare_root_home(
        _layout(toolang_root),
    )
    config.write_text(
        '[prompts]\nrewrite = { ref = "acme/rewrite-v2" }\n',
        encoding="utf-8",
    )
    second, _ = prepare_root_home(
        _layout(toolang_root),
    )

    assert len(calls) == 2
    assert first.revision != second.revision
    assert first.caps[0].source.declared_ref == "acme/rewrite"
    assert second.caps[0].source.declared_ref == "acme/rewrite-v2"
    assert second.resolutions[0].declared_ref == "acme/rewrite-v2"


def test_prepare_discovers_independent_flow_module_exports(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "flows"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    (flows / "research.too").write_text(
        "agic helper:\n  Research.\n\nflow:\n  settle helper\n",
        encoding="utf-8",
    )

    state = prepare_agent_state(_layout(toolang_root))

    assert [(item.kind, name) for name, item in state.runnables.items()] == [
        ("agic", "default"),
        ("flow", "research"),
    ]
    assert tuple(state.agics) == ("default",)
    assert tuple(state.flows) == ("research",)
    program = state.modules["_flow_research"]
    assert not hasattr(state, "program")
    assert tuple(state.modules) == ("_flow_research", "agent")
    assert state.module_sources["_flow_research"] == "flows/research.too"
    assert program.find_agic("helper") is not None
    assert "helper" not in state.runnables
    helper = state.module_runnable("_flow_research", "helper", kind="agic")
    assert helper is not None
    assert helper is program.find_agic("helper")
    exported = state.runnables["research"]
    assert state.runnable_modules["research"] == "_flow_research"
    assert exported is program.find_flow("main")
    default = state.runnables["default"]
    assert state.runnable_modules["default"] == "agent"
    assert default.name == "default"


def test_unnamed_flow_export_renames_with_its_file(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "flows"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    source = flows / "research.too"
    source.write_text("flow:\n  pass\n", encoding="utf-8")
    first = prepare_agent_state(_layout(toolang_root))

    public_module, public = resolve_state_runnable(first, "research", kind="flow")
    local = resolve_bound_runnable(first, "_flow_research", "flow:research")
    assert public_module == "_flow_research"
    assert public is local
    assert public.name == local.name == "main"

    source.rename(flows / "report.too")
    second = prepare_agent_state(_layout(toolang_root))

    assert "research" in first.runnables
    assert "research" not in second.runnables
    assert second.runnables["report"].name == "main"
    assert first.revision != second.revision


@pytest.mark.parametrize(
    "authored_path",
    (
        "flows/1report.too",
        "flows/bad name.too",
        "flows/con.too",
        f"flows/{'a' * 65}.too",
        "nested/flows/report.too",
    ),
)
def test_flow_module_names_reject_nonportable_source_paths(
    authored_path: str,
) -> None:
    with pytest.raises(ValueError):
        flow_module_name(authored_path)


def test_flow_module_names_reject_casefold_collisions() -> None:
    def source(path: str) -> ProgramSource:
        text = "flow:\n  pass\n"
        return ProgramSource(
            agent_name="alice",
            kind="flow",
            authored_path=path,
            source_path=f"agents/alice/{path}",
            source_text=text,
            digest=sha256(text.encode()).hexdigest(),
        )

    with pytest.raises(StatePreparationError) as raised:
        _validate_flow_source_names(
            (source("flows/Report.too"), source("flows/report.too"))
        )

    assert raised.value.diagnostics[0].code == "invalid-flow-filename"


@pytest.mark.parametrize(
    ("source", "layer"),
    [
        ("flow research:\n  settle missing\n", "program"),
        ("flow other:\n  pass\n", "flow-extension"),
        ("flow:\n  pass\n\nflow research:\n  pass\n", "flow-extension"),
    ],
)
def test_prepare_rejects_flow_module_at_the_correct_layer(
    tmp_path: Path,
    source: str,
    layer: str,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "flows"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    (flows / "research.too").write_text(source, encoding="utf-8")

    with pytest.raises(StatePreparationError) as raised:
        prepare_agent_state(_layout(toolang_root))

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.layer == layer
    assert diagnostic.module_kind == "flow"
    assert diagnostic.authored_path == "flows/research.too"


def test_prepare_rejects_public_runnable_collisions(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "flows"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text(
        "agent alice\n\nflow research:\n  pass\n",
        encoding="utf-8",
    )
    (flows / "research.too").write_text(
        "flow research:\n  pass\n",
        encoding="utf-8",
    )

    with pytest.raises(StatePreparationError) as raised:
        prepare_agent_state(_layout(toolang_root))

    assert raised.value.diagnostics[0].layer == "state-composition"


def test_module_here_caps_are_isolated_and_reload_from_cache(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "flows"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text(
        (
            "agent alice\n\n"
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
    flow_cap = next(
        item for item in state.caps_for("_flow_research") if item.name == "style"
    )
    assert agent_cap.read_content() == "Agent style."
    assert flow_cap.read_content() == "Flow style."
    assert agent_cap.path != flow_cap.path
    assert all(item.name != "only_agent" for item in state.caps_for("_flow_research"))

    flow_path.write_text("invalid", encoding="utf-8")
    loaded = load_home_layer(_layout(toolang_root), state.home_revision)
    assert loaded.modules["_flow_research"].find_flow("main") is not None


def test_flow_modules_can_reference_the_same_cap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "flows"
    flows.mkdir(parents=True)
    source = "with skill acme/reviewer\n\nflow:\n  pass\n"
    (flows / "one.too").write_text(source, encoding="utf-8")
    (flows / "two.too").write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        cap_state, "_github_repo_default_branch", lambda _owner, _repo: "main"
    )
    monkeypatch.setattr(cap_state, "_github_remote_exists", lambda _kind, _ref: True)
    monkeypatch.setattr(
        cap_state,
        "_remote_materialized_files",
        lambda *, relative_entry_path, **_kwargs: {
            relative_entry_path.as_posix(): (
                b"---\ndescription: Review\n---\nReview carefully.\n"
            )
        },
    )

    state = prepare_agent_state(_layout(toolang_root))

    one = state.module_caps["_flow_one"][0]
    two = state.module_caps["_flow_two"][0]
    assert one.ref == two.ref
    assert one.path != two.path
    assert one.read_content() == "Review carefully."
    assert two.read_content() == "Review carefully."
    assert not (home / "agent.too").exists()
