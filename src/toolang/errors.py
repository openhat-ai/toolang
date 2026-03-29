class ToolangError(Exception):
    """Raised when Toolang source or runtime execution fails."""


class ExternalDependencyUnavailableError(ToolangError):
    """Raised when one required external dependency is temporarily unavailable."""
