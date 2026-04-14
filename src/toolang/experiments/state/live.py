"""Live state loading from prepared locks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..caps import active_job_entries, effective_cap_entries
from .program import LiveProgram, load_live_program
from .prepared import PreparedEntry, PreparedState


@dataclass(frozen=True, slots=True)
class LiveState:
    """In-memory state used by the next run."""

    fingerprint: str
    global_fingerprint: str
    agent_fingerprint: str
    updated_at: str
    loaded_at: str
    enabled_loops: tuple[str, ...]
    program: LiveProgram
    cap_entries: tuple[PreparedEntry, ...]
    job_entries: tuple[PreparedEntry, ...]
    status: str = "live"

    @property
    def caps(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.cap_entries)

    @property
    def jobs(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.job_entries)

    def to_snapshot(self, *, operational_facts: dict[str, object]) -> dict[str, object]:
        """Return a JSON-friendly snapshot."""

        return {
            "fingerprint": self.fingerprint,
            "global_fingerprint": self.global_fingerprint,
            "agent_fingerprint": self.agent_fingerprint,
            "updated_at": self.updated_at,
            "loaded_at": self.loaded_at,
            "enabled_loops": list(self.enabled_loops),
            "program": self.program.to_snapshot(),
            "caps": list(self.caps),
            "jobs": list(self.jobs),
            "status": self.status,
            "queue_pending": operational_facts["queue_pending"],
            "active_runs": operational_facts["active_runs"],
            "completed_runs": operational_facts["completed_runs"],
        }


def load_live_state(
    prepared: PreparedState,
    *,
    enabled_loops: tuple[str, ...],
) -> LiveState:
    """Load one live state from prepared locks."""

    return LiveState(
        fingerprint=prepared.fingerprint,
        global_fingerprint=prepared.global_lock.fingerprint,
        agent_fingerprint=prepared.agent_lock.fingerprint,
        updated_at=prepared.updated_at,
        loaded_at=datetime.now(timezone.utc).isoformat(),
        enabled_loops=enabled_loops,
        program=load_live_program(prepared.program),
        cap_entries=effective_cap_entries(prepared.global_lock, prepared.agent_lock),
        job_entries=active_job_entries(prepared.agent_lock),
    )
