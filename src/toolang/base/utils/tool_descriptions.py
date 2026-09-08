"""Plain-text building blocks for tool-owned lifecycle descriptions."""

from ..types.tool import ToolStatus


def describe_action(
    status: ToolStatus, verbs: tuple[str, str, str], target: str
) -> str:
    """Describe an action without choosing its terminal presentation."""

    verb, running, succeeded = verbs
    action = {
        "running": running,
        "succeeded": succeeded,
        "failed": f"Failed to {verb}",
        "canceled": f"Canceled {running.lower()}",
    }[status]
    return f"{action} {target}" + ("..." if status == "running" else "")


def workspace_label(name: str, path: str) -> str:
    """Display a logical workspace path, without resolving its host location."""

    relative = path.lstrip("/")
    return f"[{name}] {relative if relative not in {'', '.'} else '/'}"
