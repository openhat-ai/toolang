from pathlib import Path
import sqlite3

import pytest

from toolang.stats.records import STATS_STORE_SCHEMA, StatsRecords


@pytest.mark.parametrize("version", [0, 1, STATS_STORE_SCHEMA + 1])
def test_incompatible_stats_database_is_rejected_without_mutation(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "stats.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE original(value TEXT)")
        connection.execute("INSERT INTO original VALUES ('preserved')")
        connection.execute(f"PRAGMA user_version={version}")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="statistics"):
        StatsRecords(tmp_path / "runs.db", path).initialize()

    assert path.read_bytes() == before
    assert not path.with_name("stats.db-wal").exists()


def test_empty_stats_database_initializes_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "stats.db"
    records = StatsRecords(tmp_path / "runs.db", path)
    records.initialize()
    records.initialize()
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == STATS_STORE_SCHEMA
        )
        assert connection.execute("SELECT initialized FROM state").fetchone() == (0,)
