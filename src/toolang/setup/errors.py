"""Structured diagnostics for rejected Agent Setup candidates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SetupDiagnostic:
    """One stable explanation for a rejected Setup candidate."""

    code: str
    message: str
