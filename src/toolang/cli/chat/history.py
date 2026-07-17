"""Persistent input history for the terminal chat UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ChatInputHistoryStore:
    """Small JSONL-backed store for local chat input history."""

    path: Path
    limit: int = 500
    compact_limit: int = 2000
    compact_size_bytes: int = 1_000_000

    def load(self) -> list[str]:
        records = self._load_records()
        return [record["text"] for record in records[-self.limit :]]

    def append(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"created_at": _utc_now(), "text": text}
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")
        self._compact_if_needed()

    def _compact_if_needed(self) -> None:
        if not self.path.exists():
            return
        try:
            too_large = self.path.stat().st_size > self.compact_size_bytes
        except OSError:
            return
        records = self._load_records()
        if not too_large and len(records) <= self.compact_limit:
            return
        self._write_records(records[-self.limit :])

    def _load_records(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        records: list[dict[str, str]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            record = _parse_record(line)
            if record is not None:
                records.append(record)
        return records

    def _write_records(self, records: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(
            f"{json.dumps(record, ensure_ascii=False, separators=(',', ':'))}\n"
            for record in records
        )
        self.path.write_text(data, encoding="utf-8")


def _parse_record(line: str) -> dict[str, str] | None:
    try:
        value: Any = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    created_at = value.get("created_at")
    return {
        "created_at": created_at if isinstance(created_at, str) else "",
        "text": text,
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
