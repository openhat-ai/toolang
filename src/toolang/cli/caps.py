from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer

from toolang.errors import ToolangError
from toolang.layout import (
    agent_source_path,
    agents_db_path,
    global_caps_dir,
    global_source_path,
    shared_caps_dir,
    shared_source_path,
)
from toolang_caps.github import fetch_github_artifact, resolve_github_cap_ref
from toolang_caps.models import CapKind
from toolang_caps.source_ops import (
    add_cap_ref,
    create_local_cap,
    delete_local_cap,
    install_local_cap,
    local_cap_path,
    prune_empty_local_kind_dir,
    remove_cap_ref,
)

from .support import _resolve_cli_agent, _toolang_root

skill_app = typer.Typer(
    help="Skill commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
skill_local_app = typer.Typer(
    help="Local skill commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
service_app = typer.Typer(
    help="Service commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
service_local_app = typer.Typer(
    help="Local service commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
prompt_app = typer.Typer(
    help="Prompt commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
prompt_local_app = typer.Typer(
    help="Local prompt commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
psyche_app = typer.Typer(
    help="Psyche commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
psyche_local_app = typer.Typer(
    help="Local psyche commands",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


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


@skill_app.command("add", no_args_is_help=True)
def skill_add(
    ref: Annotated[str, typer.Argument(help="Skill ref in owner/name form")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_add("skill", ref=ref, scope=scope, agent=agent)


@skill_app.command("remove", no_args_is_help=True)
def skill_remove(
    name: Annotated[str, typer.Argument(help="Skill name")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_remove("skill", name=name, scope=scope, agent=agent)


@skill_local_app.command("new", no_args_is_help=True)
def skill_local_new(
    name: Annotated[str, typer.Argument(help="Skill name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Initialize the local skill from a remote ref"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_new("skill", name=name, scope=scope, from_ref=from_ref, agent=agent)


@skill_local_app.command("path", no_args_is_help=True)
def skill_local_path(
    name: Annotated[str, typer.Argument(help="Skill name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_path("skill", name=name, scope=scope, agent=agent)


@skill_local_app.command("delete", no_args_is_help=True)
def skill_local_delete(
    name: Annotated[str, typer.Argument(help="Skill name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_delete("skill", name=name, scope=scope, agent=agent)


@service_app.command("add", no_args_is_help=True)
def service_add(
    ref: Annotated[str, typer.Argument(help="Service ref in owner/name form")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_add("service", ref=ref, scope=scope, agent=agent)


@service_app.command("remove", no_args_is_help=True)
def service_remove(
    name: Annotated[str, typer.Argument(help="Service name")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_remove("service", name=name, scope=scope, agent=agent)


@service_local_app.command("new", no_args_is_help=True)
def service_local_new(
    name: Annotated[str, typer.Argument(help="Service name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Initialize the local service from a remote ref"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_new("service", name=name, scope=scope, from_ref=from_ref, agent=agent)


@service_local_app.command("path", no_args_is_help=True)
def service_local_path(
    name: Annotated[str, typer.Argument(help="Service name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_path("service", name=name, scope=scope, agent=agent)


@service_local_app.command("delete", no_args_is_help=True)
def service_local_delete(
    name: Annotated[str, typer.Argument(help="Service name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_delete("service", name=name, scope=scope, agent=agent)


@prompt_app.command("add", no_args_is_help=True)
def prompt_add(
    ref: Annotated[str, typer.Argument(help="Prompt ref in owner/name form")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_add("prompt", ref=ref, scope=scope, agent=agent)


@prompt_app.command("remove", no_args_is_help=True)
def prompt_remove(
    name: Annotated[str, typer.Argument(help="Prompt name")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_remove("prompt", name=name, scope=scope, agent=agent)


@prompt_local_app.command("new", no_args_is_help=True)
def prompt_local_new(
    name: Annotated[str, typer.Argument(help="Prompt name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Initialize the local prompt from a remote ref"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_new("prompt", name=name, scope=scope, from_ref=from_ref, agent=agent)


@prompt_local_app.command("path", no_args_is_help=True)
def prompt_local_path(
    name: Annotated[str, typer.Argument(help="Prompt name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_path("prompt", name=name, scope=scope, agent=agent)


@prompt_local_app.command("delete", no_args_is_help=True)
def prompt_local_delete(
    name: Annotated[str, typer.Argument(help="Prompt name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_delete("prompt", name=name, scope=scope, agent=agent)


@psyche_app.command("add", no_args_is_help=True)
def psyche_add(
    ref: Annotated[str, typer.Argument(help="Psyche ref in owner/name form")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_add("psyche", ref=ref, scope=scope, agent=agent)


@psyche_app.command("remove", no_args_is_help=True)
def psyche_remove(
    name: Annotated[str, typer.Argument(help="Psyche name")],
    scope: Annotated[
        Literal["agent", "shared", "global"],
        typer.Option(help="Target scope"),
    ] = "agent",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve agent or shared scope"),
    ] = None,
) -> None:
    _cap_remove("psyche", name=name, scope=scope, agent=agent)


@psyche_local_app.command("new", no_args_is_help=True)
def psyche_local_new(
    name: Annotated[str, typer.Argument(help="Psyche name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Initialize the local psyche from a remote ref"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_new("psyche", name=name, scope=scope, from_ref=from_ref, agent=agent)


@psyche_local_app.command("path", no_args_is_help=True)
def psyche_local_path(
    name: Annotated[str, typer.Argument(help="Psyche name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_path("psyche", name=name, scope=scope, agent=agent)


@psyche_local_app.command("delete", no_args_is_help=True)
def psyche_local_delete(
    name: Annotated[str, typer.Argument(help="Psyche name")],
    scope: Annotated[
        Literal["shared", "global"],
        typer.Option(help="Target local scope"),
    ] = "shared",
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent selector used to resolve shared scope"),
    ] = None,
) -> None:
    _cap_local_delete("psyche", name=name, scope=scope, agent=agent)


def register_cap_commands(app: typer.Typer) -> None:
    skill_app.add_typer(skill_local_app, name="local", no_args_is_help=True)
    service_app.add_typer(service_local_app, name="local", no_args_is_help=True)
    prompt_app.add_typer(prompt_local_app, name="local", no_args_is_help=True)
    psyche_app.add_typer(psyche_local_app, name="local", no_args_is_help=True)

    app.add_typer(psyche_app, name="psyche", no_args_is_help=True)
    app.add_typer(skill_app, name="skill", no_args_is_help=True)
    app.add_typer(service_app, name="service", no_args_is_help=True)
    app.add_typer(prompt_app, name="prompt", no_args_is_help=True)


def _cap_add(
    kind: CapKind,
    *,
    ref: str,
    scope: Literal["agent", "shared", "global"],
    agent: str | None,
) -> None:
    target = _resolve_cap_scope_target(scope=scope, agent=agent)
    changed = add_cap_ref(target.source_path, kind, ref)
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
    changed = remove_cap_ref(
        target.source_path,
        kind,
        name,
        delete_when_empty=target.source_path.name == "agents.too",
    )
    if not changed:
        raise ToolangError(f"{kind.title()} {name!r} is not referenced in {target.source_path}.")
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


def _resolve_cap_scope_target(
    *,
    scope: Literal["agent", "shared", "global"],
    agent: str | None,
) -> CapSourceTarget:
    toolang_root = _toolang_root()
    if scope == "global":
        return CapSourceTarget(
            toolang_root=toolang_root,
            agent_home=None,
            agent_name=None,
            source_path=global_source_path(toolang_root),
        )

    if agent is not None:
        resolved = _resolve_cli_agent(agent, db_path=agents_db_path(toolang_root))
        if scope == "shared":
            source_path = shared_source_path(resolved.agent_home)
        else:
            source_path = agent_source_path(resolved.agent_home, resolved.agent_name)
        return CapSourceTarget(
            toolang_root=toolang_root,
            agent_home=resolved.agent_home,
            agent_name=resolved.agent_name,
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
        shared_source_path(inferred.agent_home)
        if scope == "shared"
        else agent_source_path(inferred.agent_home, inferred.agent_name or "")
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
    if scope == "global":
        kind_dir = global_caps_dir(toolang_root, kind)
        return CapLocalTarget(
            toolang_root=toolang_root,
            agent_home=None,
            kind=kind,
            kind_dir=kind_dir,
            cap_path=local_cap_path(kind_dir, kind, name),
        )

    if agent is not None:
        resolved = _resolve_cli_agent(agent, db_path=agents_db_path(toolang_root))
        kind_dir = shared_caps_dir(resolved.agent_home, kind)
        return CapLocalTarget(
            toolang_root=toolang_root,
            agent_home=resolved.agent_home,
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
    kind_dir = shared_caps_dir(inferred.agent_home, kind)
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
