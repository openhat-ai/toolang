"""Independent authored-resource validation before layer persistence."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from hashlib import sha256
from tarfile import TarError
from yaml import YAMLError
import re
from typing import Literal

from toolang.catalog.chore import ChoreFile
from toolang.catalog.chores import Chore, file_chore, inline_chores
from toolang.catalog.types import CAP_KIND_BY_DIR
from toolang.lang.ast import AgicDecl, FlowDecl, Program
from toolang.lang.errors import ToolangSourceError
from toolang.common.progress import ProgressSink
from .source import ProgramSource, SourceFile, SourceSnapshot
from .state import (
    StateCap,
    _local_entry_from_file,
    materialize_program_caps,
    layer_remote_cache,
    program_runnable_index,
    runnable_export,
    runnable_module_name,
    _RemoteEntryCache,
)
from .types import StateFile
from .errors import StatePreparationError, StateDiagnostic


@dataclass(frozen=True, slots=True)
class AcceptedProgram:
    name: str
    source: ProgramSource
    program: Program
    caps: tuple[StateCap, ...]
    files: dict[str, bytes]


@dataclass(frozen=True, slots=True)
class AcceptedSources:
    snapshot: SourceSnapshot
    records: tuple[StateFile, ...]
    programs: tuple[AcceptedProgram, ...]
    chores: tuple[Chore, ...]


def accept_sources(
    authored: SourceSnapshot,
    *,
    scope: Literal["root", "home"],
    previous_module_caps: dict[str, tuple[StateCap, ...]] | None = None,
    progress: ProgressSink | None = None,
    reuse_remote: bool = True,
    include_programs: bool = True,
) -> AcceptedSources:
    """Exclude invalid units without changing the captured observations."""
    prefix = Path("agents") / authored.agent_name
    records: dict[str, StateFile] = {}
    units: dict[str, list[SourceFile]] = defaultdict(list)
    excluded: set[str] = set()
    chores: dict[str, Chore] = {}

    def key(path: str) -> str:
        return Path(path).relative_to(prefix).as_posix() if scope == "home" else path

    def unit(path: str) -> str:
        parts = Path(path).parts
        return "/".join(parts[:2]) if parts[0] == "skills" and len(parts) >= 2 else path

    for file in authored.files:
        if file.origin != ("agent" if scope == "home" else "root"):
            continue
        relative = key(file.relative_path)
        records[relative] = StateFile(scope, relative, file.raw_digest)
        units[unit(relative)].append(file)

    def reject(name: str, error: Exception, code: str = "invalid-source") -> None:
        excluded.add(name)
        message = str(error) or type(error).__name__
        message = message.replace(str(authored.toolang_root) + "/", "")
        match = re.search(r"\bline (\d+)\b", message)
        line = (
            int(match.group(1))
            if match
            else getattr(getattr(error, "location", None), "line", None)
        )
        for file in units.get(name, ()):
            relative = key(file.relative_path)
            records[relative] = replace(
                records[relative], accepted=False, code=code, message=message, line=line
            )

    for issue in authored.issues:
        if (scope == "home") != issue.path.startswith("agents/"):
            continue
        relative = key(issue.path)
        records[relative] = StateFile(
            scope, relative, None, False, issue.code, issue.message
        )
        failed_unit = unit(relative)
        reject(failed_unit, ValueError(issue.message), issue.code)
        # A failed directory listing makes every contained unit unavailable.
        for name in units:
            if name.startswith(relative + "/"):
                reject(name, ValueError(issue.message), issue.code)

    for name, files in units.items():
        if name in excluded:
            continue
        try:
            definitions = [file for file in files if file.category == "cap"]
            for file in definitions:
                relative = Path(key(file.relative_path))
                kind = CAP_KIND_BY_DIR.get(relative.parts[0])
                if kind is None:
                    continue
                if kind == "skill" and relative.name != "SKILL.md":
                    continue
                if kind != "skill" and (
                    len(relative.parts) != 2 or relative.suffix != ".md"
                ):
                    continue
                entry = _local_entry_from_file(authored, file)
                if (
                    entry is not None
                    and entry.meta.get("name") is not None
                    and entry.meta["name"] != entry.name
                ):
                    raise ValueError("cap name does not match its authored path")

            for file in files:
                if file.category == "chore":
                    relative = key(file.relative_path)
                    chores[relative] = replace(
                        file_chore(ChoreFile.parse(file.read_text(), path=file.path)),
                        path=Path(relative),
                    )
        except (OSError, ValueError, TypeError, YAMLError) as exc:
            reject(name, exc)

    program_files = [
        file
        for files in units.values()
        for file in files
        if include_programs
        and file.category == "program"
        and unit(key(file.relative_path)) not in excluded
    ]
    external: dict[str, tuple[Program, AgicDecl | FlowDecl]] = {}
    programs: list[AcceptedProgram] = []
    names: dict[str, list[str]] = defaultdict(list)
    for file in program_files:
        relative = key(file.relative_path)
        if relative != "agent.too":
            names[Path(relative).stem.casefold()].append(relative)
    for paths in names.values():
        if len(paths) > 1:
            for path in paths:
                reject(
                    path,
                    ValueError(f"Runnable filenames collide: {', '.join(paths)}"),
                    "invalid-runnable-filename",
                )

    def prepare_program(file: SourceFile) -> AcceptedProgram:
        relative = key(file.relative_path)
        try:
            name = (
                "agent" if relative == "agent.too" else runnable_module_name(relative)
            )
        except ValueError as exc:
            raise StatePreparationError(
                StateDiagnostic(
                    "runnable-extension",
                    "runnable",
                    relative,
                    None,
                    "invalid-runnable-filename",
                    str(exc),
                )
            ) from exc
        source = ProgramSource(
            authored.agent_name,
            "agent" if name == "agent" else "runnable",
            relative,
            file.relative_path,
            file.read_text(),
            file.digest,
        )
        program = (
            source.parse(external_runnables=external)
            if name == "agent" and external
            else source.parse()
        )
        program_runnable_index(program)
        if name != "agent":
            try:
                runnable_export(relative, program)
            except ValueError as exc:
                raise StatePreparationError(
                    StateDiagnostic(
                        "runnable-extension",
                        "runnable",
                        relative,
                        None,
                        "invalid-runnable-export",
                        str(exc),
                    )
                ) from exc
        cache: _RemoteEntryCache = (
            layer_remote_cache(
                authored,
                scope="home",
                entries=(previous_module_caps or {}).get(name, ()),
            )
            if reuse_remote
            else {}
        )
        caps, generated = materialize_program_caps(
            authored,
            source,
            program=program,
            remote_cache=cache or None,
            progress=progress,
        )
        return AcceptedProgram(name, source, program, caps, generated)

    for file in sorted(
        program_files, key=lambda file: key(file.relative_path) == "agent.too"
    ):
        relative = key(file.relative_path)
        if unit(relative) in excluded:
            continue
        try:
            prepared = prepare_program(file)
            programs.append(prepared)
            if prepared.name != "agent":
                public, local = runnable_export(relative, prepared.program)
                external[public] = (
                    prepared.program,
                    program_runnable_index(prepared.program)[local],
                )
        except (ToolangSourceError, ValueError, OSError, YAMLError, TarError) as exc:
            reject(
                relative,
                exc,
                exc.diagnostics[0].code
                if isinstance(exc, StatePreparationError)
                else "invalid-program",
            )

    owners: dict[str, list[AcceptedProgram]] = defaultdict(list)
    for prepared in programs:
        exports = (
            program_runnable_index(prepared.program)
            if prepared.name == "agent"
            else {
                runnable_export(prepared.source.authored_path, prepared.program)[
                    0
                ]: None
            }
        )
        for public in exports:
            owners[public].append(prepared)
    for public, conflicting in owners.items():
        if len(conflicting) > 1:
            paths = sorted(
                f"{item.source.authored_path}:{next(value.span.line for value in program_runnable_index(item.program).values() if value.name in {public, None})}"
                for item in conflicting
            )
            for prepared in conflicting:
                reject(
                    prepared.source.authored_path,
                    ValueError(
                        f"Public runnable name is not unique: {public} ({', '.join(paths)})"
                    ),
                    "public-runnable-conflict",
                )
    chore_owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path, chore in chores.items():
        if path not in excluded:
            chore_owners[chore.id].append((path, chore.name))
    for prepared in programs:
        if prepared.name == "agent" and prepared.source.authored_path not in excluded:
            for chore in inline_chores(
                prepared.program.chores, path=Path(prepared.source.authored_path)
            ):
                chore_owners[chore.id].append(
                    (prepared.source.authored_path, chore.name)
                )
    for definitions in chore_owners.values():
        if len({name for _, name in definitions}) > 1:
            paths = sorted({path for path, _ in definitions})
            for path in paths:
                reject(
                    path,
                    ValueError(f"Chore identity collision: {', '.join(paths)}"),
                    "chore-identity-conflict",
                )
    programs = [item for item in programs if item.source.authored_path not in excluded]
    if (
        include_programs
        and scope == "home"
        and not any(item.name == "agent" for item in programs)
    ):
        source = ProgramSource(
            authored.agent_name,
            "agent",
            "agent.too",
            f"agents/{authored.agent_name}/agent.too",
            "",
            sha256(b"").hexdigest(),
        )
        programs.append(
            AcceptedProgram("agent", source, Program.from_source(""), (), {})
        )
    snapshot = replace(
        authored,
        files=tuple(
            file
            for file in authored.files
            if file.origin != ("agent" if scope == "home" else "root")
            or unit(key(file.relative_path)) not in excluded
        ),
    )
    return AcceptedSources(
        snapshot,
        tuple(records[path] for path in sorted(records)),
        tuple(programs),
        tuple(chores[path] for path in sorted(chores) if path not in excluded),
    )
