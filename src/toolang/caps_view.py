from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from toolang.files.sync_state import SyncState
from toolang.layout import synced_caps_root
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
    ref: str
    repo: str
    source_path: str
    rev: str


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
    result.skills = _load_skills(root, names=set(state.refs.skills))
    result.services = _load_inline_caps(root, prepared, "service")
    result.prompts = _load_inline_caps(root, prepared, "prompt")
    result.psyches = _load_inline_caps(root, prepared, "psyche")
    return result


def _load_skills(root, *, names: set[str]) -> list[SkillCapView]:
    skill_dir = root / "skills"
    items: list[SkillCapView] = []
    for meta_path in sorted(skill_dir.glob("*.meta.json")):
        meta = SkillMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if meta.name not in names:
            continue
        items.append(
            SkillCapView(
                name=meta.name,
                path=meta.path,
                entry_path=meta.entry_path,
                files=list(meta.files),
                ref=meta.ref,
                repo=meta.repo,
                source_path=meta.source_path,
                rev=meta.rev,
            )
        )
    return items


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
