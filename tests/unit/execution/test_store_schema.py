from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.store import RunStore


@pytest.mark.parametrize("schema_version", (28, 29, 30, 31, 32, 34))
@pytest.mark.parametrize("read_only", (False, True))
def test_run_store_rejects_any_other_schema_without_modifying_it(
    tmp_path: Path,
    schema_version: int,
    read_only: bool,
) -> None:
    path = tmp_path / "runs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE historical_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO historical_state VALUES ('preserved')")
    connection.execute(f"PRAGMA user_version={schema_version}")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(RunStoreSchemaError) as raised:
        RunStore(path, read_only=read_only)

    assert raised.value.version == schema_version
    assert raised.value.current == 33
    assert raised.value.read_only is read_only
    assert path.read_bytes() == before
    connection = sqlite3.connect(path)
    try:
        assert (
            int(connection.execute("PRAGMA user_version").fetchone()[0])
            == schema_version
        )
        assert connection.execute("SELECT value FROM historical_state").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()


def test_run_store_rejects_a_nonempty_unversioned_database(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE historical_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO historical_state VALUES ('preserved')")
    connection.commit()
    connection.close()

    with pytest.raises(RunStoreSchemaError) as raised:
        RunStore(path)

    assert raised.value.version == 0
    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 0
        assert connection.execute("SELECT value FROM historical_state").fetchone() == (
            "preserved",
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'runs'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_run_store_opens_the_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"

    RunStore(path).close()
    reopened = RunStore(path)
    reopened.close()
    read_only = RunStore(path, read_only=True)
    read_only.close()

    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 33
        columns = {
            table: {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("threads", "runs", "steps")
        }
        assert "id" in columns["threads"]
        assert "thread_id" not in columns["threads"]
        assert "occur" in columns["runs"]
        assert "occurrence" not in columns["runs"]
        assert "occur" in columns["steps"]
        assert "occurrence" not in columns["steps"]
    finally:
        connection.close()
