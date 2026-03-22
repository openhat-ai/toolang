"""Internal path naming helpers for capability storage."""

from __future__ import annotations

from toolang.concepts.caps import CapKind

_SECTION_DIR_BY_CAP_KIND = {
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
    "psyche": "psyches",
}


def section_dir_name(kind: CapKind) -> str:
    """Return the directory name used for one stored cap kind."""

    return _SECTION_DIR_BY_CAP_KIND[kind]
