"""Safe, bounded OSC 0 publication, independent of terminal identity."""

from collections.abc import Callable

from wcwidth import iter_graphemes, strip_sequences, wcswidth

TITLE_WIDTH = 128


def safe_text(text: str) -> str:
    """Remove terminal instructions and non-line display controls."""
    return "".join(
        char
        for char in strip_sequences(text)
        if ord(char) >= 32
        and not 127 <= ord(char) <= 159
        and not 0xD800 <= ord(char) <= 0xDFFF
        and char not in "\u2028\u2029"
    )


def label_text(text: str) -> str:
    return " ".join(safe_text(" ".join(strip_sequences(text).split())).split())


def clip_title(text: str, *, width: int = TITLE_WIDTH) -> str:
    """Clip already sanitized text without splitting graphemes or trimming spaces."""
    if width <= 0:
        return ""
    if wcswidth(text) <= width:
        return text
    parts: list[str] = []
    used = 0
    for cluster in iter_graphemes(text):
        size = max(0, wcswidth(cluster))
        if used + size > width - 1:
            break
        parts.append(cluster)
        used += size
    return "".join(parts) + "…"


def agent_label(name: str) -> str:
    return label_text(name) or "agent"


class TerminalTitle:
    """Deduplicate successful payloads and clear once after a successful write."""

    def __init__(
        self,
        *,
        prefix: str,
        write: Callable[[str], object],
        flush: Callable[[], object],
        enabled: bool,
    ) -> None:
        self.enabled = enabled
        self._prefix = safe_text(prefix)
        self._write = write
        self._flush = flush
        self._written: str | None = None
        self._closed = False

    def set(self, label: str) -> bool:
        if not self.enabled or self._closed:
            return False
        payload = clip_title(self._prefix + label_text(label))
        if payload == self._written:
            return True
        if not self._emit(payload):
            return False
        self._written = payload
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._written is not None:
            self._emit("")

    def _emit(self, payload: str) -> bool:
        try:
            self._write(f"\x1b]0;{payload}\x07")
            self._flush()
        except (OSError, ValueError):
            return False
        return True
