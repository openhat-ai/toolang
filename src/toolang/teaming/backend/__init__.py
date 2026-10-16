"""Storage contracts; concrete implementations are selected by the factory."""

from .protocol import Backend, EventBackend

__all__ = ["Backend", "EventBackend"]
