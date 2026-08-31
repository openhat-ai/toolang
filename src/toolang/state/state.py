"""Resolved capability data and immutable runtime Agent State."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import tarfile
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import frontmatter

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
from ..lang.ast import (
    AgicDecl,
    CapDecl,
    FlowDecl,
    Parameter,
    Program,
    Span,
    to_data,
)
from toolang.common.github import (
    GitHubRef,
    github_raw_url,
    parse_github_ref,
    parse_github_url,
)

from .types import (
    EntryKind,
    EntryShape,
    CapScope,
    RunnableKind,
    CapForm,
    SourceOrigin,
)

CAP_KINDS: tuple[EntryKind, ...] = CATALOG_CAP_KINDS
EMBEDDED_CAP_KINDS = frozenset({"psyche", "service", "prompt"})
FILE_BACKED_KINDS = frozenset({"psyche", "service", "prompt"})
DIR_NAME_BY_KIND: dict[EntryKind, str] = CAP_DIR_BY_KIND
KIND_BY_DIR_NAME: dict[str, EntryKind] = CAP_KIND_BY_DIR
REMOTE_CAP_MATERIALIZE_WORKERS = 4
_FLOW_FILENAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
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

    def to_data(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> MaterializedFile:
        size = data["size"]
        if not isinstance(size, int) or isinstance(size, bool):
            raise TypeError("resolved file size must be an integer")
        return cls(
            path=str(data["path"]),
            size=size,
            sha256=str(data["sha256"]),
        )


@dataclass(frozen=True, slots=True)
class CapResolution:
    """Persisted resolution of one referenced or configured capability."""

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

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "kind": self.kind,
            "name": self.name,
            "form": self.form,
            "declared_ref": self.declared_ref,
            "resolved_ref": self.resolved_ref,
            "definition": self.definition,
            "materialized": self.materialized,
            "content_hash": self.content_hash,
            "files": [file.to_data() for file in self.files],
        }
        if self.line is not None:
            data["line"] = self.line
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> CapResolution:
        raw_files = data["files"]
        if not isinstance(raw_files, list):
            raise TypeError("resolved cap files must be a list")
        line = data.get("line")
        return cls(
            kind=cast(EntryKind, str(data["kind"])),
            name=str(data["name"]),
            form=cast(CapForm, str(data["form"])),
            declared_ref=str(data["declared_ref"]),
            resolved_ref=str(data["resolved_ref"]),
            definition=str(data["definition"]),
            materialized=str(data["materialized"]),
            content_hash=str(data["content_hash"]),
            files=tuple(
                MaterializedFile.from_data(cast(dict[str, object], file))
                for file in raw_files
                if isinstance(file, dict)
            ),
            line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        )


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

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CapSource:
        raw_line = data.get("line")
        return cls(
            origin=cast(SourceOrigin, str(data["origin"])),
            form=cast(CapForm, str(data["form"])),
            path=str(data["path"]),
            updated_at=str(data["updated_at"]),
            fingerprint=str(data["fingerprint"]),
            declared_ref=(
                str(data["declared_ref"])
                if data.get("declared_ref") is not None
                else None
            ),
            line=raw_line if isinstance(raw_line, int) else None,
        )


@dataclass(frozen=True, slots=True)
class StateCap:
    """One capability backed by an immutable State layer path."""

    kind: EntryKind
    name: str
    shape: EntryShape
    ref: str
    path: str
    source: CapSource
    meta: Mapping[str, object]

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

        return Path(self.path).read_text(encoding="utf-8")

    def read_content(self) -> str:
        """Read the capability body lazily from its immutable layer file."""

        return frontmatter.loads(self.read_text()).content.strip()

    def to_snapshot(self) -> dict[str, object]:
        return self.to_data()

    @classmethod
    def from_data(cls, data: dict[str, object]) -> StateCap:
        return cls(
            kind=cast(EntryKind, str(data["kind"])),
            name=str(data["name"]),
            shape=cast(EntryShape, str(data["shape"])),
            ref=str(data["ref"]),
            path=str(data["path"]),
            source=CapSource.from_data(cast(dict[str, object], data["source"])),
            meta=dict(cast(dict[str, object], data.get("meta", {}))),
        )


_RUNTIME_DEFAULT_AGIC = AgicDecl(
    name="default",
    input=Parameter(name="_", type_name="Part[]", span=Span(line=1)),
    span=Span(line=1),
)


def effective_agics(program: Program) -> tuple[AgicDecl, ...]:
    """Return authored agics plus the implicit runtime default when needed."""

    if program.find_agic("default") is not None:
        return program.agics
    return (*program.agics, _RUNTIME_DEFAULT_AGIC)


def public_runnable_index(
    modules: Mapping[str, Program],
    module_sources: Mapping[str, str],
) -> tuple[dict[str, AgicDecl | FlowDecl], dict[str, str]]:
    """Index unique public runnable bindings across State Programs."""

    result: dict[str, AgicDecl | FlowDecl] = {}
    owners: dict[str, str] = {}

    def add(name: str, module: str, declaration: AgicDecl | FlowDecl) -> None:
        if name in result:
            raise ValueError(f"Public runnable name is not unique: {name}")
        result[name] = declaration
        owners[name] = module

    agent = modules.get("agent")
    if agent is None:
        raise ValueError("home State layer is missing the agent module")
    for agic in effective_agics(agent):
        add(agic.name, "agent", agic)
    for flow in agent.flows:
        add(flow.name, "agent", flow)
    for module, program in modules.items():
        if module == "agent":
            continue
        public_name, local_name = flow_export(module_sources[module], program)
        declaration = program.find_flow(local_name)
        if declaration is None:  # pragma: no cover - flow_export invariant
            raise ValueError(
                f"Public flow declaration not found: {module}${local_name}"
            )
        add(public_name, module, declaration)
    return result, owners


def module_runnable_index(
    modules: Mapping[str, Program],
) -> dict[str, AgicDecl | FlowDecl]:
    """Index every runnable declaration by Program and local identity."""

    result: dict[str, AgicDecl | FlowDecl] = {}
    for module, program in modules.items():
        for declaration in (*effective_agics(program), *program.flows):
            result[_module_runnable_key(module, declaration.kind, declaration.name)] = (
                declaration
            )
    return result


def flow_export(source: str, program: Program) -> tuple[str, str]:
    public_name = Path(source).stem
    candidates = tuple(
        flow
        for flow in program.flows
        if not flow.name_explicit or flow.name == public_name
    )
    if len(candidates) != 1:
        raise ValueError(f"flow module export does not match its program: {source}")
    return public_name, candidates[0].name


def program_term_data(
    *,
    name: str,
    source: str,
    digest: str,
    program: Program,
    here_caps: tuple[StateCap, ...],
) -> dict[str, object]:
    """Serialize one source-bound Program term for the State layer document."""

    public_name, local_name = (
        (None, None) if name == "agent" else flow_export(source, program)
    )
    return {
        "name": name,
        "kind": "agent" if name == "agent" else "flow",
        "authored_path": source,
        "materialized_path": f"files/{source}",
        "digest": digest,
        "program": to_data(program),
        "export": (
            {"public_name": public_name, "local_name": local_name}
            if public_name is not None
            else None
        ),
        "here_caps": [cap.to_data() for cap in sorted(here_caps, key=_entry_sort_key)],
    }


def _module_runnable_key(module: str, kind: str, local_name: str) -> str:
    return f"{module}${kind}:{local_name}"


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


@dataclass(frozen=True, slots=True)
class AgentState:
    """Effective State terms and aggregate indexes fixed for one top-level run."""

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
    base_caps: tuple[StateCap, ...] | None = None
    revision_dir: Path | None = None
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
        _require_revision(self.revision, name="Agent State revision")
        if self.revision != agent_state_revision(
            self.root_revision,
            self.home_revision,
        ):
            raise ValueError("Agent State revision does not match its layer revisions")
        if self.revision_dir is not None and self.revision_dir.name != self.revision:
            raise ValueError(
                "Agent State revision directory does not match its revision"
            )
        object.__setattr__(self, "root_config", freeze_mapping(self.root_config))
        object.__setattr__(self, "home_config", freeze_mapping(self.home_config))
        object.__setattr__(self, "config", freeze_mapping(self.config))
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

    def module_runnable(
        self,
        module: str,
        local_name: str,
        *,
        kind: str | None = None,
    ) -> AgicDecl | FlowDecl | None:
        """Return one Program-local runnable declaration."""

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
        """Return effective root/home/here caps for one executing module."""

        base = tuple(self.caps.values()) if self.base_caps is None else self.base_caps
        here = self.module_caps.get(module)
        if here is None:
            raise ValueError(f"Program not found: {module}")
        return effective_caps(base, here)

    def to_snapshot(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "root_revision": self.root_revision,
            "home_revision": self.home_revision,
            "caps": [cap.path for cap in self.caps.values()],
            "modules": [
                program_term_data(
                    name=name,
                    source=self.module_sources[name],
                    digest=self.module_digests[name],
                    program=program,
                    here_caps=self.module_caps[name],
                )
                for name, program in self.modules.items()
            ],
        }


@dataclass(frozen=True, slots=True)
class StateResources:
    """Process-local effective cap collections for one durable Agent State."""

    caps_by_module: Mapping[str, tuple[StateCap, ...]]

    def __post_init__(self) -> None:
        values = {name: tuple(entries) for name, entries in self.caps_by_module.items()}
        if tuple(sorted(values)) != tuple(values):
            raise ValueError("State resource modules must be sorted by name")
        object.__setattr__(self, "caps_by_module", freeze_mapping(values))

    def caps_for(self, module: str) -> tuple[StateCap, ...]:
        """Return the precomputed effective caps for one Program module."""

        try:
            return self.caps_by_module[module]
        except KeyError as exc:
            raise ValueError(f"Program not found: {module}") from exc


@dataclass(frozen=True, slots=True)
class StatePublication:
    """One durable Agent State paired with its effective runtime resources."""

    state: AgentState
    resources: StateResources

    def __post_init__(self) -> None:
        if not isinstance(self.state, AgentState):
            raise TypeError("State publication requires an AgentState")
        if not isinstance(self.resources, StateResources):
            raise TypeError("State publication requires StateResources")
        if set(self.resources.caps_by_module) != set(self.state.modules):
            raise ValueError("State publication resources must cover every module")

    @property
    def revision(self) -> str:
        """Return the durable State revision represented by this publication."""

        return self.state.revision

    @property
    def root_revision(self) -> str:
        return self.state.root_revision

    @property
    def home_revision(self) -> str:
        return self.state.home_revision

    @property
    def root_config(self) -> Mapping[str, object]:
        return self.state.root_config

    @property
    def home_config(self) -> Mapping[str, object]:
        return self.state.home_config

    @property
    def config(self) -> Mapping[str, object]:
        return self.state.config

    @property
    def caps(self) -> Mapping[str, StateCap]:
        return self.state.caps

    @property
    def modules(self) -> Mapping[str, Program]:
        return self.state.modules

    @property
    def module_sources(self) -> Mapping[str, str]:
        return self.state.module_sources

    @property
    def module_digests(self) -> Mapping[str, str]:
        return self.state.module_digests

    @property
    def module_caps(self) -> Mapping[str, tuple[StateCap, ...]]:
        return self.state.module_caps

    @property
    def psyches(self) -> Mapping[str, StateCap]:
        return self.state.psyches

    @property
    def skills(self) -> Mapping[str, StateCap]:
        return self.state.skills

    @property
    def services(self) -> Mapping[str, StateCap]:
        return self.state.services

    @property
    def prompts(self) -> Mapping[str, StateCap]:
        return self.state.prompts

    @property
    def agics(self) -> Mapping[str, AgicDecl]:
        return self.state.agics

    @property
    def flows(self) -> Mapping[str, FlowDecl]:
        return self.state.flows

    @property
    def runnables(self) -> Mapping[str, AgicDecl | FlowDecl]:
        return self.state.runnables

    @property
    def runnable_modules(self) -> Mapping[str, str]:
        return self.state.runnable_modules

    @property
    def module_runnables(self) -> Mapping[str, AgicDecl | FlowDecl]:
        return self.state.module_runnables

    def caps_for(self, module: str) -> tuple[StateCap, ...]:
        """Return the already-filtered cap collection for one module."""

        return self.resources.caps_for(module)


def publish_state_resources(
    state: AgentState,
    *,
    agent_name: str,
    allow_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
) -> StatePublication:
    """Derive cap-kind allow policy once for one immutable State revision."""

    from .collections import cap_dataset
    from .config import CAP_ALLOW_FIELDS, resolve_cap_allows

    allows = resolve_cap_allows(
        (state.root_config, state.home_config),
        overrides=allow_overrides,
    )
    by_module: dict[str, tuple[StateCap, ...]] = {}
    for module in state.modules:
        base = state.caps_for(module)
        selected_ids: set[tuple[str, str, str]] = set()
        for allow_field in CAP_ALLOW_FIELDS:
            kind = cast(EntryKind, allow_field.removesuffix("s"))
            entries = tuple(item for item in base if item.kind == kind)
            queries = allows.get(allow_field)
            if queries is None:
                selected = entries
            elif not queries:
                selected = ()
            else:
                selected = tuple(
                    cast(StateCap, item.record)
                    for item in cap_dataset(
                        entries,
                        agent_name=agent_name,
                        kind=kind,
                    ).query(queries)
                )
            selected_ids.update((item.kind, item.name, item.ref) for item in selected)
        by_module[module] = tuple(
            item for item in base if (item.kind, item.name, item.ref) in selected_ids
        )
    resources = StateResources({name: by_module[name] for name in sorted(by_module)})
    return StatePublication(state=state, resources=resources)


def compose_agent_state(
    *,
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
    revision_dir: Path | None = None,
) -> AgentState:
    """Compose runtime State from one exact root/home layer pair."""

    effective_base = effective_caps(root_caps, home_caps)
    agent_here = module_caps.get("agent", ())
    return AgentState(
        revision=agent_state_revision(root_revision, home_revision),
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
    elif name != flow_module_name(source):
        raise ValueError("flow Program name must match its source path")
    _require_revision(digest, name="State Program digest")
    if any(cap.scope != "here" for cap in here_caps):
        raise ValueError("State Program caps must have here scope")
    if tuple(sorted(here_caps, key=_entry_sort_key)) != here_caps:
        raise ValueError("State Program capabilities must be sorted")
    if len({_entry_sort_key(cap) for cap in here_caps}) != len(here_caps):
        raise ValueError("State Program capabilities must be unique")
    if name != "agent":
        flow_export(source, program)
    for cap in here_caps:
        path = Path(cap.path)
        if path.is_absolute():
            continue
        parts = path.parts[1:] if path.parts[:1] == ("files",) else path.parts
        if parts[:4] != ("caps", cap.source.form, name, cap.kind):
            raise ValueError("State Program cap path must include its module name")


def _merge_config(
    base: Mapping[str, object], override: Mapping[str, object]
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config(
                cast(Mapping[str, object], current),
                cast(Mapping[str, object], value),
            )
        else:
            merged[key] = value
    return merged


def agent_state_revision(root_revision: str, home_revision: str) -> str:
    """Return the canonical revision of one exact root/home layer pair."""

    _require_revision(root_revision, name="root revision")
    _require_revision(home_revision, name="home revision")
    document = {
        "home_revision": home_revision,
        "root_revision": root_revision,
        "schema": 1,
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def flow_module_name(authored_path: str) -> str:
    """Return the stable portable module name for one direct flow source."""

    path = Path(authored_path)
    if len(path.parts) != 2 or path.parts[0] != "flows" or path.suffix != ".too":
        raise ValueError(f"Flow module source path is invalid: {authored_path!r}")
    stem = path.stem
    if _FLOW_FILENAME_RE.fullmatch(stem) is None:
        raise ValueError(f"Flow module filename is not portable: {stem!r}")
    if stem.casefold() in _WINDOWS_RESERVED_FILENAMES:
        raise ValueError(f"Flow module filename is reserved on Windows: {stem!r}")
    return f"_flow_{stem}"


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
        or path.parts[:1] != ("files",)
        or "\\" in value
    ):
        raise ValueError(f"{label} path must be portable and inside files/")


def resolve_remote_ref(
    kind: EntryKind,
    ref: str,
    *,
    progress: ProgressSink | None = None,
    progress_id: str | None = None,
) -> str:
    """Resolve one remote cap ref or shorthand to a canonical ref."""

    text = ref.strip()
    item_id = progress_id or _cap_progress_id(kind, text)
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="resolve",
        label=f"Resolving {kind}...",
        status="running",
        detail=text,
    )
    try:
        if "://" in text:
            canonical = canonicalize_remote_ref(kind, text)
            if canonical.startswith("github://") and not _github_remote_exists(
                kind, canonical
            ):
                raise ValueError(
                    f"remote {kind} not found or missing entry file: {ref}"
                )
        else:
            candidates = _remote_ref_candidates(kind, text)
            canonical = next(
                (
                    candidate
                    for candidate in candidates
                    if _github_remote_exists(kind, candidate)
                ),
                "",
            )
            if not canonical:
                message = (
                    f"invalid remote ref: {ref}"
                    if not candidates
                    else f"could not resolve remote {kind} shorthand: {ref}"
                )
                raise ValueError(message)
    except Exception as exc:
        emit_progress(
            progress,
            id=item_id,
            kind="prepare",
            stage="resolve",
            label=f"Failed to resolve {kind}",
            status="failed",
            detail=str(exc),
        )
        raise
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="resolve",
        label=f"Resolved {kind}",
        status="ok",
        detail=canonical,
    )
    return canonical


def canonicalize_remote_ref(kind: EntryKind, ref: str) -> str:
    """Canonicalize one explicit or shorthand remote cap ref."""

    text = ref.strip()
    if "://" in text:
        github_ref = parse_github_url(text)
        if github_ref is None and text.startswith("github://"):
            github_ref = parse_github_ref(text)
        if github_ref is not None:
            if kind == "skill" and Path(github_ref.path).name == "SKILL.md":
                github_ref = GitHubRef(
                    owner=github_ref.owner,
                    repo=github_ref.repo,
                    path=Path(github_ref.path).parent.as_posix(),
                    rev=github_ref.rev,
                )
            return github_ref.render()
        return text
    candidates = _remote_ref_candidates(kind, text)
    if not candidates:
        raise ValueError(f"invalid remote ref: {ref}")
    return candidates[0]


def remote_entry_name(kind: EntryKind, ref: str) -> str:
    """Return the default authored name for one remote cap ref."""

    canonical_ref = canonicalize_remote_ref(kind, ref)
    if canonical_ref.startswith("github://"):
        path = parse_github_ref(canonical_ref).path.rstrip("/")
        if not path:
            raise ValueError(f"invalid remote ref: {ref}")
        return Path(path).name if kind == "skill" else Path(path).stem
    parsed = urlparse(canonical_ref)
    path = parsed.path.rstrip("/")
    if not path:
        raise ValueError(f"invalid remote ref: {ref}")
    name = Path(path).name.split("@", 1)[0]
    return name if kind == "skill" else Path(name).stem


def _remote_ref_candidates(kind: EntryKind, ref: str) -> tuple[str, ...]:
    slash_count = ref.count("/")
    if slash_count == 2:
        owner, repo, name = ref.split("/", 2)
        if not owner or not repo or not name:
            return ()
        return _remote_ref_candidates_for_repo(kind, owner, repo, name)
    if slash_count != 1:
        return ()
    owner, name = ref.split("/", 1)
    if not owner or not name:
        return ()
    repositories: dict[EntryKind, tuple[tuple[str, str], ...]] = {
        "skill": (
            ("agents", f"skills/{name}"),
            ("agent-skills", name),
            ("agent-skills", f"skills/{name}"),
            ("skills", name),
            ("skills", f"skills/{name}"),
        ),
        "service": (
            ("agents", f"services/{name}.md"),
            ("agent-services", f"{name}.md"),
            ("services", f"{name}.md"),
        ),
        "prompt": (
            ("agents", f"prompts/{name}.md"),
            ("agent-prompts", f"{name}.md"),
            ("prompts", f"{name}.md"),
        ),
        "psyche": (
            ("agents", f"psyches/{name}.md"),
            ("agent-psyches", f"{name}.md"),
            ("psyches", f"{name}.md"),
        ),
    }
    return tuple(
        _github_remote_ref_with_default_branch(owner, repo, path)
        for repo, path in repositories[kind]
    )


def _remote_ref_candidates_for_repo(
    kind: EntryKind,
    owner: str,
    repo: str,
    name: str,
) -> tuple[str, ...]:
    return tuple(
        _github_remote_ref_with_default_branch(owner, repo, path)
        for path in _remote_path_candidates_for_repo(kind, repo, name)
    )


def _remote_path_candidates_for_repo(
    kind: EntryKind,
    repo: str,
    name: str,
) -> tuple[str, ...]:
    if kind == "skill":
        if repo in {"agent-skills", "skills"}:
            return (name, f"skills/{name}")
        return (f"skills/{name}", name)
    directory = DIR_NAME_BY_KIND[kind]
    if repo in {f"agent-{directory}", directory}:
        return (f"{name}.md",)
    return (f"{directory}/{name}.md", f"{name}.md")


def _github_remote_ref_with_default_branch(owner: str, repo: str, path: str) -> str:
    try:
        rev = _github_repo_default_branch(owner, repo)
    except ValueError:
        rev = "main"
    return GitHubRef(owner=owner, repo=repo, path=path, rev=rev).render()


def _github_remote_exists(kind: EntryKind, ref: str) -> bool:
    github_ref = parse_github_ref(ref)
    probe_ref = github_ref
    if kind == "skill":
        probe_ref = GitHubRef(
            owner=github_ref.owner,
            repo=github_ref.repo,
            path=(Path(github_ref.path) / "SKILL.md").as_posix(),
            rev=github_ref.rev,
        )
    request = Request(
        github_raw_url(probe_ref),
        method="HEAD",
        headers={"User-Agent": "toolang/0.1"},
    )
    try:
        with urlopen(request, timeout=30):
            return True
    except (HTTPError, URLError):
        return False


@lru_cache
def _github_repo_default_branch(owner: str, repo: str) -> str:
    api_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    )
    request = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "toolang/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"could not resolve GitHub default branch: {owner}/{repo}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("default_branch"), str):
        raise ValueError(f"unexpected GitHub repository response: {owner}/{repo}")
    return data["default_branch"]


@dataclass(frozen=True, slots=True)
class _RemoteEntryRequest:
    scope: CapScope
    kind: EntryKind
    ref: str
    name: str | None
    relative_config_path: Path
    source_fingerprint: str
    source_mtime_ns: int
    form: Literal["configured", "referenced"]
    source_line: int | None = None

    @property
    def progress_id(self) -> str:
        return _cap_progress_id(self.kind, self.ref)


@dataclass(frozen=True, slots=True)
class _CachedRemoteEntry:
    ref: str
    files: tuple[tuple[str, bytes], ...]


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


def entry_source(entry: StateCap, *, agent_name: str) -> str:
    """Return the human-facing source location for one capability."""

    form = entry_form(entry)
    if form in {"referenced", "configured"}:
        ref = entry_ref(entry, agent_name=agent_name)
        if not ref.startswith("github://"):
            return ref
        try:
            github = parse_github_ref(ref)
        except ValueError:
            return ref
        view = "blob" if entry.shape == "file" else "tree"
        return (
            f"https://github.com/{quote(github.owner, safe='')}/"
            f"{quote(github.repo, safe='')}/{view}/{quote(github.rev, safe='/')}/"
            f"{quote(github.path, safe='/')}"
        )
    source = entry_definition_file(entry)
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
        entry = _local_entry_from_file(authored, item)
        if entry is None:
            continue
        entry_scope_value: CapScope = "root" if item.origin == "root" else "home"
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
            materialize=materialize_remote,
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
    files.update(embedded_files)
    files.update(use_files)
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
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    """Materialize only the here-scoped caps owned by one program module."""

    embedded, embedded_files = _collect_program_embedded_entries(
        authored,
        scope="home",
        materialize=True,
        program_source=source,
    )
    referenced, referenced_files = _collect_program_use_entries(
        authored,
        scope="home",
        materialize=True,
        remote_cache=remote_cache,
        progress=progress,
        program_source=source,
    )
    files = dict(embedded_files)
    files.update(referenced_files)
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
            files=files,
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
    toolang_root: Path,
    entry: StateCap,
) -> tuple[tuple[str, bytes], ...] | None:
    entry_path = toolang_root / entry.path
    if entry.shape == "dir":
        root = entry_path.parent
        if not root.is_dir():
            return None
        return tuple(
            (path.relative_to(root).as_posix(), path.read_bytes())
            for path in sorted(item for item in root.rglob("*") if item.is_file())
        )
    if not entry_path.is_file():
        return None
    return (("", entry_path.read_bytes()),)


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


def _remap_cached_remote_files(
    cached: _CachedRemoteEntry,
    relative_entry_path: Path,
) -> dict[str, bytes]:
    if len(cached.files) == 1 and cached.files[0][0] == "":
        return {relative_entry_path.as_posix(): cached.files[0][1]}
    root = relative_entry_path.parent
    return {
        (root / relative_path).as_posix(): content
        for relative_path, content in cached.files
    }


def _emit_cached_remote_progress(
    request: _RemoteEntryRequest,
    canonical_ref: str,
    *,
    progress: ProgressSink | None,
) -> None:
    text = request.ref.strip()
    if "://" not in text:
        emit_progress(
            progress,
            id=request.progress_id,
            kind="prepare",
            stage="resolve",
            label=f"Resolved {request.kind} {canonical_ref}",
            status="ok",
            detail=canonical_ref,
        )
        return
    emit_progress(
        progress,
        id=request.progress_id,
        kind="prepare",
        stage="fetch",
        label=f"Fetched {request.kind} {canonical_ref}",
        status="ok",
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
    scope: CapScope = "root" if item.origin == "root" else "home"
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
    if kind in FILE_BACKED_KINDS and len(local_parts) == 2:
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
    latest_mtime_ns = max(item.mtime_ns for item in files)
    return CapSource(
        origin="local",
        form="authored",
        path=root_relative_path.as_posix(),
        updated_at=_mtime_text(latest_mtime_ns),
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
        updated_at=_mtime_text(item.mtime_ns),
        fingerprint=item.digest,
    )


def _mtime_text(mtime_ns: int) -> str:
    return datetime.fromtimestamp(
        mtime_ns / 1_000_000_000,
        tz=timezone.utc,
    ).isoformat()


def _source_record(
    *,
    root_relative_path: Path,
    absolute_path: Path,
    origin: SourceOrigin,
    form: CapForm,
    shape: Literal["file", "dir"],
    line: int | None = None,
) -> CapSource:
    fingerprint = (
        _dir_fingerprint(absolute_path)
        if shape == "dir"
        else _file_fingerprint(absolute_path)
    )
    return CapSource(
        origin=origin,
        form=form,
        path=root_relative_path.as_posix(),
        updated_at=_updated_at(absolute_path, shape=shape),
        fingerprint=fingerprint,
        line=line,
    )


def _file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    by_ref: dict[str, StateCap] = {}
    for entry in sorted(entries, key=_entry_sort_key):
        by_ref.setdefault(entry.ref, entry)
    return tuple(sorted(by_ref.values(), key=_entry_sort_key))


def _dir_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(item.read_bytes()).hexdigest().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _updated_at(path: Path, *, shape: Literal["file", "dir"]) -> str:
    if shape == "file":
        return datetime.fromtimestamp(
            path.stat().st_mtime_ns / 1_000_000_000, tz=timezone.utc
        ).isoformat()
    timestamps = [item.stat().st_mtime_ns for item in path.rglob("*") if item.is_file()]
    timestamps.append(path.stat().st_mtime_ns)
    latest = max(timestamps)
    return datetime.fromtimestamp(latest / 1_000_000_000, tz=timezone.utc).isoformat()


def _load_meta_text(text: str) -> dict[str, object]:
    post = frontmatter.loads(text)
    return cast(dict[str, object], _json_compatible(dict(post.metadata)))


def _json_compatible(value: object) -> object:
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
        config_origin = "root" if item_scope == "root" else "agent"
        config_file = next(
            (
                item
                for item in authored.files
                if item.category == "config" and item.origin == config_origin
            ),
            None,
        )
        if config_file is None:
            continue
        for entry in ConfiguredCaps(config_file.path).parse(
            config_file.read_text(), kinds=kinds
        ):
            requests.append(
                _RemoteEntryRequest(
                    scope=item_scope,
                    kind=entry.kind,
                    ref=entry.ref,
                    name=entry.name,
                    relative_config_path=Path(config_file.relative_path),
                    source_fingerprint=config_file.digest,
                    source_mtime_ns=config_file.mtime_ns,
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
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    if scope == "root" or (program_source is None and authored.program_path is None):
        return (), {}
    program_source = program_source or authored.load_program()
    program = program_source.parse()
    relative_program_path = Path(program_source.source_path)
    program_file = authored.program_file(program_source)
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
                source_mtime_ns=(program_file.mtime_ns if program_file else 0),
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
    materialize: bool = False,
    program_source: ProgramSource | None = None,
) -> tuple[tuple[StateCap, ...], dict[str, bytes]]:
    del materialize
    if scope == "root" or (program_source is None and authored.program_path is None):
        return (), {}
    program_source = program_source or authored.load_program()
    program = program_source.parse()
    relative_program_path = Path(program_source.source_path)
    program_path = authored.toolang_root / relative_program_path
    entries: list[StateCap] = []
    files: dict[str, bytes] = {}
    seen: dict[tuple[EntryKind, str], int] = {}
    for cap in program.caps:
        kind = _embedded_cap_kind(cap)
        if kind is None:
            continue
        if kinds is not None and kind not in kinds:
            continue
        key = (kind, cap.name)
        existing_line = seen.get(key)
        if existing_line is not None:
            raise ValueError(
                f"duplicate embedded {kind} cap: {cap.name} "
                f"(lines {existing_line} and {cap.span.line})"
            )
        seen[key] = cap.span.line
        entry, entry_files = _embedded_entry_from_cap(
            kind=kind,
            cap=cap,
            relative_program_path=relative_program_path,
            program_path=program_path,
            source_line=cap.span.line,
        )
        entries.append(entry)
        files.update(entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _embedded_cap_kind(cap: CapDecl) -> EntryKind | None:
    if cap.kind not in EMBEDDED_CAP_KINDS:
        return None
    return cap.kind


def _embedded_entry_from_cap(
    *,
    kind: EntryKind,
    cap: CapDecl,
    relative_program_path: Path,
    program_path: Path,
    source_line: int,
) -> tuple[StateCap, dict[str, bytes]]:
    relative_entry_path = _relative_embedded_entry_path(kind=kind, name=cap.name)
    content = _embedded_materialized_content(cap)
    return (
        StateCap(
            kind=kind,
            name=cap.name,
            shape="file",
            ref=f"inline://{DIR_NAME_BY_KIND[kind]}/{cap.name}",
            path=relative_entry_path.as_posix(),
            source=_source_record(
                root_relative_path=relative_program_path,
                absolute_path=program_path,
                origin="local",
                form="inline",
                shape="file",
                line=source_line,
            ),
            meta=_load_meta_text(content.decode("utf-8")),
        ),
        {relative_entry_path.as_posix(): content},
    )


def _relative_embedded_entry_path(
    *,
    kind: EntryKind,
    name: str,
) -> Path:
    return Path("inline") / DIR_NAME_BY_KIND[kind] / f"{name}.md"


def _embedded_materialized_content(cap: CapDecl) -> bytes:
    if not cap.meta:
        return cap.body.encode("utf-8")
    post = frontmatter.Post(cap.body, **dict(cap.meta))
    return frontmatter.dumps(post).encode("utf-8")


def _remote_entry_from_ref(
    *,
    scope: CapScope,
    kind: EntryKind,
    ref: str,
    name: str | None,
    relative_config_path: Path,
    source_fingerprint: str,
    source_mtime_ns: int,
    form: Literal["configured", "referenced"],
    source_line: int | None = None,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[StateCap, dict[str, bytes]]:
    request = _RemoteEntryRequest(
        scope=scope,
        kind=kind,
        ref=ref,
        name=name,
        relative_config_path=relative_config_path,
        source_fingerprint=source_fingerprint,
        source_mtime_ns=source_mtime_ns,
        form=form,
        source_line=source_line,
    )
    progress_id = request.progress_id
    cached = _cached_remote_entry(remote_cache, request)
    if cached is not None:
        canonical_ref = cached.ref
        _emit_cached_remote_progress(request, canonical_ref, progress=progress)
    elif materialize and "://" not in ref:
        canonical_ref = resolve_remote_ref(
            kind,
            ref,
            progress=progress,
            progress_id=progress_id,
        )
    else:
        canonical_ref = canonicalize_remote_ref(kind, ref)
    if name is None:
        name = remote_entry_name(kind, canonical_ref)
    relative_entry_path = _relative_remote_entry_path(
        kind=kind,
        name=name,
        form=form,
    )
    if cached is not None:
        entry_files = _remap_cached_remote_files(cached, relative_entry_path)
    elif materialize and progress is not None:
        entry_files = _remote_materialized_files(
            relative_entry_path=relative_entry_path,
            kind=kind,
            name=name,
            ref=canonical_ref,
            progress=progress,
            progress_id=progress_id,
        )
    elif materialize:
        entry_files = _remote_materialized_files(
            relative_entry_path=relative_entry_path,
            kind=kind,
            name=name,
            ref=canonical_ref,
        )
    else:
        entry_files = {
            relative_entry_path.as_posix(): _remote_placeholder_content(
                kind=kind,
                name=name,
                ref=canonical_ref,
            )
        }
    if materialize and cached is None:
        emit_progress(
            progress,
            id=progress_id,
            kind="prepare",
            stage="materialize",
            label=f"Updating {kind} {name}...",
            status="running",
            detail=str(relative_entry_path),
        )
    try:
        entry_content = entry_files[relative_entry_path.as_posix()]
        entry = StateCap(
            kind=kind,
            name=name,
            shape="dir" if kind == "skill" else "file",
            ref=canonical_ref,
            path=relative_entry_path.as_posix(),
            source=CapSource(
                origin="remote",
                form=form,
                path=relative_config_path.as_posix(),
                updated_at=_mtime_text(source_mtime_ns),
                fingerprint=source_fingerprint,
                declared_ref=ref,
                line=source_line,
            ),
            meta=_load_meta_text(entry_content.decode("utf-8")),
        )
    except Exception as exc:
        if materialize:
            emit_progress(
                progress,
                id=progress_id,
                kind="prepare",
                stage="materialize",
                label=f"Failed to update {kind} {name}",
                status="failed",
                detail=str(exc),
            )
        raise
    if materialize:
        if cached is not None:
            emit_progress(
                progress,
                id=progress_id,
                kind="prepare",
                stage="materialize",
                label=f"Skipped updating {kind} {name}",
                status="skipped",
                detail="cached",
            )
        else:
            emit_progress(
                progress,
                id=progress_id,
                kind="prepare",
                stage="materialize",
                label=f"Updating {kind} {name}...",
                status="running",
                detail=str(relative_entry_path),
            )
            emit_progress(
                progress,
                id=progress_id,
                kind="prepare",
                stage="materialize",
                label=f"Updated {kind} {name}",
                status="ok",
            )
    return entry, entry_files


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
        files.update(entry_files)
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
        files.update(entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


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
        source_mtime_ns=request.source_mtime_ns,
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
    ref = request.ref.strip()
    object_label = f" {request.name}" if request.name is not None else ""
    if "://" in ref:
        try:
            ref = canonicalize_remote_ref(request.kind, ref)
        except ValueError:
            pass
        emit_progress(
            progress,
            id=request.progress_id,
            kind="prepare",
            stage="fetch",
            label=f"Fetching {request.kind}{object_label}...",
            status="pending",
            detail=ref,
        )
        return
    emit_progress(
        progress,
        id=request.progress_id,
        kind="prepare",
        stage="resolve",
        label=f"Resolving {request.kind}{object_label}...",
        status="pending",
        detail=ref,
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


def _remote_materialized_files(
    *,
    relative_entry_path: Path,
    kind: EntryKind,
    name: str,
    ref: str,
    progress: ProgressSink | None = None,
    progress_id: str | None = None,
) -> dict[str, bytes]:
    if not ref.startswith("github://"):
        raise ValueError(f"unsupported remote {kind} ref: {ref}")
    github_ref = parse_github_ref(ref)
    item_id = progress_id or _cap_progress_id(kind, ref)
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="fetch",
        label=f"Fetching {kind} {name}...",
        status="running",
        detail=ref,
    )
    if kind == "skill":
        try:
            files = _fetch_github_directory(github_ref)
        except Exception as exc:
            emit_progress(
                progress,
                id=item_id,
                kind="prepare",
                stage="fetch",
                label=f"Failed to fetch {kind} {name}",
                status="failed",
                detail=str(exc),
            )
            raise
        if "SKILL.md" not in files:
            emit_progress(
                progress,
                id=item_id,
                kind="prepare",
                stage="fetch",
                label=f"Failed to fetch {kind} {name}",
                status="failed",
                detail=f"remote skill is missing SKILL.md: {ref}",
            )
            raise ValueError(f"remote skill is missing SKILL.md: {ref}")
        root = relative_entry_path.parent
        materialized = {
            (root / relative_path).as_posix(): content
            for relative_path, content in files.items()
        }
    else:
        try:
            materialized = {
                relative_entry_path.as_posix(): _fetch_github_file(github_ref)
            }
        except Exception as exc:
            emit_progress(
                progress,
                id=item_id,
                kind="prepare",
                stage="fetch",
                label=f"Failed to fetch {kind} {name}",
                status="failed",
                detail=str(exc),
            )
            raise
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="fetch",
        label=f"Fetched {kind} {name}",
        status="ok",
        detail=f"{len(materialized)} {'file' if len(materialized) == 1 else 'files'}",
    )
    return materialized


def _cap_progress_id(kind: EntryKind, ref: str) -> str:
    return f"cap:{kind}:{ref.strip()}"


def _fetch_github_directory(ref: GitHubRef) -> dict[str, bytes]:
    root = ref.path.strip("/")
    prefix = f"{root}/"
    archive_url = (
        f"https://codeload.github.com/{quote(ref.owner, safe='')}/"
        f"{quote(ref.repo, safe='')}/tar.gz/{quote(ref.rev, safe='')}"
    )
    archive_bytes = _fetch_url_bytes(archive_url)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            member_path = Path(member.name)
            path = "/".join(member_path.parts[1:])
            if not path.startswith(prefix):
                continue
            relative_path = path.removeprefix(prefix)
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            files[relative_path] = extracted.read()
    if not files:
        raise ValueError(f"could not fetch remote directory: {ref.render()}")
    return files


def _fetch_github_file(ref: GitHubRef) -> bytes:
    return _fetch_url_bytes(github_raw_url(ref))


def _fetch_url_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "toolang/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except (HTTPError, URLError) as exc:
        raise ValueError(f"could not fetch remote content: {url}") from exc
