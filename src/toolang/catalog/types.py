"""Shared authored-catalog vocabulary and scalar types."""

from typing import Literal


JobKind = Literal["task", "chore"]
JobStage = Literal["draft", "ready", "archived"]
CapKind = Literal["psyche", "skill", "service", "prompt"]

JOB_KINDS: tuple[JobKind, ...] = ("task", "chore")
JOB_STAGES: tuple[JobStage, ...] = ("draft", "ready", "archived")
CAP_KINDS: tuple[CapKind, ...] = ("psyche", "skill", "service", "prompt")
CAP_DIR_BY_KIND: dict[CapKind, str] = {
    "psyche": "psyches",
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
}
CAP_KIND_BY_DIR: dict[str, CapKind] = {
    directory: kind for kind, directory in CAP_DIR_BY_KIND.items()
}
CAP_DIRECTORY_NAMES = tuple(CAP_DIR_BY_KIND.values())
DEFAULT_CHORE_SCHEDULE = "FREQ=HOURLY;INTERVAL=1"
