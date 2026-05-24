"""Cap subcommands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import quote

import click
import typer
from typer.core import TyperGroup

from .. import caps as cap_store
from .. import templates
from ..execution.records import UpdateKind
from ..features import watch as watch_feature
from ..state.durable import scan_durable_state
from ..state.prepared import EntryKind, PreparedEntry, PreparedState, PreparedVisibility
from .progress import CliProgress, as_progress_sink, make_cli_progress
from .utils import (
    _OptionalPrefixAgentCommand,
    _OptionalPrefixAgentGroup,
    _OptionalPrefixAgentListCommand,
    _OptionalPrefixAgentTemplateCommand,
    _append_agent_update,
    _context_agent,
    _context_root,
    _echo_block,
    _echo_table,
    _wrap_user_error,
)

CapKind = Literal["skill", "psyche", "prompt", "service"]
CapForm = Literal["inline", "cited", "remote", "local"]
CapScope = Literal["global", "agent"]
CAP_KINDS: tuple[CapKind, ...] = ("psyche", "skill", "service", "prompt")
CAP_FORMS: tuple[CapForm, ...] = ("inline", "cited", "remote", "local")
CAP_SCOPES: tuple[CapScope, ...] = ("global", "agent")


def register_cap_commands(app: typer.Typer, *, rich_help_panel: str | None = None) -> None:
    _register_cap_kind_commands(app, rich_help_panel=rich_help_panel)
    caps_app = typer.Typer(
        help="Manage all caps.",
        add_completion=False,
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    caps_app.command(
        "list",
        help="List caps of all kinds.",
        cls=_OptionalPrefixAgentListCommand,
    )(_list_all_caps)
    app.add_typer(caps_app, name="caps", hidden=True)


def register_standalone_caps_commands(app: typer.Typer, *, rich_help_panel: str | None = None) -> None:
    app.command(
        "list",
        help="List caps of all kinds.",
        cls=_OptionalPrefixAgentListCommand,
    )(_list_all_caps)
    _register_cap_kind_commands(
        app,
        rich_help_panel=rich_help_panel,
        group_cls=_OptionalPrefixAgentGroup,
    )


def _kind_command_cls(label: str) -> type[_OptionalPrefixAgentCommand]:
    return type(
        f"{label.title().replace(' ', '')}ScopeCommand",
        (_OptionalPrefixAgentCommand,),
        {"argument_help": f"Apply to this agent's {label} instead of global {label}."},
    )


def _kind_list_command_cls(label: str) -> type[_OptionalPrefixAgentListCommand]:
    return type(
        f"{label.title().replace(' ', '')}ListScopeCommand",
        (_OptionalPrefixAgentListCommand,),
        {"argument_help": f"Also include this agent's {label}."},
    )


def _kind_template_command_cls(label: str) -> type[_OptionalPrefixAgentTemplateCommand]:
    return type(
        f"{label.title().replace(' ', '')}TemplateScopeCommand",
        (_OptionalPrefixAgentTemplateCommand,),
        {"argument_help": f"Apply to this agent's {label} instead of global {label}."},
    )


def _kind_group_cls(group_cls: type[TyperGroup] | None, label: str) -> type[TyperGroup] | None:
    if group_cls is None:
        return None
    return type(
        f"{label.title().replace(' ', '')}ScopeGroup",
        (group_cls,),
        {"argument_help": f"Apply to this agent's {label} instead of global {label}."},
    )


def _register_cap_kind_commands(
    app: typer.Typer,
    *,
    rich_help_panel: str | None = None,
    group_cls: type[TyperGroup] | None = None,
) -> None:
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
            help=lambda kind: f"Create a local {kind}.",
            factory=_make_new_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="edit",
            help=lambda kind: f"Edit a local {kind}.",
            factory=_make_edit_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="delete",
            help=lambda kind: f"Delete a local {kind}.",
            factory=_make_delete_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="add",
            help=lambda kind: f"Add a remote {kind}.",
            factory=_make_add_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="remove",
            help=lambda kind: f"Remove a remote {kind}.",
            factory=_make_remove_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="template",
            help=lambda kind: f"Inspect {kind} templates.",
            factory=_make_template_command,
        ),
    )

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
        app.add_typer(
            cap_app,
            name=kind,
            no_args_is_help=True,
            rich_help_panel=rich_help_panel,
        )


def _list_all_caps(
    ctx: typer.Context,
    filter_: Annotated[
        str | None,
        typer.Option(
            "--filter",
            help=(
                "Filter by kind, form, or scope CSV: psyche, skill, service, prompt, "
                "inline, cited, remote, local, global, agent."
            ),
        ),
    ] = None,
) -> None:
    selected_agent = _context_agent(ctx)
    agent_name = selected_agent or "default"
    effective_visibility = "all" if selected_agent else "shared"
    kind_filter, form_filter, scope_filter = _parse_cap_filter(filter_)
    entries = _all_cap_entries(
        _context_root(ctx),
        agent_name,
        visibility=effective_visibility,
        prepare=selected_agent is not None,
        kinds=set(cast(tuple[EntryKind, ...], tuple(kind_filter or CAP_KINDS))),
    )
    rows = [
        (
            cast(CapKind, entry.kind),
            entry.name,
            _entry_source(entry, agent_name=agent_name),
            _entry_form(entry),
            _entry_scope_label(entry, agent_name=agent_name),
        )
        for entry in entries
        if _entry_matches_filters(
            entry,
            agent_name=agent_name,
            kind_filter=kind_filter,
            form_filter=form_filter,
            scope_filter=scope_filter,
        )
    ]
    if not rows:
        typer.echo("No caps found.")
        return
    kind_order = {kind: index for index, kind in enumerate(CAP_KINDS)}
    rows.sort(key=lambda item: (kind_order[item[0]], item[1], item[3], item[4], item[2]))
    _echo_table(
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
                help=(
                    "Filter by form or scope CSV: inline, cited, remote, local, global, agent."
                ),
            ),
        ] = None,
    ) -> None:
        selected_agent = _context_agent(ctx)
        agent_name = selected_agent or "default"
        effective_visibility = "all" if selected_agent else "shared"
        _, form_filter, scope_filter = _parse_cap_filter(filter_)
        entries = _all_cap_entries(
            _context_root(ctx),
            agent_name,
            visibility=effective_visibility,
            prepare=selected_agent is not None,
            kinds={cast(EntryKind, kind)},
        )
        rows = [
            (
                entry.name,
                _entry_source(entry, agent_name=agent_name),
                _entry_form(entry),
                _entry_scope_label(entry, agent_name=agent_name),
            )
            for entry in entries
            if _entry_matches_filters(
                entry,
                agent_name=agent_name,
                kind_filter=None,
                form_filter=form_filter,
                scope_filter=scope_filter,
            )
        ]
        if not rows:
            typer.echo(f"No {kind}s found.")
            return
        rows.sort(key=lambda item: (item[0], item[2], item[3], item[1]))
        _echo_table(
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
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        if _entry_exists(
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
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
        path = _wrap_user_error(
            cap_store.put_local_entry_text,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            text=text,
        )
        if selected_agent:
            progress = _make_cap_write_progress()
            try:
                _refresh_and_append_cap_update(
                    _context_root(ctx),
                    selected_agent,
                    kind=kind,
                    name=name,
                    visibility=visibility,
                    progress=progress,
                )
            finally:
                progress.finish(details=False)
        typer.echo(f"Created {kind} {name}: {path}")

    return new_cap


def _make_edit_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def edit_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        try:
            text = cap_store.load_local_entry_text(
                _context_root(ctx),
                agent_name,
                visibility=visibility,
                kind=cast(EntryKind, kind),
                name=name,
            )
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
        path = _wrap_user_error(
            cap_store.put_local_entry_text,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            text=updated_text,
        )
        if selected_agent:
            progress = _make_cap_write_progress()
            try:
                _refresh_and_append_cap_update(
                    _context_root(ctx),
                    selected_agent,
                    kind=kind,
                    name=name,
                    visibility=visibility,
                    progress=progress,
                )
            finally:
                progress.finish(details=False)
        typer.echo(f"Updated {kind} {name}: {path}")

    return edit_cap


def _make_add_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def add_cap(
        ctx: typer.Context,
        ref: str = typer.Argument(..., help=f"{title} ref"),
    ) -> None:
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        progress = _make_cap_write_progress()
        try:
            cap_store.add_remote_entry(
                _context_root(ctx),
                agent_name,
                visibility=visibility,
                kind=cast(EntryKind, kind),
                ref=ref,
                progress=as_progress_sink(progress),
            )
        except ValueError as exc:
            progress.finish(details=False)
            message = str(exc)
            if "conflicting entries" in message:
                raise click.ClickException(
                    f"{title} {cap_store.remote_entry_name(cast(EntryKind, kind), ref)} already exists"
                ) from exc
            raise click.ClickException(f"Remote {kind} {ref} not found") from exc
        entry = _named_entry(
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=cap_store.remote_entry_name(cast(EntryKind, kind), ref),
            source_origin="remote",
            source_form="remote",
        )
        if selected_agent:
            try:
                _refresh_and_append_cap_update(
                    _context_root(ctx),
                    selected_agent,
                    kind=kind,
                    name=entry.name,
                    visibility=visibility,
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
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        progress = _make_cap_write_progress() if selected_agent else None
        entry = _named_entry(
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            source_origin="remote",
            source_form="remote",
        )
        removed = _wrap_user_error(
            cap_store.remove_remote_entry,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise click.ClickException(f"{title} {name} not found")
        if selected_agent:
            try:
                _refresh_and_append_cap_update(
                    _context_root(ctx),
                    selected_agent,
                    kind=kind,
                    name=name,
                    visibility=visibility,
                    progress=progress,
                )
            finally:
                if progress is not None:
                    progress.finish(details=False)
        typer.echo(f"Removed {kind} {name}: {entry.ref}")

    return remove_cap


def _make_delete_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def delete_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        entry = _named_entry(
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            source_origin="local",
        )
        deleted_path = _context_root(ctx) / entry.path
        if entry.shape == "dir":
            deleted_path = deleted_path.parent
        removed = _wrap_user_error(
            cap_store.remove_local_entry,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise click.ClickException(f"{title} {name} not found")
        if selected_agent:
            progress = _make_cap_write_progress()
            try:
                _refresh_and_append_cap_update(
                    _context_root(ctx),
                    selected_agent,
                    kind=kind,
                    name=name,
                    visibility=visibility,
                    progress=progress,
                )
            finally:
                progress.finish(details=False)
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
            _echo_block(templates.load_template(kind, name).raw_text.rstrip("\n"))
            return
        specs = templates.list_templates(kind)
        if not specs:
            typer.echo(f"No {kind} templates found.")
            return
        rows = [(item.name, item.description or "-") for item in specs]
        _echo_table(("TEMPLATE", "DESCRIPTION"), rows)

    return template


def _target_visibility(ctx: typer.Context) -> tuple[PreparedVisibility, str]:
    agent_name = _context_agent(ctx)
    if agent_name:
        return "private", agent_name
    return "shared", "default"


def _entry_matches_filters(
    entry: PreparedEntry,
    *,
    agent_name: str,
    kind_filter: set[CapKind] | None,
    form_filter: set[CapForm] | None,
    scope_filter: set[CapScope] | None,
) -> bool:
    if kind_filter is not None and entry.kind not in kind_filter:
        return False
    if form_filter is not None and _entry_form(entry) not in form_filter:
        return False
    return scope_filter is None or _entry_scope_label(entry, agent_name=agent_name) in scope_filter


def _entry_source(entry: PreparedEntry, *, agent_name: str) -> str:
    form = _entry_form(entry)
    if form in {"cited", "remote"}:
        return _external_source_url(cap_store.entry_ref(entry, agent_name=agent_name), entry=entry)
    source = cap_store.entry_definition_file(entry)
    if form == "inline":
        line = cap_store.entry_line(entry)
        return f"{source}:{line}" if line is not None else source
    return source


def _external_source_url(ref: str, *, entry: PreparedEntry) -> str:
    if not ref.startswith("github://"):
        return ref
    body = ref.removeprefix("github://")
    try:
        repo_ref, rev = body.rsplit("@", 1)
        owner, repo, path = repo_ref.split("/", 2)
    except ValueError:
        return ref
    if not owner or not repo or not path or not rev:
        return ref
    view = "blob" if entry.shape == "file" else "tree"
    return (
        f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/{view}/{quote(rev, safe='/')}/{quote(path, safe='/')}"
    )


def _entry_form(entry: PreparedEntry) -> CapForm:
    return cap_store.entry_form(entry)


def _entry_is_global(entry: PreparedEntry, *, agent_name: str) -> bool:
    return cap_store.entry_scope(entry, agent_name=agent_name) == "global"


def _entry_scope_label(entry: PreparedEntry, *, agent_name: str) -> CapScope:
    return "global" if _entry_is_global(entry, agent_name=agent_name) else "agent"


def _parse_cap_filter(
    value: str | None,
) -> tuple[set[CapKind] | None, set[CapForm] | None, set[CapScope] | None]:
    if value is None:
        return None, None, None
    kinds: set[CapKind] = set()
    forms: set[CapForm] = set()
    scopes: set[CapScope] = set()
    for item in _split_csv(value, option_name="--filter"):
        if item in CAP_KINDS:
            kinds.add(cast(CapKind, item))
            continue
        if item in CAP_FORMS:
            forms.add(cast(CapForm, item))
            continue
        if item in CAP_SCOPES:
            scopes.add(cast(CapScope, item))
            continue
        expected = ", ".join((*CAP_KINDS, *CAP_FORMS, *CAP_SCOPES))
        raise click.ClickException(f"invalid --filter value: {item}; expected one of {expected}")
    return kinds or None, forms or None, scopes or None


def _split_csv(value: str, *, option_name: str) -> tuple[str, ...]:
    items = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    if not items:
        raise click.ClickException(f"{option_name} requires at least one value")
    return items


def _all_cap_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility | Literal["all"],
    prepare: bool,
    kinds: set[EntryKind],
) -> tuple[PreparedEntry, ...]:
    durable = _wrap_user_error(scan_durable_state, toolang_root, agent_name)
    if prepare and durable.program_source is not None:
        progress = make_cli_progress(
            prepare_summary_label="Resolved",
            show_materialize_summary=True,
        )
        try:
            prepared = _wrap_user_error(
                watch_feature.build_prepared_state,
                durable,
                progress=as_progress_sink(progress),
            )
            entries = _prepared_cap_entries(prepared, visibility=visibility, kinds=kinds)
            progress.set_prepare_total(len(entries))
            return entries
        finally:
            progress.finish(details=False)
    return cap_store.list_entries(
        toolang_root,
        agent_name,
        visibility=None if visibility == "all" else visibility,
        kinds=kinds,
    )


def _prepared_cap_entries(
    prepared: PreparedState,
    *,
    visibility: PreparedVisibility | Literal["all"],
    kinds: set[EntryKind],
) -> tuple[PreparedEntry, ...]:
    entries: list[PreparedEntry] = []
    if visibility in {"all", "shared"}:
        entries.extend(entry for entry in prepared.shared_lock.entries if entry.kind in kinds)
    if visibility in {"all", "private"}:
        entries.extend(entry for entry in prepared.private_lock.entries if entry.kind in kinds)
    return tuple(entries)


def _named_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
    source_origin: Literal["local", "remote"] | None = None,
    source_form: cap_store.EntryForm | None = None,
) -> PreparedEntry:
    entries = cap_store.list_entries(
        toolang_root,
        agent_name,
        visibility=visibility,
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


def _entry_exists(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> bool:
    return any(
        entry.name == name
        for entry in cap_store.list_entries(
            toolang_root,
            agent_name,
            visibility=visibility,
            kinds={kind},
        )
    )


def _append_cap_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: CapKind,
    name: str,
    visibility: PreparedVisibility,
) -> None:
    update_kind = cast(UpdateKind, f"{kind}_changed")
    _append_agent_update(
        toolang_root,
        agent_name,
        update_kind,
        {
            "name": name,
            "visibility": visibility,
        },
    )


def _make_cap_write_progress() -> CliProgress:
    return make_cli_progress(
        prepare_summary_label="Resolved",
        show_materialize_summary=True,
    )


def _refresh_and_append_cap_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: CapKind,
    name: str,
    visibility: PreparedVisibility,
    progress: CliProgress | None = None,
) -> None:
    durable = _wrap_user_error(scan_durable_state, toolang_root, agent_name)
    prepared = _wrap_user_error(
        watch_feature.build_prepared_state,
        durable,
        progress=as_progress_sink(progress),
    )
    if progress is not None:
        progress.set_prepare_total(_prepared_cap_count(prepared))
    _append_cap_update(
        toolang_root,
        agent_name,
        kind=kind,
        name=name,
        visibility=visibility,
    )


def _prepared_cap_count(prepared: PreparedState) -> int:
    return sum(
        1
        for entry in (*prepared.shared_lock.entries, *prepared.private_lock.entries)
        if entry.kind in CAP_KINDS
    )
