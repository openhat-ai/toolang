from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from toolang.execution.errors import RunStoreSchemaError
from toolang.driver.chore.store import ChoreStore
from toolang.execution.store import RunStore
from toolang.execution.types import ContentRef


@pytest.mark.parametrize(
    "schema_version",
    [
        pytest.param(28, id="historical"),
        *[pytest.param(version, id=f"old-{version}") for version in range(51, 61)],
        pytest.param(62, id="future"),
    ],
)
@pytest.mark.parametrize("read_only", (False, True))
@pytest.mark.parametrize("store_type", (RunStore, ChoreStore))
def test_run_store_rejects_any_other_schema_without_modifying_it(
    tmp_path: Path,
    schema_version: int,
    read_only: bool,
    store_type: type[RunStore] | type[ChoreStore],
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
        store_type(path, read_only=read_only)

    assert raised.value.version == schema_version
    assert raised.value.current == 61
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
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 61
        columns = {
            table: {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("threads", "runs", "steps", "controls", "contents", "seqs")
        }
        assert columns == {
            "threads": {
                "horizon",
                "id",
                "origin",
                "created_at",
                "updated_at",
            },
            "runs": {
                "report",
                "_begin_cursor",
                "_end_cursor",
                "id",
                "parent",
                "thread",
                "control",
                "state",
                "output",
                "occur",
                "status",
                "error",
                "created_at",
                "started_at",
                "finished_at",
            },
            "steps": {
                "_begin_cursor",
                "_end_cursor",
                "preceded_by",
                "aborted_by",
                "id",
                "run",
                "path",
                "kind",
                "input",
                "state",
                "output",
                "occur",
                "spec",
                "report",
                "status",
                "error",
                "started_at",
                "finished_at",
            },
            "controls": {
                "_event_cursor",
                "triggered_by",
                "id",
                "scope",
                "target",
                "index",
                "kind",
                "request_id",
                "client_id",
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
            "seqs": {"family", "last_tick", "last_seq"},
        }
        primary_keys = {
            table: tuple(
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
                if int(row[5]) > 0
            )
            for table in ("threads", "runs", "steps", "controls", "contents", "seqs")
        }
        assert primary_keys == {
            "threads": ("id",),
            "runs": ("id",),
            "steps": ("id",),
            "controls": ("id",),
            "contents": ("id",),
            "seqs": ("family",),
        }
    finally:
        connection.close()


@pytest.mark.parametrize("family_name", ("local", "run"))
def test_missing_allocator_table_is_recreated_without_overwriting_records(
    tmp_path: Path, family_name: str
) -> None:
    from tests.support.execution_fixtures import accept_run
    from toolang.base.types.message import Message
    from toolang.common.ids import LOCAL_ID_FAMILY, RUN_ID_FAMILY, allocate_id

    family = LOCAL_ID_FAMILY if family_name == "local" else RUN_ID_FAMILY
    prefix = "chat" if family_name == "local" else "run"
    table = "threads" if family_name == "local" else "runs"
    path = tmp_path / "runs.db"
    now = family.bucket_started_at(100)

    def create_record(store: RunStore, identifier: str) -> None:
        if family_name == "local":
            store.create_thread(thread_id=identifier, origin="chat")
        else:
            accept_run(
                store,
                run_id=identifier,
                parent=None,
                thread="chat_target",
                input=Message.user("original"),
                context={},
                request_id=None,
                created_at=now.isoformat(),
            )

    store = RunStore(path)
    try:
        if family_name == "run":
            store.create_thread(thread_id="chat_target", origin="chat")
        first = allocate_id(path, family=family, agent_name="alice", now=now)
        create_record(store, f"{prefix}_{first.value}")
        threads, runs, controls = (
            store.list_threads(),
            store.list_runs(),
            store.list_controls(),
        )
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE seqs")

    reopened = RunStore(path)
    try:
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT * FROM seqs").fetchall() == []
        repeated = allocate_id(path, family=family, agent_name="alice", now=now)
        assert repeated.value == first.value
        with pytest.raises(ValueError, match="already exists"):
            create_record(reopened, f"{prefix}_{repeated.value}")
        with sqlite3.connect(path) as connection:
            with pytest.raises(
                sqlite3.IntegrityError, match="UNIQUE constraint failed"
            ):
                connection.execute(f"INSERT INTO {table} SELECT * FROM {table}")
        assert reopened.list_threads() == threads
        assert reopened.list_runs() == runs
        assert reopened.list_controls() == controls
        next_id = allocate_id(path, family=family, agent_name="alice", now=now)
        assert next_id.seq == 1
        if family_name == "run":
            reopened.finish_run(run_id=f"run_{first.value}", output=None)
        create_record(reopened, f"{prefix}_{next_id.value}")
    finally:
        reopened.close()


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


def test_run_store_concurrent_first_open(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    connect = sqlite3.connect
    barrier = threading.Barrier(8)

    def simultaneous_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        barrier.wait(timeout=10)
        return connection

    monkeypatch.setattr(sqlite3, "connect", simultaneous_connect)

    def open_store(path):
        store = RunStore(path)
        try:
            assert store.list_threads() == []
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        for index in range(4):
            path = tmp_path / f"runs-{index}.db"
            list(pool.map(open_store, [path] * 8))


@pytest.mark.parametrize("database_exists", [False, True])
def test_legacy_json_blocks_writing_without_import_or_deletion(
    tmp_path, database_exists
):
    path = tmp_path / "runs.db"
    if database_exists:
        RunStore(path).close()
    before = path.read_bytes() if database_exists else None
    legacy = path.with_name("ids.json")
    legacy.write_text("not even valid JSON")
    with pytest.raises(RuntimeError, match="requires a fresh runtime"):
        RunStore(path)
    assert legacy.read_text() == "not even valid JSON"
    assert (path.read_bytes() if path.exists() else None) == before
    if database_exists:
        readonly = RunStore(path, read_only=True)
        readonly.close()
        assert path.read_bytes() == before
