"""Cap subcommands."""

from toolang.catalog import source as cap_source

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
from ...common.layout import AgentLayout, home_edit_lock_path, root_edit_lock_path
from toolang.catalog import cap as cap_store
from toolang.catalog import config as cap_config
from toolang.catalog.types import CAP_KINDS, CapKind
from toolang.state import state as cap_state
from toolang.state.prepare import (
    inspect_root_caps,
    inspect_agent_caps,
    prepare_agent_state,
)
from ..common.context import context_agent, context_layout, context_root, user_call
from ..common.output import echo_collection_summary
from ..common.records import check_output_options, echo_records
from ..common.routing import (
    OptionalPrefixAgentCommand,
    OptionalPrefixAgentListCommand,
)

if TYPE_CHECKING:
    from toolang.state.state import StateCap

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


def _kind_list_command_cls(
    label: str, agent_help: str
) -> type[OptionalPrefixAgentListCommand]:
    return type(
        f"{label.title().replace(' ', '')}ListScopeCommand",
        (OptionalPrefixAgentListCommand,),
        {"argument_help": f"{agent_help}; omit for root {label} only"},
    )


def create_cap_apps(
    *,
    group_cls: type[TyperGroup] | None = None,
    agent_help: str = "Local agent name",
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
            help=lambda kind: f"Wire a {kind} Source Ref",
            factory=_make_add_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="remove",
            help=lambda kind: f"Unwire a {kind}",
            factory=_make_remove_cap_command,
            no_args_is_help=True,
        ),
    )

    apps: dict[CapKind, typer.Typer] = {}
    for kind in cap_titles:
        title = cap_titles[kind]
        label = cap_labels[kind]
        command_cls = _kind_command_cls(label)
        list_command_cls = _kind_list_command_cls(label, agent_help)
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
                cls=(list_command_cls if spec.name == "list" else command_cls),
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
            help="TQ query over cap (kind/name), source, and tags; repeat for union",
        ),
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", "-a", help="Include allow-excluded caps")
    ] = False,
    json_: Annotated[
        bool, typer.Option("--json", help="Write public cap records as JSON")
    ] = False,
    human: Annotated[
        bool, typer.Option("--human", help="Display a table (default)")
    ] = False,
) -> None:
    from toolang.state.collections import query_cap_views

    check_output_options(human=human, json_=json_)

    selected_agent = context_agent(ctx)
    agent_name = selected_agent or "default"
    layout = (
        context_layout(ctx)
        if selected_agent is not None
        else AgentLayout.resident(context_root(ctx), agent_name)
    )
    entries, allowed = _cap_entries(
        layout,
        include_agent=selected_agent is not None,
        kinds=set(CAP_KINDS),
    )
    selected = user_call(
        query_cap_views,
        entries if all_ else allowed,
        root=layout.root,
        agent_name=agent_name,
        queries=query,
        allowed=_allowed_cap_keys(allowed),
    )
    echo_records(
        [view.data for view in selected],
        ("cap", "description", "location", "tags"),
        json_=json_,
    )
    if not json_:
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
                help="TQ query over cap (kind/name), source, and tags; repeat for union",
            ),
        ] = None,
        all_: Annotated[
            bool, typer.Option("--all", "-a", help="Include allow-excluded caps")
        ] = False,
        json_: Annotated[
            bool, typer.Option("--json", help="Write public cap records as JSON")
        ] = False,
        human: Annotated[
            bool, typer.Option("--human", help="Display a table (default)")
        ] = False,
    ) -> None:
        from toolang.state.collections import cap_collection

        check_output_options(human=human, json_=json_)

        selected_agent = context_agent(ctx)
        agent_name = selected_agent or "default"
        layout = (
            context_layout(ctx)
            if selected_agent is not None
            else AgentLayout.resident(context_root(ctx), agent_name)
        )
        entries, allowed = _cap_entries(
            layout,
            include_agent=selected_agent is not None,
            kinds={kind},
        )
        dataset = cap_collection(
            entries if all_ else allowed,
            root=layout.root,
            agent_name=agent_name,
            kind=kind,
            allowed=_allowed_cap_keys(allowed),
        )
        selected = user_call(dataset.query, query)
        echo_records(
            [view.data for view in selected],
            ("cap", "description", "location", "tags"),
            json_=json_,
        )
        if not json_:
            echo_collection_summary(len(selected), kind)

    return list_caps


def _make_new_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def new_cap(
        ctx: typer.Context,
        name: Annotated[
            str,
            typer.Argument(metavar="NAME", click_type=TextType(), help=f"{title} name"),
        ],
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = context_agent(ctx)
        authored = _authored_caps(context_root(ctx), agent_name, scope)
        if user_call(authored.get, kind, name) is not None:
            raise ClickException(f"{title} {name} already exists")
        text = edit_markdown(templates.load_template(kind).raw_text)
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
        catalog = _authored_caps(context_root(ctx), agent_name, scope)
        if not catalog.path(kind, name).is_file():
            raise ClickException(f"{title} {name} not found")
        text = user_call(catalog.read, kind, name)
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
            typer.Argument(
                metavar="SOURCE-REF",
                click_type=TextType(),
                help=f"{title} source: gh:/github: ref or complete HTTP(S) URL",
            ),
        ],
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = context_agent(ctx)
        from ..common.progress import make_cli_progress

        progress = make_cli_progress(preparation=True)
        try:
            with progress:
                canonical_ref = cap_source.resolve_remote_ref(
                    kind,
                    ref,
                    progress=progress.sink,
                )
                name = cap_source.remote_entry_name(kind, canonical_ref)
                entry = _configured_caps(context_root(ctx), agent_name, scope).create(
                    cap_config.CapRef(kind=kind, name=name, ref=canonical_ref)
                )
            if selected_agent:
                _refresh_agent_state(
                    context_root(ctx),
                    selected_agent,
                )
        except CatalogConflictError as exc:
            raise ClickException(
                f"{title} {cap_source.remote_entry_name(kind, ref)} already exists"
            ) from exc
        except ValueError as exc:
            if progress.failure_stage is not None:
                raise ClickException(progress.failure_message(exc)) from exc
            message = str(exc)
            if "conflicting entries" in message:
                raise ClickException(
                    f"{title} {cap_source.remote_entry_name(kind, ref)} already exists"
                ) from exc
            raise ClickException(str(exc)) from exc
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
        entry = user_call(
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
        path = user_call(
            _authored_caps(context_root(ctx), agent_name, scope).remove_file,
            kind,
            name,
        )
        deleted_path = path.parent if kind == "skill" else path
        if selected_agent:
            _refresh_agent_state(
                context_root(ctx),
                selected_agent,
            )
        typer.echo(f"{kind.title()} {name} deleted: {deleted_path}")

    return delete_cap


def _target_scope(ctx: typer.Context) -> tuple[MutableScope, str]:
    agent_name = context_agent(ctx)
    if agent_name:
        if context_layout(ctx).placement != "resident":
            raise ClickException(
                "cap changes require a resident agent; clone the source first"
            )
        return "home", agent_name
    return "root", "default"


def _entry_form(entry: "StateCap") -> CapForm:
    return cap_state.entry_form(entry)


def _entry_scope_label(entry: "StateCap", *, agent_name: str) -> CapScope:
    return cap_state.entry_scope(entry, agent_name=agent_name)


def _cap_entries(
    layout: AgentLayout,
    *,
    include_agent: bool,
    kinds: set[EntryKind],
) -> "tuple[tuple[StateCap, ...], tuple[StateCap, ...]]":
    """Inspect root caps, optionally overlaid with the selected agent's caps."""

    from ..common.progress import make_cli_progress

    if not include_agent and not layout.root.exists():
        return (), ()
    progress = make_cli_progress(preparation=True)
    try:
        with progress:
            if not include_agent:
                result = user_call(
                    inspect_root_caps,
                    layout,
                    kinds=kinds,
                    progress=progress.sink,
                )
            else:
                result = user_call(
                    inspect_agent_caps, layout, kinds=kinds, progress=progress.sink
                )
            progress.complete_preparation()
            return result
    except Exception as exc:
        if progress.failure_stage is not None:
            raise ClickException(progress.failure_message(exc)) from exc
        raise


def _allowed_cap_keys(
    entries: "tuple[StateCap, ...]",
) -> frozenset[tuple[EntryKind, str]]:
    return frozenset((cap.kind, cap.name) for cap in entries)


def _cap_directory(
    toolang_root: Path,
    agent_name: str,
    scope: MutableScope,
) -> Path:
    return toolang_root if scope == "root" else toolang_root / "agents" / agent_name


def _cap_edit_lock(toolang_root: Path, agent_name: str, scope: MutableScope) -> Path:
    if scope == "root":
        return root_edit_lock_path(toolang_root)
    return home_edit_lock_path(_cap_directory(toolang_root, agent_name, scope))


def _authored_caps(
    toolang_root: Path,
    agent_name: str,
    scope: MutableScope,
) -> cap_store.AuthoredCaps:
    return cap_store.AuthoredCaps(
        _cap_directory(toolang_root, agent_name, scope),
        lock_path=_cap_edit_lock(toolang_root, agent_name, scope),
    )


def _configured_caps(
    toolang_root: Path,
    agent_name: str,
    scope: MutableScope,
) -> cap_config.ConfiguredCaps:
    return cap_config.ConfiguredCaps(
        _cap_directory(toolang_root, agent_name, scope) / "config.toml",
        lock_path=_cap_edit_lock(toolang_root, agent_name, scope),
    )


def _refresh_agent_state(
    toolang_root: Path,
    agent_name: str,
) -> None:
    from ..common.progress import make_cli_progress

    progress = make_cli_progress(preparation=True)
    try:
        with progress:
            user_call(
                prepare_agent_state,
                AgentLayout.resident(toolang_root, agent_name),
                progress=progress.sink,
            )
            progress.complete_preparation()
    except Exception as exc:
        message = (
            progress.failure_message(exc)
            if progress.failure_stage is not None
            else str(exc)
        )
        raise ClickException(
            f"The change was saved, but agent State could not be published: {message}"
        ) from exc
