from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from toolang.files.program import SyncedProgram


class InputFingerprint(BaseModel):
    mtime_ns: int
    size: int | None = None


class SyncState(BaseModel):
    version: int = 1
    synced_at: datetime
    source_file: str
    inputs: dict[str, InputFingerprint] = Field(default_factory=dict)
    program: SyncedProgram

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )

    def to_program(self):
        return self.program.to_program()
