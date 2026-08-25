from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.store import RunStore


def test_run_store_rejects_a_newer_schema_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO future_state VALUES ('preserved')")
    connection.execute("PRAGMA user_version=30")
    connection.commit()
    connection.close()

    with pytest.raises(RunStoreSchemaError) as raised:
        RunStore(path)

    assert raised.value.version == 30
    assert raised.value.current == 29
    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 30
        assert connection.execute("SELECT value FROM future_state").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()


@pytest.mark.parametrize("read_only", [False, True])
def test_run_store_rejects_schema_28_without_migrating(
    tmp_path: Path,
    read_only: bool,
) -> None:
    path = tmp_path / "runs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE old_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO old_state VALUES ('preserved')")
    connection.execute("PRAGMA user_version=28")
    connection.commit()
    connection.close()

    with pytest.raises(RunStoreSchemaError) as raised:
        RunStore(path, read_only=read_only)

    assert raised.value.version == 28
    assert raised.value.current == 29
    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 28
        assert connection.execute("SELECT value FROM old_state").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()
