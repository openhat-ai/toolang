from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from toolang.agent.refs import ResolvedAgentRef
from toolang.ast import DeclBlock, ParamDecl, Program, SourceSpan
from toolang.cap_scopes import CapScopeSelection
from toolang.layout import agent_synced_caps_root, global_synced_caps_root, synced_caps_root
from toolang_caps.models import (
    InlineCapKind,
    InlineCapMeta,
    SkillMeta,
    TEXT_CAP_KINDS,
)

if TYPE_CHECKING:
    from toolang.agent.prepared import PreparedAgent


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


def build_effective_program(
    source_program: Program,
    ref: ResolvedAgentRef,
    *,
    cap_scopes: CapScopeSelection,
) -> Program:
    declarations = [
        declaration
        for declaration in source_program.declarations
        if declaration.kind not in TEXT_CAP_KINDS
    ]
    for kind in TEXT_CAP_KINDS:
        for declaration in _load_text_declarations(ref, kind, cap_scopes=cap_scopes):
            declarations.append(declaration)
    return Program(
        uses=list(source_program.uses),
        declarations=declarations,
        thunks=list(source_program.thunks),
    )


def load_prepared_caps(prepared: PreparedAgent) -> CapsView:
    return CapsView(
        skills=_load_skill_views(prepared.ref, cap_scopes=prepared.cap_scopes),
        services=_load_inline_views(prepared.ref, "service", cap_scopes=prepared.cap_scopes),
        prompts=_load_inline_views(prepared.ref, "prompt", cap_scopes=prepared.cap_scopes),
        psyches=_load_inline_views(prepared.ref, "psyche", cap_scopes=prepared.cap_scopes),
    )


def _load_skill_views(ref: ResolvedAgentRef, *, cap_scopes: CapScopeSelection) -> list[SkillCapView]:
    items = _overlay_layers(*_skill_scope_layers(ref, cap_scopes=cap_scopes))
    return [items[name] for name in sorted(items)]


def _load_skills(root) -> dict[str, SkillCapView]:
    skill_dir = root / "skills"
    items: dict[str, SkillCapView] = {}
    if not skill_dir.exists():
        return items
    for meta_path in sorted(skill_dir.glob("*.meta.json")):
        meta = SkillMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
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


def _load_inline_views(
    ref: ResolvedAgentRef,
    kind: Literal["service", "prompt", "psyche"],
    *,
    cap_scopes: CapScopeSelection,
) -> list[InlineCapView]:
    items = _overlay_layers(*_inline_scope_layers(ref, kind, cap_scopes=cap_scopes))
    return [
        InlineCapView(
            kind=kind,
            name=meta.name,
            language=meta.language,
            path=meta.path,
            params=[param.model_dump(mode="python") for param in meta.params],
            front_matter=meta.front_matter,
        )
        for _, meta in sorted(items.items())
    ]


def _load_text_declarations(
    ref: ResolvedAgentRef,
    kind: InlineCapKind,
    *,
    cap_scopes: CapScopeSelection,
) -> list[DeclBlock]:
    items = _overlay_layers(*_inline_scope_layers(ref, kind, cap_scopes=cap_scopes))
    return [
        DeclBlock(
            kind=kind,
            name=meta.name,
            language=meta.language,
            body=meta.raw_text,
            header_suffix=f"```{meta.language or ''}",
            span=SourceSpan(0),
            params=[
                ParamDecl(name=param.name, optional=param.optional)
                for param in meta.params
            ],
        )
        for _, meta in sorted(items.items())
    ]


def _load_inline_meta(root, kind: InlineCapKind) -> dict[str, InlineCapMeta]:
    kind_dir = root / f"{kind}s" if kind != "psyche" else root / "psyches"
    items: dict[str, InlineCapMeta] = {}
    if not kind_dir.exists():
        return items
    for meta_path in sorted(kind_dir.glob("*.meta.json")):
        meta = InlineCapMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        items[meta.name] = meta
    return items


def _overlay_layers(*layers: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        merged.update(layer)
    return merged


def _skill_scope_layers(
    ref: ResolvedAgentRef,
    *,
    cap_scopes: CapScopeSelection,
) -> list[dict[str, SkillCapView]]:
    layers: list[dict[str, SkillCapView]] = []
    if cap_scopes.include_global:
        layers.append(_load_skills(global_synced_caps_root(ref.toolang_root)))
    if cap_scopes.include_shared:
        layers.append(_load_skills(synced_caps_root(ref.agent_home)))
    layers.append(_load_skills(agent_synced_caps_root(ref.agent_home, ref.agent_name)))
    return layers


def _inline_scope_layers(
    ref: ResolvedAgentRef,
    kind: InlineCapKind,
    *,
    cap_scopes: CapScopeSelection,
) -> list[dict[str, InlineCapMeta]]:
    layers: list[dict[str, InlineCapMeta]] = []
    if cap_scopes.include_global:
        layers.append(_load_inline_meta(global_synced_caps_root(ref.toolang_root), kind))
    if cap_scopes.include_shared:
        layers.append(_load_inline_meta(synced_caps_root(ref.agent_home), kind))
    layers.append(_load_inline_meta(agent_synced_caps_root(ref.agent_home, ref.agent_name), kind))
    return layers
