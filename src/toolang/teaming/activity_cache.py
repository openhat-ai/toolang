"""Activity publication boundaries, query identity, and offline cache selection.

These rules are independent of the storage driver and contain no I/O.
"""

import hashlib
import json
import time

from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivityMetrics, ActivitySnapshot


def publication_key(agent: str, pages: list[ActivitySnapshot]) -> tuple[str, str]:
    if not pages or any(page.agent != agent for page in pages):
        raise ValueError("Activity snapshot belongs to another agent")
    first = pages[0]
    expected = 0
    for page in pages:
        if page.observed is None:
            raise ValueError("Published activity requires an observation time")
        if page.offset != expected or (
            page.session,
            page.revision,
            query_key(page),
        ) != (first.session, first.revision, query_key(first)):
            raise ValueError("Activity pages must share one boundary and query")
        expected = page.next_offset
    if expected is not None:
        raise ValueError("Activity publication is missing pages")
    query = query_key(first)
    # Keep the default publication plus the last requested range. Older
    # queries need a live source; never mislabel another range's statistics.
    slot = "default" if query == query_key(ActivityQuery()) else "query"
    return slot, query


def query_key(query: ActivityQuery | ActivitySnapshot) -> str:
    fields = (
        (
            query.since,
            float(query.recent) if query.recent is not None else None,
            query.text,
            query.active,
        )
        if isinstance(query, ActivityQuery)
        else (
            query.since,
            float(query.recent) if query.recent is not None else None,
            query.filter,
            query.active_only,
        )
    )
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def select_cached(
    agent: str,
    query: ActivityQuery,
    entries: dict[str, tuple[str, list[ActivitySnapshot]]],
) -> list[ActivitySnapshot]:
    fallback = None
    for slot in ("query", "default"):
        if slot not in entries:
            continue
        cached_query, pages = entries[slot]
        if cached_query == query_key(query):
            return pages
        if slot == "default":
            fallback = pages
    if fallback:
        fallback = cached_selection(fallback, query)
        for page in fallback:
            page.complete = False
            page.coverage = (
                "Requested range unavailable offline; showing last cached activity"
            )
            if page.since != query.since:
                page.stats = ActivityMetrics(
                    model=None, tool=None, cost=None, time=None, complete=False
                )
                for node in [*page.threads, *page.roots, *page.paths]:
                    node.stats = page.stats
            page.since = query.since
            page.recent = query.recent
            page.filter = query.text
            page.active_only = query.active
        return fallback
    unknown = ActivityMetrics(
        model=None, tool=None, cost=None, time=None, complete=False
    )
    return [
        ActivitySnapshot(
            agent=agent,
            revision=0,
            observed=None,
            since=query.since,
            recent=query.recent,
            filter=query.text,
            active_only=query.active,
            complete=False,
            coverage="No activity snapshot available",
            stats=unknown,
            total=unknown,
        )
    ]


def cached_selection(
    pages: list[ActivitySnapshot], query: ActivityQuery
) -> list[ActivitySnapshot]:
    """Apply narrower visibility to cached metadata without changing frozen Stats."""
    first = pages[0].model_copy(deep=True)
    roots = [node for page in pages for node in page.roots]
    paths = [node for page in pages for node in page.paths]
    cutoff = time.time() - query.recent if query.recent is not None else 0
    eligible = [
        node
        for node in roots
        if node.status in {"pending", "running"} or node.changed >= cutoff
    ]
    text = query.text.casefold()
    matches = set()
    for node in [*eligible, *paths]:
        if any(
            text in str(value).casefold()
            for value in (
                first.agent,
                node.thread,
                node.id,
                node.title,
                node.summary,
                node.status,
            )
        ):
            matches.add(node.root)
    roots = [
        node
        for node in eligible
        if (not query.active or node.status in {"pending", "running"})
        and (not text or node.root in matches)
    ]
    first.roots = roots
    first.paths = []
    first.active = sum(node.status in {"pending", "running"} for node in eligible)
    first.failed = sum(node.status == "failed" for node in eligible)
    first.eligible, first.matched, first.available = (
        len(eligible),
        len(roots),
        len(roots),
    )
    first.offset, first.next_offset = 0, None
    for thread in first.threads:
        thread.active = sum(
            node.thread == thread.id and node.status in {"pending", "running"}
            for node in eligible
        )
        thread.failed = sum(
            node.thread == thread.id and node.status == "failed" for node in eligible
        )
    eligible_threads = [
        thread for thread in first.threads if thread.active or thread.changed >= cutoff
    ]
    first.threads = [
        thread
        for thread in eligible_threads
        if (not query.active or thread.active)
        and (
            not text
            or text in first.agent.casefold()
            or text in thread.id.casefold()
            or any(node.thread == thread.id for node in roots)
        )
    ]
    first.thread_eligible = len(eligible_threads)
    first.thread_matched = len(first.threads)
    return [first]
