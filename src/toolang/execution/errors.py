"""Execution-owned exception types."""

from __future__ import annotations

from collections.abc import Iterable


class RunStoreSchemaError(RuntimeError):
    """Raised when one run store cannot be opened by this runtime."""

    def __init__(
        self,
        version: int,
        *,
        current: int,
        supported: Iterable[int],
        read_only: bool,
    ) -> None:
        self.version = version
        self.current = current
        self.supported = tuple(sorted(set(supported)))
        self.read_only = read_only
        expected = ", ".join(str(item) for item in self.supported)
        super().__init__(
            f"unsupported run store schema version: {version}; expected {expected}"
        )
