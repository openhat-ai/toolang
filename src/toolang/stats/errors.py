"""Statistics availability errors, independent of execution health."""


class StatsUnavailable(RuntimeError):
    """The derived store cannot currently serve statistics."""
