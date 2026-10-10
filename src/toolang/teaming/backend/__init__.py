"""Storage contracts; concrete implementations are selected by the factory."""

from .protocol import ActivityBackend, Backend, EventBackend

__all__ = ["ActivityBackend", "Backend", "EventBackend"]
