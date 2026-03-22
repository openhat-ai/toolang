from __future__ import annotations

from datetime import datetime, timezone

from toolang.concepts.persisted.program import SyncedProgram
from toolang.concepts.persisted.sync_state import (
    InputFingerprint,
    LockEntry,
    LockedAgentRefs,
    SyncState,
)


def test_sync_state_round_trip(tmp_path) -> None:
    path = tmp_path / "alice.state.json"
    state = SyncState(
        synced_at=datetime(2026, 3, 19, 8, 0, 0, tzinfo=timezone.utc),
        source_file="alice.too",
        inputs={
            "alice.too": InputFingerprint(mtime_ns=1, size=42),
        },
        program=SyncedProgram(),
        agent_refs=LockedAgentRefs(
            skills={
                "pdf-processing": LockEntry(
                    ref="briceyan/pdf-processing",
                    repo="briceyan/agent-skills",
                    path="skills/pdf-processing",
                    rev="abc123",
                )
            }
        ),
        shared_refs=LockedAgentRefs(
            skills={
                "repo-search": LockEntry(path="skills/repo-search"),
            }
        ),
    )

    state.save(path)
    loaded = SyncState.load(path)

    assert loaded == state
