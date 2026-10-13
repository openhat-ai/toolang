"""Orchestration for creating pure authored chore definitions."""

from __future__ import annotations

from pathlib import Path
import frontmatter

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.common.ids import LOCAL_ID_FAMILY, allocate_id
from toolang.common.layout import AgentLayout
from toolang.execution.store import RunStore

from .chores import load_chores


def allocate_authored_chore_name(
    layout: AgentLayout, *, catalog: AuthoredChores | None = None
) -> str:
    """Allocate a filename when the caller did not supply a declaration name."""
    RunStore(layout.run_store).close()
    effective = catalog or AuthoredChores(layout.home)
    with effective.write_lock():
        return allocate_id(
            layout.run_store,
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


def create_chore(layout: AgentLayout, definition: ChoreFile) -> ChoreFile:
    """Create a home definition only when it can become effective."""
    _require_resident(layout)
    for chore in load_chores(layout):
        if chore.name == definition.name and chore.line is not None:
            raise CatalogConflictError(
                f"chore {chore.name} is defined at {chore.location}; edit that source"
            )
    return AuthoredChores(layout.home).create(definition)


def editable_chore(layout: AgentLayout, reference: str) -> ChoreFile:
    """Require the effective definition to be an editable home Markdown file."""
    _require_resident(layout)
    chore = next(
        (
            item
            for item in load_chores(layout)
            if reference in {item.id, item.name, item.ref}
        ),
        None,
    )
    if chore is None:
        catalog = AuthoredChores(layout.home)
        try:
            path = catalog.raw_path(reference)
        except CatalogNotFoundError:
            raise CatalogNotFoundError(f"chore not found: {reference}") from None
        raise CatalogConflictError(
            f"chore source is invalid at {path}; replace the complete file with state__update"
        )
    if chore.line is not None or chore.path.parent != layout.home / "chores":
        raise CatalogConflictError(
            f"chore {chore.name} is defined at {chore.location}; edit that source"
        )
    entry = AuthoredChores(layout.home).get(chore.name)
    if entry is None:
        raise CatalogNotFoundError(f"chore not found: {reference}")
    return entry


def editable_chore_path(layout: AgentLayout, reference: str) -> Path:
    """Protect inline winners while allowing raw repair of malformed home files."""
    _require_resident(layout)
    visible = next(
        (
            item
            for item in load_chores(layout)
            if reference in {item.id, item.name, item.ref, f"chores/{item.name}.md"}
        ),
        None,
    )
    if visible is not None and visible.line is not None:
        raise CatalogConflictError(
            f"chore {visible.name} is defined at {visible.location}; edit that source"
        )
    return AuthoredChores(layout.home).raw_path(reference)


def _require_resident(layout: AgentLayout) -> None:
    if layout.placement != "resident":
        raise CatalogConflictError(
            "chore changes require a resident agent; clone the source first"
        )
