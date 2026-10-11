from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading

import frontmatter
import pytest

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.layout import AgentLayout
from toolang.lang.ast import Program
from toolang.work.state import load_chores, merge_chores, program_chores
from toolang.work.store import ChoreStore
from toolang.work.watcher import ChoreWatcher


def _job(kind, job_id: str, body: str, *, stage="ready") -> ChoreFile:
    content = frontmatter.dumps(frontmatter.Post(body, None, id=job_id))
    return ChoreFile.parse(content, name=job_id)


def test_job_watcher_current_returns_published_snapshot_without_rescanning(
    tmp_path: Path, monkeypatch
) -> None:
    watcher = ChoreWatcher(AgentLayout.resident(tmp_path / "toolang", "alice"))
    published = watcher.current()
    monkeypatch.setattr(
        "toolang.work.watcher.load_chores",
        lambda *_args, **_kwargs: pytest.fail("current() must not scan authored jobs"),
    )

    assert watcher.current() is published


def test_inline_chore_shadows_home_and_root_without_changing_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    root_file = AuthoredChores(root).create(_job("chore", "review", "Root."))
    AuthoredChores(home).create(_job("chore", "review", "Home."))
    layout = AgentLayout.resident(root, "alice")
    (home_definition,) = load_chores(layout)
    assert home_definition.body == "Home."
    program = Program.from_source("chore review:\n  Inline.\n")
    effective = merge_chores((home_definition,), program_chores(program))
    assert effective[root_file.id].body == "Inline."
    assert effective[root_file.id].thread_id == home_definition.thread_id
    assert (
        merge_chores((home_definition,), (home_definition,))[root_file.id]
        == home_definition
    )


def test_job_store_claims_once_across_connections(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    AuthoredChores(root / "agents" / "alice").create(
        _job("task", "review", "Review this.")
    )
    jobs = merge_chores(
        load_chores(AgentLayout.resident(root, "alice")),
        program_chores(Program.from_source("")),
    )
    path = root / "agents" / "alice" / ".runtime" / "jobs.db"
    stores = (ChoreStore(path), ChoreStore(path))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for store in stores:
        store.reconcile(chores=jobs, now=now)
    barrier = threading.Barrier(3)
    claims = []
    lock = threading.Lock()

    def claim(store: ChoreStore) -> None:
        barrier.wait()
        try:
            claimed = store.claim(
                chore=next(iter(jobs.values())),
                trigger="source",
                run_id=f"run_{id(store)}",
                now=now,
            )
        except ValueError:
            claimed = None
        with lock:
            claims.append(claimed)

    threads = [threading.Thread(target=claim, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    for store in stores:
        store.close()

    assert len([claim for claim in claims if claim is not None]) == 1
