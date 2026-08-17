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
    connection.execute("PRAGMA user_version=24")
    connection.commit()
    connection.close()

    with pytest.raises(RunStoreSchemaError) as raised:
        RunStore(path)

    assert raised.value.version == 24
    assert raised.value.current == 23
    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 24
        assert connection.execute("SELECT value FROM future_state").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()
