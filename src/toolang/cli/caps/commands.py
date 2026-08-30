"""Cap subcommands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import click
import typer
from typer.core import TyperGroup

from ...catalog import templates
from ...catalog.errors import CatalogConflictError
from ...common.layout import AgentLayout
from toolang.catalog import cap as cap_store
from toolang.catalog import config as cap_config
from toolang.catalog.types import CAP_KINDS, CapKind
from toolang.state import state as cap_state
from toolang.state.prepare import prepare_agent_state
from ..common.context import context_agent, context_root, user_call
from ..common.output import echo_block, echo_table
from ..common.query import query_items
from ..common.routing import (
    OptionalPrefixAgentCommand,
    OptionalPrefixAgentListCommand,
    OptionalPrefixAgentTemplateCommand,
)

if TYPE_CHECKING:
    from toolang.state.state import AgentState
    from toolang.state.state import StateCap
    from ..common.progress import CliProgress

EntryKind = CapKind
MutableScope = Literal["root", "home"]
CapForm = Literal["authored", "inline", "configured", "referenced"]
CapScope = Literal["root", "home", "here"]
CAP_FORMS: tuple[CapForm, ...] = (
    "authored",
    "inline",
    "configured",
    "referenced",
)
CAP_SCOPES: tuple[CapScope, ...] = ("root", "home", "here")


def _kind_command_cls(label: str) -> type[OptionalPrefixAgentCommand]:
    return type(
        f"{label.title().replace(' ', '')}ScopeCommand",
        (OptionalPrefixAgentCommand,),
        {
            "argument_help": f"Apply to this agent's home {label} instead of root {label}."
        },
    )


def _kind_list_command_cls(label: str) -> type[OptionalPrefixAgentListCommand]:
    return type(
        f"{label.title().replace(' ', '')}ListScopeCommand",
        (OptionalPrefixAgentListCommand,),
        {"argument_help": f"Also include this agent's home {label}."},
    )


def _kind_template_command_cls(label: str) -> type[OptionalPrefixAgentTemplateCommand]:
    return type(
        f"{label.title().replace(' ', '')}TemplateScopeCommand",
        (OptionalPrefixAgentTemplateCommand,),
        {
            "argument_help": f"Apply to this agent's home {label} instead of root {label}."
        },
    )


def _kind_group_cls(
    group_cls: type[TyperGroup] | None, label: str
) -> type[TyperGroup] | None:
    if group_cls is None:
        return None
    return type(
        f"{label.title().replace(' ', '')}ScopeGroup",
        (group_cls,),
        {
            "argument_help": f"Apply to this agent's home {label} instead of root {label}."
        },
    )


def create_cap_apps(
    *,
    group_cls: type[TyperGroup] | None = None,
) -> dict[CapKind, typer.Typer]:
    cap_titles: dict[CapKind, str] = {
        "psyche": "Psyche",
        "skill": "Skill",
        "service": "Service",
        "prompt": "Prompt",
    }
    cap_labels: dict[CapKind, str] = {
        "psyche": "psyches",
        "skill": "skills",
        "service": "services",
        "prompt": "prompts",
    }
    cap_group_help: dict[CapKind, str] = {
        "psyche": "Manage psyche caps.",
        "skill": "Manage skill caps.",
        "service": "Manage service caps.",
        "prompt": "Manage prompt caps.",
    }
    cap_list_help: dict[CapKind, str] = {
        "psyche": "List psyches.",
        "skill": "List skills.",
        "service": "List services.",
        "prompt": "List prompts.",
    }

    @dataclass(frozen=True, slots=True)
    class CapCommandSpec:
        name: str
        help: Callable[[CapKind], str]
        factory: Callable[[CapKind, str], Callable[..., None]]
        no_args_is_help: bool = False

    command_specs: tuple[CapCommandSpec, ...] = (
        CapCommandSpec(
            name="list",
            help=lambda kind: cap_list_help[kind],
            factory=_make_cap_list_command,
        ),
        CapCommandSpec(
            name="new",
            help=lambda kind: f"Create a file-backed {kind}.",
            factory=_make_new_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="edit",
            help=lambda kind: f"Edit a file-backed {kind}.",
            factory=_make_edit_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="delete",
            help=lambda kind: f"Delete a file-backed {kind}.",
            factory=_make_delete_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="add",
            help=lambda kind: f"Wire a {kind} ref.",
            factory=_make_add_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="remove",
            help=lambda kind: f"Unwire a {kind}.",
            factory=_make_remove_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="template",
            help=lambda kind: f"Inspect {kind} templates.",
            factory=_make_template_command,
        ),
    )

    apps: dict[CapKind, typer.Typer] = {}
    for kind in cap_titles:
        title = cap_titles[kind]
        label = cap_labels[kind]
        command_cls = _kind_command_cls(label)
        list_command_cls = _kind_list_command_cls(label)
        template_command_cls = _kind_template_command_cls(label)
        cap_app = typer.Typer(
            help=cap_group_help[kind],
            cls=_kind_group_cls(group_cls, label),
            add_completion=False,
            no_args_is_help=True,
            pretty_exceptions_enable=False,
            pretty_exceptions_show_locals=False,
        )
        for spec in command_specs:
            cap_app.command(
                spec.name,
                help=spec.help(kind),
                no_args_is_help=spec.no_args_is_help,
                cls=(
                    template_command_cls
                    if spec.name == "template"
                    else list_command_cls
                    if spec.name == "list"
                    else command_cls
                ),
            )(spec.factory(kind, title))
        apps[kind] = cap_app
    return apps


def list_caps(
    ctx: typer.Context,
    query: Annotated[
        list[str] | None,
        typer.Option(
            "--query",
            "-q",
            help="Query cap collections. Repeat to add matches; see 'too query'.",
        ),
    ] = None,
) -> None:
    from toolang.state.collections import cap_table, query_cap_views

    selected_agent = context_agent(ctx)
    agent_name = selected_agent or "default"
    effective_scope = "all" if selected_agent else "root"
    entries = _all_cap_entries(
        context_root(ctx),
        agent_name,
        scope=effective_scope,
        prepare=selected_agent is not None,
        kinds=set(CAP_KINDS),
    )
    selected = user_call(
        query_cap_views,
        entries,
        agent_name=agent_name,
        queries=query,
    )
    headers, rows = cap_table(selected)
    if not rows:
        typer.echo("No caps matched query." if query else "No caps found.")
        return
    echo_table(headers, rows)


def _make_cap_list_command(kind: CapKind, title: str) -> Callable[..., None]:
    def list_caps(
        ctx: typer.Context,
        query: list[str] | None = typer.Option(
            None,
            "--query",
            "-q",
            help=(f"Query {kind}s. Repeat to add matches; see 'too query {kind}s'."),
        ),
    ) -> None:
        from toolang.state.collections import cap_dataset, cap_table

        selected_agent = context_agent(ctx)
        agent_name = selected_agent or "default"
        effective_scope = "all" if selected_agent else "root"
        entries = _all_cap_entries(
            context_root(ctx),
            agent_name,
            scope=effective_scope,
            prepare=selected_agent is not None,
            kinds={kind},
        )
        dataset = cap_dataset(entries, agent_name=agent_name, kind=kind)
        selected = query_items(dataset, query)
        headers, rows = cap_table(selected, kind=kind)
        if not rows:
            typer.echo(f"No {kind}s matched query." if query else f"No {kind}s found.")
            return
        echo_table(headers, rows)

    return list_caps


def _make_new_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def new_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
        template: Annotated[
            str,
            typer.Option("--template", "-t", help="Template name."),
        ] = "default",
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = context_agent(ctx)
        if _local_entry_exists(
            context_root(ctx),
            agent_name,
            scope=scope,
            kind=kind,
            name=name,
        ):
            raise click.ClickException(f"{title} {name} already exists")
        text = click.edit(
            templates.render_template(kind, template, name=name, agent_name=agent_name),
            extension=".md",
            require_save=True,
        )
        if text is None:
            typer.echo("No changes")
            raise typer.Exit()
        cap = user_call(cap_store.CapFile.parse, text, kind=kind, name=name)
        saved = user_call(
            _authored_caps(context_root(ctx), agent_name, scope).create,
            cap,
        )
        if selected_agent:
            _refresh_agent_state(
                context_root(ctx),
                selected_agent,
            )
        typer.echo(f"{kind.title()} {name} created: {saved.path}")

    return new_cap


def _make_edit_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def edit_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = context_agent(ctx)
        try:
            existing = _authored_caps(context_root(ctx), agent_name, scope).get(
                kind, name
            )
            if existing is None:
                raise FileNotFoundError(name)
            text = existing.content
        except FileNotFoundError as exc:
            raise click.ClickException(f"{title} {name} not found") from exc
        updated_text = click.edit(
            text,
            extension=".md",
            require_save=True,
        )
        if updated_text is None or updated_text == text:
            typer.echo("No changes")
            raise typer.Exit()
        cap = user_call(cap_store.CapFile.parse, updated_text, kind=kind, name=name)
        saved = user_call(
            _authored_caps(context_root(ctx), agent_name, scope).update,
            cap,
        )
        if selected_agent:
            _refresh_agent_state(
                context_root(ctx),
                selected_agent,
            )
        typer.echo(f"{kind.title()} {name} updated: {saved.path}")

    return edit_cap


def _make_add_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def add_cap(
        ctx: typer.Context,
        ref: str = typer.Argument(..., help=f"{title} ref"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = context_agent(ctx)
        progress = _make_cap_write_progress()
        try:
            with progress:
                canonical_ref = cap_state.resolve_remote_ref(
                    kind,
                    ref,
                    progress=progress.sink,
                )
                name = cap_state.remote_entry_name(kind, canonical_ref)
                _configured_caps(context_root(ctx), agent_name, scope).create(
                    cap_config.CapRef(kind=kind, name=name, ref=canonical_ref)
                )
                if selected_agent:
                    _refresh_agent_state(
                        context_root(ctx),
                        selected_agent,
                        progress=progress,
                    )
        except CatalogConflictError as exc:
            raise click.ClickException(
                f"{title} {cap_state.remote_entry_name(kind, ref)} already exists"
            ) from exc
        except ValueError as exc:
            if progress.failure_stage is not None:
                raise click.ClickException(progress.failure_message(exc)) from exc
            message = str(exc)
            if "conflicting entries" in message:
                raise click.ClickException(
                    f"{title} {cap_state.remote_entry_name(kind, ref)} already exists"
                ) from exc
            raise click.ClickException(f"Configured {kind} {ref} not found") from exc
        entry = _named_entry(
            context_root(ctx),
            agent_name,
            scope=scope,
            kind=kind,
            name=name,
            source_origin="remote",
            source_form="configured",
        )
        typer.echo(f"{kind.title()} {entry.name} added: {entry.ref}")

    return add_cap


def _make_remove_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def remove_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = context_agent(ctx)
        entry = _named_entry(
            context_root(ctx),
            agent_name,
            scope=scope,
            kind=kind,
            name=name,
            source_origin="remote",
            source_form="configured",
        )
        user_call(
            _configured_caps(context_root(ctx), agent_name, scope).remove,
            kind,
            name,
        )
        if selected_agent:
            _refresh_agent_state(
                context_root(ctx),
                selected_agent,
            )
        typer.echo(f"{kind.title()} {name} removed: {entry.ref}")

    return remove_cap


def _make_delete_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def delete_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = context_agent(ctx)
        entry = _named_entry(
            context_root(ctx),
            agent_name,
            scope=scope,
            kind=kind,
            name=name,
            source_origin="local",
        )
        deleted_path = context_root(ctx) / entry.path
        if entry.shape == "dir":
            deleted_path = deleted_path.parent
        user_call(
            _authored_caps(context_root(ctx), agent_name, scope).remove,
            kind,
            name,
        )
        if selected_agent:
            _refresh_agent_state(
                context_root(ctx),
                selected_agent,
            )
        typer.echo(f"{kind.title()} {name} deleted: {deleted_path}")

    return delete_cap


def _make_template_command(kind: CapKind, title: str) -> Callable[..., None]:
    del title

    def template(
        name: Annotated[
            str | None,
            typer.Argument(help="Template name", metavar="NAME", hidden=True),
        ] = None,
    ) -> None:
        if name is not None:
            echo_block(templates.load_template(kind, name).raw_text.rstrip("\n"))
            return
        specs = templates.list_templates(kind)
        if not specs:
            typer.echo(f"No {kind} templates found.")
            return
        rows = [(item.name, item.description or "-") for item in specs]
        echo_table(("TEMPLATE", "DESCRIPTION"), rows)

    return template


def _target_scope(ctx: typer.Context) -> tuple[MutableScope, str]:
    agent_name = context_agent(ctx)
    if agent_name:
        return "home", agent_name
    return "root", "default"


def _entry_form(entry: StateCap) -> CapForm:
    return cap_state.entry_form(entry)


def _entry_scope_label(entry: StateCap, *, agent_name: str) -> CapScope:
    return cap_state.entry_scope(entry, agent_name=agent_name)


def _all_cap_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: CapScope | Literal["all"],
    prepare: bool,
    kinds: set[EntryKind],
) -> tuple[StateCap, ...]:
    if prepare and (toolang_root / "agents" / agent_name / "agent.too").is_file():
        from ..common.progress import make_cli_progress

        progress = make_cli_progress()
        try:
            with progress:
                state = user_call(
                    prepare_agent_state,
                    AgentLayout.resident(toolang_root, agent_name),
                    progress=progress.sink,
                )
                entries = _state_cap_entries(state, scope=scope, kinds=kinds)
                return entries
        except Exception as exc:
            if progress.failure_stage is not None:
                raise click.ClickException(progress.failure_message(exc)) from exc
            raise
    return cap_state.list_entries(
        toolang_root,
        agent_name,
        scope=None if scope == "all" else scope,
        kinds=kinds,
    )


def _state_cap_entries(
    state: AgentState,
    *,
    scope: CapScope | Literal["all"],
    kinds: set[EntryKind],
) -> tuple[StateCap, ...]:
    return tuple(
        cap
        for cap in state.caps.values()
        if cap.kind in kinds and (scope == "all" or cap.scope == scope)
    )


def _named_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: MutableScope,
    kind: EntryKind,
    name: str,
    source_origin: Literal["local", "remote"] | None = None,
    source_form: cap_state.CapForm | None = None,
) -> StateCap:
    entries = cap_state.list_entries(
        toolang_root,
        agent_name,
        scope=scope,
        kinds={kind},
    )
    for entry in entries:
        if entry.name != name:
            continue
        if source_origin is not None and entry.source.origin != source_origin:
            continue
        if source_form is not None and entry.source.form != source_form:
            continue
        return entry
    raise click.ClickException(f"{kind.title()} {name} not found")


def _local_entry_exists(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: MutableScope,
    kind: EntryKind,
    name: str,
) -> bool:
    return _authored_caps(toolang_root, agent_name, scope).get(kind, name) is not None


def _cap_directory(
    toolang_root: Path,
    agent_name: str,
    scope: MutableScope,
) -> Path:
    return toolang_root if scope == "root" else toolang_root / "agents" / agent_name


def _authored_caps(
    toolang_root: Path,
    agent_name: str,
    scope: MutableScope,
) -> cap_store.AuthoredCaps:
    return cap_store.AuthoredCaps(_cap_directory(toolang_root, agent_name, scope))


def _configured_caps(
    toolang_root: Path,
    agent_name: str,
    scope: MutableScope,
) -> cap_config.ConfiguredCaps:
    return cap_config.ConfiguredCaps(
        _cap_directory(toolang_root, agent_name, scope) / "config.toml"
    )


def _make_cap_write_progress() -> CliProgress:
    from ..common.progress import make_cli_progress

    return make_cli_progress()


def _refresh_agent_state(
    toolang_root: Path,
    agent_name: str,
    *,
    progress: CliProgress | None = None,
) -> None:
    if progress is not None:
        _prepare_agent_state_with_progress(toolang_root, agent_name, progress)
        return
    with _make_cap_write_progress() as owned_progress:
        _prepare_agent_state_with_progress(toolang_root, agent_name, owned_progress)


def _prepare_agent_state_with_progress(
    toolang_root: Path,
    agent_name: str,
    progress: CliProgress,
) -> None:
    try:
        user_call(
            prepare_agent_state,
            AgentLayout.resident(toolang_root, agent_name),
            progress=progress.sink,
        )
    except Exception as exc:
        if progress.failure_stage is not None:
            raise click.ClickException(progress.failure_message(exc)) from exc
        raise
