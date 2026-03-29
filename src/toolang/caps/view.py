"""Scope-aware capability views used during runtime preparation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from toolang.concepts.caps import CapFrontmatter, CapKind, CapSidecar, ServiceFrontmatter
from toolang.concepts.identity import AgentRef
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.program import Program
from toolang.program.ast import DeclBlock, ParamDecl, SourceSpan

from .scope import CapScopeSelection

if TYPE_CHECKING:
    from toolang.agent.prepared import PreparedAgent


DECLARED_CAP_KINDS: tuple[Literal["service", "prompt", "psyche"], ...] = (
    "service",
    "prompt",
    "psyche",
)


class CapView(BaseModel):
    """Runtime view of one materialized non-skill capability."""

    kind: Literal["service", "prompt", "psyche"]
    name: str
    scope: Literal["agent", "shared", "global"]
    language: str | None = None
    path: str
    params: list[dict[str, Any]] = Field(default_factory=list)
    front_matter: CapFrontmatter | None = None
    content: str = ""
    ref: str | None = None
    repo: str | None = None
    source_path: str | None = None
    rev: str | None = None

    def service_catalog_item(self) -> dict[str, Any]:
        """Render one service-view item as a tool-runtime catalog entry."""

        if self.kind != "service":
            raise ValueError("service catalog entries can only be built from service caps")
        front_matter = (
            self.front_matter if isinstance(self.front_matter, ServiceFrontmatter) else None
        )
        return {
            "name": self.name,
            "transport": front_matter.transport if front_matter is not None else None,
            "target": front_matter.target if front_matter is not None else None,
            "description": front_matter.description if front_matter is not None else None,
            "command": front_matter.command if front_matter is not None else None,
            "args": list(front_matter.args) if front_matter is not None else [],
            "port": front_matter.port if front_matter is not None else None,
            "env_vars": (
                front_matter.required_env_vars() if front_matter is not None else []
            ),
        }


class SkillCapView(BaseModel):
    """Runtime view of one materialized skill cap."""

    kind: Literal["skill"] = "skill"
    name: str
    scope: Literal["agent", "shared", "global"]
    path: str
    entry_path: str
    files: list[str] = Field(default_factory=list)
    front_matter: CapFrontmatter | None = None
    content: str = ""
    ref: str | None = None
    repo: str | None = None
    source_path: str
    rev: str | None = None


class CapsView(BaseModel):
    """Effective capability set visible to one prepared agent."""

    skills: list[SkillCapView] = Field(default_factory=list)
    services: list[CapView] = Field(default_factory=list)
    prompts: list[CapView] = Field(default_factory=list)
    psyches: list[CapView] = Field(default_factory=list)


def build_effective_program(
    source_program: Program,
    ref: AgentRef,
    *,
    cap_scopes: CapScopeSelection,
) -> Program:
    declarations = [
        declaration
        for declaration in source_program.declarations
        if declaration.kind not in DECLARED_CAP_KINDS
    ]
    for kind in DECLARED_CAP_KINDS:
        for declaration in _load_cap_declarations(ref, kind, cap_scopes=cap_scopes):
            declarations.append(declaration)
    return Program(
        uses=list(source_program.uses),
        declarations=declarations,
        thunks=list(source_program.thunks),
    )


def load_prepared_caps(prepared: PreparedAgent) -> CapsView:
    return CapsView(
        skills=_load_skill_views(prepared.ref, cap_scopes=prepared.cap_scopes),
        services=_load_cap_views(prepared.ref, "service", cap_scopes=prepared.cap_scopes),
        prompts=_load_cap_views(prepared.ref, "prompt", cap_scopes=prepared.cap_scopes),
        psyches=_load_cap_views(prepared.ref, "psyche", cap_scopes=prepared.cap_scopes),
    )


def _load_skill_views(ref: AgentRef, *, cap_scopes: CapScopeSelection) -> list[SkillCapView]:
    items: dict[str, SkillCapView] = {}
    for scope, layer in _skill_scope_layers(ref, cap_scopes=cap_scopes):
        for name, meta in layer.items():
            items[name] = SkillCapView(
                scope=scope,
                name=meta.name,
                path=meta.path,
                entry_path=meta.entry_path or "",
                files=list(meta.asset_files),
                front_matter=meta.front_matter,
                content=meta.raw_text,
                ref=meta.ref,
                repo=meta.repo,
                source_path=meta.source_path or "",
                rev=meta.rev,
            )
    return [items[name] for name in sorted(items)]


def _load_skills(root) -> dict[str, CapSidecar]:
    skill_dir = root / "skills"
    items: dict[str, CapSidecar] = {}
    if not skill_dir.exists():
        return items
    for meta_path in sorted(skill_dir.glob("*.meta.json")):
        meta = CapSidecar.model_validate_json(meta_path.read_text(encoding="utf-8"))
        items[meta.name] = meta
    return items


def _load_cap_views(
    ref: AgentRef,
    kind: Literal["service", "prompt", "psyche"],
    *,
    cap_scopes: CapScopeSelection,
) -> list[CapView]:
    items: dict[str, CapView] = {}
    for scope, layer in _cap_scope_layers(ref, kind, cap_scopes=cap_scopes):
        for name, meta in layer.items():
            items[name] = CapView(
                kind=kind,
                name=meta.name,
                scope=scope,
                language=meta.language,
                path=meta.path,
                params=[param.model_dump(mode="python") for param in meta.params],
                front_matter=meta.front_matter,
                content=meta.raw_text,
                ref=meta.ref,
                repo=meta.repo,
                source_path=meta.source_path,
                rev=meta.rev,
            )
    return [items[name] for name in sorted(items)]


def _load_cap_declarations(
    ref: AgentRef,
    kind: CapKind,
    *,
    cap_scopes: CapScopeSelection,
) -> list[DeclBlock]:
    items: dict[str, CapSidecar] = {}
    for _, layer in _cap_scope_layers(ref, kind, cap_scopes=cap_scopes):
        items.update(layer)
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


def _load_cap_meta(root, kind: CapKind) -> dict[str, CapSidecar]:
    kind_dir = root / f"{kind}s" if kind != "psyche" else root / "psyches"
    items: dict[str, CapSidecar] = {}
    if not kind_dir.exists():
        return items
    for meta_path in sorted(kind_dir.glob("*.meta.json")):
        meta = CapSidecar.model_validate_json(meta_path.read_text(encoding="utf-8"))
        items[meta.name] = meta
    return items


def _skill_scope_layers(
    ref: AgentRef,
    *,
    cap_scopes: CapScopeSelection,
) -> list[tuple[Literal["agent", "shared", "global"], dict[str, CapSidecar]]]:
    layers: list[tuple[Literal["agent", "shared", "global"], dict[str, CapSidecar]]] = []
    if cap_scopes.include_global:
        layers.append(
            ("global", _load_skills(ToolangRoot.resolve(ref.root).global_synced_caps_root))
        )
    if cap_scopes.include_shared:
        layers.append(("shared", _load_skills(AgentHome.resolve(ref.home).synced_caps_root)))
    layers.append(
        ("agent", _load_skills(AgentHome.resolve(ref.home).room(ref.name).synced_caps_root))
    )
    return layers


def _cap_scope_layers(
    ref: AgentRef,
    kind: CapKind,
    *,
    cap_scopes: CapScopeSelection,
) -> list[tuple[Literal["agent", "shared", "global"], dict[str, CapSidecar]]]:
    layers: list[tuple[Literal["agent", "shared", "global"], dict[str, CapSidecar]]] = []
    if cap_scopes.include_global:
        layers.append(
            (
                "global",
                _load_cap_meta(ToolangRoot.resolve(ref.root).global_synced_caps_root, kind),
            )
        )
    if cap_scopes.include_shared:
        layers.append(
            (
                "shared",
                _load_cap_meta(AgentHome.resolve(ref.home).synced_caps_root, kind),
            )
        )
    layers.append(
        (
            "agent",
            _load_cap_meta(AgentHome.resolve(ref.home).room(ref.name).synced_caps_root, kind),
        )
    )
    return layers
