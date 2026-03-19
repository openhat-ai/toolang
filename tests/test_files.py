from __future__ import annotations

from datetime import datetime, timezone

from toolang.files.config import ModelEntry, ModelsSection, ToolangConfig
from toolang.files.program import SyncedProgram
from toolang.files.sync_state import InputFingerprint, LockEntry, LockedAgentRefs, SyncState
from toolang_caps.models import CapEntry


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


def test_sync_state_round_trip(tmp_path) -> None:
    path = tmp_path / "alice.state.json"
    state = SyncState(
        synced_at=datetime(2026, 3, 19, 8, 0, 0, tzinfo=timezone.utc),
        source_file="alice.too",
        inputs={
            "alice.too": InputFingerprint(mtime_ns=1, size=42),
        },
        program=SyncedProgram(),
        refs=LockedAgentRefs(
            skills={
                "pdf-processing": LockEntry(
                    ref="briceyan/pdf-processing",
                    repo="briceyan/agent-skills",
                    path="skills/pdf-processing",
                    rev="abc123",
                )
            }
        ),
    )

    state.save(path)
    loaded = SyncState.load(path)

    assert loaded == state
