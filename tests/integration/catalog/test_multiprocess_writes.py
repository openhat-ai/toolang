from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from pathlib import Path

import frontmatter

from toolang.catalog.cap import AuthoredCaps, CapFile
from toolang.catalog.config import CapRef, ConfiguredCaps
from toolang.catalog.errors import CatalogConflictError
from toolang.catalog.job import AuthoredJobs, JobFile, JobKind
from toolang.common.layout import AgentLayout
from toolang.work.authoring import (
    allocate_authored_job_id,
    assign_missing_authored_job_ids,
    new_job_file,
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


def _create_same_job(directory: str, kind: JobKind) -> str:
    catalog = AuthoredJobs(Path(directory))
    content = frontmatter.dumps(frontmatter.Post("Run it.", None, id="same-id"))
    job = JobFile.parse(content, kind=kind)
    try:
        catalog.create(job)
    except CatalogConflictError:
        return "conflict"
    return "created"


def _assign_manual_job(root: str) -> None:
    assign_missing_authored_job_ids(AgentLayout.resident(Path(root), "alice"))


def _allocate_and_create_job(root: str) -> None:
    root_path = Path(root)
    job_id = allocate_authored_job_id(AgentLayout.resident(root_path, "alice"))
    AuthoredJobs(root_path / "agents" / "alice").create(
        new_job_file(
            kind="task",
            job_id=job_id,
            title="Generated",
            body="Run it.",
        )
    )


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
                ["task", "chore"],
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
    manual = root / "agents" / "alice" / "tasks" / "manual.md"
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

    jobs = AuthoredJobs(root / "agents" / "alice").list(kind="task")
    assert len(jobs) == 2
    assert len({job.id for job in jobs}) == 2
