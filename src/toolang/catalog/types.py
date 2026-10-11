"""Shared authored-catalog vocabulary and scalar types."""

from typing import Literal


CapKind = Literal["psyche", "skill", "service", "prompt"]

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
