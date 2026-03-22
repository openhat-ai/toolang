from __future__ import annotations

import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Literal, Sequence

import httpx

from toolang.agent.refs import resolve_agent_ref
from toolang.agent.registry import (
    KnownAgentRecord,
    KnownAgentSnapshot,
    delete_running_agent,
    find_known_agents_by_id_prefix,
    find_known_agents_by_name,
    list_known_agents,
    upsert_known_agent,
)
from toolang.bus.db import BusStore
from toolang.bus.events import AgentUpdated, utc_now
from toolang.caps import CapScopeSelection
from toolang.errors import ToolangError
from toolang.layout import (
    agent_run_path,
    agents_db_path,
    bus_events_db_path,
    ensure_toolang_root_layout,
    resolve_toolang_root,
)
from toolang_concepts.identity import AgentRef
from toolang_concepts.persisted._toml import load_toml
from toolang_concepts.persisted.activation_state import ActivationState
from toolang_concepts.sandbox import HOST_SANDBOX
from toolang.sandbox import sandbox_process_alive


def _toolang_root() -> Path:
    root = resolve_toolang_root(os.environ.get("TOOLANG_ROOT", "~/.toolang"))
    return ensure_toolang_root_layout(root)


def _resolve_cli_agent(raw: str, *, db_path: Path | None = None) -> AgentRef:
    text = raw.strip()
    if not text:
        raise ToolangError("Agent selector may not be empty.")

    toolang_root = _toolang_root()
    registry_path = db_path or agents_db_path(toolang_root)
    guest_resolver = _guest_resolver()

    if not _looks_like_explicit_source_selector(text):
        resolved_from_registry = _resolve_known_agent(
            text,
            db_path=registry_path,
            toolang_root=toolang_root,
            guest_resolver=guest_resolver,
        )
        if resolved_from_registry is not None:
            return resolved_from_registry

    return resolve_agent_ref(
        text,
        cwd=Path.cwd(),
        toolang_root=toolang_root,
        guest_resolver=guest_resolver,
    )


def _resolve_runtime_cap_scopes(
    agent: AgentRef,
    *,
    shared_caps: bool | None,
    global_caps: bool | None,
) -> CapScopeSelection:
    defaults = _default_runtime_cap_scopes(agent)
    return CapScopeSelection(
        include_shared=defaults.include_shared if shared_caps is None else shared_caps,
        include_global=defaults.include_global if global_caps is None else global_caps,
    )


def _default_runtime_cap_scopes(agent: AgentRef) -> CapScopeSelection:
    if agent.kind == "resident":
        return CapScopeSelection(include_shared=True, include_global=True)
    if agent.kind == "roaming":
        return CapScopeSelection(include_shared=True, include_global=False)
    return CapScopeSelection(include_shared=False, include_global=False)


def _resolve_resident_target(raw: str) -> AgentRef:
    toolang_root = _toolang_root()
    agent_ref = resolve_agent_ref(
        raw,
        cwd=Path.cwd(),
        toolang_root=toolang_root,
        guest_resolver=_guest_resolver(),
    )
    if agent_ref.kind != "resident":
        raise ToolangError(
            "Resident agent targets must use resident shorthand or an agent:// URI."
        )
    return agent_ref


def _load_clone_source_text(agent: AgentRef) -> str:
    if agent.source.exists():
        return agent.source.read_text(encoding="utf-8")

    if agent.kind == "visiting":
        response = httpx.get(agent.uri, follow_redirects=True, timeout=10.0)
        response.raise_for_status()
        return response.text

    raise ToolangError(f"Agent source file not found: {agent.source}")


def _append_agent_updated(
    toolang_root: Path,
    agent: AgentRef,
    *,
    update_kind: str,
    detail: str,
) -> None:
    bus = BusStore(bus_events_db_path(toolang_root))
    bus.append(
        AgentUpdated(
            at=utc_now(),
            agent_uri=agent.uri,
            agent_id=agent.id[:12],
            name=agent.name,
            update_kind=update_kind,
            detail=detail,
            agent_home=str(agent.home),
            source_file=agent.source.name,
        )
    )
    bus.close()


def _guest_resolver():
    guest_base_url = os.environ.get("TOOLANG_GUEST_BASE_URL", "").strip()
    guest_resolver = None
    if guest_base_url:
        base = guest_base_url.rstrip("/")

        def resolve_guest_name(name: str) -> str:
            return f"{base}/{name.lstrip('/')}"

        guest_resolver = resolve_guest_name
    return guest_resolver


def _cors_allow_origins() -> list[str] | None:
    raw = os.environ.get("TOOLANG_CORS_ORIGINS", "").strip()
    if not raw:
        return None
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or None


def _resolve_known_agent(
    raw: str,
    *,
    db_path: Path,
    toolang_root: Path,
    guest_resolver,
) -> AgentRef | None:
    if _looks_like_agent_id(raw):
        by_id = _select_known_agent(find_known_agents_by_id_prefix(db_path, raw), raw, "agent id")
        if by_id is not None:
            return resolve_agent_ref(
                by_id.agent_uri,
                cwd=Path.cwd(),
                toolang_root=toolang_root,
                guest_resolver=guest_resolver,
            )

    by_name = _select_known_agent(find_known_agents_by_name(db_path, raw), raw, "agent name")
    if by_name is not None:
        return resolve_agent_ref(
            by_name.agent_uri,
            cwd=Path.cwd(),
            toolang_root=toolang_root,
            guest_resolver=guest_resolver,
        )

    if not _looks_like_agent_id(raw):
        by_id = _select_known_agent(find_known_agents_by_id_prefix(db_path, raw), raw, "agent id")
        if by_id is not None:
            return resolve_agent_ref(
                by_id.agent_uri,
                cwd=Path.cwd(),
                toolang_root=toolang_root,
                guest_resolver=guest_resolver,
            )
    return None


def _select_known_agent(
    records: list[KnownAgentRecord],
    raw: str,
    label: str,
) -> KnownAgentRecord | None:
    if not records:
        return None
    if len(records) > 1:
        matches = ", ".join(record.agent_uri for record in records)
        raise ToolangError(f"Ambiguous {label} {raw!r}: {matches}")
    return records[0]


def _remember_agent(agent: AgentRef, *, db_path: Path) -> None:
    upsert_known_agent(
        db_path,
        KnownAgentRecord.from_resolved_agent(
            agent,
            updated_at=datetime.now(timezone.utc),
        ),
    )


def _fresh_known_agents(db_path: Path) -> list[KnownAgentSnapshot]:
    snapshots = list_known_agents(db_path)
    stale_snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.running_status is not None
        and not sandbox_process_alive(
            sandbox_spec=snapshot.sandbox or HOST_SANDBOX,
            pid=snapshot.pid,
            agent_name=snapshot.agent_name,
            agent_id=snapshot.agent_id,
        )
    ]
    if not stale_snapshots:
        return snapshots

    for snapshot in stale_snapshots:
        delete_running_agent(db_path, snapshot.agent_uri)
        run_path = agent_run_path(Path(snapshot.agent_home), snapshot.agent_name)
        if run_path.exists():
            now = datetime.now(timezone.utc)
            run_state = ActivationState.load(run_path)
            run_state.model_copy(update={"status": "stopped", "heartbeat_at": now}).save(
                run_path
            )
    return list_known_agents(db_path)


def _format_rows(headers: tuple[str, ...], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * widths[index] for index in range(len(headers))),
    ]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


def _init_install_note(shell: Literal["zsh", "bash", "fish"]) -> str:
    shell_file = {
        "zsh": "~/.zshrc",
        "bash": "~/.bashrc",
        "fish": "~/.config/fish/config.fish",
    }[shell]
    return (
        f"# Add the emitted block to {shell_file}.\n"
        "# Remove everything between the toolang markers to uninstall.\n"
        "#\n"
        "# Append it with:\n"
        f"#   toolang init {shell} >> {shell_file}\n"
    )


def _posix_init_script() -> str:
    return """# >>> toolang shell helpers >>>
toohome() {
  builtin cd -- "$(command toolang home "$@")"
}

tooroom() {
  builtin cd -- "$(command toolang room "$@")"
}
# <<< toolang shell helpers <<<"""


def _fish_init_script() -> str:
    return """# >>> toolang shell helpers >>>
function toohome
    cd (command toolang home $argv)
end

function tooroom
    cd (command toolang room $argv)
end
# <<< toolang shell helpers <<<"""


def _looks_like_explicit_source_selector(text: str) -> bool:
    return (
        "://" in text
        or text.startswith("guest:")
        or text.startswith(("./", "../", "/", "~"))
        or text.endswith(".too")
        or "/" in text
    )


def _looks_like_agent_id(text: str) -> bool:
    return len(text) >= 7 and all(character in "0123456789abcdef" for character in text.lower())


def _toolang_version() -> str:
    try:
        return package_version("toolang")
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        project = load_toml(pyproject_path).get("project", {})
        version = project.get("version")
        if isinstance(version, str) and version:
            return version
        raise ToolangError(f"Could not determine package version from {pyproject_path}.")
