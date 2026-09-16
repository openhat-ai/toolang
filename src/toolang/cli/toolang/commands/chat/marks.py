"""Keep the tmux marks in sync with one chat session.

``Marks`` (``cli.common.tmux``) knows how to write the pane and window options,
``ChatMarks`` owns when to write them. The distinction matters because the two
values become available at different times:

- the agent is known before the first prompt;
- the thread id exists up front only when chat resumes a thread, otherwise it is
  created on the first submission;
- the thread title is derived from the run that created the thread, so it can
  only be read back once that run is finished.

Every step is idempotent and best-effort: lookups never raise and mark failures
never reach the user.
"""

from __future__ import annotations

from collections.abc import Callable

from toolang.cli.common.tmux import (
    MARK_AGENT,
    MARK_NAMES,
    MARK_THREAD_ID,
    MARK_THREAD_TITLE,
    Marks,
    clip_title,
)

TitleLookup = Callable[[str], str | None]


class ChatMarks:
    """Publish agent, thread id, and thread title for one chat session."""

    def __init__(
        self,
        *,
        agent: str,
        marks: Marks | None,
        title_lookup: TitleLookup,
    ) -> None:
        self.agent = agent
        self.marks = marks
        self._title_lookup = title_lookup
        self._thread_id: str | None = None
        self._title_published = False

    @classmethod
    def disabled(cls) -> ChatMarks:
        """Marks for a chat that is not running inside tmux."""

        return cls(agent="", marks=None, title_lookup=lambda _thread_id: None)

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
        self.marks.set(MARK_AGENT, self.agent)
        if thread_id:
            self.set_thread(thread_id)
            # a resumed thread usually has runs, so its title exists already
            self.refresh_title()

    def set_thread(self, thread_id: str) -> None:
        """Publish a thread id, which may arrive long after the start.

        A thread created by this session has no title until its first run
        finishes, so this only records the id; ``refresh_title`` (called when a
        run ends) publishes the title.
        """

        if self.marks is None or thread_id == self._thread_id:
            return
        self._thread_id = thread_id
        self._title_published = False
        self.marks.set(MARK_THREAD_ID, thread_id)
        # name the window after the thread as soon as the id exists; the title
        # follows when its run finishes
        self.marks.name_window(thread_id)

    def refresh_title(self) -> None:
        """Publish the thread title once it exists.

        Callers may call this after every run: a thread title becomes available
        only after the run that created the thread, and it is published once per
        thread.
        """

        if self.marks is None or self._thread_id is None or self._title_published:
            return
        title = self._lookup_title(self._thread_id)
        if not title:
            return
        clipped = clip_title(title)
        self.marks.set(MARK_THREAD_TITLE, clipped)
        self.marks.title_pane(clipped)
        self._title_published = True

    def clear(self) -> None:
        """Remove the marks this session published."""

        if self.marks is None:
            return
        self.marks.clear(*MARK_NAMES)
        self._thread_id = None
        self._title_published = True

    def _lookup_title(self, thread_id: str) -> str | None:
        try:
            title = self._title_lookup(thread_id)
        except Exception:
            return None
        return title if isinstance(title, str) and title else None
