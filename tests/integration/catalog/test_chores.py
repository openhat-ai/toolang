from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading

import pytest

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.layout import AgentLayout
from toolang.lang.ast import Program
from toolang.catalog.chores import load_chores, merge_chores, inline_chores
from toolang.driver.chore.store import ChoreStore
from toolang.state.watcher import StateWatcher


def test_chore_watcher_current_returns_published_snapshot_without_rescanning(
    tmp_path: Path, monkeypatch
) -> None:
    layout = AgentLayout.resident(tmp_path / "toolang", "alice")
    layout.home.mkdir(parents=True)
    import asyncio

    watcher = StateWatcher(layout)
    published = asyncio.run(watcher.refresh())
    monkeypatch.setattr(
        "toolang.state.watcher.observe_home_source",
        lambda *_: pytest.fail("current() must not scan authored sources"),
    )
    assert watcher.current() is published
    assert not (layout.home / "chores").exists()
    assert not (layout.root / "chores").exists()


def test_inline_chore_shadows_home_and_root_without_changing_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    root_file = AuthoredChores(root).create(ChoreFile.parse("Root.", name="review"))
    AuthoredChores(home).create(ChoreFile.parse("Home.", name="review"))
    layout = AgentLayout.resident(root, "alice")
    (home_definition,) = load_chores(layout)
    assert home_definition.body == "Home."
    program = Program.from_source("chore review:\n  Inline.\n")
    effective = merge_chores(
        (home_definition,), inline_chores(program.chores, path=layout.program)
    )
    assert effective[root_file.id].body == "Inline."
    assert effective[root_file.id].thread_id == home_definition.thread_id
    assert (
        merge_chores((home_definition,), (home_definition,))[root_file.id]
        == home_definition
    )


def test_chore_store_claims_once_across_connections(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    AuthoredChores(root / "agents" / "alice").create(
        ChoreFile.parse("Review this.", name="review")
    )
    chores = merge_chores(
        load_chores(AgentLayout.resident(root, "alice")),
        inline_chores(
            Program.from_source("").chores, path=root / "agents/alice/agent.too"
        ),
    )
    path = root / "agents" / "alice" / ".runtime" / "runs.db"
    stores = (ChoreStore(path), ChoreStore(path))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for store in stores:
        store.reconcile(chores=chores, now=now)
    barrier = threading.Barrier(3)
    claims = []
    lock = threading.Lock()

    def claim(store: ChoreStore) -> None:
        barrier.wait()
        try:
            claimed = store.claim(
                chore=next(iter(chores.values())),
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
