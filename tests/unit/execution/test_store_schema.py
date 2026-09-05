from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.store import RunStore
from toolang.execution.types import ContentRef


@pytest.mark.parametrize("schema_version", (28, 29, 30, 31, 32, 33, 34, 36))
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
    assert raised.value.current == 35
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
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 35
        columns = {
            table: {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("threads", "runs", "steps", "controls", "contents")
        }
        assert columns == {
            "threads": {
                "id",
                "origin",
                "peer",
                "created_at",
                "updated_at",
            },
            "runs": {
                "id",
                "parent",
                "thread",
                "control",
                "state",
                "output",
                "occur",
                "status",
                "error",
                "ejected_by",
                "created_at",
                "started_at",
                "finished_at",
            },
            "steps": {
                "id",
                "run",
                "path",
                "kind",
                "input",
                "state",
                "output",
                "occur",
                "given",
                "noted",
                "status",
                "error",
                "created_at",
                "started_at",
                "finished_at",
                "ejected_by",
            },
            "controls": {
                "id",
                "scope",
                "target",
                "index",
                "kind",
                "request",
                "status",
                "error",
                "timing",
                "payload",
                "created_at",
                "finished_at",
                "_claimed",
                "_revision",
            },
            "contents": {"id", "value"},
        }
        primary_keys = {
            table: tuple(
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
                if int(row[5]) > 0
            )
            for table in ("threads", "runs", "steps", "controls", "contents")
        }
        assert primary_keys == {
            "threads": ("id",),
            "runs": ("id",),
            "steps": ("id",),
            "controls": ("id",),
            "contents": ("id",),
        }
    finally:
        connection.close()


def test_content_store_deduplicates_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    value = b"durable content\x00"
    store = RunStore(path)

    first = store.put_content(value)
    second = store.put_content(value)
    store.close()

    assert first == second
    assert str(first).startswith("sha256_")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM contents").fetchone() == (1,)
    finally:
        connection.close()
    reopened = RunStore(path)
    try:
        assert reopened.get_content(first) == value
    finally:
        reopened.close()


def test_content_store_rejects_corrupted_bytes(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    ref = store.put_content(b"original")
    store.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE contents SET value = ? WHERE id = ?",
            (b"corrupted", str(ref)),
        )
        connection.commit()
    finally:
        connection.close()

    reopened = RunStore(path)
    try:
        with pytest.raises(ValueError, match=f"content is corrupted: {ref}"):
            reopened.get_content(ref)
        with pytest.raises(TypeError, match="requires a ContentRef"):
            reopened.get_content("sha256_" + "0" * 64)  # type: ignore[arg-type]
        assert reopened.get_content(ContentRef("sha256_" + "0" * 64)) is None
    finally:
        reopened.close()
