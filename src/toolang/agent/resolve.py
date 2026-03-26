"""Agent reference resolution.

This module resolves user-facing agent selectors into canonical agent identity,
home placement, and local source paths.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import SplitResult, urlsplit

from toolang.concepts.layout import ToolangRoot
from toolang.errors import ToolangError
from toolang.concepts.identity import (
    AgentRef,
    AgentSelector,
    agent_home_name,
    agent_id,
)


def resolve_agent_ref(
    selector: AgentSelector,
    *,
    cwd: Path,
    toolang_root: Path,
) -> AgentRef:
    """Resolve one agent selector into a canonical local runtime reference."""
    text = selector.strip()
    if not text:
        raise ToolangError("Agent reference may not be empty.")

    if text.startswith("agent:"):
        resident = text.removeprefix("agent:")
        if not resident.strip():
            raise ToolangError("Resident agent selector may not be empty.")
        return replace(
            _resolve_resident_shorthand(resident, toolang_root=toolang_root),
            selector=text,
        )

    if "://" in text:
        return _resolve_uri(text, toolang_root=toolang_root)

    if _looks_like_local_path(text):
        return _resolve_local_path(text, cwd=cwd, toolang_root=toolang_root)

    if _looks_like_hosted_shorthand(text):
        return _resolve_https(f"https://{text}", toolang_root=toolang_root)

    return _resolve_resident_shorthand(text, toolang_root=toolang_root)


def _resolve_uri(text: str, *, toolang_root: Path) -> AgentRef:
    parsed = urlsplit(text)
    if parsed.scheme == "agent":
        return _resolve_agent_uri(parsed, toolang_root=toolang_root)
    if parsed.scheme == "file":
        return _resolve_file_uri(parsed, toolang_root=toolang_root)
    if parsed.scheme in {"http", "https"}:
        return _resolve_https(text, toolang_root=toolang_root)
    raise ToolangError(f"Unsupported agent URI scheme: {parsed.scheme}")


def _resolve_agent_uri(parsed: SplitResult, *, toolang_root: Path) -> AgentRef:
    root = ToolangRoot.resolve(toolang_root)
    home_name = parsed.netloc
    if not home_name:
        raise ToolangError("Resident agent URI must include a home name.")

    path = PurePosixPath(parsed.path)
    filename = path.name
    if path.parent != PurePosixPath("/") or not filename.endswith(".too"):
        raise ToolangError("Resident agent URI must look like agent://<home>/<agent>.too")

    agent_name = filename[:-4]
    home = root.resident_home(home_name)
    agent_uri = f"agent://{home_name}/{filename}"
    return AgentRef(
        selector=agent_uri,
        kind="resident",
        uri=agent_uri,
        id=agent_id(agent_uri),
        root=root.path,
        home=home.path,
        name=agent_name,
        source=home.source(agent_name),
    )


def _resolve_file_uri(parsed: SplitResult, *, toolang_root: Path) -> AgentRef:
    path = Path(parsed.path).expanduser().resolve()
    return _resolve_absolute_local_path(path, toolang_root=toolang_root, raw=parsed.geturl())


def _resolve_https(text: str, *, toolang_root: Path) -> AgentRef:
    root = ToolangRoot.resolve(toolang_root)
    parsed = urlsplit(text)
    if not parsed.netloc:
        raise ToolangError(f"Invalid visiting agent URI: {text}")

    basename = PurePosixPath(parsed.path).name
    filename = _source_filename_from_url_basename(basename)
    agent_name = filename[:-4]
    agent_uri = parsed.geturl()
    home_name = agent_home_name(agent_uri, agent_name=agent_name, kind="visiting")
    home = root.visiting_home(home_name)
    return AgentRef(
        selector=text,
        kind="visiting",
        uri=agent_uri,
        id=agent_id(agent_uri),
        root=root.path,
        home=home.path,
        name=agent_name,
        source=home.source(agent_name),
    )


def _resolve_local_path(text: str, *, cwd: Path, toolang_root: Path) -> AgentRef:
    raw_path = Path(text).expanduser()
    path = raw_path if raw_path.is_absolute() else (cwd / raw_path)
    return _resolve_absolute_local_path(path.resolve(), toolang_root=toolang_root, raw=text)


def _resolve_absolute_local_path(
    path: Path, *, toolang_root: Path, raw: str
) -> AgentRef:
    root = ToolangRoot.resolve(toolang_root)
    if path.suffix != ".too":
        raise ToolangError(f"Local agent path must end with .too: {path}")

    resident_prefix = root.path / "agents"
    try:
        relative = path.relative_to(resident_prefix)
    except ValueError:
        relative = None

    if relative is not None and len(relative.parts) == 2:
        home_name, filename = relative.parts
        if filename.endswith(".too"):
            agent_name = filename[:-4]
            agent_uri = f"agent://{home_name}/{filename}"
            home = root.resident_home(home_name)
            return AgentRef(
                selector=raw,
                kind="resident",
                uri=agent_uri,
                id=agent_id(agent_uri),
                root=root.path,
                home=home.path,
                name=agent_name,
                source=path,
            )

    agent_uri = path.as_uri()
    return AgentRef(
        selector=raw,
        kind="roaming",
        uri=agent_uri,
        id=agent_id(agent_uri),
        root=root.path,
        home=path.parent,
        name=path.stem,
        source=path,
    )


def _resolve_resident_shorthand(text: str, *, toolang_root: Path) -> AgentRef:
    root = ToolangRoot.resolve(toolang_root)
    parts = [part for part in text.split("/") if part]
    if not parts or len(parts) > 2:
        raise ToolangError(f"Unsupported resident shorthand: {text}")

    home_name = parts[0]
    if len(parts) == 1:
        agent_name = home_name
    else:
        agent_name = parts[1][:-4] if parts[1].endswith(".too") else parts[1]

    filename = f"{agent_name}.too"
    agent_uri = f"agent://{home_name}/{filename}"
    home = root.resident_home(home_name)
    return AgentRef(
        selector=text,
        kind="resident",
        uri=agent_uri,
        id=agent_id(agent_uri),
        root=root.path,
        home=home.path,
        name=agent_name,
        source=home.source(agent_name),
    )


def _looks_like_local_path(text: str) -> bool:
    return (
        text.startswith(("./", "../", "/", "~"))
        or text.endswith(".too")
        or text.endswith(".too/")
    )


def _looks_like_hosted_shorthand(text: str) -> bool:
    if "/" not in text:
        return False
    host, _, _ = text.partition("/")
    return "." in host


def _source_filename_from_url_basename(basename: str) -> str:
    clean = basename.strip()
    if not clean:
        return "agent.too"
    if clean.endswith(".too"):
        return clean
    return f"{clean}.too"
