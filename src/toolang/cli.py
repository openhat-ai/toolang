from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Annotated, Literal, Sequence

import click
import typer
from dotenv import load_dotenv

from toolang.bus.app import serve_bus_app
from toolang.bus.db import BusStore
from toolang.bus.events import AgentUpdated, utc_now
from toolang.agent_refs import ResolvedAgentRef, resolve_agent_ref
from toolang.agent_registry import (
    KnownAgentRecord,
    KnownAgentSnapshot,
    delete_running_agent,
    find_known_agents_by_id_prefix,
    find_known_agents_by_name,
    get_running_agent,
    list_known_agents,
    upsert_known_agent,
)
from toolang.errors import ToolangError
from toolang.files._toml import load_toml
from toolang.files.agent_run import AgentRunState
from toolang.invoke import invoke_prepared_agent
from toolang.layout import (
    agent_log_path,
    agent_room,
    agent_run_path,
    agent_source_path,
    agents_db_path,
    bus_events_db_path,
    ensure_toolang_root_layout,
    resolve_toolang_root,
)
from toolang.prepared import prepare_agent
from toolang.server import serve_agent
from toolang.sync import sync_agent


def _version_callback(value: bool | None) -> None:
    if value:
        typer.echo(f"toolang {_toolang_version()}")
        raise typer.Exit()


app = typer.Typer(
    help="Toolang CLI",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
bus_app = typer.Typer(
    help="Bus commands",
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
app.add_typer(bus_app, name="bus")


@app.callback()
def callback(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = None,
) -> None:
    """Toolang CLI."""


@app.command(hidden=True)
def home(
    agent: Annotated[str | None, typer.Argument(help="Agent selector")] = None,
) -> None:
    toolang_root = _toolang_root()
    if agent is None:
        typer.echo(str(toolang_root))
        return
    db_path = agents_db_path(toolang_root)
    resolved = _resolve_cli_agent(agent, db_path=db_path)
    typer.echo(str(resolved.agent_home))


@app.command(hidden=True)
def source(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    db_path = agents_db_path(_toolang_root())
    resolved = _resolve_cli_agent(agent, db_path=db_path)
    typer.echo(str(agent_source_path(resolved.agent_home, resolved.agent_name)))


@app.command(hidden=True)
def room(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    db_path = agents_db_path(_toolang_root())
    resolved = _resolve_cli_agent(agent, db_path=db_path)
    typer.echo(str(agent_room(resolved.agent_home, resolved.agent_name)))


@app.command(hidden=True)
def init(
    shell: Annotated[
        Literal["zsh", "bash", "fish"],
        typer.Argument(help="Shell to initialize"),
    ],
) -> None:
    typer.echo(_init_install_note(shell), err=True)
    if shell == "fish":
        typer.echo(_fish_init_script())
        return
    typer.echo(_posix_init_script())


@app.command("list")
def list_agents() -> None:
    db_path = agents_db_path(_toolang_root())
    snapshots = _fresh_known_agents(db_path)
    if not snapshots:
        typer.echo("No agents found.")
        return

    rows = [
        (
            snapshot.agent_id,
            snapshot.running_status or "stopped",
            snapshot.agent_name,
            snapshot.agent_uri,
            snapshot.endpoint or "-",
        )
        for snapshot in snapshots
    ]
    typer.echo(_format_rows(("ID", "STATUS", "NAME", "URI", "ENDPOINT"), rows))


@app.command()
def invoke(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
    thunk: Annotated[str | None, typer.Option(help="Thunk name to invoke")] = None,
    user_input: Annotated[
        str | None,
        typer.Option("--input", help="User input for a thunk(user) entrypoint"),
    ] = None,
    model: Annotated[str | None, typer.Option(help="Override model selection")] = None,
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    bus_db_path = bus_events_db_path(toolang_root)
    prepared = prepare_agent(_resolve_cli_agent(agent, db_path=db_path))
    _remember_agent(prepared.ref, db_path=db_path)
    selected_thunk = prepared.program.get_thunk(thunk)

    if selected_thunk.input_name and user_input is None and not sys.stdin.isatty():
        user_input = sys.stdin.read()

    result = invoke_prepared_agent(
        prepared,
        selected_thunk,
        bus_db_path=bus_db_path,
        user_input=user_input,
        model=model,
    )
    typer.echo(result.output)


@app.command()
def sync(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    bus_db_path = bus_events_db_path(toolang_root)
    agent_ref = _resolve_cli_agent(agent, db_path=db_path)
    sync_agent(agent_ref)
    _remember_agent(agent_ref, db_path=db_path)
    bus = BusStore(bus_db_path)
    bus.append(
        AgentUpdated(
            at=utc_now(),
            agent_uri=agent_ref.agent_uri,
            agent_id=agent_ref.agent_id[:12],
            name=agent_ref.agent_name,
            update_kind="sync",
            detail="sync completed",
            agent_home=str(agent_ref.agent_home),
            source_file=agent_ref.source_path.name,
        )
    )
    bus.close()
    typer.echo("synced")


@app.command()
def serve(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
    host: Annotated[str, typer.Option(help="Host interface to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8765,
) -> None:
    toolang_root = _toolang_root()
    db_path = agents_db_path(toolang_root)
    bus_db_path = bus_events_db_path(toolang_root)
    prepared = prepare_agent(_resolve_cli_agent(agent, db_path=db_path))
    _remember_agent(prepared.ref, db_path=db_path)
    serve_agent(
        prepared,
        agents_db_path=db_path,
        bus_db_path=bus_db_path,
        host=host,
        port=port,
        cors_allow_origins=_cors_allow_origins(),
    )


@app.command()
def start(
    agent: Annotated[str, typer.Argument(help="Agent selector")],
    host: Annotated[str, typer.Option(help="Host interface to bind")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Port to bind; chooses a free port by default")] = None,
) -> None:
    db_path = agents_db_path(_toolang_root())
    prepared = prepare_agent(_resolve_cli_agent(agent, db_path=db_path))
    _remember_agent(prepared.ref, db_path=db_path)
    _drop_stale_running_agent(db_path, prepared.ref)

    active = get_running_agent(db_path, prepared.ref.agent_uri)
    if active is not None:
        raise ToolangError(f"Agent is already being served: {prepared.ref.agent_uri}")

    selected_port = port if port is not None else _pick_free_port(host)
    endpoint = f"http://{host}:{selected_port}"
    log_path = agent_log_path(prepared.ref.agent_home, prepared.ref.agent_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-c",
        "from toolang.cli import main; raise SystemExit(main())",
        "serve",
        prepared.ref.agent_uri,
        "--host",
        host,
        "--port",
        str(selected_port),
    ]
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(prepared.ref.agent_home),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    _wait_for_running_agent(
        db_path=db_path,
        agent=prepared.ref,
        process=process,
        endpoint=endpoint,
        log_path=log_path,
    )
    typer.echo(f"started {prepared.ref.agent_id[:12]} {endpoint}")


@bus_app.command("serve")
def bus_serve(
    host: Annotated[str, typer.Option(help="Host interface to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8780,
) -> None:
    toolang_root = _toolang_root()
    serve_bus_app(
        bus_events_db_path(toolang_root),
        host=host,
        port=port,
        cors_allow_origins=_cors_allow_origins(),
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    try:
        app(
            args=list(argv) if argv is not None else None,
            prog_name="toolang",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except (FileNotFoundError, ToolangError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    return 0


def _resolve_cli_agent(raw: str, *, db_path: Path | None = None) -> ResolvedAgentRef:
    toolang_root = _toolang_root()
    guest_resolver = _guest_resolver()
    text = raw.strip()
    resolved_db_path = db_path if db_path is not None else agents_db_path(toolang_root)

    if _looks_like_explicit_source_selector(text):
        return resolve_agent_ref(
            text,
            cwd=Path.cwd(),
            toolang_root=toolang_root,
            guest_resolver=guest_resolver,
        )

    resolved_from_registry = _resolve_known_agent(
        text,
        db_path=resolved_db_path,
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


def _toolang_root() -> Path:
    root = resolve_toolang_root(os.environ.get("TOOLANG_ROOT", "~/.toolang"))
    return ensure_toolang_root_layout(root)


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
) -> ResolvedAgentRef | None:
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


def _remember_agent(agent: ResolvedAgentRef, *, db_path: Path) -> None:
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
        if snapshot.pid is not None and not _pid_exists(snapshot.pid)
    ]
    if not stale_snapshots:
        return snapshots

    for snapshot in stale_snapshots:
        delete_running_agent(db_path, snapshot.agent_uri)
        run_path = agent_run_path(Path(snapshot.agent_home), snapshot.agent_name)
        if run_path.exists():
            now = datetime.now(timezone.utc)
            run_state = AgentRunState.load(run_path)
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
        f"Add the emitted block to {shell_file}.\n"
        "Remove everything between the toolang markers to uninstall."
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


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _drop_stale_running_agent(db_path: Path, agent: ResolvedAgentRef) -> None:
    existing = get_running_agent(db_path, agent.agent_uri)
    if existing is None:
        return
    if _pid_exists(existing.pid):
        return
    delete_running_agent(db_path, agent.agent_uri)
    run_path = agent_run_path(agent.agent_home, agent.agent_name)
    if run_path.exists():
        now = datetime.now(timezone.utc)
        run_state = AgentRunState.load(run_path)
        run_state.model_copy(update={"status": "stopped", "heartbeat_at": now}).save(run_path)


def _wait_for_running_agent(
    *,
    db_path: Path,
    agent: ResolvedAgentRef,
    process: subprocess.Popen,
    endpoint: str,
    log_path: Path,
) -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ToolangError(
                f"Agent server exited before startup completed. See log: {log_path}"
            )
        active = get_running_agent(db_path, agent.agent_uri)
        if active is not None:
            return
        time.sleep(0.1)
    raise ToolangError(f"Timed out waiting for agent server startup at {endpoint}.")


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
