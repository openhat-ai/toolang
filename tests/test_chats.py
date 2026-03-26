from __future__ import annotations

import sqlite3
from pathlib import Path

from toolang.runtime.chats import ChatStore


def test_chat_store_backfills_legacy_message_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "chats.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE threads (
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
    conn.execute(
        """
        CREATE TABLE messages (
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
    conn.execute(
        """
        INSERT INTO threads(id, agent_uri, agent_id, agent_name, title, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "owner",
            "agent://alice/alice.too",
            "aliceid",
            "alice",
            "hello",
            "2026-03-26T10:00:00Z",
            "2026-03-26T10:00:00Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO messages(thread_id, turn_id, seq, role, origin, channel, sender, text, created_at, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "owner",
            "turn-1",
            1,
            "assistant",
            "chat",
            "api",
            "owner",
            "hello world",
            "2026-03-26T10:00:01Z",
            "{}",
        ),
    )
    conn.commit()
    conn.close()

    store = ChatStore(db_path)
    try:
        messages = store.recent_messages(thread_id="owner")
    finally:
        store.close()

    assert len(messages) == 1
    assert messages[0].id == "turn-1:assistant:1"
    assert len(messages[0].parts) == 1
    assert messages[0].parts[0].type == "text"
    assert messages[0].parts[0].text == "hello world"
