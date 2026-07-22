"""Persistent file request state and input rendering."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import mimetypes
from pathlib import Path
import sqlite3
import threading
from typing import cast

from toolang.up import process as agents
from toolang.execution.types import RunStatus
from .records import FileRequestRecord
from .types import FileRequestStatus, FileSnapshot

_SCHEMA_VERSION = 2
_TEXT_PART_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".csv",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".mdx",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_IMAGE_PART_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
}
_AUDIO_PART_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".flac",
}
_VIDEO_PART_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
    ".mpeg",
    ".mpg",
    ".3gp",
    ".3g2",
    ".ogv",
}
_TEXT_MEDIA_TYPES = {
    "application/javascript",
    "application/json",
    "application/toml",
    "application/xml",
    "application/yaml",
    "application/x-ndjson",
    "application/x-sh",
    "application/x-yaml",
}
_FILE_THREAD_HASH_CHARS = 12


class FileRequestStore:
    """SQLite-backed file request claim and completion store."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path.as_posix(), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def claim(
        self,
        snapshot: FileSnapshot,
        *,
        run_id: str,
        thread_id: str,
        now: datetime | None = None,
    ) -> FileRequestRecord | None:
        """Persist and claim one unseen file fingerprint."""

        current = _utc(now)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO file_requests(
                    watch_root, relative_path, absolute_path, size, mtime_ns,
                    fingerprint, thread_id, status, run_id, error, first_seen_at,
                    processed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, NULL, ?, NULL, ?)
                """,
                (
                    snapshot.watch_root,
                    snapshot.relative_path,
                    snapshot.absolute_path,
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.fingerprint,
                    thread_id,
                    run_id,
                    _iso(current),
                    _iso(current),
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return None
            row = self._conn.execute(
                "SELECT * FROM file_requests WHERE request_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            self._conn.commit()
        return _record_from_row(row) if row is not None else None

    def finish_run(
        self,
        *,
        run_id: str,
        run_status: RunStatus,
        error: str | None = None,
        now: datetime | None = None,
    ) -> FileRequestRecord | None:
        """Mark one claimed file request with the terminal run status."""

        current = _utc(now)
        status: FileRequestStatus
        if run_status == "finished":
            status = "finished"
        elif run_status == "canceled":
            status = "canceled"
        else:
            status = "failed"
        processed_at = _iso(current) if status == "finished" else None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM file_requests WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                """
                UPDATE file_requests
                SET status = ?, error = ?, processed_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (status, error, processed_at, _iso(current), run_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM file_requests WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._conn.commit()
        return _record_from_row(updated) if updated is not None else None

    def list(self) -> tuple[FileRequestRecord, ...]:
        """Return all persisted file request rows in deterministic order."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM file_requests ORDER BY first_seen_at, request_id",
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION:
                self._conn.execute("DROP TABLE IF EXISTS file_requests")
            self._create_schema()
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_requests_status ON file_requests(status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_requests_path ON file_requests(watch_root, relative_path)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_requests_thread ON file_requests(thread_id)"
            )
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._conn.commit()

    def _create_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_requests (
                request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_root TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                absolute_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                status TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                error TEXT,
                first_seen_at TEXT NOT NULL,
                processed_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(watch_root, relative_path, fingerprint)
            )
            """
        )


def files_db_path(toolang_root: Path, agent_name: str) -> Path:
    """Return the file request database path."""

    return agents.agent_room(toolang_root, agent_name) / "files.db"


def open_file_request_store(toolang_root: Path, agent_name: str) -> FileRequestStore:
    """Open the file request store for one agent."""

    return FileRequestStore(files_db_path(toolang_root, agent_name))


def fingerprint_file(path: Path) -> str:
    """Return a sha256 fingerprint for one file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_thread_id(path: Path | str) -> str:
    """Return the stable file request thread id for one absolute source path."""

    resolved = Path(path).expanduser().resolve()
    digest = hashlib.sha256(resolved.as_posix().encode("utf-8")).hexdigest()
    return f"script_{digest[:_FILE_THREAD_HASH_CHARS]}"


def render_file_input(path: Path) -> tuple[str, list[dict[str, str]]]:
    """Render one file path using the same part conventions as invoke."""

    resolved = path.expanduser().resolve()
    part_type = path_part_type(resolved)
    if part_type == "text":
        text = resolved.read_text(encoding="utf-8")
        return text, [{"type": "text", "text": text, "path": str(resolved)}]
    return f"Attached {part_type}: {resolved}", [
        {"type": part_type, "path": str(resolved)}
    ]


def path_part_type(path: Path) -> str:
    """Return the multimodal part type inferred from a path extension."""

    ext = path.suffix.lower()
    if ext in _TEXT_PART_EXTENSIONS or _is_text_media_type(path):
        return "text"
    if ext in _IMAGE_PART_EXTENSIONS:
        return "image"
    if ext in _AUDIO_PART_EXTENSIONS:
        return "audio"
    if ext in _VIDEO_PART_EXTENSIONS:
        return "video"
    return "file"


def _is_text_media_type(path: Path) -> bool:
    media_type, _encoding = mimetypes.guess_type(path.as_posix())
    if media_type is None:
        return False
    return media_type.startswith("text/") or media_type in _TEXT_MEDIA_TYPES


def _record_from_row(row: sqlite3.Row) -> FileRequestRecord:
    return FileRequestRecord(
        request_id=int(row["request_id"]),
        watch_root=str(row["watch_root"]),
        relative_path=str(row["relative_path"]),
        absolute_path=str(row["absolute_path"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        fingerprint=str(row["fingerprint"]),
        thread_id=str(row["thread_id"]),
        status=cast(FileRequestStatus, str(row["status"])),
        run_id=str(row["run_id"]),
        error=str(row["error"]) if row["error"] is not None else None,
        first_seen_at=str(row["first_seen_at"]),
        processed_at=str(row["processed_at"])
        if row["processed_at"] is not None
        else None,
        updated_at=str(row["updated_at"]),
    )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat()
