"""Durable conversation bindings and batch receipts in the execution store."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import sqlite3
from typing import TYPE_CHECKING

from .types import ConversationScope

if TYPE_CHECKING:
    from .store import RunStore


@dataclass(frozen=True, slots=True)
class TalkBatch:
    scope: ConversationScope
    after: str
    through: str
    payload: str

    @property
    def request_id(self) -> str:
        identity = json.dumps(
            [self.scope.backend, self.scope.conversation, self.after],
            separators=(",", ":"),
        )
        return "talk:" + sha256(identity.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class TalkReceipt:
    batch: TalkBatch
    run: str
    notice_attempted: bool


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS talk_bindings (
            backend TEXT NOT NULL,
            conversation TEXT NOT NULL,
            thread TEXT NOT NULL UNIQUE REFERENCES threads(id),
            PRIMARY KEY (backend, conversation)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS talk_batches (
            backend TEXT NOT NULL,
            conversation TEXT NOT NULL,
            after_cursor TEXT NOT NULL,
            through_cursor TEXT NOT NULL,
            payload TEXT NOT NULL,
            run TEXT NOT NULL UNIQUE REFERENCES runs(id),
            notice_attempted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (backend, conversation, after_cursor)
        )
    """)


class TalkStore:
    """Share the RunStore transaction; never maintain a second database."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def binding(self, scope: ConversationScope) -> str | None:
        with self.store.read_transaction():
            row = self.store._conn.execute(
                "SELECT thread FROM talk_bindings WHERE backend = ? AND conversation = ?",
                (scope.backend, scope.conversation),
            ).fetchone()
        return str(row["thread"]) if row is not None else None

    def bind(self, scope: ConversationScope, thread: str) -> bool:
        """Caller creates the thread and binding in the same write transaction."""
        with self.store.write_transaction():
            previous = self.binding(scope)
            if previous is not None:
                if previous != thread:
                    raise ValueError("Conversation is already bound to another thread")
                return False
            if self.store.get_thread(thread_id=thread) is not None:
                raise ValueError(f"Talk thread ID is already occupied: {thread}")
            self.store.create_thread(thread_id=thread)
            self.store._conn.execute(
                "INSERT INTO talk_bindings(backend, conversation, thread) VALUES (?, ?, ?)",
                (scope.backend, scope.conversation, thread),
            )
            return True

    def get(self, scope: ConversationScope, after: str) -> TalkReceipt | None:
        with self.store.read_transaction():
            row = self.store._conn.execute(
                "SELECT * FROM talk_batches WHERE backend = ? AND conversation = ? AND after_cursor = ?",
                (scope.backend, scope.conversation, after),
            ).fetchone()
        if row is None:
            return None
        return TalkReceipt(
            TalkBatch(scope, after, str(row["through_cursor"]), str(row["payload"])),
            str(row["run"]),
            bool(row["notice_attempted"]),
        )

    def accept(self, batch: TalkBatch, run: str, thread: str) -> None:
        """Join root admission so the cursor range can never lose its Run."""
        if self.binding(batch.scope) != thread:
            raise ValueError("Talk batch does not match its bound thread")
        self.store._conn.execute(
            "INSERT INTO talk_batches(backend, conversation, after_cursor, through_cursor, payload, run) VALUES (?, ?, ?, ?, ?, ?)",
            (
                batch.scope.backend,
                batch.scope.conversation,
                batch.after,
                batch.through,
                batch.payload,
                run,
            ),
        )

    def attempt_notice(self, run: str) -> bool:
        with self.store.write_transaction():
            if self.store.is_imported(run):
                return False
            return bool(
                self.store._conn.execute(
                    "UPDATE talk_batches SET notice_attempted = 1 WHERE run = ? AND notice_attempted = 0",
                    (run,),
                ).rowcount
            )
