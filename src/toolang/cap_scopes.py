from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CapScope = Literal["agent", "shared", "global"]


@dataclass(frozen=True, slots=True)
class CapScopeSelection:
    include_shared: bool = True
    include_global: bool = True

    def includes(self, scope: CapScope) -> bool:
        if scope == "agent":
            return True
        if scope == "shared":
            return self.include_shared
        return self.include_global

    def labels(self) -> tuple[CapScope, ...]:
        labels: list[CapScope] = ["agent"]
        if self.include_shared:
            labels.append("shared")
        if self.include_global:
            labels.append("global")
        return tuple(labels)
