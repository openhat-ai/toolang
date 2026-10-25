"""One-shot runtime listings for the selected Toolang root."""

from json import JSONDecodeError
from pathlib import Path

import httpx
from pydantic import ValidationError
from rich import box
from rich.console import Console
from rich.text import Text
import typer

from toolang.cli.common.context import context_root
from toolang.cli.common.output import echo_table, runtime_status_label
from toolang.common.errors import ToolangError
from toolang.setup.teaming import load_teaming_root
from toolang.teaming.schemas import HubInfo
from toolang.up.config import format_drivers
from toolang.up.observation import RuntimeInventory
from toolang.up.types import RuntimeInstanceSnapshot
from toolang.up.watcher import RuntimeWatcher


def ps(ctx: typer.Context) -> None:
    """Show agents and hub status"""
    _show(context_root(ctx), residents_only=False)


def ls(ctx: typer.Context) -> None:
    """List agents and their status"""
    _show(context_root(ctx), residents_only=True)


def _show(root: Path, *, residents_only: bool) -> None:
    try:
        inventory = RuntimeWatcher(root, residents_only=residents_only).refresh()
    except (OSError, RuntimeError, ValueError, ToolangError) as exc:
        inventory = RuntimeInventory(
            RuntimeInstanceSnapshot((), (f"agent discovery: {exc}",), False), ()
        )
    render_snapshot(
        inventory,
        hub_line=None if residents_only else _hub_line(root),
        console=Console(highlight=False),
    )


def _hub_line(root: Path) -> str:
    try:
        endpoint = load_teaming_root(root).hub
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(endpoint + "/info")
            response.raise_for_status()
            info = HubInfo.model_validate(response.json())
            health = client.get(
                endpoint + "/healthz", headers={"X-Toolang-Backend": info.identity}
            )
            if health.status_code != 200:
                return "hub not running"
            ready = health.json()
            if (
                not isinstance(ready, dict)
                or ready.keys() != {"ok"}
                or not isinstance(ready["ok"], bool)
            ):
                raise ValueError("invalid hub readiness response")
            if not ready["ok"]:
                return "hub not running"
        return f"hub running, {info.version.strip() or 'unknown'}, {endpoint}"
    except httpx.HTTPError:
        return "hub not running"
    except (JSONDecodeError, ValidationError):
        return "hub unknown: invalid hub response"
    except (OSError, RuntimeError, ValueError, ToolangError) as exc:
        return f"hub unknown: {' '.join(str(exc).split())}"


def _safe_text(console: Console, value: str) -> str:
    encoding = getattr(console.file, "encoding", None)
    if encoding:
        return value.encode(encoding, "backslashreplace").decode(encoding)
    return value


def render_snapshot(
    inventory: RuntimeInventory, *, hub_line: str | None, console: Console
) -> None:
    """Render the same rows for ps and ls; only ps adds hub and placement details."""
    snapshot = inventory.snapshot
    instances = snapshot.instances
    states = [runtime_status_label(item.state) for item in instances]
    ports = {
        source.instance.identity: source.observation.port
        for source in inventory.sources
        if source.observation is not None
    }
    rows = []
    for item, state in zip(instances, states, strict=True):
        address = item.label.partition("#")[0]
        drivers = (
            format_drivers(frozenset(item.drivers))
            if state == "up" and item.drivers is not None
            else "-"
        )
        port = ports.get(item.identity)
        rows.append(
            (
                _safe_text(console, address),
                state,
                drivers,
                str(port) if port is not None else "-",
            )
        )
    rows.sort(key=lambda row: ("up", "down", "unknown").index(row[1]))
    if rows:
        echo_table(
            ("AGENT", "STATUS", "DRIVERS", "PORT"),
            rows,
            justify=("left", "left", "left", "right"),
            max_widths=(max(20, console.width),) * 4,
            console=console,
            table_box=box.SIMPLE,
            show_footer=True,
        )
    running = (
        str(states.count("up"))
        if snapshot.membership_complete and "unknown" not in states
        else "-"
    )
    summary = f"{len(instances)} {'agent' if len(instances) == 1 else 'agents'}"
    if instances or snapshot.partial:
        summary += f", {running} running"
    placements = (
        ("resident", "visiting", "roaming") if hub_line is not None else ("resident",)
    )
    for placement in placements:
        count = sum(item.placement == placement for item in instances)
        if count:
            summary += f", {count} {placement}"
    if snapshot.partial or "unknown" in states:
        summary += ", partial"
    console.print(Text(summary), soft_wrap=True)
    if hub_line is not None:
        console.print(Text(_safe_text(console, hub_line)), soft_wrap=True)
    for diagnostic in (
        *snapshot.diagnostics,
        *(
            f"{item.label.partition('#')[0]}: {item.diagnostic}"
            for item in instances
            if item.diagnostic
        ),
    ):
        console.print(
            Text(_safe_text(console, f"partial: {diagnostic}")), soft_wrap=True
        )
