"""Recognize canonical runtime operations without presentation dependencies."""

_RUNTIME_OPERATIONS: dict[str, str] = {
    f"runtime__{name}": name
    for name in ("run", "spawn", "await", "exec", "chdir", "pick", "honor", "compact")
}


def runtime_operation_name(tool_name: str) -> str | None:
    """Return an operation for an exact captured runtime tool identity.

    Callers must also require the runtime plugin; a toolset name alone does not
    identify its implementation.
    """
    return _RUNTIME_OPERATIONS.get(tool_name)
