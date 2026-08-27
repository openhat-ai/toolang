"""Prepare self-contained root and home State layers."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import logging
from pathlib import Path
import re

from toolang.common.layout import AgentLayout

from ..common.progress import ProgressSink
from ..lang.ast import Program
from .state import (
    AgentState,
    KIND_BY_DIR_NAME,
    StateModule,
    ProgramModuleExport,
    compose_agent_state,
    flow_module_name,
    public_runnable_catalog,
)
from .errors import StateDiagnostic, StatePreparationError, StateValidationLayer
from .config import parse_config
from .state import (
    materialize_program_caps,
    materialize_scope,
    layer_remote_cache,
)
from .source import (
    SourceFile,
    SourceSnapshot,
    ProgramSource,
    read_authored_source,
    read_root_source,
)
from .cache import (
    HomeLayer,
    LayerScope,
    RootLayer,
    agent_revision_dir,
    load_agent_revisions,
    load_current_revision,
    load_home_layer,
    load_root_layer,
    layer_lock,
    persist_agent_revision,
    publish_layer_current,
    write_layer,
)
from .state import (
    CapResolution,
    StateCap,
    CapScope,
    MaterializedFile,
)
from .source import SourceTree, scan_home_source, scan_root_source

_MAX_SOURCE_SNAPSHOT_ATTEMPTS = 3
_ERROR_LINE_RE = re.compile(r"\bline (\d+)\b", re.IGNORECASE)
logger = logging.getLogger(__name__)


def prepare_agent_state(
    layout: AgentLayout,
    *,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> AgentState:
    """Prepare and compose the immutable runtime state for one agent."""

    root, home = prepare_root_home(
        layout,
        force=force,
        progress=progress,
    )
    revision = persist_agent_revision(
        layout,
        root_revision=root.revision,
        home_revision=home.revision,
    )
    return compose_layer_state(
        root,
        home,
        program_source=layout.program.relative_to(layout.root).as_posix(),
        revision_dir=agent_revision_dir(layout, revision),
    )


def compose_layer_state(
    root: RootLayer,
    home: HomeLayer,
    *,
    program_source: str,
    revision_dir: Path | None = None,
) -> AgentState:
    """Compose runtime State from one exact root/home layer pair."""

    return compose_agent_state(
        root_revision=root.revision,
        home_revision=home.revision,
        root_config=root.config,
        home_config=home.config,
        program_source=program_source,
        program=home.program,
        root_caps=root.caps,
        home_caps=home.caps,
        modules=home.modules,
        revision_dir=revision_dir,
    )


def load_agent_state(
    layout: AgentLayout,
    revision: str | None = None,
) -> AgentState:
    """Load one durable Agent State without consulting authored source."""

    effective, root_revision, home_revision = load_agent_revisions(layout, revision)
    state = compose_layer_state(
        load_root_layer(layout, root_revision),
        load_home_layer(layout, home_revision),
        program_source=layout.program.relative_to(layout.root).as_posix(),
        revision_dir=agent_revision_dir(layout, effective),
    )
    if state.revision != effective:
        raise ValueError("Agent State composition revision mismatch")
    return state


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

    _require_root(layout)
    _require_agent_home(layout)
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

    _require_root(layout)
    source = scan_root_source(layout.root)
    current = _matching_root(
        layout,
        source=source,
        force=force,
    )
    if current is not None:
        return current
    with layer_lock(layout, "root"):
        source = scan_root_source(layout.root)
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


def prepare_home(
    layout: AgentLayout,
    *,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> HomeLayer:
    """Build or reuse one agent-home State layer."""

    _require_agent_home(layout)
    source = scan_home_source(layout.root, layout.name)
    current = _matching_home(
        layout,
        source=source,
        force=force,
    )
    if current is not None:
        return current
    with layer_lock(layout, "home"):
        source = scan_home_source(layout.root, layout.name)
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
    source: SourceTree,
    force: bool,
) -> RootLayer | None:
    if force:
        return None
    try:
        revision = load_current_revision(layout, "root")
        layer = load_root_layer(layout, revision)
        if layer.source != source:
            return None
        return layer
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _matching_home(
    layout: AgentLayout,
    *,
    source: SourceTree,
    force: bool,
) -> HomeLayer | None:
    if force:
        return None
    try:
        revision = load_current_revision(layout, "home")
        layer = load_home_layer(layout, revision)
        if layer.source != source:
            return None
        return layer
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _prepare_layer(
    layout: AgentLayout,
    *,
    scope: LayerScope,
    cap_scope: CapScope,
    reuse_remote: bool,
    progress: ProgressSink | None,
) -> str:
    for _ in range(_MAX_SOURCE_SNAPSHOT_ATTEMPTS):
        source = _scan_scope_source(layout, scope=scope)
        authored = (
            read_root_source(layout.root)
            if scope == "root"
            else read_authored_source(layout.root, layout.name)
        )
        previous_entries = (
            _previous_state_caps(
                layout,
                scope=scope,
            )
            if reuse_remote
            else ()
        )
        remote_cache = layer_remote_cache(
            authored,
            scope=cap_scope,
            entries=previous_entries,
        )
        entries, generated_files = materialize_scope(
            authored,
            scope=cap_scope,
            remote_cache=remote_cache or None,
            progress=progress,
            include_program=scope == "root",
        )
        modules: tuple[StateModule, ...] = ()
        module_entries: tuple[StateCap, ...] = ()
        module_sources: dict[str, ProgramSource] = {}
        if scope == "home":
            drafts = _program_module_drafts(authored)
            previous_modules = _previous_program_modules(layout)
            materialized: list[StateModule] = []
            all_module_entries: list[StateCap] = []
            for draft, module_source in drafts:
                module_sources[draft.name] = module_source
                previous = next(
                    (item for item in previous_modules if item.name == draft.name),
                    None,
                )
                module_cache = layer_remote_cache(
                    authored,
                    scope="home",
                    entries=previous.here_caps if previous is not None else (),
                )
                try:
                    here_entries, here_files = materialize_program_caps(
                        authored,
                        module_source,
                        remote_cache=module_cache or None,
                        progress=progress,
                    )
                except Exception as exc:
                    raise _module_error(
                        module_source,
                        layer="program",
                        code="module-cap-preparation",
                        error=exc,
                    ) from exc
                here_entries, here_files = _namespace_module_materialization(
                    draft.name,
                    here_entries,
                    here_files,
                )
                generated_files.update(here_files)
                all_module_entries.extend(here_entries)
                materialized.append(replace(draft, here_caps=here_entries))
            modules = tuple(materialized)
            module_entries = tuple(all_module_entries)
        files = _snapshot_files(
            authored,
            generated_files,
            cap_scope=cap_scope,
        )
        for module in modules:
            source_text = module_sources[module.name].source_text
            files.setdefault(module.authored_path, source_text.encode("utf-8"))
        entries = tuple(
            _snapshot_entry(
                entry,
                agent_name=authored.agent_name,
                files=files,
                cap_scope=cap_scope,
            )
            for entry in entries
        )
        if modules:
            modules = tuple(
                replace(
                    module,
                    here_caps=tuple(
                        _snapshot_entry(
                            entry,
                            agent_name=authored.agent_name,
                            files=files,
                            cap_scope=cap_scope,
                        )
                        for entry in module.here_caps
                    ),
                )
                for module in modules
            )
            module_entries = tuple(
                entry for module in modules for entry in module.here_caps
            )
        resolutions = _cap_resolutions((*entries, *module_entries), files)
        if source != _scan_scope_source(layout, scope=scope):
            continue
        revision = write_layer(
            layout=layout,
            scope=scope,
            source=source,
            resolutions=resolutions,
            config=_snapshot_config(files),
            caps=entries,
            modules=modules,
            files=files,
        )
        publish_layer_current(
            layout,
            scope,
            revision,
        )
        return revision
    raise RuntimeError(f"{scope} source changed repeatedly while preparing")


def _previous_state_caps(
    layout: AgentLayout,
    *,
    scope: LayerScope,
) -> tuple[StateCap, ...]:
    try:
        if scope == "root":
            return load_root_layer(layout).caps
        return load_home_layer(layout).caps
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return ()


def _previous_program_modules(
    layout: AgentLayout,
) -> tuple[StateModule, ...]:
    try:
        return load_home_layer(layout).modules
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return ()


def _scan_scope_source(
    layout: AgentLayout,
    *,
    scope: LayerScope,
) -> SourceTree:
    if scope == "root":
        return scan_root_source(layout.root)
    return scan_home_source(layout.root, layout.name)


def _require_root(layout: AgentLayout) -> None:
    if not layout.root.is_dir():
        raise FileNotFoundError(f"Toolang root not found: {layout.root}")


def _require_agent_home(layout: AgentLayout) -> None:
    if not layout.home.is_dir():
        raise FileNotFoundError(f"agent home not found: {layout.home}")


def _program_module_drafts(
    authored: SourceSnapshot,
) -> tuple[tuple[StateModule, ProgramSource], ...]:
    sources = authored.load_programs()
    programs: list[tuple[ProgramSource, Program]] = []
    diagnostics: list[StateDiagnostic] = []
    for source in sources:
        try:
            programs.append((source, source.parse()))
        except Exception as exc:
            diagnostics.append(
                _diagnostic(
                    source,
                    layer="program",
                    code="invalid-program",
                    error=exc,
                )
            )
    if diagnostics:
        raise StatePreparationError(*diagnostics)

    flow_sources = tuple(
        source for source, _program in programs if source.kind == "flow"
    )
    _validate_flow_source_names(flow_sources)

    drafts: list[tuple[StateModule, ProgramSource]] = []
    extension_diagnostics: list[StateDiagnostic] = []
    for source, program in programs:
        if source.kind == "agent":
            drafts.append(
                (
                    StateModule(
                        name="agent",
                        kind="agent",
                        authored_path=source.authored_path,
                        materialized_path=f"files/{source.authored_path}",
                        digest=source.digest,
                        program=program,
                    ),
                    source,
                )
            )
            continue
        try:
            export = _flow_module_export(source, program)
        except ValueError as exc:
            extension_diagnostics.append(
                _diagnostic(
                    source,
                    layer="flow-extension",
                    code="invalid-flow-export",
                    error=exc,
                )
            )
            continue
        drafts.append(
            (
                StateModule(
                    name=flow_module_name(source.authored_path),
                    kind="flow",
                    authored_path=source.authored_path,
                    materialized_path=f"files/{source.authored_path}",
                    digest=source.digest,
                    program=program,
                    export=export,
                ),
                source,
            )
        )
    if extension_diagnostics:
        raise StatePreparationError(*extension_diagnostics)

    modules = tuple(draft for draft, _source in drafts)
    try:
        public_runnable_catalog(modules)
    except ValueError as exc:
        agent = sources[0]
        raise StatePreparationError(
            _diagnostic(
                agent,
                layer="state-composition",
                code="public-runnable-conflict",
                error=exc,
            )
        ) from exc
    return tuple(drafts)


def _flow_module_export(
    source: ProgramSource,
    program: Program,
) -> ProgramModuleExport:
    name = Path(source.authored_path).stem
    flow_module_name(source.authored_path)
    candidates = tuple(
        flow for flow in program.flows if not flow.name_explicit or flow.name == name
    )
    if not candidates:
        raise ValueError(
            f"Flow module {source.authored_path!r} must contain an unnamed flow "
            f"or flow {name!r}"
        )
    if len(candidates) > 1:
        raise ValueError(f"Flow module entry is ambiguous: {source.authored_path}")
    return ProgramModuleExport(public_name=name, local_name=candidates[0].name)


def _validate_flow_source_names(sources: tuple[ProgramSource, ...]) -> None:
    seen: dict[str, str] = {}
    diagnostics: list[StateDiagnostic] = []
    for source in sources:
        name = Path(source.authored_path).stem
        error: ValueError | None = None
        try:
            flow_module_name(source.authored_path)
        except ValueError as exc:
            error = exc
        if error is None and (existing := seen.get(name.casefold())):
            error = ValueError(
                f"Flow module filenames collide under case folding: "
                f"{existing!r} and {name!r}"
            )
        elif error is None:
            seen[name.casefold()] = name
        if error is not None:
            diagnostics.append(
                _diagnostic(
                    source,
                    layer="flow-extension",
                    code="invalid-flow-filename",
                    error=error,
                )
            )
    if diagnostics:
        raise StatePreparationError(*diagnostics)


def _diagnostic(
    source: ProgramSource,
    *,
    layer: StateValidationLayer,
    code: str,
    error: Exception,
) -> StateDiagnostic:
    message = str(error) or type(error).__name__
    match = _ERROR_LINE_RE.search(message)
    return StateDiagnostic(
        layer=layer,
        module_kind=source.kind,
        authored_path=source.authored_path,
        line=int(match.group(1)) if match is not None else None,
        code=code,
        message=message,
    )


def _module_error(
    source: ProgramSource,
    *,
    layer: StateValidationLayer,
    code: str,
    error: Exception,
) -> StatePreparationError:
    return StatePreparationError(
        _diagnostic(source, layer=layer, code=code, error=error)
    )


def _namespace_module_materialization(
    module: str,
    entries: tuple[StateCap, ...],
    files: dict[str, bytes],
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    remapped_entries: list[StateCap] = []
    remapped_files: dict[str, bytes] = {}
    for entry in entries:
        source_path = Path(entry.path)
        target_path = _generated_cap_path(source_path, module=module)
        remapped_entries.append(replace(entry, path=target_path.as_posix()))
        source_root = (
            source_path.parent if entry.shape == "file" else source_path.parent
        )
        target_root = target_path.parent
        for path, content in files.items():
            candidate = Path(path)
            if candidate == source_path:
                remapped_files[target_path.as_posix()] = content
            elif entry.shape == "dir" and candidate.is_relative_to(source_root):
                remapped_files[
                    (target_root / candidate.relative_to(source_root)).as_posix()
                ] = content
    return tuple(remapped_entries), remapped_files


def _snapshot_files(
    authored: SourceSnapshot,
    generated_files: dict[str, bytes],
    *,
    cap_scope: CapScope,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for item in authored.files:
        if not _file_belongs_to_cap_scope(item, cap_scope=cap_scope):
            continue
        target = _authored_snapshot_path(
            item,
            agent_name=authored.agent_name,
            cap_scope=cap_scope,
        )
        files[target.as_posix()] = item.content
    for path, content in generated_files.items():
        target = _generated_snapshot_path(Path(path))
        files[target.as_posix()] = content
    return files


def _file_belongs_to_cap_scope(
    item: SourceFile,
    *,
    cap_scope: CapScope,
) -> bool:
    return item.origin == ("root" if cap_scope == "root" else "agent")


def _authored_snapshot_path(
    item: SourceFile,
    *,
    agent_name: str,
    cap_scope: CapScope,
) -> Path:
    relative = _scope_relative_path(
        Path(item.relative_path),
        agent_name=agent_name,
        cap_scope=cap_scope,
    )
    if item.category == "cap":
        if len(relative.parts) < 2:
            raise ValueError(f"unexpected authored cap path: {relative}")
        kind = KIND_BY_DIR_NAME.get(relative.parts[0])
        if kind is None:
            raise ValueError(f"unexpected authored cap directory: {relative.parts[0]}")
        return Path("caps") / "authored" / kind / Path(*relative.parts[1:])
    if item.category == "program":
        return relative
    if item.category == "config":
        return Path("config.toml")
    return Path("authored") / relative


def _generated_snapshot_path(
    path: Path,
) -> Path:
    if path.parts[:1] == ("caps",):
        return path
    return _generated_cap_path(path)


def _generated_cap_path(path: Path, *, module: str | None = None) -> Path:
    if len(path.parts) < 3:
        raise ValueError(f"unexpected materialized cap path: {path}")
    form = path.parts[0]
    if form not in {"inline", "configured", "referenced"}:
        raise ValueError(f"unexpected materialized cap bucket: {path.parts[0]}")
    kind = KIND_BY_DIR_NAME.get(path.parts[1])
    if kind is None:
        raise ValueError(f"unexpected materialized cap directory: {path.parts[1]}")
    prefix = Path("caps") / form
    if module is not None:
        prefix /= module
    return prefix / kind / Path(*path.parts[2:])


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
    entry: StateCap,
    *,
    agent_name: str,
    files: dict[str, bytes],
    cap_scope: CapScope,
) -> StateCap:
    path = _entry_snapshot_path(
        entry,
        agent_name=agent_name,
        cap_scope=cap_scope,
    )
    source = replace(entry.source, path=entry.source.path)
    if source.origin == "remote":
        source = replace(
            source,
            fingerprint=_remote_snapshot_fingerprint(
                source.fingerprint,
                path=path,
                shape=entry.shape,
                files=files,
            ),
        )
    return replace(
        entry,
        path=f"files/{path.as_posix()}",
        source=source,
    )


def _entry_snapshot_path(
    entry: StateCap,
    *,
    agent_name: str,
    cap_scope: CapScope,
) -> Path:
    if entry.source.form == "authored":
        relative = _scope_relative_path(
            Path(entry.path),
            agent_name=agent_name,
            cap_scope=cap_scope,
        )
        if len(relative.parts) < 2:
            raise ValueError(f"unexpected authored cap path: {relative}")
        return Path("caps") / "authored" / entry.kind / Path(*relative.parts[1:])
    return _generated_snapshot_path(Path(entry.path))


def _remote_snapshot_fingerprint(
    authored_fingerprint: str,
    *,
    path: Path,
    shape: str,
    files: dict[str, bytes],
) -> str:
    selected = (
        [(path.as_posix(), files[path.as_posix()])]
        if shape == "file" and path.as_posix() in files
        else [
            (candidate, content)
            for candidate, content in files.items()
            if Path(candidate).is_relative_to(path.parent)
        ]
    )
    digest = sha256()
    digest.update(authored_fingerprint.encode("ascii"))
    digest.update(b"\0")
    for candidate, content in sorted(selected):
        digest.update(candidate.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256(content).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def _cap_resolutions(
    entries: tuple[StateCap, ...],
    files: dict[str, bytes],
) -> tuple[CapResolution, ...]:
    resolutions: list[CapResolution] = []
    for entry in entries:
        if entry.source.origin != "remote":
            continue
        materialized = Path(entry.path)
        selected = _entry_materialized_files(entry, files)
        resolved_files = [
            MaterializedFile(
                path=f"files/{path}",
                size=len(content),
                sha256=sha256(content).hexdigest(),
            )
            for path, content in selected
        ]
        digest = sha256()
        for file in resolved_files:
            digest.update(file.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file.sha256.encode("ascii"))
            digest.update(b"\n")
        resolutions.append(
            CapResolution(
                kind=entry.kind,
                name=entry.name,
                form=entry.source.form,
                declared_ref=entry.source.declared_ref or entry.ref,
                resolved_ref=entry.ref,
                line=entry.source.line,
                definition=(f"files/{_resolved_definition_path(entry).as_posix()}"),
                materialized=materialized.as_posix(),
                content_hash=digest.hexdigest(),
                files=tuple(resolved_files),
            )
        )
    resolutions.sort(
        key=lambda resolution: (
            resolution.kind,
            resolution.name,
            resolution.form,
            resolution.definition,
            resolution.materialized,
        )
    )
    return tuple(resolutions)


def _resolved_definition_path(entry: StateCap) -> Path:
    if entry.source.form == "configured":
        return Path("config.toml")
    path = Path(entry.source.path)
    if path.parts[:1] == ("agents",) and len(path.parts) >= 3:
        return Path(*path.parts[2:])
    return path


def _entry_materialized_files(
    entry: StateCap,
    files: dict[str, bytes],
) -> list[tuple[str, bytes]]:
    path = Path(entry.path)
    relative = Path(*path.parts[1:])
    if entry.shape == "file":
        content = files.get(relative.as_posix())
        return [] if content is None else [(relative.as_posix(), content)]
    root = relative.parent
    return sorted(
        (candidate, content)
        for candidate, content in files.items()
        if Path(candidate).is_relative_to(root)
    )


def _snapshot_config(files: dict[str, bytes]) -> dict[str, object]:
    content = files.get("config.toml")
    if content is None:
        return {}
    return parse_config(content)
