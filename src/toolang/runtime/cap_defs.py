"""Helpers for authored cap-definition mutations exposed by the runtime API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from toolang.caps.files import (
    delete_local_cap,
    local_cap_path,
    prune_empty_local_kind_dir,
    write_local_cap,
)
from toolang.concepts.caps import CapKind
from toolang.concepts.identity import AgentRef
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.errors import ToolangError
from toolang.program import Program

CapMutationScope = Literal["agent", "shared", "global"]
CapMutationSource = Literal["local", "remote"]


@dataclass(frozen=True, slots=True)
class CapMutationResult:
    kind: CapKind
    name: str
    scope: CapMutationScope
    source: CapMutationSource
    locator: str
    path: str | None = None
    ref: str | None = None
    detail: str = ""


def put_cap_definition(
    agent: AgentRef,
    *,
    kind: CapKind,
    name: str,
    scope: str,
    source: str | None,
    ref: str | None,
    content: str | None,
) -> CapMutationResult:
    """Create or replace one authored cap definition."""

    _require_mutable_agent(agent)
    normalized_scope = normalize_cap_scope(scope)
    normalized_source = _resolved_source(
        source,
        ref=ref,
        content=content,
    )

    if normalized_source == "remote":
        target = _scope_program_path(agent, normalized_scope)
        program = Program.load(target)
        if ref is None or not ref.strip():
            raise ToolangError("Remote cap definitions require a non-empty 'ref'.")
        changed = program.add_cap_ref(kind, ref.strip())
        program.save(target)
        return CapMutationResult(
            kind=kind,
            name=name,
            scope=normalized_scope,
            source="remote",
            locator=ref.strip(),
            path=str(target),
            ref=ref.strip(),
            detail=(
                f"{kind.title()} {name!r} was already attached from {ref.strip()!r}."
                if not changed
                else f"Attached remote {kind} {name!r} from {ref.strip()!r}."
            ),
        )

    if normalized_scope == "agent":
        raise ToolangError("Local cap files are only supported for shared or global scope.")
    if content is None:
        raise ToolangError("Local cap definitions require 'content'.")

    kind_dir = _scope_local_kind_dir(agent, kind, normalized_scope)
    target = local_cap_path(kind_dir, kind, name)
    write_local_cap(
        target,
        kind,
        content=content,
    )
    return CapMutationResult(
        kind=kind,
        name=name,
        scope=normalized_scope,
        source="local",
        locator=str(target),
        path=str(target),
        detail=f"Wrote local {kind} {name!r} at {target}.",
    )


def delete_cap_definition(
    agent: AgentRef,
    *,
    kind: CapKind,
    name: str,
    scope: str,
    source: str | None,
) -> CapMutationResult:
    """Delete one authored cap definition."""

    _require_mutable_agent(agent)
    normalized_scope = normalize_cap_scope(scope)
    normalized_source = _resolved_delete_source(
        agent,
        kind=kind,
        name=name,
        scope=normalized_scope,
        source=source,
    )

    if normalized_source == "remote":
        target = _scope_program_path(agent, normalized_scope)
        program = Program.load(target)
        changed = program.remove_cap_ref(
            kind,
            name,
            delete_when_empty=target.name == "agents.too",
        )
        if not changed:
            raise ToolangError(f"{kind.title()} {name!r} is not referenced in {target}.")
        if target.name == "agents.too" and not program.to_source():
            if target.exists():
                target.unlink()
        else:
            program.save(target)
        return CapMutationResult(
            kind=kind,
            name=name,
            scope=normalized_scope,
            source="remote",
            locator=str(target),
            path=str(target),
            detail=f"Removed remote {kind} {name!r} from {target}.",
        )

    if normalized_scope == "agent":
        raise ToolangError("Agent scope does not support local cap files.")
    kind_dir = _scope_local_kind_dir(agent, kind, normalized_scope)
    target = local_cap_path(kind_dir, kind, name)
    if not delete_local_cap(target):
        raise ToolangError(f"Local {kind} not found: {target}")
    prune_empty_local_kind_dir(kind_dir)
    return CapMutationResult(
        kind=kind,
        name=name,
        scope=normalized_scope,
        source="local",
        locator=str(target),
        path=str(target),
        detail=f"Deleted local {kind} {name!r} at {target}.",
    )


def normalize_cap_scope(scope: str) -> CapMutationScope:
    text = scope.strip().lower()
    if text == "home":
        text = "shared"
    if text not in {"agent", "shared", "global"}:
        raise ToolangError(f"Unsupported cap scope: {scope}")
    return cast(CapMutationScope, text)


def _resolved_source(
    source: str | None,
    *,
    ref: str | None,
    content: str | None,
) -> CapMutationSource:
    if source is not None:
        text = source.strip().lower()
        if text == "ref":
            text = "remote"
        if text not in {"local", "remote"}:
            raise ToolangError(f"Unsupported cap source: {source}")
        normalized = cast(CapMutationSource, text)
    elif ref is not None and ref.strip():
        normalized = "remote"
    elif content is not None:
        normalized = "local"
    else:
        raise ToolangError("Cap writes require either local content or a remote ref.")

    if normalized == "remote":
        if content is not None:
            raise ToolangError("Remote cap definitions do not accept local content.")
        if ref is None or not ref.strip():
            raise ToolangError("Remote cap definitions require a non-empty 'ref'.")
        return "remote"

    if ref is not None and ref.strip():
        raise ToolangError("Local cap definitions do not accept 'ref'.")
    return "local"


def _resolved_delete_source(
    agent: AgentRef,
    *,
    kind: CapKind,
    name: str,
    scope: CapMutationScope,
    source: str | None,
) -> CapMutationSource:
    if source is not None:
        text = source.strip().lower()
        if text == "ref":
            text = "remote"
        if text not in {"local", "remote"}:
            raise ToolangError(f"Unsupported cap source: {source}")
        return cast(CapMutationSource, text)

    candidates: list[CapMutationSource] = []
    if scope != "agent":
        kind_dir = _scope_local_kind_dir(agent, kind, scope)
        local_path = local_cap_path(kind_dir, kind, name)
        if local_path.exists():
            candidates.append("local")
    if _program_has_cap_ref(_scope_program_path(agent, scope), kind, name):
        candidates.append("remote")

    if not candidates:
        raise ToolangError(
            f"No authored {kind} definition named {name!r} was found in {scope} scope."
        )
    if len(candidates) > 1:
        raise ToolangError(
            "Multiple authored cap definitions match this request. Pass an explicit 'source'."
        )
    return candidates[0]


def _require_mutable_agent(agent: AgentRef) -> None:
    if agent.kind == "visiting":
        raise ToolangError(
            "Visiting agents do not support cap mutations through the runtime API."
        )


def _scope_program_path(agent: AgentRef, scope: CapMutationScope) -> Path:
    if scope == "global":
        return ToolangRoot.resolve(agent.root).global_source_path
    if scope == "shared":
        return AgentHome.resolve(agent.home).shared_source_path
    return agent.source


def _scope_local_kind_dir(
    agent: AgentRef,
    kind: CapKind,
    scope: Literal["shared", "global"],
) -> Path:
    if scope == "global":
        return ToolangRoot.resolve(agent.root).global_caps_dir(kind)
    return AgentHome.resolve(agent.home).shared_caps_dir(kind)


def _program_has_cap_ref(path: Path, kind: CapKind, name: str) -> bool:
    if not path.exists():
        return False
    program = Program.load(path)
    return any(_cap_name_from_ref(item.reference) == name for item in program.uses_by_kind(kind))


def _cap_name_from_ref(ref: str) -> str:
    return ref.rpartition("/")[2] or ref
