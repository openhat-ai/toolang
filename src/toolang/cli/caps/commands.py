"""Cap subcommands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import quote

import click
import typer
from typer.core import TyperGroup

from ...catalog import templates
from ...catalog.errors import CatalogConflictError
from ...common.errors import ToolangError
from ...common.github import parse_github_ref
from ...common.layout import AgentLayout
from toolang.catalog import cap as cap_store
from toolang.catalog import config as cap_config
from toolang.catalog.types import CAP_KINDS, CapKind
from toolang.state import state as cap_state
from toolang.state.prepare import prepare_agent_state
from ..common.context import context_agent, context_root, user_call
from ..common.output import echo_block, echo_table
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
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            "-f",
            help="Filter caps with selector-list syntax.",
        ),
    ] = None,
) -> None:
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
    try:
        selected_entries = cap_state.select_cap_entries(
            entries,
            _cap_filter_selectors(filter_, implicit_kind=None),
            agent_name=agent_name,
        )
    except ToolangError as exc:
        raise click.ClickException(str(exc)) from exc
    rows = [
        (
            entry.kind,
            entry.name,
            _entry_source(entry, agent_name=agent_name),
            _entry_form(entry),
            _entry_scope_label(entry, agent_name=agent_name),
        )
        for entry in selected_entries
    ]
    if not rows:
        typer.echo("No caps found.")
        return
    kind_order = {kind: index for index, kind in enumerate(CAP_KINDS)}
    rows.sort(
        key=lambda item: (kind_order[item[0]], item[1], item[3], item[4], item[2])
    )
    echo_table(
        ("KIND", "CAP", "SOURCE", "FORM", "SCOPE"),
        rows,
    )


def _make_cap_list_command(kind: CapKind, title: str) -> Callable[..., None]:
    def list_caps(
        ctx: typer.Context,
        filter_: Annotated[
            str | None,
            typer.Option(
                "--filter",
                "-f",
                help="Filter caps with selector-list syntax.",
            ),
        ] = None,
    ) -> None:
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
        try:
            selected_entries = cap_state.select_cap_entries(
                entries,
                _cap_filter_selectors(filter_, implicit_kind=kind),
                agent_name=agent_name,
                implicit_kind=kind,
            )
        except ToolangError as exc:
            raise click.ClickException(str(exc)) from exc
        rows = [
            (
                entry.name,
                _entry_source(entry, agent_name=agent_name),
                _entry_form(entry),
                _entry_scope_label(entry, agent_name=agent_name),
            )
            for entry in selected_entries
        ]
        if not rows:
            typer.echo(f"No {kind}s found.")
            return
        rows.sort(key=lambda item: (item[0], item[2], item[3], item[1]))
        echo_table(
            (title.upper(), "SOURCE", "FORM", "SCOPE"),
            rows,
        )

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
                progress_total=1,
            )
        typer.echo(f"Created {kind} {name}: {saved.path}")

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
                progress_total=1,
            )
        typer.echo(f"Updated {kind} {name}: {saved.path}")

    return edit_cap


def _make_add_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def add_cap(
        ctx: typer.Context,
        ref: str = typer.Argument(..., help=f"{title} ref"),
    ) -> None:
        from ..common.progress import as_progress_sink

        scope, agent_name = _target_scope(ctx)
        selected_agent = context_agent(ctx)
        progress = _make_cap_write_progress()
        try:
            canonical_ref = cap_state.resolve_remote_ref(
                kind, ref, progress=as_progress_sink(progress)
            )
            name = cap_state.remote_entry_name(kind, canonical_ref)
            _configured_caps(context_root(ctx), agent_name, scope).create(
                cap_config.CapRef(kind=kind, name=name, ref=canonical_ref)
            )
        except CatalogConflictError as exc:
            progress.finish(details=False)
            raise click.ClickException(
                f"{title} {cap_state.remote_entry_name(kind, ref)} already exists"
            ) from exc
        except ValueError as exc:
            progress.finish(details=False)
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
        if selected_agent:
            try:
                _refresh_agent_state(
                    context_root(ctx),
                    selected_agent,
                    progress_total=1,
                    progress=progress,
                )
            finally:
                progress.finish(details=False)
        else:
            progress.finish(details=False)
        typer.echo(f"Added {kind} {entry.name}: {entry.ref}")

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
                progress_total=0,
            )
        typer.echo(f"Removed {kind} {name}: {entry.ref}")

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
                progress_total=0,
            )
        typer.echo(f"Deleted {kind} {name}: {deleted_path}")

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


def _entry_source(entry: StateCap, *, agent_name: str) -> str:
    form = _entry_form(entry)
    if form in {"referenced", "configured"}:
        return _external_source_url(
            cap_state.entry_ref(entry, agent_name=agent_name), entry=entry
        )
    source = cap_state.entry_definition_file(entry)
    if form == "inline":
        line = cap_state.entry_line(entry)
        return f"{source}:{line}" if line is not None else source
    return source


def _external_source_url(ref: str, *, entry: StateCap) -> str:
    if not ref.startswith("github://"):
        return ref
    try:
        github = parse_github_ref(ref)
    except ValueError:
        return ref
    view = "blob" if entry.shape == "file" else "tree"
    return (
        f"https://github.com/{quote(github.owner, safe='')}/{quote(github.repo, safe='')}"
        f"/{view}/{quote(github.rev, safe='/')}/{quote(github.path, safe='/')}"
    )


def _entry_form(entry: StateCap) -> CapForm:
    return cap_state.entry_form(entry)


def _entry_scope_label(entry: StateCap, *, agent_name: str) -> CapScope:
    return cap_state.entry_scope(entry, agent_name=agent_name)


def _cap_filter_selectors(
    value: str | None, *, implicit_kind: EntryKind | None
) -> tuple[str, ...]:
    if value is None:
        return ()
    items = cap_state.split_cap_selectors((value,))
    if not items:
        raise click.ClickException("--filter requires at least one value")
    legacy_tokens = tuple(item.lower() for item in items)
    legacy_values = {*CAP_KINDS, *CAP_FORMS, *CAP_SCOPES}
    if all(token in legacy_values for token in legacy_tokens):
        forms = [token for token in legacy_tokens if token in CAP_FORMS]
        scopes = [token for token in legacy_tokens if token in CAP_SCOPES]
        filters = ",".join((*forms, *scopes))
        filter_suffix = f"[{filters}]" if filters else ""
        kinds = [token for token in legacy_tokens if token in CAP_KINDS]
        if implicit_kind is not None:
            return (f"*{filter_suffix}",)
        if kinds:
            return tuple(f"{kind}/*{filter_suffix}" for kind in kinds)
        return (f"*{filter_suffix}",)
    return items


def _all_cap_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: CapScope | Literal["all"],
    prepare: bool,
    kinds: set[EntryKind],
) -> tuple[StateCap, ...]:
    if prepare and (toolang_root / "agents" / agent_name / "agent.too").is_file():
        from ..common.progress import as_progress_sink, make_cli_progress

        progress = make_cli_progress(
            prepare_summary_label="Resolved",
            show_materialize_summary=True,
        )
        try:
            state = user_call(
                prepare_agent_state,
                AgentLayout.resident(toolang_root, agent_name),
                progress=as_progress_sink(progress),
            )
            entries = _state_cap_entries(state, scope=scope, kinds=kinds)
            progress.set_prepare_total(len(entries))
            return entries
        finally:
            progress.finish(details=False)
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

    return make_cli_progress(
        prepare_summary_label="Resolved",
        show_materialize_summary=True,
    )


def _refresh_agent_state(
    toolang_root: Path,
    agent_name: str,
    *,
    progress_total: int,
    progress: CliProgress | None = None,
) -> None:
    from ..common.progress import as_progress_sink

    owned_progress = progress is None
    active = progress or _make_cap_write_progress()
    try:
        user_call(
            prepare_agent_state,
            AgentLayout.resident(toolang_root, agent_name),
            progress=as_progress_sink(active),
        )
        active.set_prepare_total(progress_total)
    finally:
        if owned_progress:
            active.finish(details=False)
