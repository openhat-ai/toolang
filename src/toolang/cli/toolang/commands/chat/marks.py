"""Keep the tmux marks in sync with one chat session.

``Marks`` (``cli.common.tmux``) knows how to write each option at its scope,
``ChatMarks`` owns when to write them. The distinction matters because the values
become available at different times:

- the pad kind is known as soon as chat starts;
- the thread id exists up front only when chat resumes a thread, otherwise it is
  created on the first submission;
- the thread title is derived from the run that created the thread, so it can
  be read back once that run is accepted, long before it finishes.

The agent mark is the launcher's, written on the session when it determines
where chat runs; chat never writes it.

Every step is idempotent and best-effort: lookups never raise and mark failures
never reach the user.
"""

from __future__ import annotations

from collections.abc import Callable

from toolang.cli.common.tmux import (
    MARK_PAD,
    MARK_THREAD_ID,
    MARK_THREAD_TITLE,
    PAD_CHAT,
    Marks,
    clip_title,
)

TitleLookup = Callable[[str], str | None]


class ChatMarks:
    """Publish pad kind, thread id, and thread title for one chat session."""

    def __init__(
        self,
        *,
        marks: Marks | None,
        title_lookup: TitleLookup,
    ) -> None:
        self.marks = marks
        self._title_lookup = title_lookup
        self._thread_id: str | None = None
        self._title_published = False

    @classmethod
    def disabled(cls) -> ChatMarks:
        """Marks for a chat that is not running inside tmux."""

        return cls(marks=None, title_lookup=lambda _thread_id: None)

    @property
    def active(self) -> bool:
        return self.marks is not None

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    def start(self, thread_id: str | None) -> None:
        """Publish what is already known when the session starts."""

        if self.marks is None:
            return
        self.marks.set(MARK_PAD, PAD_CHAT)
        if thread_id:
            self.set_thread(thread_id)
            # a resumed thread usually has runs, so its title exists already
            self.refresh_title()

    def set_thread(self, thread_id: str) -> None:
        """Publish a thread id, which may arrive long after the start.

        A thread created by this session has no title until its first run
        exists, so this only records the id; ``refresh_title`` (called when a
        run is accepted and again when it ends) publishes the title.
        """

        if self.marks is None or thread_id == self._thread_id:
            return
        self._thread_id = thread_id
        self._title_published = False
        self.marks.set(MARK_THREAD_ID, thread_id)
        self.marks.name_window(thread_id)

    def refresh_title(self) -> None:
        """Publish the thread title once it exists.

        Callers may call this whenever a run is accepted and again when it
        ends: the title's value is durable from acceptance on, and it is
        published once per thread.
        """

        if self.marks is None or self._thread_id is None or self._title_published:
            return
        title = self._lookup_title(self._thread_id)
        if not title:
            return
        self.marks.set(MARK_THREAD_TITLE, clip_title(title))
        self._title_published = True

    def clear(self) -> None:
        """Remove the pad mark this session published.

        The thread marks and the window name belong to the thread container and
        stay; only the pane's role ends with chat.
        """

        if self.marks is None:
            return
        self.marks.clear(MARK_PAD)
        self._thread_id = None
        self._title_published = True

    def _lookup_title(self, thread_id: str) -> str | None:
        try:
            title = self._title_lookup(thread_id)
        except Exception:
            return None
        return title if isinstance(title, str) and title else None
