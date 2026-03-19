from __future__ import annotations

from datetime import datetime, timezone

from toolang.files import (
    CapEntry,
    InputFingerprint,
    LockEntry,
    ModelEntry,
    ModelsSection,
    SyncState,
    ToolangConfig,
    ToolangLock,
)


def test_toolang_config_round_trip(tmp_path) -> None:
    path = tmp_path / "toolang.toml"
    config = ToolangConfig(
        skills={"pdf-processing": CapEntry(ref="briceyan/pdf-processing")},
        prompts={"release-notes": CapEntry(path="prompts/release-notes.md")},
        models=ModelsSection(
            default=["gpt-5.3"],
            named={
                "gpt-5.3": ModelEntry(
                    provider="openai",
                    model="gpt-5.3",
                    api_key_env="OPENAI_API_KEY",
                )
            },
        ),
    )

    config.save(path)
    loaded = ToolangConfig.load(path)

    assert loaded == config


def test_toolang_lock_round_trip(tmp_path) -> None:
    path = tmp_path / "toolang.lock"
    lock = ToolangLock(
        skills={
            "pdf-processing": LockEntry(
                ref="briceyan/pdf-processing",
                resolved="github.com/briceyan/agent-skills@abc123:skills/pdf-processing",
            )
        }
    )

    lock.save(path)
    loaded = ToolangLock.load(path)

    assert loaded == lock


def test_sync_state_round_trip(tmp_path) -> None:
    path = tmp_path / "sync.json"
    state = SyncState(
        synced_at=datetime(2026, 3, 19, 8, 0, 0, tzinfo=timezone.utc),
        source_file="alice.too",
        agent_room=".toolang/agent/alice/",
        synced_caps=".toolang/.sync/",
        inputs={
            "alice.too": InputFingerprint(mtime_ns=1, size=42),
        },
    )

    state.save(path)
    loaded = SyncState.load(path)

    assert loaded == state
