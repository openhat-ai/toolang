"""Retired Activity storage is rejected without rewriting execution facts."""

import asyncio
from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import TextPart
from toolang.execution.store import RunStore
from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.types import ThreadPrefix

LEGACY_SCHEMA = Path(__file__).parents[2] / "fixtures/execution/activity.sql"


def _facts(connection):
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if not row[0].startswith("activity_")
    ]
    return {
        table: [
            tuple(row)
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        ]
        for table in tables
    }


def _activity_objects(connection):
    return connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name LIKE 'activity_%' OR name LIKE 'idx_activity_%' ORDER BY name"
    ).fetchall()


@pytest.mark.parametrize("read_only", [False, True])
def test_legacy_activity_store_is_rejected_without_modifying_it(tmp_path, read_only):
    async def execute():
        harness = ExecutionHarness.create(
            tmp_path, source="flow example:\n  let result = Done\n", responses=[]
        )
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="example", primary=(TextPart("input"),)
                )
            )
        return harness.store.db_path

    path = asyncio.run(execute())
    with sqlite3.connect(path) as connection:
        assert not _activity_objects(connection)
        connection.executescript(LEGACY_SCHEMA.read_text())
        # Keep both execution facts and arbitrary extensions untouched.
        connection.execute("CREATE TABLE activity_extension (value BLOB)")
        connection.execute("INSERT INTO activity_extension VALUES (?)", (b"keep",))
        connection.execute("INSERT INTO activity_meta VALUES (1, 7, 'old', 0, 1)")
        connection.execute("PRAGMA user_version=58")
        before = _facts(connection)
        old_schema = _activity_objects(connection)

    before_bytes = path.read_bytes()
    with pytest.raises(RunStoreSchemaError):
        RunStore(path, read_only=read_only)
    assert path.read_bytes() == before_bytes
    with sqlite3.connect(path) as connection:
        # Inspect the original SQL values, including serialized typed refs,
        # content bytes, ID counters and persisted canonical cursors.
        assert _facts(connection) == before
        assert (
            connection.execute("SELECT value FROM activity_extension").fetchone()[0]
            == b"keep"
        )
        assert _activity_objects(connection) == old_schema
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 58
