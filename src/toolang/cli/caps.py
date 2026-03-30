from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer

from toolang.agent.prepared import prepare_agent
from toolang.caps.github import (
    fetch_github_artifact,
    resolve_github_cap_ref,
    validate_github_cap_ref,
)
from toolang.caps import load_prepared_caps
from toolang.caps.files import (
    create_local_cap,
    delete_local_cap,
    install_local_cap,
    local_cap_path,
    prune_empty_local_kind_dir,
)
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.caps import CapKind
from toolang.errors import ToolangError
from toolang.program import Program
from toolang.tools.plugins.service_use import start_service_auth

from .support import _resolve_cli_agent, _toolang_root

SourceScope = Annotated[
    Literal["agent", "shared", "global"],
    typer.Option(help="Target scope"),
]
LocalScope = Annotated[
    Literal["shared", "global"],
    typer.Option(help="Target local scope"),
]
ScopedAgent = Annotated[
    str | None,
    typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
]
LocalScopedAgent = Annotated[
    str | None,
    typer.Option("--agent", help="Agent selector used to resolve shared scope"),
]


@dataclass(frozen=True, slots=True)
class CapSourceTarget:
    toolang_root: Path
    agent_home: Path | None
    agent_name: str | None
    source_path: Path


@dataclass(frozen=True, slots=True)
class CapLocalTarget:
    toolang_root: Path
    agent_home: Path | None
    kind: CapKind
    kind_dir: Path
    cap_path: Path


@dataclass(frozen=True, slots=True)
class InferredAgentContext:
    agent_home: Path
    agent_name: str | None


@dataclass(frozen=True, slots=True)
class CapCommandSpec:
    kind: CapKind
    title: str


CAP_COMMAND_SPECS = (
    CapCommandSpec(kind="psyche", title="Psyche"),
    CapCommandSpec(kind="skill", title="Skill"),
    CapCommandSpec(kind="service", title="Service"),
    CapCommandSpec(kind="prompt", title="Prompt"),
)


def register_cap_commands(app: typer.Typer) -> None:
    for spec in CAP_COMMAND_SPECS:
        cap_app, local_app = _build_cap_apps(spec)
        cap_app.add_typer(local_app, name="local", no_args_is_help=True)
        app.add_typer(cap_app, name=spec.kind, no_args_is_help=True)


def _build_cap_apps(spec: CapCommandSpec) -> tuple[typer.Typer, typer.Typer]:
    cap_app = _new_cap_group(f"{spec.title} commands")
    local_app = _new_cap_group(f"Local {spec.kind} commands")

    @cap_app.command("add", no_args_is_help=True)
    def add(
        ref: str = typer.Argument(help=f"{spec.title} ref in owner/name form"),
        scope: SourceScope = "agent",
        agent: ScopedAgent = None,
    ) -> None:
        _cap_add(spec.kind, ref=ref, scope=scope, agent=agent)

    @cap_app.command("remove", no_args_is_help=True)
    def remove(
        name: str = typer.Argument(help=f"{spec.title} name"),
        scope: SourceScope = "agent",
        agent: ScopedAgent = None,
    ) -> None:
        _cap_remove(spec.kind, name=name, scope=scope, agent=agent)

    if spec.kind == "service":
        @cap_app.command("auth", no_args_is_help=True)
        def auth(
            agent: Annotated[str, typer.Argument(help="Agent selector")],
            name: Annotated[str, typer.Argument(help="Service name")],
            wait: Annotated[
                bool,
                typer.Option(
                    "--wait/--no-wait",
                    help="Wait for OAuth completion before returning.",
                ),
            ] = True,
        ) -> None:
            _service_auth(agent=agent, service_name=name, wait=wait)

    @local_app.command("new", no_args_is_help=True)
    def local_new(
        name: str = typer.Argument(help=f"{spec.title} name"),
        scope: LocalScope = "shared",
        from_ref: str | None = typer.Option(
            None,
            "--from",
            help=f"Initialize the local {spec.kind} from a remote ref",
        ),
        agent: LocalScopedAgent = None,
    ) -> None:
        _cap_local_new(spec.kind, name=name, scope=scope, from_ref=from_ref, agent=agent)

    @local_app.command("path", no_args_is_help=True)
    def local_path(
        name: str = typer.Argument(help=f"{spec.title} name"),
        scope: LocalScope = "shared",
        agent: LocalScopedAgent = None,
    ) -> None:
        _cap_local_path(spec.kind, name=name, scope=scope, agent=agent)

    @local_app.command("delete", no_args_is_help=True)
    def local_delete(
        name: str = typer.Argument(help=f"{spec.title} name"),
        scope: LocalScope = "shared",
        agent: LocalScopedAgent = None,
    ) -> None:
        _cap_local_delete(spec.kind, name=name, scope=scope, agent=agent)

    return cap_app, local_app


def _new_cap_group(help_text: str) -> typer.Typer:
    return typer.Typer(
        help=help_text,
        add_completion=False,
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )


def _cap_add(
    kind: CapKind,
    *,
    ref: str,
    scope: Literal["agent", "shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_scope_target(scope=scope, agent=agent)
    program = Program.load(target.source_path)
    ref_text = ref.strip()
    changed = program.add_cap_ref(kind, ref_text)
    if changed:
        validate_github_cap_ref(kind, ref_text)
        program.save(target.source_path)
    typer.echo(str(target.source_path))
    if not changed:
        typer.echo("unchanged", err=True)


def _cap_remove(
    kind: CapKind,
    *,
    name: str,
    scope: Literal["agent", "shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_scope_target(scope=scope, agent=agent)
    program = Program.load(target.source_path)
    changed = program.remove_cap_ref(
        kind,
        name,
        delete_when_empty=target.source_path.name == "agents.too",
    )
    if not changed:
        raise ToolangError(f"{kind.title()} {name!r} is not referenced in {target.source_path}.")
    if target.source_path.name == "agents.too" and not program.to_source():
        if target.source_path.exists():
            target.source_path.unlink()
    else:
        program.save(target.source_path)
    typer.echo(str(target.source_path))


def _cap_local_new(
    kind: CapKind,
    *,
    name: str,
    scope: Literal["shared", "global"],
    from_ref: str | None,
    agent: str | None,
) -> None:
    target = _resolve_cap_local_target(kind=kind, scope=scope, agent=agent, name=name)
    if from_ref is None:
        create_local_cap(target.cap_path, kind, name)
        typer.echo(str(target.cap_path))
        return

    resolved = resolve_github_cap_ref(kind, from_ref)
    source_path, _ = fetch_github_artifact(resolved)
    try:
        install_local_cap(target.cap_path, kind, source_path)
    finally:
        shutil.rmtree(source_path.parent.parent, ignore_errors=True)
    typer.echo(str(target.cap_path))


def _cap_local_path(
    kind: CapKind,
    *,
    name: str,
    scope: Literal["shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_local_target(kind=kind, scope=scope, agent=agent, name=name)
    typer.echo(str(target.cap_path))


def _cap_local_delete(
    kind: CapKind,
    *,
    name: str,
    scope: Literal["shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_local_target(kind=kind, scope=scope, agent=agent, name=name)
    if not delete_local_cap(target.cap_path):
        raise ToolangError(f"Local {kind} not found: {target.cap_path}")
    prune_empty_local_kind_dir(target.kind_dir)
    typer.echo(str(target.cap_path))


def _service_auth(*, agent: str, service_name: str, wait: bool) -> None:
    toolang_root = _toolang_root()
    db_path = ToolangRoot.resolve(toolang_root).agents_db_path
    resolved = _resolve_cli_agent(agent, db_path=db_path)
    prepared = prepare_agent(resolved)
    visible_caps = load_prepared_caps(prepared)
    result = start_service_auth(
        resolved,
        service_name=service_name,
        visible_services=[item.service_catalog_item() for item in visible_caps.services],
        wait=wait,
    )
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))


def _resolve_cap_scope_target(
    *,
    scope: Literal["agent", "shared", "global"],
    agent: str | None,
) -> CapSourceTarget:
    toolang_root = _toolang_root()
    root = ToolangRoot.resolve(toolang_root)
    if scope == "global":
        return CapSourceTarget(
            toolang_root=toolang_root,
            agent_home=None,
            agent_name=None,
            source_path=root.global_source_path,
        )

    if agent is not None:
        resolved = _resolve_cli_agent(agent, db_path=root.agents_db_path)
        home = AgentHome.resolve(resolved.home)
        if scope == "shared":
            source_path = home.shared_source_path
        else:
            source_path = home.source(resolved.name)
        return CapSourceTarget(
            toolang_root=toolang_root,
            agent_home=resolved.home,
            agent_name=resolved.name,
            source_path=source_path,
        )

    inferred = _infer_agent_context_from_cwd(Path.cwd(), toolang_root)
    if inferred is None:
        raise ToolangError(
            f"Could not infer a {scope} scope target from the current directory. "
            "Run the command from an agent home or pass --agent."
        )
    if scope == "agent" and inferred.agent_name is None:
        raise ToolangError(
            "Could not infer a single agent source from the current directory. Pass --agent."
        )
    source_path = (
        AgentHome.resolve(inferred.agent_home).shared_source_path
        if scope == "shared"
        else AgentHome.resolve(inferred.agent_home).source(inferred.agent_name or "")
    )
    return CapSourceTarget(
        toolang_root=toolang_root,
        agent_home=inferred.agent_home,
        agent_name=inferred.agent_name,
        source_path=source_path,
    )


def _resolve_cap_local_target(
    *,
    kind: CapKind,
    scope: Literal["shared", "global"],
    agent: str | None,
    name: str,
) -> CapLocalTarget:
    toolang_root = _toolang_root()
    root = ToolangRoot.resolve(toolang_root)
    if scope == "global":
        kind_dir = root.global_caps_dir(kind)
        return CapLocalTarget(
            toolang_root=toolang_root,
            agent_home=None,
            kind=kind,
            kind_dir=kind_dir,
            cap_path=local_cap_path(kind_dir, kind, name),
        )

    if agent is not None:
        resolved = _resolve_cli_agent(agent, db_path=root.agents_db_path)
        kind_dir = AgentHome.resolve(resolved.home).shared_caps_dir(kind)
        return CapLocalTarget(
            toolang_root=toolang_root,
            agent_home=resolved.home,
            kind=kind,
            kind_dir=kind_dir,
            cap_path=local_cap_path(kind_dir, kind, name),
        )

    inferred = _infer_agent_context_from_cwd(Path.cwd(), toolang_root)
    if inferred is None:
        raise ToolangError(
            "Could not infer a shared scope target from the current directory. "
            "Run the command from an agent home or pass --agent."
        )
    kind_dir = AgentHome.resolve(inferred.agent_home).shared_caps_dir(kind)
    return CapLocalTarget(
        toolang_root=toolang_root,
        agent_home=inferred.agent_home,
        kind=kind,
        kind_dir=kind_dir,
        cap_path=local_cap_path(kind_dir, kind, name),
    )


def _infer_agent_context_from_cwd(cwd: Path, toolang_root: Path) -> InferredAgentContext | None:
    resolved_cwd = cwd.resolve()
    for candidate in (resolved_cwd, *resolved_cwd.parents):
        if candidate == toolang_root:
            break
        if _is_managed_agent_home(candidate, toolang_root) or _looks_like_roaming_home(candidate):
            agent_name = _infer_agent_name(candidate, resolved_cwd)
            return InferredAgentContext(agent_home=candidate, agent_name=agent_name)
    return None


def _is_managed_agent_home(candidate: Path, toolang_root: Path) -> bool:
    parent = candidate.parent
    grandparent = parent.parent
    return grandparent == toolang_root and parent.name in {"agents", "guests"}


def _looks_like_roaming_home(candidate: Path) -> bool:
    if not candidate.is_dir():
        return False
    if (candidate / ".toolang").exists():
        return True
    return any(
        path.is_file()
        for path in candidate.glob("*.too")
        if path.name != "agents.too"
    )


def _infer_agent_name(agent_home: Path, cwd: Path) -> str | None:
    try:
        relative = cwd.relative_to(agent_home)
    except ValueError:
        return None

    if len(relative.parts) >= 3 and relative.parts[:2] == (".toolang", "agents"):
        return relative.parts[2]

    agent_names = sorted(
        path.stem
        for path in agent_home.glob("*.too")
        if path.name != "agents.too"
    )
    if len(agent_names) == 1:
        return agent_names[0]
    return None
