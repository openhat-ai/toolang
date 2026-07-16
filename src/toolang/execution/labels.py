"""Human-readable execution labels."""

from __future__ import annotations

def executable_label(kind: str | None, name: str | None) -> str:
    """Return one compact executable label."""

    normalized_kind = (kind or "run").strip() or "run"
    normalized_name = (name or "").strip()
    return (
        f"{normalized_kind}:{normalized_name}"
        if normalized_name
        else normalized_kind
    )
