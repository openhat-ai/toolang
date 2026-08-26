"""Build self-contained root and home prepared versions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import logging
from pathlib import Path
import re

from toolang.common.layout import AgentLayout

from ..common.progress import ProgressSink
from ..lang.ast import Program
from .state import (
    AgentState,
    PreparedProgramModule,
    ProgramModuleExport,
    compose_agent_state,
    public_runnable_catalog,
)
from .errors import StateDiagnostic, StatePreparationError, StateValidationLayer
from .config import parse_config
from .state import (
    materialize_program_caps,
    materialize_visibility,
    prepared_remote_cache,
)
from .source import (
    AuthoredFile,
    AuthoredSource,
    ProgramSource,
    read_authored_source,
    read_root_source,
)
from .cache import (
    HomePrepared,
    PreparedScope,
    RootPrepared,
    load_current_version,
    load_version_source,
    load_home_prepared,
    load_root_prepared,
    prepare_lock,
    prepared_version_dir,
    publish_current,
    write_prepared,
)
from .state import (
    CapResolution,
    PreparedCap,
    PreparedVisibility,
    ResolvedFile,
)
from .source import Source, scan_home_source, scan_root_source

_PREPARED_SCHEMA = 2
_MAX_SOURCE_SNAPSHOT_ATTEMPTS = 3
_RUNNABLE_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_ERROR_LINE_RE = re.compile(r"\bline (\d+)\b", re.IGNORECASE)
logger = logging.getLogger(__name__)


def prepare_agent_state(
    layout: AgentLayout,
    *,
    toolang_version: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> AgentState:
    """Prepare and compose the immutable runtime state for one agent."""

    root, home = prepare_root_home(
        layout,
        toolang_version=toolang_version,
        force=force,
        progress=progress,
    )
    return compose_prepared_state(
        root,
        home,
        program_source=str(layout.program.relative_to(layout.root)),
    )


def compose_prepared_state(
    root: RootPrepared,
    home: HomePrepared,
    *,
    program_source: str,
) -> AgentState:
    """Compose runtime state from one exact pair of prepared cache layers."""

    return compose_agent_state(
        root_version=root.version,
        home_version=home.version,
        toolang_version=root.toolang_version,
        root_config=root.config,
        home_config=home.config,
        program_source=program_source,
        program=home.program,
        root_caps=root.caps,
        home_caps=home.caps,
        modules=home.modules,
        loaded_at=datetime.now(timezone.utc).isoformat(),
    )


def refresh_agent_state(
    layout: AgentLayout,
    *,
    toolang_version: str,
    progress: ProgressSink | None = None,
) -> AgentState:
    """Explicitly refresh remote resolutions and prepare one agent state."""

    return prepare_agent_state(
        layout,
        toolang_version=toolang_version,
        force=True,
        progress=progress,
    )


def prepare_root_home(
    layout: AgentLayout,
    *,
    toolang_version: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> tuple[RootPrepared, HomePrepared]:
    """Prepare and load the shared root and one agent home."""

    _require_root(layout)
    _require_agent_home(layout)
    root = prepare_root(
        layout,
        toolang_version=toolang_version,
        force=force,
        progress=progress,
    )
    home = prepare_home(
        layout,
        toolang_version=toolang_version,
        force=force,
        progress=progress,
    )
    return root, home


def prepare_root(
    layout: AgentLayout,
    *,
    toolang_version: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> RootPrepared:
    """Build or reuse the root prepared cache shared by every agent."""

    _require_root(layout)
    source = scan_root_source(layout.root)
    current = _matching_root(
        layout,
        source=source,
        force=force,
    )
    if current is not None:
        return current
    with prepare_lock(layout, "root"):
        source = scan_root_source(layout.root)
        current = _matching_root(
            layout,
            source=source,
            force=force,
        )
        if current is not None:
            return current
        _build_prepared(
            layout,
            scope="root",
            visibility="shared",
            toolang_version=toolang_version,
            reuse_remote=not force,
            progress=progress,
        )
        return load_root_prepared(layout)


def prepare_home(
    layout: AgentLayout,
    *,
    toolang_version: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> HomePrepared:
    """Build or reuse one agent-home prepared cache."""

    _require_agent_home(layout)
    source = scan_home_source(layout.root, layout.name)
    current = _matching_home(
        layout,
        source=source,
        force=force,
    )
    if current is not None:
        return current
    with prepare_lock(layout, "home"):
        source = scan_home_source(layout.root, layout.name)
        current = _matching_home(
            layout,
            source=source,
            force=force,
        )
        if current is not None:
            return current
        _build_prepared(
            layout,
            scope="home",
            visibility="private",
            toolang_version=toolang_version,
            reuse_remote=not force,
            progress=progress,
        )
        return load_home_prepared(layout)


def _matching_root(
    layout: AgentLayout,
    *,
    source: Source,
    force: bool,
) -> RootPrepared | None:
    if force:
        return None
    try:
        version = load_current_version(layout, "root")
        version_dir = prepared_version_dir(layout, "root", version)
        if load_version_source(version_dir) != source:
            return None
        return load_root_prepared(layout, version)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _matching_home(
    layout: AgentLayout,
    *,
    source: Source,
    force: bool,
) -> HomePrepared | None:
    if force:
        return None
    try:
        version = load_current_version(layout, "home")
        version_dir = prepared_version_dir(layout, "home", version)
        if load_version_source(version_dir) != source:
            return None
        return load_home_prepared(layout, version)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _build_prepared(
    layout: AgentLayout,
    *,
    scope: PreparedScope,
    visibility: PreparedVisibility,
    toolang_version: str,
    reuse_remote: bool,
    progress: ProgressSink | None,
) -> bytes:
    for _ in range(_MAX_SOURCE_SNAPSHOT_ATTEMPTS):
        source = _scan_scope_source(layout, scope=scope)
        authored = (
            read_root_source(layout.root)
            if scope == "root"
            else read_authored_source(layout.root, layout.name)
        )
        previous_entries = (
            _previous_prepared_caps(
                layout,
                scope=scope,
            )
            if reuse_remote
            else ()
        )
        remote_cache = prepared_remote_cache(
            authored,
            visibility=visibility,
            entries=previous_entries,
        )
        entries, generated_files = materialize_visibility(
            authored,
            visibility=visibility,
            remote_cache=remote_cache or None,
            progress=progress,
            include_program=scope == "root",
        )
        modules: tuple[PreparedProgramModule, ...] = ()
        module_entries: tuple[PreparedCap, ...] = ()
        module_sources: dict[str, ProgramSource] = {}
        if scope == "home":
            drafts = _program_module_drafts(authored)
            previous_modules = _previous_program_modules(layout)
            materialized: list[PreparedProgramModule] = []
            all_module_entries: list[PreparedCap] = []
            for draft, module_source in drafts:
                module_sources[draft.identity] = module_source
                previous = next(
                    (
                        item
                        for item in previous_modules
                        if item.identity == draft.identity
                    ),
                    None,
                )
                module_cache = prepared_remote_cache(
                    authored,
                    visibility="private",
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
                    draft.identity,
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
            visibility=visibility,
        )
        for module in modules:
            source_text = module_sources[module.identity].source_text
            files.setdefault(module.authored_path, source_text.encode("utf-8"))
        entries = tuple(
            _snapshot_entry(
                entry,
                agent_name=authored.agent_name,
                files=files,
                visibility=visibility,
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
                            visibility=visibility,
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
        prepared = _prepared_document(
            entries,
            modules=modules,
            authored=authored,
            files=files,
            scope=scope,
            toolang_version=toolang_version,
        )
        if source != _scan_scope_source(layout, scope=scope):
            continue
        version = write_prepared(
            layout=layout,
            scope=scope,
            source=source,
            resolutions=resolutions,
            prepared=prepared,
            files=files,
        )
        publish_current(
            layout,
            scope,
            version,
        )
        return version
    raise RuntimeError(f"{scope} source changed repeatedly while preparing")


def _previous_prepared_caps(
    layout: AgentLayout,
    *,
    scope: PreparedScope,
) -> tuple[PreparedCap, ...]:
    try:
        if scope == "root":
            return load_root_prepared(layout).caps
        return load_home_prepared(layout).caps
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return ()


def _previous_program_modules(
    layout: AgentLayout,
) -> tuple[PreparedProgramModule, ...]:
    try:
        return load_home_prepared(layout).modules
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return ()


def _scan_scope_source(
    layout: AgentLayout,
    *,
    scope: PreparedScope,
) -> Source:
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
    authored: AuthoredSource,
) -> tuple[tuple[PreparedProgramModule, ProgramSource], ...]:
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

    drafts: list[tuple[PreparedProgramModule, ProgramSource]] = []
    extension_diagnostics: list[StateDiagnostic] = []
    for source, program in programs:
        if source.kind == "agent":
            drafts.append(
                (
                    PreparedProgramModule(
                        identity="agent",
                        kind="agent",
                        authored_path=source.authored_path,
                        prepared_path=f"files/{source.authored_path}",
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
                PreparedProgramModule(
                    identity=f"flow:{export.public_name}",
                    kind="flow",
                    authored_path=source.authored_path,
                    prepared_path=f"files/{source.authored_path}",
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
    if _RUNNABLE_NAME_RE.fullmatch(name) is None:
        raise ValueError(f"Flow module filename is not a runnable name: {name!r}")
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
    identity: str,
    entries: tuple[PreparedCap, ...],
    files: dict[str, bytes],
) -> tuple[tuple[PreparedCap, ...], dict[str, bytes]]:
    namespace = identity.replace(":", "-")
    prefix = Path("modules") / namespace
    return (
        tuple(replace(entry, path=str(prefix / entry.path)) for entry in entries),
        {str(prefix / path): content for path, content in files.items()},
    )


def _snapshot_files(
    authored: AuthoredSource,
    generated_files: dict[str, bytes],
    *,
    visibility: PreparedVisibility,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for item in authored.files:
        if not _file_belongs_to_visibility(item, visibility=visibility):
            continue
        target = _authored_snapshot_path(
            item,
            agent_name=authored.agent_name,
            visibility=visibility,
        )
        files[str(target)] = item.content
    for path, content in generated_files.items():
        target = _generated_snapshot_path(Path(path))
        files[str(target)] = content
    return files


def _file_belongs_to_visibility(
    item: AuthoredFile,
    *,
    visibility: PreparedVisibility,
) -> bool:
    return item.origin == ("root" if visibility == "shared" else "agent")


def _authored_snapshot_path(
    item: AuthoredFile,
    *,
    agent_name: str,
    visibility: PreparedVisibility,
) -> Path:
    relative = _scope_relative_path(
        Path(item.relative_path),
        agent_name=agent_name,
        visibility=visibility,
    )
    if item.category == "cap":
        return Path("authored") / relative
    if item.category == "program":
        return relative
    if item.category == "config":
        return Path("config.toml")
    return Path("authored") / relative


def _generated_snapshot_path(
    path: Path,
) -> Path:
    if len(path.parts) < 3:
        raise ValueError(f"unexpected materialized cap path: {path}")
    if path.parts[0] == "modules":
        if len(path.parts) < 5 or path.parts[2] not in {"cited", "inline"}:
            raise ValueError(f"unexpected module cap path: {path}")
        return path
    if path.parts[0] not in {"cited", "inline", "wired"}:
        raise ValueError(f"unexpected materialized cap bucket: {path.parts[0]}")
    return path


def _scope_relative_path(
    path: Path,
    *,
    agent_name: str,
    visibility: PreparedVisibility,
) -> Path:
    if visibility == "shared":
        return path
    prefix = Path("agents") / agent_name
    try:
        return path.relative_to(prefix)
    except ValueError as exc:
        raise ValueError(f"home source is outside the agent directory: {path}") from exc


def _snapshot_entry(
    entry: PreparedCap,
    *,
    agent_name: str,
    files: dict[str, bytes],
    visibility: PreparedVisibility,
) -> PreparedCap:
    path = _entry_snapshot_path(
        entry,
        agent_name=agent_name,
        visibility=visibility,
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
        path=f"files/{path}",
        source=source,
    )


def _entry_snapshot_path(
    entry: PreparedCap,
    *,
    agent_name: str,
    visibility: PreparedVisibility,
) -> Path:
    if entry.source.form == "file":
        relative = _scope_relative_path(
            Path(entry.path),
            agent_name=agent_name,
            visibility=visibility,
        )
        return Path("authored") / relative
    return _generated_snapshot_path(Path(entry.path))


def _remote_snapshot_fingerprint(
    authored_fingerprint: str,
    *,
    path: Path,
    shape: str,
    files: dict[str, bytes],
) -> str:
    selected = (
        [(str(path), files[str(path)])]
        if shape == "file" and str(path) in files
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
    entries: tuple[PreparedCap, ...],
    files: dict[str, bytes],
) -> tuple[CapResolution, ...]:
    resolutions: list[CapResolution] = []
    for entry in entries:
        if entry.source.origin != "remote":
            continue
        materialized = Path(entry.path)
        selected = _entry_materialized_files(entry, files)
        resolved_files = [
            ResolvedFile(
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
                authored_ref=entry.source.authored_ref or entry.ref,
                resolved_ref=entry.ref,
                line=entry.source.line,
                definition=f"files/{_resolved_definition_path(entry)}",
                materialized=str(materialized),
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
        )
    )
    return tuple(resolutions)


def _resolved_definition_path(entry: PreparedCap) -> Path:
    if entry.source.form == "wired":
        return Path("config.toml")
    path = Path(entry.source.path)
    if path.parts[:1] == ("agents",) and len(path.parts) >= 3:
        return Path(*path.parts[2:])
    return path


def _entry_materialized_files(
    entry: PreparedCap,
    files: dict[str, bytes],
) -> list[tuple[str, bytes]]:
    path = Path(entry.path)
    relative = Path(*path.parts[1:])
    if entry.shape == "file":
        content = files.get(str(relative))
        return [] if content is None else [(str(relative), content)]
    root = relative.parent
    return sorted(
        (candidate, content)
        for candidate, content in files.items()
        if Path(candidate).is_relative_to(root)
    )


def _prepared_document(
    entries: tuple[PreparedCap, ...],
    *,
    modules: tuple[PreparedProgramModule, ...],
    authored: AuthoredSource,
    files: dict[str, bytes],
    scope: PreparedScope,
    toolang_version: str,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": _PREPARED_SCHEMA,
        "scope": scope,
        "toolang_version": toolang_version,
        "config": _snapshot_config(files),
        "caps": [entry.to_data() for entry in entries],
    }
    if scope == "home":
        document["modules"] = [module.to_data() for module in modules]
    return document


def _snapshot_config(files: dict[str, bytes]) -> dict[str, object]:
    content = files.get("config.toml")
    if content is None:
        return {}
    return parse_config(content)
