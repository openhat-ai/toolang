"""Shared orchestration for creating authored jobs."""

from __future__ import annotations

import frontmatter

from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.types import DEFAULT_CHORE_SCHEDULE, JobKind, JobStage
from toolang.common.ids import LOCAL_ID_FAMILY, allocate_id
from toolang.common.layout import AgentLayout


def allocate_authored_job_id(
    layout: AgentLayout,
    *,
    catalog: AuthoredJobs | None = None,
) -> str:
    """Allocate one id that is unique across all authored job kinds and stages."""

    effective_catalog = catalog or AuthoredJobs(layout.home)
    with effective_catalog.write_lock():
        return allocate_id(
            layout.id_state,
            family=LOCAL_ID_FAMILY,
            exists=effective_catalog.contains_id,
        ).value


def assign_missing_authored_job_ids(
    layout: AgentLayout,
    *,
    catalog: AuthoredJobs | None = None,
    stage: JobStage | None = None,
) -> tuple[JobFile, ...]:
    """Assign and persist ids missing from manually authored job files."""

    effective_catalog = catalog or AuthoredJobs(layout.home)
    return effective_catalog.assign_missing_ids(
        lambda: allocate_authored_job_id(
            layout,
            catalog=effective_catalog,
        ),
        stage=stage,
    )


def new_job_file(
    *,
    kind: JobKind,
    job_id: str,
    title: str | None,
    body: str,
    schedule: str | None = None,
    stage: JobStage = "ready",
) -> JobFile:
    """Build one new authored job from caller-resolved values."""

    meta: dict[str, object] = {"id": job_id}
    if title is not None and title.strip():
        meta["title"] = title.strip()
    if kind == "chore":
        meta["schedule"] = schedule or DEFAULT_CHORE_SCHEDULE
    content = frontmatter.dumps(frontmatter.Post(body, None, **meta))
    return JobFile.parse(content, kind=kind, stage=stage)
