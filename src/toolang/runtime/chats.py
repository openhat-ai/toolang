from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(slots=True)
class ChatThread:
    id: str
    agent_uri: str
    agent_id: str
    agent_name: str
    title: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ChatMessage:
    id: int
    thread_id: str
    turn_id: str
    seq: int
    role: str
    origin: str
    channel: str | None
    sender: str
    text: str
    created_at: str
    meta: dict[str, Any]


@dataclass(slots=True)
class ChatTurn:
    thread_id: str
    turn_id: str
    messages: list[ChatMessage]
    started_at: str | None
    finished_at: str | None


class ChatStore:
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

    def ensure_thread(
        self,
        *,
        agent_uri: str,
        agent_id: str,
        agent_name: str,
        thread_id: str,
        title: str | None = None,
        at: str | None = None,
    ) -> ChatThread:
        now = at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO threads(
                    id, agent_uri, agent_id, agent_name, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    agent_uri = excluded.agent_uri,
                    agent_id = excluded.agent_id,
                    agent_name = excluded.agent_name,
                    title = COALESCE(excluded.title, threads.title),
                    updated_at = excluded.updated_at
                """,
                (thread_id, agent_uri, agent_id, agent_name, title, now, now),
            )
            row = self._conn.execute(
                """
                SELECT id, agent_uri, agent_id, agent_name, title, created_at, updated_at
                FROM threads
                WHERE id = ?
                """,
                (thread_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("thread upsert returned no row")
        return _thread_from_row(row)

    def append_message(
        self,
        *,
        agent_uri: str,
        agent_id: str,
        agent_name: str,
        thread_id: str,
        turn_id: str,
        role: str,
        origin: str,
        channel: str | None,
        sender: str,
        text: str,
        meta: dict[str, Any] | None = None,
        at: str | None = None,
    ) -> ChatMessage:
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported role: {role}")
        now = at or utc_now()
        self.ensure_thread(
            agent_uri=agent_uri,
            agent_id=agent_id,
            agent_name=agent_name,
            thread_id=thread_id,
            title=_thread_title(text),
            at=now,
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            next_seq = int(row["seq"]) + 1 if row is not None else 1
            meta_json = json.dumps(meta or {}, ensure_ascii=False, separators=(",", ":"))
            cursor = self._conn.execute(
                """
                INSERT INTO messages(
                    thread_id, turn_id, seq, role, origin, channel, sender, text, created_at, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    turn_id,
                    next_seq,
                    role,
                    origin,
                    channel,
                    sender,
                    text,
                    now,
                    meta_json,
                ),
            )
            self._conn.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ?",
                (now, thread_id),
            )
            row = self._conn.execute(
                """
                SELECT
                    id, thread_id, turn_id, seq, role, origin, channel, sender, text, created_at, meta_json
                FROM messages
                WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("message insert returned no row")
        return _message_from_row(row)

    def recent_messages(self, *, thread_id: str, limit: int = 20) -> list[ChatMessage]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    id, thread_id, turn_id, seq, role, origin, channel, sender, text, created_at, meta_json
                FROM messages
                WHERE thread_id = ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [_message_from_row(row) for row in reversed(rows)]

    def recent_openai_messages(self, *, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [self.to_openai_message(item) for item in self.recent_messages(thread_id=thread_id, limit=limit)]

    def get_thread(self, *, thread_id: str) -> ChatThread | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, agent_uri, agent_id, agent_name, title, created_at, updated_at
                FROM threads
                WHERE id = ?
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return _thread_from_row(row)

    def list_threads(self, *, agent_uri: str | None = None, limit: int = 50) -> list[ChatThread]:
        params: tuple[Any, ...]
        query = """
            SELECT id, agent_uri, agent_id, agent_name, title, created_at, updated_at
            FROM threads
        """
        if agent_uri is None:
            query += " ORDER BY updated_at DESC LIMIT ?"
            params = (limit,)
        else:
            query += " WHERE agent_uri = ? ORDER BY updated_at DESC LIMIT ?"
            params = (agent_uri, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_thread_from_row(row) for row in rows]

    def messages_for_turn(self, *, thread_id: str, turn_id: str) -> list[ChatMessage]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    id, thread_id, turn_id, seq, role, origin, channel, sender, text, created_at, meta_json
                FROM messages
                WHERE thread_id = ? AND turn_id = ?
                ORDER BY seq ASC
                """,
                (thread_id, turn_id),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    def get_turn(self, *, thread_id: str, turn_id: str) -> ChatTurn | None:
        messages = self.messages_for_turn(thread_id=thread_id, turn_id=turn_id)
        if not messages:
            return None
        return ChatTurn(
            thread_id=thread_id,
            turn_id=turn_id,
            messages=messages,
            started_at=messages[0].created_at,
            finished_at=messages[-1].created_at,
        )

    def recent_turns(self, *, thread_id: str, limit: int = 8) -> list[ChatTurn]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT turn_id, MIN(seq) AS first_seq
                FROM messages
                WHERE thread_id = ?
                GROUP BY turn_id
                ORDER BY first_seq DESC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        turn_ids = [str(row["turn_id"]) for row in reversed(rows)]
        turns: list[ChatTurn] = []
        for turn_id in turn_ids:
            turn = self.get_turn(thread_id=thread_id, turn_id=turn_id)
            if turn is not None:
                turns.append(turn)
        return turns

    @staticmethod
    def to_openai_message(message: ChatMessage) -> dict[str, Any]:
        return {
            "role": message.role,
            "content": message.text,
        }

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    agent_uri TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    channel TEXT,
                    sender TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_threads_agent ON threads(agent_uri, updated_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_thread_seq ON messages(thread_id, seq)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_turn ON messages(thread_id, turn_id)"
            )
            self._conn.commit()


def _thread_from_row(row: sqlite3.Row) -> ChatThread:
    return ChatThread(
        id=str(row["id"]),
        agent_uri=str(row["agent_uri"]),
        agent_id=str(row["agent_id"]),
        agent_name=str(row["agent_name"]),
        title=row["title"] if row["title"] is None else str(row["title"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _message_from_row(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        id=int(row["id"]),
        thread_id=str(row["thread_id"]),
        turn_id=str(row["turn_id"]),
        seq=int(row["seq"]),
        role=str(row["role"]),
        origin=str(row["origin"]),
        channel=row["channel"] if row["channel"] is None else str(row["channel"]),
        sender=str(row["sender"]),
        text=str(row["text"]),
        created_at=str(row["created_at"]),
        meta=json.loads(str(row["meta_json"])),
    )


def _thread_title(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    return stripped[:120]
