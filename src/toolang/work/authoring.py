"""Orchestration for creating pure authored chore definitions."""

from __future__ import annotations

import frontmatter

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.ids import LOCAL_ID_FAMILY, allocate_id
from toolang.common.layout import AgentLayout


def allocate_authored_chore_name(
    layout: AgentLayout, *, catalog: AuthoredChores | None = None
) -> str:
    """Allocate a filename when the caller did not supply a declaration name."""
    effective = catalog or AuthoredChores(layout.home)
    with effective.write_lock():
        return allocate_id(
            layout.id_state,
            family=LOCAL_ID_FAMILY,
            agent_name=layout.name,
            exists=effective.contains_name,
        ).value


def new_chore_file(
    *, name: str, title: str | None, body: str, schedule: str | None = None
) -> ChoreFile:
    meta: dict[str, object] = {}
    if title is not None and title.strip():
        meta["title"] = title.strip()
    if schedule is not None:
        meta["schedule"] = schedule
    content = frontmatter.dumps(frontmatter.Post(body, None, **meta))
    return ChoreFile.parse(content, name=name)
