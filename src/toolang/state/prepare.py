"""Prepare self-contained root and home State layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
import logging
from pathlib import Path

from toolang.common.layout import AgentLayout, ensure_scratch_workspace
from toolang.common.source import AcquiredSource
from toolang.catalog import source as cap_source

from ..common.progress import ProgressSink, emit_progress
from ..lang.ast import Program, parse_cap_declarations
from .acceptance import accept_sources
from .state import (
    AgentState,
    StateCap,
    materialize_program_caps,
    materialize_scope,
    layer_remote_cache,
    compose_agent_state,
    effective_caps,
    merge_materialized_files,
    _allowed_caps,
)
from .config import (
    canonical_state_config,
    normalize_cap_overrides,
    parse_config,
    resolve_cap_allows,
)
from .types import CapScope, EntryKind, StateFile
from .source import (
    SourceChangedError,
    scan_home_source,
    scan_root_source,
    SourceFile,
    SourceManifest,
    SourceObservation,
    SourceSnapshot,
    parse_agent_program,
    observe_home_source,
    observe_root_source,
    read_authored_source,
    read_root_source,
    source_manifest_from_snapshot,
)
from .cache import (
    AGENT_STATE_FORMAT,
    HomeLayer,
    LayerScope,
    RootLayer,
    _agent_publish_lock,
    _persist_agent_revision,
    agent_revision_dir,
    load_agent_revisions,
    load_current_revision,
    load_home_layer,
    load_root_layer,
    layer_build_lock,
    publish_layer_current,
    publish_agent_current,
    write_layer,
)

_MAX_SOURCE_SNAPSHOT_ATTEMPTS = 3
logger = logging.getLogger(__name__)


def parse_visiting_agent(
    layout: AgentLayout,
    *,
    ref: str,
    source: str | None = None,
    progress_id: str | None = None,
    progress: ProgressSink | None = None,
) -> Program:
    """Parse the visiting agent before any root or home dependency preparation."""

    text = source if source is not None else layout.program.read_bytes().decode("utf-8")
    digest = sha256(text.encode("utf-8")).hexdigest()
    prepared = None
    try:
        home = load_home_layer(layout)
        if (
            home.schema == AGENT_STATE_FORMAT
            and home.module_digests.get("agent") == digest
        ):
            prepared = home.modules.get("agent")
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        pass
    return parse_agent_program(
        layout.program,
        text,
        ref=ref,
        progress=progress,
        prepared=prepared,
        progress_id=progress_id,
    )


def prepare_agent_state(
    layout: AgentLayout,
    *,
    force: bool = False,
    progress: ProgressSink | None = None,
    allow_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
    previous: AgentState | None = None,
    workspace_additions: Mapping[str, str] | None = None,
) -> AgentState:
    """Prepare and compose the immutable runtime state for one agent."""

    _require_place(layout)
    _require_agent_home(layout)
    ensure_scratch_workspace(layout.home)
    overrides = normalize_cap_overrides(allow_overrides)
    with _agent_publish_lock(layout, progress=progress):
        root, home = prepare_root_home(
            layout,
            force=force,
            progress=progress,
        )
        revision = _persist_agent_revision(
            layout,
            root_revision=root.revision,
            home_revision=home.revision,
            allow_overrides=overrides,
            workspace_additions=workspace_additions,
            publish=False,
        )
        created_at = load_agent_revisions(layout, revision)[-1]
        revision_dir = agent_revision_dir(layout, revision)
        state = (
            previous
            if previous is not None
            and previous.revision == revision
            and previous.revision_dir == revision_dir
            else compose_layer_state(
                root,
                home,
                place=layout.place,
                name=layout.name,
                allow_overrides=overrides,
                workspace_additions=workspace_additions,
                revision_dir=revision_dir,
                created_at=created_at,
            )
        )
        publish_agent_current(layout, revision)
        return state


def compose_layer_state(
    root: RootLayer,
    home: HomeLayer,
    *,
    place: Path | None = None,
    revision_dir: Path | None = None,
    name: str,
    allow_overrides: Mapping[str, tuple[str, ...]] | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    created_at: str | None = None,
) -> AgentState:
    """Compose runtime State from one exact root/home layer pair."""

    return compose_agent_state(
        place=place,
        name=name,
        allow_overrides=allow_overrides,
        workspace_additions=workspace_additions,
        root_revision=root.revision,
        home_revision=home.revision,
        created_at=created_at,
        source_requests=tuple(
            (resolution.kind, resolution.declared_ref, resolution.resolved_ref)
            for resolution in (*root.resolutions, *home.resolutions)
        ),
        root_config=root.config,
        home_config=home.config,
        root_caps=root.caps,
        home_caps=home.caps,
        modules=home.modules,
        module_sources=home.module_sources,
        module_digests=home.module_digests,
        module_caps=home.module_caps,
        files=(
            *(root.records or _source_files("root", root.source)),
            *(home.records or _source_files("home", home.source)),
        ),
        revision_dir=revision_dir,
        chores=home.chores,
    )


def _source_files(scope: LayerScope, source: SourceManifest) -> tuple[StateFile, ...]:
    return tuple(StateFile(scope, item.path, item.digest) for item in source.files)


def load_agent_state(
    layout: AgentLayout,
    revision: str | None = None,
) -> AgentState:
    """Load one durable Agent State without consulting authored source."""

    (
        effective,
        root_revision,
        home_revision,
        name,
        overrides,
        workspace_additions,
        created_at,
    ) = load_agent_revisions(layout, revision)
    root = load_root_layer(layout, root_revision)
    home = load_home_layer(layout, home_revision)
    state = compose_layer_state(
        root,
        home,
        revision_dir=agent_revision_dir(layout, effective),
        place=layout.place,
        name=name,
        created_at=created_at,
        allow_overrides=overrides,
        workspace_additions=workspace_additions,
    )
    if state.revision != effective:
        raise ValueError("Agent State composition revision mismatch")
    return state


def load_state_caps(
    layout: AgentLayout, state: AgentState, *, scope: LayerScope
) -> tuple[StateCap, ...]:
    """Read one captured layer, including caps shadowed by another scope."""

    if scope == "root":
        return load_root_layer(layout, state.root_revision).caps
    return load_home_layer(layout, state.home_revision).caps


def refresh_agent_state(
    layout: AgentLayout,
    *,
    progress: ProgressSink | None = None,
) -> AgentState:
    """Explicitly refresh remote resolutions and prepare one agent state."""

    return prepare_agent_state(
        layout,
        force=True,
        progress=progress,
    )


def prepare_root_home(
    layout: AgentLayout,
    *,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> tuple[RootLayer, HomeLayer]:
    """Prepare and load the shared root and one agent home."""

    _require_place(layout)
    _require_agent_home(layout)
    if layout.placement == "visiting":
        parse_visiting_agent(layout, ref=str(layout.program), progress=progress)
    root = prepare_root(
        layout,
        force=force,
        progress=progress,
    )
    home = prepare_home(
        layout,
        force=force,
        progress=progress,
    )
    return root, home


def prepare_root(
    layout: AgentLayout,
    *,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> RootLayer:
    """Build or reuse the root State layer shared by every agent."""

    _require_place(layout)
    source = scan_root_source(layout.place)
    current = _matching_root(
        layout,
        source=source,
        force=force,
    )
    if current is not None:
        return current
    with layer_build_lock(layout, "root", progress=progress):
        source = scan_root_source(layout.place)
        current = _matching_root(
            layout,
            source=source,
            force=force,
        )
        if current is not None:
            return current
        _prepare_layer(
            layout,
            scope="root",
            cap_scope="root",
            reuse_remote=not force,
            progress=progress,
        )
        return load_root_layer(layout)


def inspect_root_caps(
    layout: AgentLayout,
    *,
    kinds: set[EntryKind],
    progress: ProgressSink | None = None,
) -> tuple[tuple[StateCap, ...], tuple[StateCap, ...]]:
    """Read complete and allowed caps from the shared root layer, without a home."""

    root = prepare_root(layout, progress=progress)
    entries = tuple(cap for cap in root.caps if cap.kind in kinds)
    allowed = _allowed_caps(
        entries,
        place=layout.place,
        agent_name="",
        allows=resolve_cap_allows((root.config,)),
    )
    return entries, allowed


def inspect_agent_caps(
    layout: AgentLayout,
    *,
    kinds: set[EntryKind],
    progress: ProgressSink | None = None,
) -> tuple[tuple[StateCap, ...], tuple[StateCap, ...]]:
    """Resolve current agent capabilities without preparing executable State."""
    _require_agent_home(layout)
    root = prepare_root(layout, progress=progress)
    authored = read_authored_source(layout.place, layout.name)
    accepted = accept_sources(authored, scope="home", include_programs=False)
    for record in accepted.records:
        if not record.accepted:
            logger.warning(
                "source.excluded path=%s message=%s", record.key, record.message
            )
    authored = accepted.snapshot
    previous = _previous_home_layer(layout)
    home, _ = materialize_scope(
        authored,
        scope="home",
        include_program=False,
        progress=progress,
        remote_cache=layer_remote_cache(
            authored, scope="home", entries=previous.caps if previous else ()
        ),
    )
    try:
        source = authored.load_program()
        here, _ = materialize_program_caps(
            authored,
            source,
            program=parse_cap_declarations(source.source_text),
            progress=progress,
            remote_cache=layer_remote_cache(
                authored,
                scope="home",
                entries=previous.module_caps.get("agent", ()) if previous else (),
            ),
        )
    except Exception as exc:
        logger.warning("source.excluded path=agent.too message=%s", exc)
        here = ()
    entries = tuple(
        cap
        for cap in effective_caps(effective_caps(root.caps, home), here)
        if cap.kind in kinds
    )
    config_path = f"agents/{layout.name}/config.toml"
    home_config = next(
        (
            parse_config(item.content)
            for item in authored.files
            if item.relative_path == config_path
        ),
        {},
    )
    allowed = _allowed_caps(
        entries,
        place=layout.place,
        agent_name=layout.name,
        allows=resolve_cap_allows((root.config, home_config)),
    )
    return entries, allowed


def prepare_home(
    layout: AgentLayout,
    *,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> HomeLayer:
    """Build or reuse one agent-home State layer."""

    _require_agent_home(layout)
    source = scan_home_source(layout.place, layout.name)
    current = _matching_home(
        layout,
        source=source,
        force=force,
    )
    if current is not None:
        return current
    with layer_build_lock(layout, "home", progress=progress):
        source = scan_home_source(layout.place, layout.name)
        current = _matching_home(
            layout,
            source=source,
            force=force,
        )
        if current is not None:
            return current
        _prepare_layer(
            layout,
            scope="home",
            cap_scope="home",
            reuse_remote=not force,
            progress=progress,
        )
        return load_home_layer(layout)


def _matching_root(
    layout: AgentLayout,
    *,
    source: SourceManifest,
    force: bool,
) -> RootLayer | None:
    if force:
        return None
    try:
        revision = load_current_revision(layout, "root")
        layer = load_root_layer(layout, revision)
        if layer.schema != AGENT_STATE_FORMAT:
            return None
        if layer.source != source:
            return None
        _validate_current_sources(layer)
        return layer
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _matching_home(
    layout: AgentLayout,
    *,
    source: SourceManifest,
    force: bool,
) -> HomeLayer | None:
    if force:
        return None
    try:
        revision = load_current_revision(layout, "home")
        layer = load_home_layer(layout, revision)
        if layer.schema != AGENT_STATE_FORMAT:
            return None
        if layer.source != source:
            return None
        if "workspaces" in layer.config:
            from toolang.common.config_sources import rebase_config

            raw = parse_config((layer.revision_dir / "source/config.toml").read_bytes())
            if (
                rebase_config(raw, layout.home).get("workspaces")
                != layer.config["workspaces"]
            ):
                return None
        acquired = _matching_program_acquisition(layout, source)
        expected = {"agent": acquired.resolution()} if acquired else {}
        if layer.module_resolutions != expected:
            return None
        _validate_current_sources(layer)
        return layer
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _validate_current_sources(layer: RootLayer | HomeLayer) -> None:
    """Ask Source owners whether effective selection still matches cached results."""
    for resolution in layer.resolutions:
        if not cap_source.reusable(
            resolution.kind, resolution.declared_ref, resolution.resolved_ref
        ):
            raise ValueError(
                "Source selection changed; recorded resolution cannot be reused"
            )


def _matching_program_acquisition(
    layout: AgentLayout, source: SourceManifest
) -> AcquiredSource | None:
    """Keep upstream provenance only for the exact captured Program bytes."""
    acquired = layout.program_acquisition
    if acquired is None:
        return None
    captured = acquired.acquisition
    digest = sha256(captured.files[captured.entry]).hexdigest()
    return (
        acquired
        if any(
            file.path == "agent.too" and file.digest == digest for file in source.files
        )
        else None
    )


def _prepare_layer(
    layout: AgentLayout,
    *,
    scope: LayerScope,
    cap_scope: CapScope,
    reuse_remote: bool,
    progress: ProgressSink | None,
) -> str:
    progress_id = f"agent:{layout.name}:{cap_scope}"
    subject = "root caps" if scope == "root" else f"home caps for {layout.name}"
    emit_progress(
        progress,
        id=progress_id,
        kind="prepare",
        stage="materialize",
        label=f"Preparing {subject}...",
        status="running",
        detail=layout.name,
        subject="state",
    )
    try:
        revision, entry_count = _prepare_layer_revision(
            layout,
            scope=scope,
            cap_scope=cap_scope,
            reuse_remote=reuse_remote,
            progress=progress,
        )
    except Exception as exc:
        emit_progress(
            progress,
            id=progress_id,
            kind="prepare",
            stage="materialize",
            label=f"Failed to prepare {subject}",
            status="failed",
            detail=str(exc),
            subject="state",
        )
        raise
    emit_progress(
        progress,
        id=progress_id,
        kind="prepare",
        stage="materialize",
        label=f"Prepared {subject}",
        status="ok",
        detail=f"{entry_count} entries",
        subject="state",
    )
    return revision


def _prepare_layer_revision(
    layout: AgentLayout,
    *,
    scope: LayerScope,
    cap_scope: CapScope,
    reuse_remote: bool,
    progress: ProgressSink | None,
) -> tuple[str, int]:
    with cap_source.acquisition_session(refresh=not reuse_remote):
        return _build_layer_revision(
            layout,
            scope=scope,
            cap_scope=cap_scope,
            progress=progress,
        )


def _build_layer_revision(
    layout: AgentLayout,
    *,
    scope: LayerScope,
    cap_scope: CapScope,
    progress: ProgressSink | None,
) -> tuple[str, int]:
    for _ in range(_MAX_SOURCE_SNAPSHOT_ATTEMPTS):
        observation = _observe_scope_source(layout, scope=scope)
        try:
            authored = (
                read_root_source(layout.place)
                if scope == "root"
                else read_authored_source(layout.place, layout.name)
            )
        except SourceChangedError:
            continue
        source = source_manifest_from_snapshot(authored, scope=scope)
        if observation != _observe_scope_source(layout, scope=scope):
            continue
        previous_entries = _previous_state_caps(layout, scope=scope)
        remote_cache = layer_remote_cache(
            authored,
            scope=cap_scope,
            entries=previous_entries,
        )
        previous = _previous_home_layer(layout) if scope == "home" else None
        accepted = accept_sources(
            authored,
            scope=scope,
            previous_module_caps=dict(previous.module_caps) if previous else {},
            progress=progress,
        )
        selected = accepted.snapshot
        entries, generated_files = materialize_scope(
            selected,
            scope=cap_scope,
            remote_cache=remote_cache or None,
            progress=progress,
            include_program=False,
        )
        modules: dict[str, Program] = {}
        module_sources: dict[str, str] = {}
        module_caps: dict[str, tuple[StateCap, ...]] = {}
        for prepared in accepted.programs:
            name, module_source, program = (
                prepared.name,
                prepared.source,
                prepared.program,
            )
            here_entries, here_files = prepared.caps, prepared.files
            merge_materialized_files(generated_files, here_files)
            modules[name] = program
            module_sources[name] = module_source.authored_path
            module_caps[name] = here_entries
        files = _snapshot_files(
            selected,
            generated_files,
            cap_scope=cap_scope,
        )
        entries = tuple(
            _snapshot_entry(
                entry,
                agent_name=authored.agent_name,
                cap_scope=cap_scope,
            )
            for entry in entries
        )
        if modules:
            module_caps = {
                name: tuple(
                    _snapshot_entry(
                        entry,
                        agent_name=authored.agent_name,
                        cap_scope=cap_scope,
                    )
                    for entry in module_caps[name]
                )
                for name in modules
            }
        module_resolutions = {}
        acquired = (
            _matching_program_acquisition(layout, source) if scope == "home" else None
        )
        if acquired is not None and "source/agent.too" in files:
            captured = acquired.acquisition
            if files.get("source/agent.too") != captured.files[captured.entry]:
                raise ValueError("visiting Program changed after acquisition")
            merge_materialized_files(
                files,
                {
                    f"materialized/{captured.key}/{key}": content
                    for key, content in captured.files.items()
                },
            )
            module_resolutions["agent"] = acquired.resolution()
        if observation != _observe_scope_source(layout, scope=scope):
            continue
        revision = write_layer(
            layout=layout,
            scope=scope,
            source=source,
            config=_snapshot_config(selected, cap_scope=cap_scope),
            caps=entries,
            modules=modules,
            module_sources=module_sources,
            module_caps=module_caps,
            files=files,
            records=accepted.records,
            chores=accepted.chores,
            module_resolutions=module_resolutions,
        )
        publish_layer_current(
            layout,
            scope,
            revision,
        )
        return revision, len(entries)
    raise RuntimeError(f"{scope} source changed repeatedly while preparing")


def _previous_state_caps(
    layout: AgentLayout,
    *,
    scope: LayerScope,
) -> tuple[StateCap, ...]:
    try:
        revision = load_current_revision(layout, scope)
        if scope == "root":
            return load_root_layer(layout, revision).caps
        return load_home_layer(layout, revision).caps
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return ()


def _previous_home_layer(layout: AgentLayout) -> HomeLayer | None:
    try:
        revision = load_current_revision(layout, "home")
        return load_home_layer(layout, revision)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _observe_scope_source(
    layout: AgentLayout,
    *,
    scope: LayerScope,
) -> SourceObservation:
    if scope == "root":
        return observe_root_source(layout.place)
    return observe_home_source(layout.place, layout.name)


def _require_place(layout: AgentLayout) -> None:
    if not layout.place.is_dir():
        raise FileNotFoundError(f"agent root not found: {layout.place}")


def _require_agent_home(layout: AgentLayout) -> None:
    if not layout.home.is_dir():
        raise FileNotFoundError(f"agent home not found: {layout.home}")


def _snapshot_files(
    authored: SourceSnapshot, generated_files: dict[str, bytes], *, cap_scope: CapScope
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for item in authored.files:
        if not _file_belongs_to_cap_scope(item, cap_scope=cap_scope):
            continue
        relative = _scope_relative_path(
            Path(item.relative_path),
            agent_name=authored.agent_name,
            cap_scope=cap_scope,
        )
        files[f"source/{relative.as_posix()}"] = (
            item.raw_content if item.raw_content is not None else item.content
        )
    for path, content in generated_files.items():
        if path in files and files[path] != content:
            raise ValueError(f"conflicting acquired file: {path}")
        files[path] = content
    return files


def _file_belongs_to_cap_scope(
    item: SourceFile,
    *,
    cap_scope: CapScope,
) -> bool:
    return item.layer == ("root" if cap_scope == "root" else "agent")


def _scope_relative_path(
    path: Path,
    *,
    agent_name: str,
    cap_scope: CapScope,
) -> Path:
    if cap_scope == "root":
        return path
    prefix = Path("agents") / agent_name
    try:
        return path.relative_to(prefix)
    except ValueError as exc:
        raise ValueError(f"home source is outside the agent directory: {path}") from exc


def _snapshot_entry(
    entry: StateCap, *, agent_name: str, cap_scope: CapScope
) -> StateCap:
    return replace(
        entry,
        path=_entry_snapshot_path(
            entry, agent_name=agent_name, cap_scope=cap_scope
        ).as_posix(),
    )


def _entry_snapshot_path(
    entry: StateCap, *, agent_name: str, cap_scope: CapScope
) -> Path:
    if entry.source.form in {"authored", "inline"}:
        relative = _scope_relative_path(
            Path(entry.path if entry.source.form == "authored" else entry.source.path),
            agent_name=agent_name,
            cap_scope=cap_scope,
        )
        return Path("source") / relative
    return Path(entry.path)


def _resolved_definition_path(entry: StateCap) -> Path:
    if entry.source.form == "configured":
        return Path("config.toml")
    path = Path(entry.source.path)
    if path.parts[:1] == ("agents",) and len(path.parts) >= 3:
        return Path(*path.parts[2:])
    return path


def _snapshot_config(
    authored: SourceSnapshot, *, cap_scope: CapScope
) -> dict[str, object]:
    item = next(
        (
            item
            for item in authored.files
            if item.category == "config"
            and _file_belongs_to_cap_scope(item, cap_scope=cap_scope)
        ),
        None,
    )
    return parse_config(canonical_state_config(item.content)) if item else {}
