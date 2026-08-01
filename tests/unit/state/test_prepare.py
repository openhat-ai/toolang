from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import state as cap_state
from toolang.state.cache import load_root_prepared, prepared_version_dir
from toolang.state.prepare import (
    compose_prepared_state,
    prepare_agent_state,
    prepare_root_home,
    refresh_agent_state,
)


def _layout(root: Path, name: str = "alice") -> AgentLayout:
    return AgentLayout.resident(root, name)


def _prepare_versions_in_process(toolang_root: str) -> tuple[str, str, str]:
    state = prepare_agent_state(
        _layout(Path(toolang_root)),
        toolang_version="0.2.7",
    )
    return (
        state.version.hex(),
        state.root_version.hex(),
        state.home_version.hex(),
    )


def _prepare_agent_versions_in_process(
    request: tuple[str, str],
) -> tuple[str, str, str]:
    toolang_root, agent_name = request
    state = prepare_agent_state(
        _layout(Path(toolang_root), agent_name),
        toolang_version="0.2.7",
    )
    return (
        state.version.hex(),
        state.root_version.hex(),
        state.home_version.hex(),
    )


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

    root, prepared_home = prepare_root_home(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )

    assert [(entry.kind, entry.name) for entry in root.caps] == [("prompt", "review")]
    assert [(entry.kind, entry.name) for entry in prepared_home.caps] == [
        ("skill", "pdf")
    ]
    assert not (toolang_root / ".caps").exists()
    assert not (home / ".caps").exists()
    assert Path(root.caps[0].path) == (
        root.version_dir / "files" / "authored" / "prompts" / "review.md"
    )
    assert Path(prepared_home.caps[0].path) == (
        prepared_home.version_dir / "files" / "authored" / "skills" / "pdf" / "SKILL.md"
    )
    assert (
        prepared_home.version_dir
        / "files"
        / "authored"
        / "skills"
        / "pdf"
        / "notes.txt"
    ).read_text(encoding="utf-8") == "asset\n"
    assert prepared_home.program.span.line == 1
    assert root.config == {"models": {"default": "root"}}
    assert prepared_home.config == {"models": {"default": "home"}}
    assert (
        prepared_version_dir(_layout(toolang_root), "root", root.version)
        == root.version_dir
    )
    state = compose_prepared_state(
        root,
        prepared_home,
        program_source="agents/alice/agent.too",
    )
    assert state.root_version == root.version
    assert state.home_version == prepared_home.version
    assert state.toolang_version == "0.2.7"
    assert len(state.version) == 32
    assert [(entry.kind, entry.name) for entry in state.caps] == [
        ("prompt", "review"),
        ("skill", "pdf"),
    ]


def test_prepare_does_not_create_missing_agent_source(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    toolang_root.mkdir()

    with pytest.raises(FileNotFoundError, match="agent home not found"):
        prepare_agent_state(
            _layout(toolang_root),
            toolang_version="0.2.7",
        )

    assert not (toolang_root / "agents" / "alice").exists()
    assert not (toolang_root / ".state").exists()


def test_prepare_does_not_create_missing_agent_program(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)

    state = prepare_agent_state(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )

    assert state.program.span.line == 1
    assert not (home / "agent.too").exists()


def test_prepare_root_home_reuses_unchanged_versions(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")

    first = prepare_root_home(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )
    second = prepare_root_home(
        _layout(toolang_root),
        toolang_version="0.2.8",
    )

    assert second[0].version == first[0].version
    assert second[1].version == first[1].version
    state = prepare_agent_state(
        _layout(toolang_root),
        toolang_version="0.2.8",
    )
    assert state.root_version == second[0].version
    assert state.home_version == second[1].version


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
        toolang_version="0.2.7",
    )

    assert len(state.caps) == 1
    entry = state.caps[0]
    home_version_dir = prepared_version_dir(
        _layout(toolang_root),
        "home",
        state.home_version,
    )
    assert Path(entry.path) == (
        home_version_dir / "files" / "inline" / "prompts" / "summarize.md"
    )
    assert Path(entry.path).read_text(encoding="utf-8") == "Summarize this."
    assert entry.read_content() == "Summarize this."
    assert entry.source.path == "agents/alice/agent.too"
    prepared = json.loads(
        (home_version_dir / "prepared.json").read_text(encoding="utf-8")
    )
    assert "content" not in prepared["caps"][0]

    (home / "agent.too").write_text(
        "agent alice\n\nprompt summarize:\n  Changed later.\n",
        encoding="utf-8",
    )
    assert entry.read_content() == "Summarize this."


def test_concurrent_processes_publish_one_root_and_home_version(
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
                _prepare_versions_in_process,
                (str(toolang_root), str(toolang_root)),
            )
        )

    assert results[0] == results[1]
    assert len(tuple((toolang_root / ".state" / "versions").iterdir())) == 1
    assert (
        len(
            tuple((toolang_root / "agents" / "alice" / ".state" / "versions").iterdir())
        )
        == 1
    )


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
                _prepare_agent_versions_in_process,
                (
                    (str(toolang_root), "alice"),
                    (str(toolang_root), "bob"),
                ),
            )
        )

    assert alice[1] == bob[1]
    root_versions = toolang_root / ".state" / "versions"
    assert [path.name for path in root_versions.iterdir()] == [alice[1]]
    assert (toolang_root / "agents" / "alice" / ".state" / "current").is_file()
    assert (toolang_root / "agents" / "bob" / ".state" / "current").is_file()


def test_remote_refresh_changes_resolved_and_root_version(
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
        toolang_version="0.2.7",
    )
    content["value"] = b"---\ndescription: Rewrite\n---\nSecond.\n"
    cached_root, _ = prepare_root_home(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )
    refreshed = refresh_agent_state(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )
    second_root = load_root_prepared(
        _layout(toolang_root),
        refreshed.root_version,
    )

    assert cached_root.version == first_root.version
    assert (
        cached_root.version_dir / "files" / "wired" / "prompts" / "rewrite.md"
    ).read_bytes() == b"---\ndescription: Rewrite\n---\nFirst.\n"
    assert second_root.version != first_root.version
    assert second_root.resolutions != first_root.resolutions
    assert (
        second_root.caps[0].source.fingerprint != first_root.caps[0].source.fingerprint
    )
    assert (
        second_root.version_dir / "files" / "wired" / "prompts" / "rewrite.md"
    ).read_bytes() == content["value"]
    assert Path(second_root.caps[0].path) == (
        second_root.version_dir / "files" / "wired" / "prompts" / "rewrite.md"
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
        toolang_version="0.2.7",
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

    second, _ = prepare_root_home(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )

    assert second.version != first.version
    assert {entry.name for entry in second.caps} == {"local", "rewrite"}
    remote = next(entry for entry in second.caps if entry.name == "rewrite")
    assert Path(remote.path).read_text(encoding="utf-8").endswith("Remote content.\n")


def test_authored_ref_change_refreshes_remote_materialization(
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
        toolang_version="0.2.7",
    )
    config.write_text(
        '[prompts]\nrewrite = { ref = "acme/rewrite-v2" }\n',
        encoding="utf-8",
    )
    second, _ = prepare_root_home(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )

    assert len(calls) == 2
    assert first.version != second.version
    assert first.caps[0].source.authored_ref == "acme/rewrite"
    assert second.caps[0].source.authored_ref == "acme/rewrite-v2"
    assert second.resolutions[0].authored_ref == "acme/rewrite-v2"


def test_prepare_repairs_invalid_current_generation_document(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    first = prepare_agent_state(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )
    version_dir = prepared_version_dir(
        _layout(toolang_root),
        "home",
        first.home_version,
    )
    prepared_path = version_dir / "prepared.json"
    prepared_path.write_text("{invalid", encoding="utf-8")

    repaired = prepare_agent_state(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )

    assert repaired.home_version == first.home_version
    assert json.loads(prepared_path.read_text(encoding="utf-8"))["scope"] == "home"
    quarantined = version_dir.parent.glob(f".{first.home_version.hex()}.invalid-*")
    assert len(tuple(quarantined)) == 1


def test_prepare_repairs_missing_prepared_cap_file(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    prompt = home / "prompts" / "review.md"
    prompt.parent.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    prompt.write_text(
        "---\ndescription: Review\n---\nReview carefully.\n",
        encoding="utf-8",
    )
    first = prepare_agent_state(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )
    prepared_cap = Path(first.caps[0].path)
    prepared_cap.unlink()

    repaired = prepare_agent_state(
        _layout(toolang_root),
        toolang_version="0.2.7",
    )

    assert repaired.home_version == first.home_version
    assert Path(repaired.caps[0].path).read_text(encoding="utf-8") == (
        "---\ndescription: Review\n---\nReview carefully.\n"
    )
