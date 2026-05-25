"""Cap subcommands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast
from urllib.parse import quote

import click
import typer
from typer.core import TyperGroup

from ..base.error import ToolangError
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

if TYPE_CHECKING:
    from .. import caps as cap_store
    from .. import templates
    from ..execution.records import UpdateKind
    from ..components.trigger import watch as watch_feature
    from ..state.prepared import PreparedEntry, PreparedState
    from .progress import CliProgress

CapKind = Literal["skill", "psyche", "prompt", "service"]
EntryKind = Literal["psyche", "skill", "service", "prompt", "task", "chore"]
PreparedVisibility = Literal["shared", "private"]
CapForm = Literal["inline", "ref", "wired", "file"]
CapScope = Literal["root", "home", "here"]
CAP_KINDS: tuple[CapKind, ...] = ("psyche", "skill", "service", "prompt")
CAP_FORMS: tuple[CapForm, ...] = ("inline", "ref", "wired", "file")
CAP_SCOPES: tuple[CapScope, ...] = ("root", "home", "here")


class _LazyModule:
    """Import a module only when one of its attributes is used."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: object | None = None

    def _load(self) -> object:
        if self._module is None:
            import importlib

            self._module = importlib.import_module(self._module_name)
        return self._module

    def __getattr__(self, name: str) -> object:
        return getattr(self._load(), name)

    def __setattr__(self, name: str, value: object) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._load(), name, value)

    def __delattr__(self, name: str) -> None:
        if name.startswith("_"):
            object.__delattr__(self, name)
            return
        delattr(self._load(), name)


if not TYPE_CHECKING:
    cap_store = _LazyModule("toolang.caps")
    templates = _LazyModule("toolang.templates")
    watch_feature = _LazyModule("toolang.components.trigger.watch")


def register_standalone_caps_commands(app: typer.Typer, *, rich_help_panel: str | None = None) -> None:
    app.command(
        "list",
        help="Inspect available caps.",
        cls=_OptionalPrefixAgentListCommand,
    )(_list_all_caps)
    _register_cap_kind_commands(
        app,
        rich_help_panel=rich_help_panel,
        group_cls=_OptionalPrefixAgentGroup,
    )


def register_toolang_caps_commands(app: typer.Typer, *, rich_help_panel: str | None = None) -> None:
    _register_cap_kind_commands(
        app,
        rich_help_panel=rich_help_panel,
        group_cls=_OptionalPrefixAgentGroup,
    )
    app.command(
        "caps",
        help="Inspect available caps.",
        cls=_OptionalPrefixAgentListCommand,
        rich_help_panel=rich_help_panel,
    )(_list_all_caps)


def _kind_command_cls(label: str) -> type[_OptionalPrefixAgentCommand]:
    return type(
        f"{label.title().replace(' ', '')}ScopeCommand",
        (_OptionalPrefixAgentCommand,),
        {"argument_help": f"Apply to this agent's {label} instead of root {label}."},
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
        {"argument_help": f"Apply to this agent's {label} instead of root {label}."},
    )


def _kind_group_cls(group_cls: type[TyperGroup] | None, label: str) -> type[TyperGroup] | None:
    if group_cls is None:
        return None
    return type(
        f"{label.title().replace(' ', '')}ScopeGroup",
        (group_cls,),
        {"argument_help": f"Apply to this agent's {label} instead of root {label}."},
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
            "-f",
            help="Filter caps with selector-list syntax.",
        ),
    ] = None,
) -> None:
    selected_agent = _context_agent(ctx)
    agent_name = selected_agent or "default"
    effective_visibility = "all" if selected_agent else "shared"
    entries = _all_cap_entries(
        _context_root(ctx),
        agent_name,
        visibility=effective_visibility,
        prepare=selected_agent is not None,
        kinds=set(cast(tuple[EntryKind, ...], CAP_KINDS)),
    )
    try:
        selected_entries = cap_store.select_cap_entries(
            entries,
            _cap_filter_selectors(filter_, implicit_kind=None),
            agent_name=agent_name,
        )
    except ToolangError as exc:
        raise click.ClickException(str(exc)) from exc
    rows = [
        (
            cast(CapKind, entry.kind),
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
                "-f",
                help="Filter caps with selector-list syntax.",
            ),
        ] = None,
    ) -> None:
        selected_agent = _context_agent(ctx)
        agent_name = selected_agent or "default"
        effective_visibility = "all" if selected_agent else "shared"
        entries = _all_cap_entries(
            _context_root(ctx),
            agent_name,
            visibility=effective_visibility,
            prepare=selected_agent is not None,
            kinds={cast(EntryKind, kind)},
        )
        try:
            selected_entries = cap_store.select_cap_entries(
                entries,
                _cap_filter_selectors(filter_, implicit_kind=cast(EntryKind, kind)),
                agent_name=agent_name,
                implicit_kind=cast(EntryKind, kind),
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
        from .progress import as_progress_sink

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
            raise click.ClickException(f"Wired {kind} {ref} not found") from exc
        entry = _named_entry(
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=cap_store.remote_entry_name(cast(EntryKind, kind), ref),
            source_origin="remote",
            source_form="wired",
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
            source_form="wired",
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


def _entry_source(entry: PreparedEntry, *, agent_name: str) -> str:
    form = _entry_form(entry)
    if form in {"ref", "wired"}:
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


def _entry_scope_label(entry: PreparedEntry, *, agent_name: str) -> CapScope:
    return cap_store.entry_scope(entry, agent_name=agent_name)


def _cap_filter_selectors(value: str | None, *, implicit_kind: EntryKind | None) -> tuple[str, ...]:
    if value is None:
        return ()
    items = cap_store.split_cap_selectors((value,))
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
    visibility: PreparedVisibility | Literal["all"],
    prepare: bool,
    kinds: set[EntryKind],
) -> tuple[PreparedEntry, ...]:
    from ..state.durable import scan_durable_state

    durable = _wrap_user_error(scan_durable_state, toolang_root, agent_name)
    if prepare and durable.program_source is not None:
        from .progress import as_progress_sink, make_cli_progress

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
    update_kind = cast("UpdateKind", f"{kind}_changed")
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
    from .progress import make_cli_progress

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
    from ..state.durable import scan_durable_state
    from .progress import as_progress_sink

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
