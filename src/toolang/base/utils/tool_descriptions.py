"""Plain-text building blocks for tool-owned lifecycle descriptions."""

from ..types.tool import ToolResult


def action_summary(
    result: ToolResult | None, verbs: tuple[str, str, str], target: str
) -> str:
    """Describe an action without choosing its terminal presentation."""

    verb, running, succeeded = verbs
    action = (
        running
        if result is None
        else f"Failed to {verb}"
        if result.error is not None
        else succeeded
    )
    return f"{action} {target}" + ("..." if result is None else "")


def workspace_label(name: str, path: str) -> str:
    """Display a logical workspace path, without resolving its host location."""

    relative = path.lstrip("/")
    return f"{name}:/" + (relative if relative != "." else "")
