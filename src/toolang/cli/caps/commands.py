"""Cap subcommands."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer
from typer._click.exceptions import ClickException
from typer.core import TyperGroup

from toolang.cli.common.parameters import TextType

from toolang.cli.common.editor import edit_markdown
from ...catalog import templates
from ...catalog.errors import CatalogConflictError
from ...common.layout import AgentLayout
from toolang.catalog import cap as cap_store
from toolang.catalog import config as cap_config
from toolang.catalog.types import CAP_KINDS, CapKind
from toolang.state import state as cap_state
from toolang.state.prepare import inspect_root_caps, prepare_agent_state
from ..common.context import context_agent, context_root, user_call
from ..common.help import CliCommand
from ..common.output import echo_block, echo_collection_summary, echo_table
from ..common.query import query_items
from ..common.routing import (
    OptionalPrefixAgentCommand,
    OptionalPrefixAgentListCommand,
)

if TYPE_CHECKING:
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
        {"argument_help": f"Modify the agent's home {label}; omit for root {label}"},
    )


def _kind_list_command_cls(label: str) -> type[OptionalPrefixAgentListCommand]:
    return type(
        f"{label.title().replace(' ', '')}ListScopeCommand",
        (OptionalPrefixAgentListCommand,),
        {"argument_help": f"Local agent name; omit for root {label} only"},
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
        "psyche": "Manage psyche caps",
        "skill": "Manage skill caps",
        "service": "Manage service caps",
        "prompt": "Manage prompt caps",
    }
    cap_list_help: dict[CapKind, str] = {
        "psyche": "List psyches",
        "skill": "List skills",
        "service": "List services",
        "prompt": "List prompts",
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
            help=lambda kind: f"Create a file-backed {kind}",
            factory=_make_new_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="edit",
            help=lambda kind: f"Edit a file-backed {kind}",
            factory=_make_edit_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="delete",
            help=lambda kind: f"Delete a file-backed {kind}",
            factory=_make_delete_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="add",
            help=lambda kind: f"Wire a {kind} ref",
            factory=_make_add_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="remove",
            help=lambda kind: f"Unwire a {kind}",
            factory=_make_remove_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="template",
            help=lambda kind: f"Inspect {kind} templates",
            factory=_make_template_command,
        ),
    )

    apps: dict[CapKind, typer.Typer] = {}
    for kind in cap_titles:
        title = cap_titles[kind]
        label = cap_labels[kind]
        command_cls = _kind_command_cls(label)
        list_command_cls = _kind_list_command_cls(label)
        cap_app = typer.Typer(
            help=cap_group_help[kind],
            cls=group_cls,
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
                    CliCommand
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
            metavar="QUERY",
            help="Query cap collections. Repeat to add matches; see 'too query'",
        ),
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", "-a", help="Include allow-excluded caps")
    ] = False,
) -> None:
    from toolang.state.collections import cap_table, query_cap_views

    selected_agent = context_agent(ctx)
    agent_name = selected_agent or "default"
    entries, allowed = _cap_entries(
        context_root(ctx),
        agent_name,
        prepare=selected_agent is not None,
        kinds=set(CAP_KINDS),
    )
    selected = user_call(
        query_cap_views,
        entries if all_ else allowed,
        agent_name=agent_name,
        queries=query,
    )
    headers, rows = cap_table(
        selected, allowed=_allowed_cap_keys(allowed) if all_ else None
    )
    if rows:
        echo_table(headers, rows)
    echo_collection_summary(
        len(selected), "cap", group=(len({cap.kind for cap in selected}), "kind")
    )


def _make_cap_list_command(kind: CapKind, title: str) -> Callable[..., None]:
    def list_caps(
        ctx: typer.Context,
        query: Annotated[
            list[str] | None,
            typer.Option(
                "--query",
                "-q",
                metavar="QUERY",
                help=(f"Query {kind}s. Repeat to add matches; see 'too query {kind}s'"),
            ),
        ] = None,
        all_: Annotated[
            bool, typer.Option("--all", "-a", help="Include allow-excluded caps")
        ] = False,
    ) -> None:
        from toolang.state.collections import cap_dataset, cap_table

        selected_agent = context_agent(ctx)
        agent_name = selected_agent or "default"
        entries, allowed = _cap_entries(
            context_root(ctx),
            agent_name,
            prepare=selected_agent is not None,
            kinds={kind},
        )
        dataset = cap_dataset(
            entries if all_ else allowed, agent_name=agent_name, kind=kind
        )
        selected = query_items(dataset, query)
        headers, rows = cap_table(
            selected, kind=kind, allowed=_allowed_cap_keys(allowed) if all_ else None
        )
        if rows:
            echo_table(headers, rows)
        echo_collection_summary(len(selected), kind)

    return list_caps


def _make_new_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def new_cap(
        ctx: typer.Context,
        name: Annotated[
            str,
            typer.Argument(metavar="NAME", click_type=TextType(), help=f"{title} name"),
        ],
        template: Annotated[
            str,
            typer.Option("--template", "-t", metavar="NAME", help="Template name"),
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
            raise ClickException(f"{title} {name} already exists")
        text = edit_markdown(
            templates.render_template(kind, template, name=name, agent_name=agent_name),
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
        name: Annotated[
            str,
            typer.Argument(metavar="NAME", click_type=TextType(), help=f"{title} name"),
        ],
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
            raise ClickException(f"{title} {name} not found") from exc
        updated_text = edit_markdown(text)
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
        ref: Annotated[
            str,
            typer.Argument(metavar="REF", click_type=TextType(), help=f"{title} ref"),
        ],
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
            raise ClickException(
                f"{title} {cap_state.remote_entry_name(kind, ref)} already exists"
            ) from exc
        except ValueError as exc:
            if progress.failure_stage is not None:
                raise ClickException(progress.failure_message(exc)) from exc
            message = str(exc)
            if "conflicting entries" in message:
                raise ClickException(
                    f"{title} {cap_state.remote_entry_name(kind, ref)} already exists"
                ) from exc
            raise ClickException(f"Configured {kind} {ref} not found") from exc
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
        name: Annotated[
            str,
            typer.Argument(metavar="NAME", click_type=TextType(), help=f"{title} name"),
        ],
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
        name: Annotated[
            str,
            typer.Argument(metavar="NAME", click_type=TextType(), help=f"{title} name"),
        ],
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
            typer.Argument(help="Template name", metavar="NAME"),
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


def _entry_form(entry: "StateCap") -> CapForm:
    return cap_state.entry_form(entry)


def _entry_scope_label(entry: "StateCap", *, agent_name: str) -> CapScope:
    return cap_state.entry_scope(entry, agent_name=agent_name)


def _cap_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    prepare: bool,
    kinds: set[EntryKind],
) -> "tuple[tuple[StateCap, ...], tuple[StateCap, ...]]":
    from ..common.progress import make_cli_progress

    if not prepare and not toolang_root.exists():
        return (), ()
    layout = AgentLayout.resident(toolang_root, agent_name)
    progress = make_cli_progress()
    try:
        with progress:
            if not prepare:
                return user_call(
                    inspect_root_caps,
                    layout,
                    kinds=kinds,
                    progress=progress.sink,
                )
            state = user_call(prepare_agent_state, layout, progress=progress.sink)
            entries = tuple(cap for cap in state.caps.values() if cap.kind in kinds)
            allowed = tuple(cap for cap in state.caps_for("agent") if cap.kind in kinds)
            return entries, allowed
    except Exception as exc:
        if progress.failure_stage is not None:
            raise ClickException(progress.failure_message(exc)) from exc
        raise


def _allowed_cap_keys(
    entries: "tuple[StateCap, ...]",
) -> frozenset[tuple[EntryKind, str]]:
    return frozenset((cap.kind, cap.name) for cap in entries)


def _named_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: MutableScope,
    kind: EntryKind,
    name: str,
    source_origin: Literal["local", "remote"] | None = None,
    source_form: cap_state.CapForm | None = None,
) -> "StateCap":
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
    raise ClickException(f"{kind.title()} {name} not found")


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


def _make_cap_write_progress() -> "CliProgress":
    from ..common.progress import make_cli_progress

    return make_cli_progress()


def _refresh_agent_state(
    toolang_root: Path,
    agent_name: str,
    *,
    progress: "CliProgress | None" = None,
) -> None:
    if progress is not None:
        _prepare_agent_state_with_progress(toolang_root, agent_name, progress)
        return
    with _make_cap_write_progress() as owned_progress:
        _prepare_agent_state_with_progress(toolang_root, agent_name, owned_progress)


def _prepare_agent_state_with_progress(
    toolang_root: Path,
    agent_name: str,
    progress: "CliProgress",
) -> None:
    try:
        user_call(
            prepare_agent_state,
            AgentLayout.resident(toolang_root, agent_name),
            progress=progress.sink,
        )
    except Exception as exc:
        if progress.failure_stage is not None:
            raise ClickException(progress.failure_message(exc)) from exc
        raise
