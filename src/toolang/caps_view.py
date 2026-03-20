from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from toolang.files.sync_state import SyncState
from toolang.layout import agent_synced_caps_root, global_synced_caps_root, synced_caps_root
from toolang.prepared import PreparedAgent
from toolang_caps.models import InlineCapMeta, SkillMeta


class InlineCapView(BaseModel):
    kind: Literal["service", "prompt", "psyche"]
    name: str
    language: str | None = None
    path: str
    params: list[dict[str, Any]] = Field(default_factory=list)
    front_matter: dict[str, Any] | None = None


class SkillCapView(BaseModel):
    kind: Literal["skill"] = "skill"
    name: str
    path: str
    entry_path: str
    files: list[str] = Field(default_factory=list)
    ref: str | None = None
    repo: str | None = None
    source_path: str
    rev: str | None = None


class CapsView(BaseModel):
    skills: list[SkillCapView] = Field(default_factory=list)
    services: list[InlineCapView] = Field(default_factory=list)
    prompts: list[InlineCapView] = Field(default_factory=list)
    psyches: list[InlineCapView] = Field(default_factory=list)


def load_prepared_caps(prepared: PreparedAgent) -> CapsView:
    root = synced_caps_root(prepared.ref.agent_home)
    result = CapsView()
    if not root.exists():
        return result

    state = SyncState.load(prepared.sync_state_path)
    result.skills = _overlay_skills(
        _load_skills(
            global_synced_caps_root(prepared.ref.toolang_root),
            names=set(state.global_refs.skills),
        ),
        _load_skills(root, names=set(state.shared_refs.skills)),
        _load_skills(
            agent_synced_caps_root(prepared.ref.agent_home, prepared.ref.agent_name),
            names=set(state.agent_refs.skills),
        ),
    )
    result.services = _load_inline_caps(root, prepared, "service")
    result.prompts = _load_inline_caps(root, prepared, "prompt")
    result.psyches = _load_inline_caps(root, prepared, "psyche")
    return result


def _load_skills(root, *, names: set[str]) -> dict[str, SkillCapView]:
    skill_dir = root / "skills"
    items: dict[str, SkillCapView] = {}
    if not skill_dir.exists():
        return items
    for meta_path in sorted(skill_dir.glob("*.meta.json")):
        meta = SkillMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if meta.name not in names:
            continue
        items[meta.name] = SkillCapView(
            name=meta.name,
            path=meta.path,
            entry_path=meta.entry_path,
            files=list(meta.files),
            ref=meta.ref,
            repo=meta.repo,
            source_path=meta.source_path,
            rev=meta.rev,
        )
    return items


def _overlay_skills(*layers: dict[str, SkillCapView]) -> list[SkillCapView]:
    merged: dict[str, SkillCapView] = {}
    for layer in layers:
        merged.update(layer)
    return [merged[name] for name in sorted(merged)]


def _load_inline_caps(
    root,
    prepared: PreparedAgent,
    kind: Literal["service", "prompt", "psyche"],
) -> list[InlineCapView]:
    section = f"{kind}s" if kind != "psyche" else "psyches"
    directory = root / section
    expected = {
        declaration.name
        for declaration in prepared.program.declarations
        if declaration.kind == kind
    }
    items: list[InlineCapView] = []
    for meta_path in sorted(directory.glob("*.meta.json")):
        meta = InlineCapMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if meta.name not in expected:
            continue
        items.append(
            InlineCapView(
                kind=kind,
                name=meta.name,
                language=meta.language,
                path=meta.path,
                params=[param.model_dump(mode="python") for param in meta.params],
                front_matter=meta.front_matter,
            )
        )
    return items
