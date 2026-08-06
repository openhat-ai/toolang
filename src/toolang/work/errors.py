"""Work-owned exception types."""

from __future__ import annotations


class JobStoreSchemaError(RuntimeError):
    """Raised when one scheduler store cannot be opened without a downgrade."""

    def __init__(self, version: int, *, current: int, read_only: bool) -> None:
        self.version = version
        self.current = current
        self.read_only = read_only
        super().__init__(
            f"unsupported job store schema version: {version}; expected {current}"
        )
