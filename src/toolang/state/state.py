"""Resolved capability data and immutable runtime Agent State."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass, field, replace
import hashlib
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re
from typing import Literal, cast

import frontmatter

from toolang.catalog import source as cap_source
from .format import AGENT_STATE_FORMAT
from toolang.catalog.chores import Chore, inline_chores, merge_chores
from toolang.catalog.config import ConfiguredCaps
from toolang.catalog.types import (
    CAP_DIR_BY_KIND,
    CAP_KIND_BY_DIR,
    CAP_KINDS as CATALOG_CAP_KINDS,
)
from toolang.state.source import (
    SourceFile,
    SourceSnapshot,
    ProgramSource,
    read_authored_source,
)
from ..common.immutable import freeze_mapping, mutable_data
from ..common.progress import ProgressSink, emit_progress
from ..base.types.progress import ProgressEvent
from ..lang.ast import (
    AgicDecl,
    CapDecl,
    FlowDecl,
    Program,
)
from toolang.lang.types import unnamed_runnable_name

from .config import WORKSPACE_ORDER_KEY, validate_workspace_name
from .types import (
    EntryKind,
    EntryShape,
    CapScope,
    RunnableKind,
    CapForm,
    SourceOrigin,
    StateFile,
)

_PORTABLE_SOURCE_UPDATED_AT = "1970-01-01T00:00:00+00:00"

CAP_KINDS: tuple[EntryKind, ...] = CATALOG_CAP_KINDS
FILE_BACKED_KINDS = frozenset({"psyche", "service", "prompt"})
DIR_NAME_BY_KIND: dict[EntryKind, str] = CAP_DIR_BY_KIND
KIND_BY_DIR_NAME: dict[str, EntryKind] = CAP_KIND_BY_DIR
REMOTE_CAP_MATERIALIZE_WORKERS = 4
_RUNNABLE_FILENAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_WINDOWS_RESERVED_FILENAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class MaterializedFile:
    """One file fixed by a remote cap resolution."""

    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _require_layer_path(self.path, label="materialized file")
        if self.size < 0:
            raise ValueError("materialized file size must be non-negative")
        _require_revision(self.sha256, name="materialized file sha256")


@dataclass(frozen=True, slots=True)
class CapResolution:
    """Runtime acquisition details reconstructed from a Cap and its file map."""

    kind: EntryKind
    name: str
    form: CapForm
    declared_ref: str
    resolved_ref: str
    definition: str
    materialized: str
    content_hash: str
    files: tuple[MaterializedFile, ...]
    line: int | None = None

    def __post_init__(self) -> None:
        if self.form not in {"configured", "referenced"}:
            raise ValueError(f"invalid resolved cap form: {self.form!r}")
        if (
            not self.name
            or not self.declared_ref.strip()
            or not self.resolved_ref.strip()
        ):
            raise ValueError("resolved cap names and refs must not be empty")
        _require_layer_path(self.definition, label="resolved cap definition")
        _require_layer_path(self.materialized, label="resolved cap materialization")
        if not self.files:
            raise ValueError("resolved cap must contain materialized files")
        if tuple(sorted(self.files, key=lambda item: item.path)) != self.files:
            raise ValueError("resolved cap files must be sorted by path")
        if len({file.path for file in self.files}) != len(self.files):
            raise ValueError("resolved cap files must be unique")
        _require_revision(self.content_hash, name="resolved cap content hash")
        digest = sha256()
        for file in self.files:
            digest.update(file.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file.sha256.encode("ascii"))
            digest.update(b"\n")
        if digest.hexdigest() != self.content_hash:
            raise ValueError("resolved cap content hash does not match its files")
        if self.line is not None and self.line < 1:
            raise ValueError("resolved cap line must be positive")


@dataclass(frozen=True, slots=True)
class CapSource:
    """Authored provenance retained by one State capability."""

    origin: SourceOrigin
    form: CapForm
    path: str
    updated_at: str
    fingerprint: str
    declared_ref: str | None = None
    line: int | None = None

    def __post_init__(self) -> None:
        if self.origin not in {"local", "remote"}:
            raise ValueError(f"invalid cap source origin: {self.origin!r}")
        if self.form not in {"authored", "inline", "configured", "referenced"}:
            raise ValueError(f"invalid cap form: {self.form!r}")
        if self.origin == "local" and self.form not in {"authored", "inline"}:
            raise ValueError("local cap source must be authored or inline")
        if self.origin == "remote" and self.form not in {"configured", "referenced"}:
            raise ValueError("remote cap source must be configured or referenced")
        if self.form in {"configured", "referenced"} and not self.declared_ref:
            raise ValueError("configured and referenced caps require a declared ref")
        if self.form in {"authored", "inline"} and self.declared_ref is not None:
            raise ValueError("authored and inline caps cannot declare a ref")
        if (
            not self.path
            or Path(self.path).is_absolute()
            or ".." in Path(self.path).parts
        ):
            raise ValueError("cap source path must be relative")
        if self.line is not None and self.line < 1:
            raise ValueError("cap source line must be positive")
        _require_revision(self.fingerprint, name="cap source fingerprint")

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "origin": self.origin,
            "form": self.form,
            "path": self.path,
            "updated_at": self.updated_at,
            "fingerprint": self.fingerprint,
        }
        if self.line is not None:
            data["line"] = self.line
        if self.declared_ref is not None:
            data["declared_ref"] = self.declared_ref
        return data


@dataclass(frozen=True, slots=True)
class StateCap:
    """One parsed capability with captured provenance and file resources."""

    kind: EntryKind
    name: str
    shape: EntryShape
    ref: str
    path: str
    source: CapSource
    meta: Mapping[str, object]
    body: str | None = None
    materialized: str | None = None
    resources: tuple[MaterializedFile, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", freeze_mapping(self.meta))
        if self.kind not in CAP_KINDS:
            raise ValueError(f"invalid State cap kind: {self.kind!r}")
        if self.shape not in {"file", "dir"}:
            raise ValueError(f"invalid State cap shape: {self.shape!r}")
        if self.source.form in {"authored", "configured"} and self.scope == "here":
            raise ValueError(f"{self.source.form} cap cannot have here scope")
        if self.source.form in {"inline", "referenced"} and self.scope != "here":
            raise ValueError(f"{self.source.form} cap must have here scope")

    @property
    def effective_ref(self) -> str:
        """Identify this effective definition without exposing its source."""

        return f"{self.kind}/{self.name}"

    @property
    def revision(self) -> str:
        """Identify the effective definition independently of its snapshot path."""

        definition = {
            "kind": self.kind,
            "name": self.name,
            "meta": mutable_data(self.meta),
            "source": self.source.fingerprint,
        }
        return sha256(
            json.dumps(
                definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    @property
    def scope(self) -> CapScope:
        if self.source.form in {"inline", "referenced"}:
            return "here"
        return "home" if self.source.path.startswith("agents/") else "root"

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "shape": self.shape,
            "ref": self.ref,
            "path": self.path,
            "source": self.source.to_data(),
            "meta": mutable_data(self.meta),
        }

    def read_text(self) -> str:
        """Read this capability from its immutable layer file."""
        if self.source.form == "inline" and self.body is not None:
            post = frontmatter.Post(self.body)
            post.metadata = dict(self.meta)
            return frontmatter.dumps(post)
        return Path(self.path).read_text(encoding="utf-8")

    def read_content(self) -> str:
        """Return parsed content, reading a file only before State persistence."""
        if self.body is not None:
            return self.body
        return frontmatter.loads(self.read_text()).content.strip()

    def to_snapshot(self) -> dict[str, object]:
        return self.to_data()


def program_runnable_index(
    program: Program,
) -> dict[str, AgicDecl | FlowDecl]:
    """Bind authored names to module-local identities without changing the AST.

    An unnamed top-level agic or flow binds to `<entry:LINE>`. Declarations
    keep their AST names, and the runtime never invents an entry.
    """

    result: dict[str, AgicDecl | FlowDecl] = {}
    adhoc_lines = program.adhoc_lines
    unnamed_entry: AgicDecl | FlowDecl | None = None
    for declaration in (*program.agics, *program.flows):
        if declaration.name is not None:
            if declaration.name.startswith("<"):
                continue
            name = declaration.name
        else:
            if (
                isinstance(declaration, AgicDecl)
                and declaration.span.line in adhoc_lines
            ):
                continue
            if unnamed_entry is not None:
                raise ValueError("Runnable name is not unique: entry")
            unnamed_entry = declaration
            name = unnamed_runnable_name("entry", declaration.span.line)
        if name in result:
            raise ValueError(f"Runnable name is not unique: {name}")
        result[name] = declaration
    return result


def public_runnable_index(
    modules: Mapping[str, Program],
    module_sources: Mapping[str, str],
) -> tuple[dict[str, AgicDecl | FlowDecl], dict[str, str]]:
    """Index unique public runnable bindings across State Programs."""

    result: dict[str, AgicDecl | FlowDecl] = {}
    owners: dict[str, str] = {}

    def add(name: str, module: str, declaration: AgicDecl | FlowDecl) -> None:
        if name in result:
            previous = result[name]
            raise ValueError(
                f"Public runnable name is not unique: {name} "
                f"({module_sources[owners[name]]}:{previous.span.line} and "
                f"{module_sources[module]}:{declaration.span.line})"
            )
        result[name] = declaration
        owners[name] = module

    agent = modules.get("agent")
    if agent is None:
        raise ValueError("home State layer is missing the agent module")
    for name, declaration in program_runnable_index(agent).items():
        add(name, "agent", declaration)
    for module, program in modules.items():
        if module == "agent" or module == "runtime::talk":
            continue
        public_name, local_name = runnable_export(module_sources[module], program)
        declaration = program_runnable_index(program)[local_name]
        add(public_name, module, declaration)
    return result, owners


def module_runnable_index(
    modules: Mapping[str, Program],
) -> dict[str, AgicDecl | FlowDecl]:
    """Index every runnable declaration by Program and local identity."""

    result: dict[str, AgicDecl | FlowDecl] = {}
    for module, program in modules.items():
        for name, declaration in program_runnable_index(program).items():
            result[_module_runnable_key(module, declaration.kind, name)] = declaration
    return result


def runnable_export(source: str, program: Program) -> tuple[str, str]:
    public_name = Path(source).stem
    candidates = tuple(
        name
        for name, runnable in program_runnable_index(program).items()
        if runnable.name is None or runnable.name == public_name
    )
    if len(candidates) != 1:
        raise ValueError(
            f"runnable module must export exactly one unnamed or "
            f"filename-matching agic or flow: {source} (found {len(candidates)})"
        )
    return public_name, candidates[0]


def _module_runnable_key(module: str, kind: str, local_name: str) -> str:
    return f"{module}::{kind}:{local_name}"


def state_program(
    state: object,
    name: str = "agent",
) -> Program:
    """Return one Program while preserving legacy single-Program fixtures."""

    modules = getattr(state, "modules", None)
    if isinstance(modules, Mapping):
        program = modules.get(name)
        if isinstance(program, Program):
            return program
        raise ValueError(f"Program not found: {name}")
    if name != "agent":
        raise ValueError(f"Program not found: {name}")
    program = getattr(state, "program", None)
    if not isinstance(program, Program):
        raise TypeError("agent state is missing its program")
    return program


def state_program_source(state: object, name: str = "agent") -> str:
    """Return one Program source with legacy fixture compatibility."""

    sources = getattr(state, "module_sources", None)
    if isinstance(sources, Mapping):
        source = sources.get(name)
        if isinstance(source, str):
            return source
        raise ValueError(f"Program not found: {name}")
    if name != "agent":
        raise ValueError(f"Program not found: {name}")
    return str(getattr(state, "program_source", "agent.too"))


def state_module_caps(
    state: object,
    name: str = "agent",
) -> tuple[StateCap, ...]:
    """Return module-effective caps with legacy single-Program compatibility."""

    resolver = getattr(state, "caps_for", None)
    if callable(resolver):
        return cast(tuple[StateCap, ...], resolver(name))
    return cast(tuple[StateCap, ...], tuple(getattr(state, "caps", ())))


@dataclass(frozen=True, slots=True, weakref_slot=True)
class AgentState:
    """One immutable prepared State, including effective caps and workspaces."""

    name: str
    revision: str
    root_revision: str
    home_revision: str
    root_config: Mapping[str, object]
    home_config: Mapping[str, object]
    config: Mapping[str, object]
    caps: Mapping[str, StateCap]
    modules: Mapping[str, Program]
    module_sources: Mapping[str, str]
    module_digests: Mapping[str, str]
    module_caps: Mapping[str, tuple[StateCap, ...]]
    chores: tuple[Chore, ...] = ()
    files: tuple[StateFile, ...] = ()
    toolang_root: Path | None = None
    base_caps: tuple[StateCap, ...] | None = None
    revision_dir: Path | None = None
    allow_overrides: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    workspace_additions: Mapping[str, str] = field(default_factory=dict)
    created_at: str | None = None
    source_requests: tuple[tuple[EntryKind, str, str], ...] = ()
    runtime_sources: Mapping[str, str] = field(default_factory=dict)
    workspaces: Mapping[str, str] = field(init=False)
    caps_by_module: Mapping[str, tuple[StateCap, ...]] = field(init=False, repr=False)
    skills: Mapping[str, StateCap] = field(init=False, repr=False)
    psyches: Mapping[str, StateCap] = field(init=False, repr=False)
    services: Mapping[str, StateCap] = field(init=False, repr=False)
    prompts: Mapping[str, StateCap] = field(init=False, repr=False)
    agics: Mapping[str, AgicDecl] = field(init=False, repr=False)
    flows: Mapping[str, FlowDecl] = field(init=False, repr=False)
    runnables: Mapping[str, AgicDecl | FlowDecl] = field(init=False, repr=False)
    runnable_modules: Mapping[str, str] = field(init=False, repr=False)
    module_runnables: Mapping[str, AgicDecl | FlowDecl] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "runtime_sources", freeze_mapping(self.runtime_sources)
        )
        _require_revision(self.revision, name="Agent State revision")
        files = tuple(sorted(self.files, key=lambda item: (item.scope, item.key)))
        if len({(item.scope, item.key) for item in files}) != len(files):
            raise ValueError("Agent State file keys must be unique within each scope")
        object.__setattr__(self, "files", files)
        home = (
            self.toolang_root / "agents" / self.name
            if self.toolang_root is not None
            else Path()
        )
        authored_chores = tuple(
            replace(chore, path=home / chore.path)
            if not chore.path.is_absolute()
            else chore
            for chore in self.chores
        )
        combined = merge_chores(
            authored_chores,
            inline_chores(
                self.modules["agent"].chores,
                path=home / "agent.too",
                agent_name=self.name,
            ),
        )
        object.__setattr__(self, "chores", tuple(combined.values()))
        if self.revision != agent_state_revision(
            self.root_revision,
            self.home_revision,
            name=self.name,
            allow_overrides=self.allow_overrides,
            workspace_additions=self.workspace_additions,
            created_at=self.created_at,
        ):
            raise ValueError("Agent State revision does not match its layer revisions")
        if self.revision_dir is not None and self.revision_dir.name != self.revision:
            raise ValueError(
                "Agent State revision directory does not match its revision"
            )
        object.__setattr__(self, "root_config", freeze_mapping(self.root_config))
        object.__setattr__(self, "home_config", freeze_mapping(self.home_config))
        object.__setattr__(self, "config", freeze_mapping(self.config))
        object.__setattr__(
            self, "allow_overrides", freeze_mapping(self.allow_overrides)
        )
        raw_workspaces = cast(Mapping[str, str], self.home_config.get("workspaces", {}))
        ordered_workspaces: dict[str, str] = {}
        raw_order = self.home_config.get(WORKSPACE_ORDER_KEY)
        if isinstance(raw_order, tuple | list):
            for name in raw_order:
                if isinstance(name, str) and name in raw_workspaces:
                    ordered_workspaces.setdefault(name, raw_workspaces[name])
        for name, path in raw_workspaces.items():
            ordered_workspaces.setdefault(name, path)
        for name, path in self.workspace_additions.items():
            validate_workspace_name(name)
            if name == "lab" or name in ordered_workspaces:
                raise ValueError(f"temporary workspace name already exists: {name}")
            if not Path(path).is_absolute():
                raise ValueError(f"temporary workspace path must be absolute: {name}")
            ordered_workspaces[name] = path
        object.__setattr__(
            self, "workspace_additions", freeze_mapping(self.workspace_additions)
        )
        object.__setattr__(self, "workspaces", freeze_mapping(ordered_workspaces))
        caps_index = dict(self.caps)
        if tuple(sorted(caps_index)) != tuple(caps_index):
            raise ValueError("Agent State caps must be sorted by identity")
        for ref, cap in caps_index.items():
            if ref != _cap_index_key(cap):
                raise ValueError("Agent State cap index key does not match its value")
        object.__setattr__(self, "caps", freeze_mapping(caps_index))
        modules = dict(self.modules)
        if not modules:
            raise ValueError("Agent State requires program modules")
        if tuple(sorted(modules)) != tuple(modules):
            raise ValueError("Agent State modules must be sorted by name")
        if "agent" not in modules:
            raise ValueError("Agent State requires exactly one agent module")
        sources = dict(self.module_sources)
        digests = dict(self.module_digests)
        caps = dict(self.module_caps)
        expected = set(modules)
        if (
            set(sources) != expected
            or set(digests) != expected
            or set(caps) != expected
        ):
            raise ValueError("Agent State Program indexes must have matching names")
        for name, program in modules.items():
            validate_program_term(
                name=name,
                source=sources[name],
                digest=digests[name],
                program=program,
                here_caps=caps[name],
            )
        object.__setattr__(self, "modules", freeze_mapping(modules))
        object.__setattr__(self, "module_sources", freeze_mapping(sources))
        object.__setattr__(self, "module_digests", freeze_mapping(digests))
        object.__setattr__(self, "module_caps", freeze_mapping(caps))
        for kind in ("skill", "psyche", "service", "prompt"):
            object.__setattr__(
                self,
                f"{kind}s" if kind != "psyche" else "psyches",
                freeze_mapping(
                    {cap.name: cap for cap in caps_index.values() if cap.kind == kind}
                ),
            )
        public_runnables, runnable_modules = public_runnable_index(modules, sources)
        object.__setattr__(
            self,
            "runnables",
            freeze_mapping(public_runnables),
        )
        object.__setattr__(
            self,
            "runnable_modules",
            freeze_mapping(runnable_modules),
        )
        object.__setattr__(
            self, "agics", _runnable_kind_index(public_runnables, "agic")
        )
        object.__setattr__(
            self, "flows", _runnable_kind_index(public_runnables, "flow")
        )
        object.__setattr__(
            self, "module_runnables", freeze_mapping(module_runnable_index(modules))
        )
        object.__setattr__(
            self, "caps_by_module", freeze_mapping(_effective_module_caps(self))
        )

    def module_runnable(
        self,
        module: str,
        local_name: str,
        *,
        kind: str | None = None,
    ) -> AgicDecl | FlowDecl | None:
        """Return one Program-local runnable declaration."""

        if local_name == "_":
            program = self.modules.get(module)
            if program is None:
                return None
            adhoc = program.adhoc_lines
            matches = tuple(
                entry
                for entry in (*program.agics, *program.flows)
                if entry.name is None
                and not (isinstance(entry, AgicDecl) and entry.span.line in adhoc)
                and (kind is None or entry.kind == kind)
            )
            if len(matches) != 1:
                return None
            return matches[0]
        matches = tuple(
            entry
            for candidate_kind in ((kind,) if kind is not None else ("agic", "flow"))
            if (
                entry := self.module_runnables.get(
                    _module_runnable_key(module, candidate_kind, local_name)
                )
            )
        )
        if len(matches) > 1:  # pragma: no cover - Program namespace invariant
            raise ValueError(f"Runnable name is not unique: {local_name}")
        return matches[0] if matches else None

    def caps_for(self, module: str) -> tuple[StateCap, ...]:
        """Return the precomputed effective caps for one executing module."""

        if module == "runtime::talk":
            module = "agent"
        if module in self.caps_by_module:
            return self.caps_by_module[module]
        # Accepted code may outlive its module. Only current external caps remain.
        base = tuple(self.caps.values()) if self.base_caps is None else self.base_caps
        return _allowed_state_caps(self, base)


def _effective_module_caps(state: AgentState) -> dict[str, tuple[StateCap, ...]]:
    """Apply configured allows and frozen startup replacements once per State."""

    base = tuple(state.caps.values()) if state.base_caps is None else state.base_caps
    return {
        module: _allowed_state_caps(state, effective_caps(base, here))
        for module, here in state.module_caps.items()
    }


def _allowed_state_caps(
    state: AgentState, caps: tuple[StateCap, ...]
) -> tuple[StateCap, ...]:
    from .config import resolve_cap_allows

    return _allowed_caps(
        caps,
        root=state.toolang_root,
        agent_name=state.name,
        allows=resolve_cap_allows(
            (state.root_config, state.home_config), overrides=state.allow_overrides
        ),
    )


def _allowed_caps(
    entries: tuple[StateCap, ...],
    *,
    root: Path | None,
    agent_name: str,
    allows: Mapping[str, tuple[str, ...] | None],
) -> tuple[StateCap, ...]:
    """Apply the same cap-kind policy to runtime and root inspection."""

    from .collections import cap_collection
    from .config import CAP_ALLOW_FIELDS

    selected_ids: set[tuple[str, str, str]] = set()
    for field_name in CAP_ALLOW_FIELDS:
        kind = cast(EntryKind, field_name.removesuffix("s"))
        candidates = tuple(cap for cap in entries if cap.kind == kind)
        queries = allows.get(field_name)
        if queries is None:
            selected = candidates
        elif not queries:
            selected = ()
        else:
            selected = tuple(
                item.record
                for item in cap_collection(
                    candidates, root=root, agent_name=agent_name, kind=kind
                ).query(queries)
            )
        selected_ids.update((cap.kind, cap.name, cap.ref) for cap in selected)
    return tuple(
        cap for cap in entries if (cap.kind, cap.name, cap.ref) in selected_ids
    )


def compose_agent_state(
    *,
    toolang_root: Path | None = None,
    root_revision: str,
    home_revision: str,
    root_config: Mapping[str, object],
    home_config: Mapping[str, object],
    root_caps: tuple[StateCap, ...],
    home_caps: tuple[StateCap, ...],
    modules: Mapping[str, Program],
    module_sources: Mapping[str, str],
    module_digests: Mapping[str, str],
    module_caps: Mapping[str, tuple[StateCap, ...]],
    files: tuple[StateFile, ...] = (),
    chores: tuple[Chore, ...] = (),
    revision_dir: Path | None = None,
    name: str,
    allow_overrides: Mapping[str, tuple[str, ...]] | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    created_at: str | None = None,
    source_requests: tuple[tuple[EntryKind, str, str], ...] = (),
) -> AgentState:
    """Compose runtime State from one exact root/home layer pair."""

    effective_base = effective_caps(root_caps, home_caps)
    agent_here = module_caps.get("agent", ())
    return AgentState(
        toolang_root=toolang_root,
        revision=agent_state_revision(
            root_revision,
            home_revision,
            name=name,
            allow_overrides=allow_overrides,
            workspace_additions=workspace_additions,
            created_at=created_at,
        ),
        name=name,
        created_at=created_at,
        source_requests=source_requests,
        allow_overrides=allow_overrides or {},
        workspace_additions=workspace_additions or {},
        root_revision=root_revision,
        home_revision=home_revision,
        root_config=root_config,
        home_config=home_config,
        config=_merge_config(root_config, home_config),
        caps=cap_index(effective_caps(effective_base, agent_here)),
        modules=modules,
        module_sources=module_sources,
        module_digests=module_digests,
        module_caps=module_caps,
        base_caps=effective_base,
        files=files,
        chores=chores,
        revision_dir=revision_dir,
    )


def _runnable_kind_index(
    runnables: Mapping[str, AgicDecl | FlowDecl],
    kind: RunnableKind,
) -> Mapping[str, AgicDecl] | Mapping[str, FlowDecl]:
    entries = {name: entry for name, entry in runnables.items() if entry.kind == kind}
    if kind == "agic":
        return freeze_mapping(cast(dict[str, AgicDecl], entries))
    return freeze_mapping(cast(dict[str, FlowDecl], entries))


def cap_index(caps: tuple[StateCap, ...]) -> Mapping[str, StateCap]:
    """Index effective capabilities by their kind-qualified public identity."""

    result = {_cap_index_key(cap): cap for cap in caps}
    if len(result) != len(caps):
        raise ValueError("Agent State capabilities must have unique identities")
    return freeze_mapping(result)


def _cap_index_key(cap: StateCap) -> str:
    return f"{cap.kind}:{cap.name}"


def validate_program_term(
    *,
    name: str,
    source: str,
    digest: str,
    program: Program,
    here_caps: tuple[StateCap, ...],
) -> None:
    if name == "agent":
        if source != "agent.too":
            raise ValueError("agent Program must use agent.too")
    elif name == "runtime::talk":
        if source != "runtime/talk.too":
            raise ValueError("runtime Talk Program must use runtime/talk.too")
    elif name != runnable_module_name(source):
        raise ValueError("runnable Program name must match its source path")
    _require_revision(digest, name="State Program digest")
    if any(cap.scope != "here" for cap in here_caps):
        raise ValueError("State Program caps must have here scope")
    if tuple(sorted(here_caps, key=_entry_sort_key)) != here_caps:
        raise ValueError("State Program capabilities must be sorted")
    if len({_entry_sort_key(cap) for cap in here_caps}) != len(here_caps):
        raise ValueError("State Program capabilities must be unique")
    if name != "agent":
        runnable_export(source, program)
    for cap in here_caps:
        path = Path(cap.path)
        if path.is_absolute():
            continue
        if cap.source.form == "inline" and path != Path("source") / source:
            raise ValueError("inline Cap must reference its module source")
        if cap.source.form == "referenced" and path.parts[:1] != ("materialized",):
            raise ValueError("referenced Cap must use its acquired files")


def _merge_config(
    base: Mapping[str, object], override: Mapping[str, object]
) -> dict[str, object]:
    merged = {key: value for key, value in base.items() if key != WORKSPACE_ORDER_KEY}
    for key, value in override.items():
        if key == WORKSPACE_ORDER_KEY:
            continue
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config(
                cast(Mapping[str, object], current),
                cast(Mapping[str, object], value),
            )
        else:
            merged[key] = value
    return merged


def agent_layers_document(
    root_revision: str,
    home_revision: str,
    *,
    name: str,
    allow_overrides: Mapping[str, tuple[str, ...]] | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    """Describe the exact inputs that identify one prepared Agent State."""

    _require_revision(root_revision, name="root revision")
    _require_revision(home_revision, name="home revision")
    return {
        **({"created_at": created_at} if created_at is not None else {}),
        "home_revision": home_revision,
        "root_revision": root_revision,
        "name": name,
        "allow_overrides": dict(allow_overrides or {}),
        "schema": AGENT_STATE_FORMAT,
        **(
            {"workspace_additions": list(workspace_additions.items())}
            if workspace_additions
            else {}
        ),
    }


def agent_state_revision(
    root_revision: str,
    home_revision: str,
    *,
    name: str,
    allow_overrides: Mapping[str, tuple[str, ...]] | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    created_at: str | None = None,
) -> str:
    """Identify the layers and frozen policy used to construct a State."""

    encoded = json.dumps(
        agent_layers_document(
            root_revision,
            home_revision,
            name=name,
            allow_overrides=allow_overrides,
            workspace_additions=workspace_additions,
            created_at=created_at,
        ),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def runnable_module_name(authored_path: str) -> str:
    """Return the stable portable module name for one direct runnable source."""

    path = Path(authored_path)
    if len(path.parts) != 2 or path.parts[0] != "runnables" or path.suffix != ".too":
        raise ValueError(f"Runnable module source path is invalid: {authored_path!r}")
    stem = path.stem
    if stem == "_":
        raise ValueError(
            "Runnable module filename is reserved for the unnamed entry alias: '_'"
        )
    if _RUNNABLE_FILENAME_RE.fullmatch(stem) is None:
        raise ValueError(f"Runnable module filename is not portable: {stem!r}")
    if stem.casefold() in _WINDOWS_RESERVED_FILENAMES:
        raise ValueError(f"Runnable module filename is reserved on Windows: {stem!r}")
    return f"runnables::{stem}"


def effective_caps(
    root: tuple[StateCap, ...],
    home: tuple[StateCap, ...],
) -> tuple[StateCap, ...]:
    """Overlay home capabilities over root capabilities."""

    effective: dict[tuple[str, str], StateCap] = {}
    for cap in (*root, *home):
        effective[(cap.kind, cap.name)] = cap
    return tuple(
        sorted(
            effective.values(),
            key=lambda cap: (cap.kind, cap.name, cap.ref),
        )
    )


def _require_revision(value: str, *, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _require_layer_path(value: str, *, label: str) -> None:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[:1] not in {("source",), ("materialized",)}
        or "\\" in value
    ):
        raise ValueError(f"{label} path must be portable and inside files/")


@dataclass(frozen=True, slots=True)
class _RemoteEntryRequest:
    scope: CapScope
    kind: EntryKind
    ref: str
    name: str | None
    relative_config_path: Path
    source_fingerprint: str
    form: Literal["configured", "referenced"]
    source_line: int | None = None

    @property
    def progress_id(self) -> str:
        return cap_source._cap_progress_id(self.kind, self.ref)

    @property
    def progress_scope(self) -> str:
        return f"{self.scope}:{self.relative_config_path}:{self.form}:{self.name or ''}"


@dataclass(frozen=True, slots=True)
class _CachedRemoteEntry:
    ref: str
    name: str
    files: tuple[tuple[str, bytes], ...]
    entry: str


_RemoteEntryCacheKey = tuple[
    CapScope,
    EntryKind,
    str,
    str | None,
    str,
]
_RemoteEntryCache = Mapping[_RemoteEntryCacheKey, _CachedRemoteEntry]


def list_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: CapScope | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[StateCap, ...]:
    """List effective capabilities projected from authored source."""

    authored = read_authored_source(toolang_root, agent_name)
    entries, _ = _collect_scope_entries_with_files(authored, scope=scope, kinds=kinds)
    return entries


def entry_origin(entry: StateCap) -> SourceOrigin:
    """Return where one State capability's content originates."""

    return entry.source.origin


def entry_form(entry: StateCap) -> CapForm:
    """Return how one State capability enters State."""

    return entry.source.form


def entry_scope(entry: StateCap, *, agent_name: str) -> CapScope:
    """Return where one State capability is available."""

    del agent_name
    return entry.scope


def entry_ref(entry: StateCap, *, agent_name: str) -> str:
    """Return the canonical external ref for one State capability."""

    origin = entry_origin(entry)
    if origin == "remote":
        return entry.ref
    if entry.source.form == "inline":
        return f"inline://{DIR_NAME_BY_KIND[entry.kind]}/{entry.name}"
    scope = entry_scope(entry, agent_name=agent_name)
    return f"{scope}://{DIR_NAME_BY_KIND[entry.kind]}/{entry.name}"


def entry_definition_file(entry: StateCap) -> str:
    """Return the authored file that defines or links one State capability."""

    return entry.source.path


def entry_source(entry: StateCap, *, agent_name: str, root: Path | None) -> str:
    """Return the human-facing source location for one capability."""

    form = entry_form(entry)
    if form in {"referenced", "configured"}:
        return cap_source.source_location(entry.ref, shape=entry.shape)
    if root is None:
        raise ValueError("local cap locations require the Toolang root")
    source_path = root / entry_definition_file(entry)
    if form == "authored" and entry.kind == "skill" and source_path.name != "SKILL.md":
        source_path /= "SKILL.md"
    source = str(source_path)
    if form == "inline" and entry.source.line is not None:
        return f"{source}:{entry.source.line}"
    return source


def entry_line(entry: StateCap) -> int | None:
    """Return the authored source line for one State capability when known."""

    return entry.source.line


def collect_local_entries(
    authored: SourceSnapshot,
    *,
    scope: CapScope | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[StateCap, ...]:
    """Collect local capabilities from authored files."""

    entries: dict[str, StateCap] = {}
    for item in authored.files:
        entry_scope_value: CapScope = "root" if item.layer == "root" else "home"
        if scope is not None and entry_scope_value != scope:
            continue
        entry = _local_entry_from_file(authored, item)
        if entry is None:
            continue
        entry_scope_value: CapScope = "root" if item.layer == "root" else "home"
        if scope is not None and entry_scope_value != scope:
            continue
        if kinds is not None and entry.kind not in kinds:
            continue
        entries.setdefault(entry.ref, entry)
    return tuple(sorted(entries.values(), key=_entry_sort_key))


def _collect_scope_entries_with_files(
    authored: SourceSnapshot,
    *,
    scope: CapScope | None = None,
    kinds: set[EntryKind] | None = None,
    materialize_remote: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
    include_program: bool = True,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    local_entries = collect_local_entries(authored, scope=scope, kinds=kinds)
    remote_entries, files = _collect_remote_entries(
        authored,
        scope=scope,
        kinds=kinds,
        materialize=materialize_remote,
        remote_cache=remote_cache,
        progress=progress,
    )
    embedded_entries, embedded_files = (
        _collect_program_embedded_entries(
            authored,
            scope=scope,
            kinds=kinds,
        )
        if include_program
        else ((), {})
    )
    use_entries, use_files = (
        _collect_program_use_entries(
            authored,
            scope=scope,
            kinds=kinds,
            materialize=materialize_remote,
            remote_cache=remote_cache,
            progress=progress,
        )
        if include_program
        else ((), {})
    )
    merge_materialized_files(files, embedded_files)
    merge_materialized_files(files, use_files)
    entries = _dedupe_entries(
        (*local_entries, *remote_entries, *embedded_entries, *use_entries)
    )
    return entries, files


def materialize_scope(
    authored: SourceSnapshot,
    *,
    scope: CapScope,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
    include_program: bool = True,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    """Build State capabilities and materialized files for one scope."""

    entries, files = _collect_scope_entries_with_files(
        authored,
        scope=scope,
        materialize_remote=True,
        remote_cache=remote_cache,
        progress=progress,
        include_program=include_program,
    )
    _ensure_no_conflicts(entries)
    return entries, files


def materialize_program_caps(
    authored: SourceSnapshot,
    source: ProgramSource,
    *,
    program: Program,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    """Materialize only the here-scoped caps owned by one program module."""

    embedded, embedded_files = _collect_program_embedded_entries(
        authored,
        scope="home",
        program_source=source,
        program=program,
    )
    referenced, referenced_files = _collect_program_use_entries(
        authored,
        scope="home",
        materialize=True,
        remote_cache=remote_cache,
        progress=progress,
        program_source=source,
        program=program,
    )
    files = dict(embedded_files)
    merge_materialized_files(files, referenced_files)
    entries = _dedupe_entries((*embedded, *referenced))
    _ensure_no_conflicts(entries)
    return entries, files


def layer_remote_cache(
    authored: SourceSnapshot,
    *,
    scope: CapScope,
    entries: tuple[StateCap, ...],
) -> _RemoteEntryCache:
    """Build reusable remote inputs from one immutable State layer."""

    cache: _RemoteEntryCache = {}
    for entry in entries:
        if entry.source.origin != "remote":
            continue
        files = _cache_entry_files(authored.toolang_root, entry)
        declared_ref = entry.source.declared_ref
        if files is None or declared_ref is None:
            continue
        key = _remote_entry_cache_key(
            scope=scope,
            kind=entry.kind,
            form=entry.source.form,
            name=entry.name if entry.source.form == "configured" else None,
            declared_ref=declared_ref,
        )
        cache[key] = _CachedRemoteEntry(
            ref=entry.ref,
            name=entry.name,
            files=files,
            entry=Path(*Path(entry.materialized or "").parts[1:]).as_posix(),
        )
    return cache


def _remote_entry_cache_key(
    *,
    scope: CapScope,
    kind: EntryKind,
    form: CapForm,
    name: str | None,
    declared_ref: str,
) -> _RemoteEntryCacheKey:
    return (
        scope,
        kind,
        form,
        name if form == "configured" else None,
        declared_ref,
    )


def _cache_entry_files(
    toolang_root: Path, entry: StateCap
) -> tuple[tuple[str, bytes], ...] | None:
    if entry.materialized is None:
        return None
    path = Path(entry.path)
    parts = Path(entry.materialized).parts
    root = path.parents[len(parts) - 1]
    acquisition = root / parts[0]
    result: list[tuple[str, bytes]] = []
    try:
        for file in entry.resources:
            target = root / Path(file.path).relative_to("materialized")
            content = target.read_bytes()
            if len(content) != file.size or sha256(content).hexdigest() != file.sha256:
                return None
            result.append((target.relative_to(acquisition).as_posix(), content))
    except OSError:
        return None
    return tuple(result) or None


def _cached_remote_entry(
    remote_cache: _RemoteEntryCache | None,
    request: _RemoteEntryRequest,
) -> _CachedRemoteEntry | None:
    if remote_cache is None:
        return None
    key = _remote_entry_cache_key(
        scope=request.scope,
        kind=request.kind,
        form=request.form,
        name=request.name,
        declared_ref=request.ref,
    )
    return remote_cache.get(key)


def _emit_cached_remote_progress(
    request: _RemoteEntryRequest,
    canonical_ref: str,
    *,
    progress: ProgressSink | None,
) -> None:
    emit_progress(
        progress,
        id=request.progress_id,
        kind="prepare",
        stage="fetch",
        label=f"Fetched {request.kind} {canonical_ref}",
        status="skipped",
        detail="cached",
    )


def authored_entries_snapshot(
    authored: SourceSnapshot,
) -> dict[str, object]:
    """Return a JSON-friendly authored definitions snapshot."""

    root_entries, _ = _collect_scope_entries_with_files(authored, scope="root")
    home_entries, _ = _collect_scope_entries_with_files(authored, scope="home")
    return {
        "program_source": authored.program_path,
        "config_paths": list(authored.config_paths),
        "root_entries": [entry.to_snapshot() for entry in root_entries],
        "home_entries": [entry.to_snapshot() for entry in home_entries],
    }


def _local_entry_from_file(
    authored: SourceSnapshot,
    item: SourceFile,
) -> StateCap | None:
    if item.category != "cap":
        return None
    scope: CapScope = "root" if item.layer == "root" else "home"
    relative_path = Path(item.relative_path)
    local_parts = _local_parts(
        relative_path, agent_name=authored.agent_name, scope=scope
    )
    if len(local_parts) < 2:
        return None
    directory_name = local_parts[0]
    kind = KIND_BY_DIR_NAME.get(directory_name)
    if kind is None:
        return None
    if kind == "skill":
        if tuple(local_parts[2:]) != ("SKILL.md",):
            return None
        return _skill_entry(authored, item, scope=scope, name=local_parts[1])
    if (
        kind in FILE_BACKED_KINDS
        and len(local_parts) == 2
        and relative_path.suffix == ".md"
    ):
        return _file_entry(item, kind=kind)
    return None


def _skill_entry(
    authored: SourceSnapshot,
    definition: SourceFile,
    *,
    scope: CapScope,
    name: str,
) -> StateCap:
    prefix = Path() if scope == "root" else Path("agents") / authored.agent_name
    root_relative_dir = prefix / DIR_NAME_BY_KIND["skill"] / name
    root_relative_file = root_relative_dir / "SKILL.md"
    return StateCap(
        kind="skill",
        name=name,
        shape="dir",
        ref=_local_cap_ref(scope, "skill", name),
        path=root_relative_file.as_posix(),
        source=_snapshot_source_record(
            authored,
            root_relative_path=root_relative_dir,
        ),
        meta=_load_meta_text(definition.read_text()),
    )


def _file_entry(
    item: SourceFile,
    *,
    kind: EntryKind,
) -> StateCap:
    relative_path = Path(item.relative_path)
    return StateCap(
        kind=kind,
        name=relative_path.stem,
        shape="file",
        ref=_local_cap_ref(
            _scope_from_relative_path(relative_path), kind, relative_path.stem
        ),
        path=relative_path.as_posix(),
        source=_snapshot_file_source_record(
            item,
            root_relative_path=relative_path,
        ),
        meta=_load_meta_text(item.read_text()),
    )


def _snapshot_source_record(
    authored: SourceSnapshot,
    *,
    root_relative_path: Path,
) -> CapSource:
    files = tuple(
        item
        for item in authored.files
        if Path(item.relative_path).is_relative_to(root_relative_path)
    )
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda candidate: candidate.relative_path):
        relative_path = Path(item.relative_path).relative_to(root_relative_path)
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.digest.encode("utf-8"))
        digest.update(b"\n")
    return CapSource(
        origin="local",
        form="authored",
        path=root_relative_path.as_posix(),
        updated_at=_PORTABLE_SOURCE_UPDATED_AT,
        fingerprint=digest.hexdigest(),
    )


def _snapshot_file_source_record(
    item: SourceFile,
    *,
    root_relative_path: Path,
) -> CapSource:
    return CapSource(
        origin="local",
        form="authored",
        path=root_relative_path.as_posix(),
        updated_at=_PORTABLE_SOURCE_UPDATED_AT,
        fingerprint=item.digest,
    )


def _ensure_no_conflicts(entries: tuple[StateCap, ...]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for entry in entries:
        key = (entry.kind, entry.name)
        existing = seen.get(key)
        if existing is not None and existing != entry.ref:
            raise ValueError(
                f"conflicting capabilities in one scope: kind={entry.kind} name={entry.name}"
            )
        seen[key] = entry.ref


def _dedupe_entries(entries: tuple[StateCap, ...]) -> tuple[StateCap, ...]:
    by_ref: dict[tuple[str, str, str, str], StateCap] = {}
    for entry in sorted(entries, key=_entry_sort_key):
        key = (entry.effective_ref, entry.ref, entry.source.path, entry.source.form)
        by_ref.setdefault(key, entry)
    return tuple(sorted(by_ref.values(), key=_entry_sort_key))


def _load_meta_text(text: str) -> dict[str, object]:
    post = frontmatter.loads(text)
    return cast(dict[str, object], _json_compatible(dict(post.metadata)))


def _json_compatible(value: object) -> object:
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("Cap metadata requires finite numbers")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _entry_sort_key(entry: StateCap) -> tuple[str, str, str]:
    return (entry.kind, entry.name, entry.ref)


def _scope_from_relative_path(relative_path: Path) -> CapScope:
    return "home" if relative_path.parts[:1] == ("agents",) else "root"


def _local_cap_ref(
    scope: CapScope,
    kind: EntryKind,
    name: str,
) -> str:
    return f"{scope}://{DIR_NAME_BY_KIND[kind]}/{name}"


def _local_parts(
    relative_path: Path, *, agent_name: str, scope: CapScope
) -> tuple[str, ...]:
    if scope == "home" and relative_path.parts[:2] == ("agents", agent_name):
        return relative_path.parts[2:]
    return relative_path.parts


def _collect_remote_entries(
    authored: SourceSnapshot,
    *,
    scope: CapScope | None = None,
    kinds: set[EntryKind] | None = None,
    materialize: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    requests = _collect_remote_entry_requests(
        authored,
        scope=scope,
        kinds=kinds,
    )
    return _materialize_remote_entry_requests(
        requests,
        materialize=materialize,
        remote_cache=remote_cache,
        progress=progress,
    )


def _collect_remote_entry_requests(
    authored: SourceSnapshot,
    *,
    scope: CapScope | None,
    kinds: set[EntryKind] | None,
) -> tuple[_RemoteEntryRequest, ...]:
    scopes = ("root", "home") if scope is None else (scope,)
    requests: list[_RemoteEntryRequest] = []
    for item_scope in scopes:
        config_layer = "root" if item_scope == "root" else "agent"
        config_file = next(
            (
                item
                for item in authored.files
                if item.category == "config" and item.layer == config_layer
            ),
            None,
        )
        if config_file is None:
            continue
        for entry in ConfiguredCaps.parse(config_file.read_text(), kinds=kinds):
            requests.append(
                _RemoteEntryRequest(
                    scope=item_scope,
                    kind=entry.kind,
                    ref=entry.ref,
                    name=entry.name,
                    relative_config_path=Path(config_file.relative_path),
                    source_fingerprint=config_file.digest,
                    form="configured",
                )
            )
    return tuple(requests)


def _collect_program_use_entries(
    authored: SourceSnapshot,
    *,
    scope: CapScope | None = None,
    kinds: set[EntryKind] | None = None,
    materialize: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
    program_source: ProgramSource | None = None,
    program: Program | None = None,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    if scope == "root" or (program_source is None and authored.program_path is None):
        return (), {}
    program_source = program_source or authored.load_program()
    program = program if program is not None else program_source.parse()
    relative_program_path = Path(program_source.source_path)
    requests: list[_RemoteEntryRequest] = []
    for use in program.withs:
        kind = use.cap_kind
        if kind not in CAP_KINDS:
            continue
        if kinds is not None and kind not in kinds:
            continue
        requests.append(
            _RemoteEntryRequest(
                scope="home",
                kind=kind,
                ref=use.reference,
                name=None,
                relative_config_path=relative_program_path,
                source_fingerprint=program_source.digest,
                form="referenced",
                source_line=use.span.line,
            )
        )
    return _materialize_remote_entry_requests(
        tuple(requests),
        materialize=materialize,
        remote_cache=remote_cache,
        progress=progress,
    )


def _collect_program_embedded_entries(
    authored: SourceSnapshot,
    *,
    scope: CapScope | None = None,
    kinds: set[EntryKind] | None = None,
    program_source: ProgramSource | None = None,
    program: Program | None = None,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    if scope == "root" or (program_source is None and authored.program_path is None):
        return (), {}
    program_source = program_source or authored.load_program()
    program = program if program is not None else program_source.parse()
    relative_program_path = Path(program_source.source_path)
    entries: list[StateCap] = []
    files: dict[str, bytes] = {}
    seen: dict[tuple[EntryKind, str], int] = {}
    for cap in program.caps:
        if kinds is not None and cap.kind not in kinds:
            continue
        key = (cap.kind, cap.name)
        existing_line = seen.get(key)
        if existing_line is not None:
            raise ValueError(
                f"duplicate embedded {cap.kind} cap: {cap.name} "
                f"(lines {existing_line} and {cap.span.line})"
            )
        seen[key] = cap.span.line
        entry, entry_files = _embedded_entry_from_cap(
            cap=cap,
            relative_program_path=relative_program_path,
            source_fingerprint=program_source.digest,
        )
        entries.append(entry)
        merge_materialized_files(files, entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _embedded_entry_from_cap(
    *,
    cap: CapDecl,
    relative_program_path: Path,
    source_fingerprint: str,
) -> tuple[StateCap, dict[str, bytes]]:
    kind = cap.kind
    relative_entry_path = Path("inline") / DIR_NAME_BY_KIND[kind] / f"{cap.name}.md"
    return (
        StateCap(
            kind=kind,
            name=cap.name,
            shape="file",
            ref=f"inline://{DIR_NAME_BY_KIND[kind]}/{cap.name}",
            path=relative_entry_path.as_posix(),
            source=CapSource(
                origin="local",
                form="inline",
                path=relative_program_path.as_posix(),
                updated_at=_PORTABLE_SOURCE_UPDATED_AT,
                fingerprint=source_fingerprint,
                line=cap.span.line,
            ),
            meta=cast(dict[str, object], _json_compatible(dict(cap.meta))),
            body=cap.body.strip(),
        ),
        {},
    )


def _remote_entry_from_ref(
    *,
    scope: CapScope,
    kind: EntryKind,
    ref: str,
    name: str | None,
    relative_config_path: Path,
    source_fingerprint: str,
    form: Literal["configured", "referenced"],
    source_line: int | None = None,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[StateCap, dict[str, bytes]]:
    from toolang.common.source import Acquisition

    request = _RemoteEntryRequest(
        scope,
        kind,
        ref,
        name,
        relative_config_path,
        source_fingerprint,
        form,
        source_line,
    )
    cached = _cached_remote_entry(remote_cache, request)
    captured = (
        Acquisition(
            cached.ref,
            cached.entry,
            dict(cached.files),
            cached.name,
        )
        if cached is not None
        else None
    )

    using_cached = False

    def report(event: ProgressEvent) -> None:
        if progress is not None:
            progress(
                replace(
                    event,
                    subject="cap",
                    ref=ref,
                    scope=request.progress_scope,
                    cached=using_cached,
                )
            )

    if materialize:
        acquired = cap_source.acquire(
            kind,
            ref,
            cached=captured,
            progress=report if progress else None,
            progress_id=request.progress_id,
        )
        canonical, name = acquired.resolved_ref, name or acquired.name
        materialized = f"{acquired.key}/{acquired.entry}"
        path = f"materialized/{materialized}"
        files = {
            f"materialized/{acquired.key}/{key}": content
            for key, content in acquired.files.items()
        }
        content = acquired.files[acquired.entry]
        using_cached = acquired is captured
        if using_cached:
            _emit_cached_remote_progress(request, canonical, progress=report)
    else:
        canonical = cap_source.canonicalize_remote_ref(kind, ref)
        name = name or cap_source.remote_entry_name(kind, canonical)
        path = _relative_remote_entry_path(kind=kind, name=name, form=form).as_posix()
        materialized = None
        content = _remote_placeholder_content(kind=kind, name=name, ref=canonical)
        files = {path: content}
    reused = materialize and acquired is captured
    if not reused:
        emit_progress(
            report,
            id=request.progress_id,
            kind="prepare",
            stage="materialize",
            label=f"Preparing {kind} {name}...",
            status="running",
            detail=path,
        )
    post = frontmatter.loads(content.decode("utf-8"))
    entry = StateCap(
        kind=kind,
        name=name,
        shape="dir" if kind == "skill" else "file",
        ref=canonical,
        path=path,
        source=CapSource(
            origin="remote",
            form=form,
            path=relative_config_path.as_posix(),
            updated_at=_PORTABLE_SOURCE_UPDATED_AT,
            fingerprint=source_fingerprint,
            declared_ref=ref,
            line=source_line,
        ),
        meta=cast(dict[str, object], _json_compatible(dict(post.metadata))),
        body=post.content.strip(),
        materialized=materialized,
    )
    emit_progress(
        report,
        id=request.progress_id,
        kind="prepare",
        stage="materialize",
        label=f"Skipped updating {kind} {name}"
        if reused
        else f"Prepared {kind} {name}",
        status="skipped" if reused else "ok",
        detail="cached" if reused else path,
        complete=True,
    )
    return entry, files


def _materialize_remote_entry_requests(
    requests: tuple[_RemoteEntryRequest, ...],
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    if not requests:
        return (), {}
    if not materialize:
        return _materialize_remote_entry_requests_serial(
            requests,
            materialize=materialize,
            remote_cache=remote_cache,
            progress=progress,
        )
    for request in requests:
        _emit_remote_entry_pending(request, progress=progress)
    entries: list[StateCap] = []
    files: dict[str, bytes] = {}
    results: list[tuple[StateCap, dict[str, bytes]] | None] = [None] * len(requests)
    first_error: BaseException | None = None
    executor = ThreadPoolExecutor(max_workers=REMOTE_CAP_MATERIALIZE_WORKERS)
    try:
        futures = {
            executor.submit(
                copy_context().run,
                _remote_entry_from_request,
                request,
                materialize=materialize,
                remote_cache=remote_cache,
                progress=progress,
            ): index
            for index, request in enumerate(requests)
        }
        try:
            for future in as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if first_error is not None:
        raise first_error
    for result in results:
        if result is None:
            continue
        entry, entry_files = result
        entries.append(entry)
        merge_materialized_files(files, entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _materialize_remote_entry_requests_serial(
    requests: tuple[_RemoteEntryRequest, ...],
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    entries: list[StateCap] = []
    files: dict[str, bytes] = {}
    for request in requests:
        entry, entry_files = _remote_entry_from_request(
            request,
            materialize=materialize,
            remote_cache=remote_cache,
            progress=progress,
        )
        entries.append(entry)
        merge_materialized_files(files, entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def merge_materialized_files(
    target: dict[str, bytes], captured: Mapping[str, bytes]
) -> None:
    """Share complete identical acquisitions without mixing different captures."""
    incoming: dict[str, dict[str, bytes]] = {}
    previous: dict[str, dict[str, bytes]] = {}
    for path, content in captured.items():
        key = Path(path).parts[1]
        incoming.setdefault(key, {})[path] = content
    for path, content in target.items():
        if not path.startswith("materialized/"):
            continue
        key = Path(path).parts[1]
        if key in incoming:
            previous.setdefault(key, {})[path] = content
    for key, files in previous.items():
        if files != incoming[key]:
            raise ValueError(f"conflicting acquired files: {key}")
    target.update(captured)


def _remote_entry_from_request(
    request: _RemoteEntryRequest,
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[StateCap, dict[str, bytes]]:
    return _remote_entry_from_ref(
        scope=request.scope,
        kind=request.kind,
        ref=request.ref,
        name=request.name,
        relative_config_path=request.relative_config_path,
        source_fingerprint=request.source_fingerprint,
        form=request.form,
        source_line=request.source_line,
        materialize=materialize,
        remote_cache=remote_cache,
        progress=progress,
    )


def _emit_remote_entry_pending(
    request: _RemoteEntryRequest,
    *,
    progress: ProgressSink | None,
) -> None:
    emit_progress(
        progress,
        id=request.progress_id,
        kind="prepare",
        stage="materialize",
        label=f"Preparing {request.kind}...",
        status="pending",
        subject="cap",
        ref=request.ref,
        scope=request.progress_scope,
    )


def _relative_remote_entry_path(
    *,
    kind: EntryKind,
    name: str,
    form: Literal["configured", "referenced"],
) -> Path:
    root = Path(form) / DIR_NAME_BY_KIND[kind] / name
    if kind == "skill":
        return root / "SKILL.md"
    return root.with_suffix(".md")


def _remote_placeholder_content(
    *,
    kind: EntryKind,
    name: str,
    ref: str,
) -> bytes:
    post = frontmatter.Post(
        f"Remote {kind} materialized from {ref}\n",
        name=name,
        ref=ref,
        remote=True,
    )
    return frontmatter.dumps(post).encode("utf-8")
