"""Structured diagnostics for rejected Agent State candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .types import ProgramKind

StateValidationLayer = Literal["program", "flow-extension", "state-composition"]


@dataclass(frozen=True, slots=True)
class StateDiagnostic:
    """One stable explanation for a rejected state candidate."""

    layer: StateValidationLayer
    module_kind: ProgramKind
    authored_path: str
    line: int | None
    code: str
    message: str


class StatePreparationError(ValueError):
    """Reject one candidate while retaining structured diagnostics."""

    def __init__(self, *diagnostics: StateDiagnostic) -> None:
        if not diagnostics:
            raise ValueError("state preparation error requires a diagnostic")
        self.diagnostics = tuple(diagnostics)
        super().__init__("; ".join(item.message for item in diagnostics))
