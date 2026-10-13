from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import sqlite3
from pathlib import Path

import frontmatter
import pytest

from toolang.catalog.cap import AuthoredCaps, CapFile
from toolang.catalog.config import CapRef, ConfiguredCaps
from toolang.catalog.errors import CatalogConflictError
from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.ids import (
    LOCAL_ID_FAMILY,
    decode_id,
)
from toolang.common.layout import AgentLayout
from toolang.execution.store import RunStore
from toolang.work.authoring import (
    allocate_authored_chore_name,
    new_chore_file,
)


def _wire_prompt(config_path: str, index: int) -> None:
    ConfiguredCaps(Path(config_path)).create(
        CapRef(
            kind="prompt",
            name=f"prompt-{index}",
            ref=f"https://example.test/prompts/{index}.md",
        )
    )


def _create_same_prompt(directory: str, body: str) -> str:
    catalog = AuthoredCaps(Path(directory))
    try:
        catalog.create(CapFile.parse(body, kind="prompt", name="root"))
    except CatalogConflictError:
        return "conflict"
    return "created"


def _create_same_job(directory: str, kind: str) -> str:
    catalog = AuthoredChores(Path(directory))
    content = frontmatter.dumps(frontmatter.Post("Run it.", None, id="same-id"))
    job = ChoreFile.parse(content, name="same-name")
    try:
        catalog.create(job)
    except CatalogConflictError:
        return "conflict"
    return "created"


def _assign_manual_job(root: str) -> None:
    from toolang.work.state import load_chores

    load_chores(AgentLayout.resident(Path(root), "alice"))


def _allocate_and_create_job(root: str) -> None:
    root_path = Path(root)
    job_id = allocate_authored_chore_name(AgentLayout.resident(root_path, "alice"))
    AuthoredChores(root_path / "agents" / "alice").create(
        new_chore_file(
            name=job_id,
            title="Generated",
            body="Run it.",
        )
    )


@pytest.mark.parametrize("agent_name", ["alice", "Alice", "bob"])
def test_authored_job_ids_use_the_layout_agent_name(
    tmp_path: Path, agent_name: str
) -> None:
    layout = AgentLayout.resident(tmp_path, agent_name)
    # Pin the saved tick beyond wall time so all agents allocate the same pair.
    tick = LOCAL_ID_FAMILY.tick_modulus - 1
    RunStore(layout.run_store).close()
    with sqlite3.connect(layout.run_store) as connection:
        connection.execute("INSERT INTO seqs VALUES ('local', ?, 6)", (tick,))

    job_id = allocate_authored_chore_name(layout)

    decoded = decode_id(job_id, family=LOCAL_ID_FAMILY, agent_name=agent_name)
    assert (decoded.tick, decoded.seq) == (tick, 7)


def test_configured_caps_preserves_all_concurrent_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        tuple(executor.map(_wire_prompt, [str(config_path)] * 12, range(12)))

    assert len(ConfiguredCaps(config_path).list()) == 12


def test_authored_caps_serializes_conflicting_creates(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        results = tuple(
            executor.map(
                _create_same_prompt,
                [str(tmp_path)] * 2,
                ["First.\n", "Second.\n"],
            )
        )

    assert sorted(results) == ["conflict", "created"]


def test_authored_jobs_serializes_global_id_uniqueness(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        results = tuple(
            executor.map(
                _create_same_job,
                [str(tmp_path)] * 2,
                ["chore", "chore"],
            )
        )

    assert sorted(results) == ["conflict", "created"]


def test_collection_write_lock_is_reentrant_for_external_transactions(
    tmp_path: Path,
) -> None:
    catalog = AuthoredCaps(tmp_path)

    with catalog.write_lock():
        created = catalog.create(
            CapFile.parse("First.\n", kind="prompt", name="review")
        )
        catalog.update(CapFile.parse("Second.\n", kind="prompt", name=created.name))

    saved = catalog.get("prompt", "review")
    assert saved is not None and saved.body == "Second."


def test_job_state_assignment_and_api_allocation_share_lock_order(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    manual = root / "agents" / "alice" / "chores" / "manual.md"
    manual.parent.mkdir(parents=True)
    manual.write_text("Manual.\n", encoding="utf-8")
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        futures = (
            executor.submit(_assign_manual_job, str(root)),
            executor.submit(_allocate_and_create_job, str(root)),
        )
        for future in futures:
            future.result(timeout=10)

    jobs = AuthoredChores(root / "agents" / "alice").list()
    assert len(jobs) == 2
    assert len({job.id for job in jobs}) == 2


def test_generated_chore_names_initialize_execution_storage(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    name = allocate_authored_chore_name(layout)
    assert len(name) == 8
    assert layout.run_store.is_file()
    assert not (layout.runtime / "ids.json").exists()
    with sqlite3.connect(layout.run_store) as connection:
        assert connection.execute("SELECT family, last_seq FROM seqs").fetchall() == [
            ("local", 0)
        ]
    readonly = RunStore(layout.run_store, read_only=True)
    try:
        assert readonly.list_runs() == []
    finally:
        readonly.close()
