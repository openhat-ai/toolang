"""Agent reference resolution.

This module resolves user-facing agent selectors into canonical agent identity,
home placement, and local source paths.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import SplitResult, urlsplit

from toolang.errors import ToolangError
from toolang.layout import agent_source_path, resident_agent_home, visiting_agent_home
from toolang_concepts.identity import AgentKind, AgentRef, AgentSelector, AgentUri

GuestResolver = Callable[[str], str]


def resolve_agent_ref(
    raw: AgentSelector,
    *,
    cwd: Path,
    toolang_root: Path,
    guest_resolver: GuestResolver | None = None,
) -> AgentRef:
    """Resolve one agent selector into a canonical local runtime reference."""
    text = raw.strip()
    if not text:
        raise ToolangError("Agent reference may not be empty.")

    if text.startswith("guest:"):
        if guest_resolver is None:
            raise ToolangError(
                "Guest shorthand requires an explicit guest resolver at the call site."
            )
        return resolve_agent_ref(
            guest_resolver(text.removeprefix("guest:")),
            cwd=cwd,
            toolang_root=toolang_root,
            guest_resolver=guest_resolver,
        )

    if "://" in text:
        return _resolve_uri(text, toolang_root=toolang_root)

    if _looks_like_local_path(text):
        return _resolve_local_path(text, cwd=cwd, toolang_root=toolang_root)

    if _looks_like_hosted_shorthand(text):
        return _resolve_https(f"https://{text}", toolang_root=toolang_root)

    return _resolve_resident_shorthand(text, toolang_root=toolang_root)


def agent_home_name(agent_uri: AgentUri, *, agent_name: str, kind: AgentKind) -> str:
    """Return the local home directory name used for one canonical agent URI."""
    if kind == "resident":
        parsed = urlsplit(agent_uri)
        return parsed.netloc
    if kind == "roaming":
        return agent_name
    return f"{agent_name}-{agent_id(agent_uri)[:12]}"


def agent_id(agent_uri: AgentUri) -> str:
    """Return the stable short-hash basis for one canonical agent URI."""
    return sha256(agent_uri.encode("utf-8")).hexdigest()


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
    home_name = parsed.netloc
    if not home_name:
        raise ToolangError("Resident agent URI must include a home name.")

    path = PurePosixPath(parsed.path)
    filename = path.name
    if path.parent != PurePosixPath("/") or not filename.endswith(".too"):
        raise ToolangError("Resident agent URI must look like agent://<home>/<agent>.too")

    agent_name = filename[:-4]
    home = resident_agent_home(toolang_root, home_name)
    agent_uri = f"agent://{home_name}/{filename}"
    return AgentRef(
        raw=agent_uri,
        agent_kind="resident",
        agent_uri=agent_uri,
        agent_id=agent_id(agent_uri),
        toolang_root=toolang_root,
        agent_home=home,
        agent_name=agent_name,
        source_path=agent_source_path(home, agent_name),
    )


def _resolve_file_uri(parsed: SplitResult, *, toolang_root: Path) -> AgentRef:
    path = Path(parsed.path).expanduser().resolve()
    return _resolve_absolute_local_path(path, toolang_root=toolang_root, raw=parsed.geturl())


def _resolve_https(text: str, *, toolang_root: Path) -> AgentRef:
    parsed = urlsplit(text)
    if not parsed.netloc:
        raise ToolangError(f"Invalid visiting agent URI: {text}")

    basename = PurePosixPath(parsed.path).name
    filename = _source_filename_from_url_basename(basename)
    agent_name = filename[:-4]
    agent_uri = parsed.geturl()
    home_name = agent_home_name(agent_uri, agent_name=agent_name, kind="visiting")
    home = visiting_agent_home(toolang_root, home_name)
    return AgentRef(
        raw=text,
        agent_kind="visiting",
        agent_uri=agent_uri,
        agent_id=agent_id(agent_uri),
        toolang_root=toolang_root,
        agent_home=home,
        agent_name=agent_name,
        source_path=home / filename,
    )


def _resolve_local_path(text: str, *, cwd: Path, toolang_root: Path) -> AgentRef:
    raw_path = Path(text).expanduser()
    path = raw_path if raw_path.is_absolute() else (cwd / raw_path)
    return _resolve_absolute_local_path(path.resolve(), toolang_root=toolang_root, raw=text)


def _resolve_absolute_local_path(
    path: Path, *, toolang_root: Path, raw: str
) -> AgentRef:
    if path.suffix != ".too":
        raise ToolangError(f"Local agent path must end with .too: {path}")

    resident_prefix = toolang_root / "agents"
    try:
        relative = path.relative_to(resident_prefix)
    except ValueError:
        relative = None

    if relative is not None and len(relative.parts) == 2:
        home_name, filename = relative.parts
        if filename.endswith(".too"):
            agent_name = filename[:-4]
            agent_uri = f"agent://{home_name}/{filename}"
            home = resident_agent_home(toolang_root, home_name)
            return AgentRef(
                raw=raw,
                agent_kind="resident",
                agent_uri=agent_uri,
                agent_id=agent_id(agent_uri),
                toolang_root=toolang_root,
                agent_home=home,
                agent_name=agent_name,
                source_path=path,
            )

    agent_uri = path.as_uri()
    return AgentRef(
        raw=raw,
        agent_kind="roaming",
        agent_uri=agent_uri,
        agent_id=agent_id(agent_uri),
        toolang_root=toolang_root,
        agent_home=path.parent,
        agent_name=path.stem,
        source_path=path,
    )


def _resolve_resident_shorthand(text: str, *, toolang_root: Path) -> AgentRef:
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
    home = resident_agent_home(toolang_root, home_name)
    return AgentRef(
        raw=text,
        agent_kind="resident",
        agent_uri=agent_uri,
        agent_id=agent_id(agent_uri),
        toolang_root=toolang_root,
        agent_home=home,
        agent_name=agent_name,
        source_path=agent_source_path(home, agent_name),
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
