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
    shared_fingerprint: str
    private_fingerprint: str
    updated_at: str
    loaded_at: str
    enabled_components: tuple[str, ...]
    program: LiveProgram
    cap_entries: tuple[PreparedEntry, ...]
    job_entries: tuple[PreparedEntry, ...]
    status: str = "live"

    @property
    def enabled_features(self) -> tuple[str, ...]:
        return self.enabled_components

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
            "shared_fingerprint": self.shared_fingerprint,
            "private_fingerprint": self.private_fingerprint,
            "updated_at": self.updated_at,
            "loaded_at": self.loaded_at,
            "enabled_components": list(self.enabled_components),
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
    enabled_components: tuple[str, ...] | None = None,
    enabled_features: tuple[str, ...] | None = None,
) -> LiveState:
    """Load one live state from prepared locks."""

    components = enabled_components if enabled_components is not None else enabled_features
    if components is None:
        raise TypeError("enabled_components is required")
    return LiveState(
        fingerprint=prepared.fingerprint,
        shared_fingerprint=prepared.shared_lock.fingerprint,
        private_fingerprint=prepared.private_lock.fingerprint,
        updated_at=prepared.updated_at,
        loaded_at=datetime.now(timezone.utc).isoformat(),
        enabled_components=components,
        program=load_live_program(prepared.program),
        cap_entries=effective_cap_entries(prepared.shared_lock, prepared.private_lock),
        job_entries=active_job_entries(prepared.private_lock),
    )
