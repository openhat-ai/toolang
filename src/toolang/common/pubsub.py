"""Isolated availability notifications; payload storage belongs to callers."""

from collections.abc import Callable
import logging

from pubsub.core import Publisher, IListenerExcHandler

_LOGGER = logging.getLogger(__name__)


class _ListenerErrors(IListenerExcHandler):
    def __call__(self, listenerID: str, topicObj: object) -> None:
        _LOGGER.exception("availability listener failed: %s", listenerID)


class Signal:
    """Strongly retain internal wakeups; callers serialize all operations."""

    def __init__(self) -> None:
        self._publisher = Publisher()
        self._listeners: set[Callable[[], None]] = set()
        self._publisher.setListenerExcHandler(_ListenerErrors())

    def connect(self, listener: Callable[[], None]) -> None:
        self._listeners.add(listener)
        self._publisher.subscribe(listener, "available")

    def disconnect(self, listener: Callable[[], None]) -> None:
        if listener in self._listeners:
            self._publisher.unsubscribe(listener, "available")
            self._listeners.remove(listener)

    def notify(self) -> None:
        self._publisher.sendMessage("available")

    def close(self) -> None:
        self._publisher.unsubAll()
        self._listeners.clear()
