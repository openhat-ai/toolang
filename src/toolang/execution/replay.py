"""Create independent execution snapshots with durable imported ownership."""

from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from .store import RunStore


def snapshot_history(
    source: Path,
    destination: Path,
    *,
    source_owner: str,
    source_root: Path,
    request_id: str,
    source_run: str | None = None,
    source_thread: str | None = None,
) -> None:
    """Install only a complete, migrated seed; never copy database/WAL files."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(f".seed-{uuid4().hex}.db")
    # Validate supported schema before SQLite creates the destination seed.
    checked = RunStore(source, read_only=True)
    checked.close()
    try:
        with closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        ) as origin:
            with closing(sqlite3.connect(stage)) as target:
                origin.backup(target)
        seed = RunStore(stage)
        try:
            with seed.write_transaction():
                from .inspection.history import RunHistory

                if source_run is not None:
                    if not source_run.startswith("run_"):
                        thread = RunHistory(seed).get_thread(source_run, run_limit=0)
                        if thread is None or thread.latest_run is None:
                            raise ValueError("rerun source thread has no visible run")
                        source_run = thread.latest_run.id
                    run = seed.get_run(run_id=source_run)
                    if run is None or run.status not in {
                        "succeeded",
                        "failed",
                        "canceled",
                    }:
                        raise ValueError(
                            "rerun source is not terminal in the captured snapshot"
                        )
                    source_thread = str(run.thread)
                elif source_thread == "latest":
                    threads = RunHistory(seed).list_threads(limit=1)
                    if not threads:
                        raise ValueError("no thread to resume")
                    source_thread = threads[0].id
                elif source_thread is not None and source_thread.startswith("run_"):
                    run = seed.get_run(run_id=source_thread)
                    if run is None:
                        raise ValueError("continuation run not found")
                    source_thread = str(run.thread)
                source_head = None
                if source_thread is not None:
                    seed.require_idle_thread(source_thread)
                    source_head = str(seed.thread_view(source_thread).head)
                # Unaccepted source intents are coordination, not imported work.
                seed._conn.execute(
                    "DELETE FROM replay_provenance WHERE destination_run IS NULL"
                )
                seed._conn.execute(
                    "INSERT OR IGNORE INTO imported_runs(run,owner) SELECT id,? FROM runs",
                    (source_owner,),
                )
                seed._conn.execute(
                    "INSERT INTO replay_provenance(request_id,source_root,source_owner,source_run,source_thread,source_head) VALUES(?,?,?,?,?,?)",
                    (
                        request_id,
                        str(source_root),
                        source_owner,
                        source_run,
                        source_thread,
                        source_head,
                    ),
                )
            seed._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            seed.close()
        if destination.exists():
            raise FileExistsError(destination)
        stage.rename(destination)
    finally:
        stage.unlink(missing_ok=True)
        stage.with_name(stage.name + "-wal").unlink(missing_ok=True)
        stage.with_name(stage.name + "-shm").unlink(missing_ok=True)
        stage.with_name(stage.name + ".init.lock").unlink(missing_ok=True)
