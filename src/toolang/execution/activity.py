"""Shared compact activity reads over committed indexes, independent of viewers."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from contextlib import closing, suppress
from datetime import datetime
import json
import math
import re
from pathlib import Path
import sqlite3
import threading
import time

from .schemas import ActivityMetrics, ActivityNode, ActivitySnapshot
from .statistics import BUCKET_FIELDS, TOKEN_FIELDS, _contributions
from .store import RunStore
from .types import RunRef, StepRef
from .values import parts_from_value
from toolang.base.types.message import TextPart

PAGE_SIZE = 200
PATH_LIMIT = 4000
THREAD_LIMIT = 10000


def duration(value: str) -> float | None:
    """Parse an activity window; keep relative Stats queries relative."""
    if value == "all":
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw])", value)
    if not match or float(match[1]) <= 0:
        raise ValueError("Use a positive duration such as 30m, 1d, 1w, or all")
    seconds = (
        float(match[1])
        * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
    )
    if not math.isfinite(seconds):
        raise ValueError("Duration must be finite")
    if seconds > 315537897600:
        raise ValueError("Duration is outside the supported date range")
    return seconds


@dataclass(frozen=True)
class ActivityQuery:
    since: str = "session"
    recent: float | None = 1800
    text: str = ""
    active: bool = False

    def __post_init__(self) -> None:
        if self.since not in {"session", "all"} and self.window is None:
            value = datetime.fromisoformat(self.since.replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("Stats timestamp must include a timezone")
        if self.recent is not None and (
            not math.isfinite(self.recent) or self.recent <= 0
        ):
            raise ValueError("Recent must be a positive duration")
        if len(self.text) > 240:
            raise ValueError("Activity filter is limited to 240 characters")

    @property
    def window(self) -> float | None:
        if re.fullmatch(r"\d+(?:\.\d+)?[smhdw]", self.since):
            return duration(self.since)
        return None

    def start(self, now: float) -> float | None:
        if self.since in {"session", "all"}:
            return None
        if self.window is not None:
            return now - self.window
        return datetime.fromisoformat(self.since.replace("Z", "+00:00")).timestamp()


class ActivityReader:
    """Share one atomic paginated projection per query and committed revision."""

    def __init__(self, path: Path, agent: str) -> None:
        self.path, self.agent = path, agent
        self._lock = threading.Lock()
        self._cache: OrderedDict[
            tuple[ActivityQuery, bool | None],
            tuple[float, list[ActivitySnapshot], float | None],
        ] = OrderedDict()
        self._listeners: dict[
            tuple[ActivityQuery, bool | None],
            set[asyncio.Queue[list[ActivitySnapshot] | Exception]],
        ] = {}
        self._publishers: dict[
            tuple[ActivityQuery, bool | None], asyncio.Task[None]
        ] = {}

    def result(self, ref: str) -> str:
        """Resolve result text only when Details is opened, outside the live feed."""
        with (
            closing(RunStore(self.path, read_only=True)) as store,
            store.read_transaction(),
        ):
            record = (
                store.get_step(ref=StepRef.parse(ref))
                if "." in ref
                else store.get_run(run_id=str(RunRef.parse(ref)))
            )
            if record is None:
                raise KeyError(ref)
            if record.output is None:
                return ""
            output = store.resolve_output(record.output)
            return "\n".join(
                part.text
                if isinstance(part, TextPart)
                else json.dumps(part.to_data(), ensure_ascii=False)
                for part in parts_from_value(output.value, content_only=True)
            )

    async def updates(
        self, query: ActivityQuery, *, live: bool | None = None
    ) -> AsyncGenerator[list[ActivitySnapshot]]:
        """Share atomic absolute updates; a slow viewer needs only the latest one."""
        queue: asyncio.Queue[list[ActivitySnapshot] | Exception] = asyncio.Queue(1)
        key = query, live
        listeners = self._listeners.setdefault(key, set())
        listeners.add(queue)
        try:
            if key not in self._publishers or self._publishers[key].done():
                self._publishers[key] = asyncio.create_task(self._publish(key))
            else:
                pages = await asyncio.to_thread(self.pages, query, live=live)
                if queue.empty():
                    queue.put_nowait(pages)
            while True:
                value = await queue.get()
                if isinstance(value, Exception):
                    raise value
                yield value
        finally:
            listeners.discard(queue)
            if not listeners:
                task = self._publishers.pop(key)
                self._listeners.pop(key)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _publish(self, key: tuple[ActivityQuery, bool | None]) -> None:
        query, live = key
        boundary = None
        deadline = 0.0
        try:
            while True:
                current = await asyncio.to_thread(self._revision)
                if current != boundary or time.monotonic() >= deadline:
                    pages = await asyncio.to_thread(self.pages, query, live=live)
                    for queue in self._listeners[key]:
                        if queue.full():
                            queue.get_nowait()
                        queue.put_nowait(pages)
                    boundary = (pages[0].session, pages[0].revision)
                    deadline = time.monotonic() + 1
                await asyncio.sleep(0.1)
        except Exception as exc:
            for queue in self._listeners[key]:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(exc)

    def _revision(self) -> tuple[str | None, int]:
        # Poll only the committed marker; copying the full projection belongs to
        # an actual publication or a clock tick, not every 100 ms probe.
        with closing(RunStore(self.path, read_only=True)) as store:
            row = store._conn.execute(
                "SELECT session,revision FROM activity_meta"
            ).fetchone()
            if row is None:
                raise ValueError("Activity metadata is unavailable")
            return row["session"], row["revision"]

    def read(
        self, query: ActivityQuery, offset: int = 0, *, now: float | None = None
    ) -> ActivitySnapshot:
        if offset < 0 or offset % PAGE_SIZE:
            raise ValueError(f"Activity offset must be a multiple of {PAGE_SIZE}")
        pages = self.pages(query, now=now)
        if offset // PAGE_SIZE >= len(pages):
            raise ValueError("Activity page no longer exists")
        return pages[offset // PAGE_SIZE]

    def pages(
        self,
        query: ActivityQuery,
        *,
        now: float | None = None,
        live: bool | None = None,
    ) -> list[ActivitySnapshot]:
        clock = time.time() if now is None else now
        with (
            self._lock,
            closing(RunStore(self.path, read_only=True)) as store,
            store.read_transaction(),
        ):
            conn = store._conn
            boundary = conn.execute("SELECT * FROM activity_meta").fetchone()
            if boundary is None:
                raise ValueError("Activity metadata is unavailable")
            revision = boundary["revision"]
            _, online, execution_now = _execution_clock(conn, boundary, clock, live)
            # Checkpoints advance without a record revision. Offline projections
            # must use the latest durable duration boundary, including Total.
            frozen_clock = None if online else execution_now
            key = query, live
            cached = self._cache.get(key)
            if (
                cached
                and (cached[1][0].revision, cached[1][0].session)
                == (revision, boundary["session"])
                and cached[2] == frozen_clock
                and cached[1][0].observed is not None
                and cached[1][0].observed <= clock < cached[0]
            ):
                pages = [page.model_copy(deep=True) for page in cached[1]]
                for page in pages:
                    assert page.observed is not None
                    advance = (
                        max(0, clock - page.observed)
                        if page.presence == "online"
                        else 0
                    )
                    seen = set()
                    for metrics in [
                        page.stats,
                        page.total,
                        *(
                            value
                            for node in [*page.threads, *page.roots, *page.paths]
                            for value in (node.stats, node.total)
                        ),
                    ]:
                        if id(metrics) not in seen and metrics.time is not None:
                            metrics.time += metrics.time_rate * advance
                            seen.add(id(metrics))
                    page.observed = clock
                if query.window is not None:
                    # Structure and lifetime totals are unchanged. Recalculate
                    # just the moving range over indexed facts, once per scope.
                    moving: dict[str, ActivityMetrics] = {}
                    for page in pages:
                        for scope, owner in [
                            ("@agent", page),
                            *(
                                (node.id, node)
                                for node in [*page.threads, *page.roots, *page.paths]
                            ),
                        ]:
                            if scope not in moving:
                                moving[scope] = _metrics(
                                    conn,
                                    scope,
                                    query.since,
                                    boundary,
                                    clock,
                                    execution_now=execution_now,
                                )
                            owner.stats = moving[scope]
                self._cache.move_to_end(key)
                return pages
            pages = _read(conn, self.agent, query, clock, live=live)
            expiry = float("inf")
            if query.recent is not None:
                row = conn.execute(
                    "SELECT MIN(changed) FROM activity_nodes WHERE current=1 AND kind IN ('thread','run') AND changed>=?",
                    (clock - query.recent,),
                ).fetchone()
                if row[0] is not None:
                    expiry = row[0] + query.recent + 0.001
            start = query.start(clock)
            if query.window is None and start is not None and start > clock:
                expiry = min(expiry, start)
            self._cache[key] = expiry, pages, frozen_clock
            self._cache.move_to_end(key)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
            return [page.model_copy(deep=True) for page in pages]


def _metrics(
    conn: sqlite3.Connection,
    scope: str,
    since: str,
    meta: sqlite3.Row,
    now: float,
    *,
    execution_now: float | None = None,
) -> ActivityMetrics:
    session = meta["session"] if since == "session" else "*"
    if session is None:
        return ActivityMetrics(
            model=None, tool=None, cost=None, time=None, complete=False
        )
    start = ActivityQuery(since).start(now)
    execution_now = now if execution_now is None else execution_now
    values = [0.0] * len(BUCKET_FIELDS)
    if start is None:
        bucket = conn.execute(
            f"SELECT {','.join(BUCKET_FIELDS)} FROM activity_buckets WHERE scope=? AND session=? AND minute=-1",
            (scope, session),
        ).fetchone()
        if bucket:
            values = list(bucket)
    else:
        boundary = (int(start // 60) + 1) * 60
        bucket = conn.execute(
            f"SELECT {','.join(f'SUM({field})' for field in BUCKET_FIELDS)} FROM activity_buckets WHERE scope=? AND session='*' AND minute>=?",
            (scope, int(boundary // 60)),
        ).fetchone()
        values = [value or 0 for value in bucket]
        for fact in conn.execute(
            """WITH boundary AS (
                SELECT id FROM activity_attempts WHERE started>=? AND started<?
                UNION SELECT id FROM activity_attempts WHERE finished>=? AND finished<?
            ) SELECT a.* FROM boundary b CROSS JOIN activity_attempts a ON a.id=b.id
              CROSS JOIN activity_owners o ON o.attempt=a.id AND o.scope=?""",
            (start, boundary, start, boundary, scope),
        ):
            for at, change in _contributions(fact):
                if start <= at < boundary:
                    values = [
                        left + right for left, right in zip(values, change, strict=True)
                    ]
    # Session/all use closed totals. A time range visits only intervals ending
    # inside it, rather than every duration ever owned by a long-lived agent.
    duration = 0.0
    incomplete = False
    if start is None:
        closed = conn.execute(
            "SELECT total,incomplete FROM activity_durations WHERE scope=? AND session=?",
            (scope, session),
        ).fetchone()
        if closed:
            duration, incomplete = closed["total"], bool(closed["incomplete"])
    else:
        for fact in conn.execute(
            """SELECT a.* FROM activity_attempts a INDEXED BY idx_activity_finished
            CROSS JOIN activity_owners o ON o.attempt=a.id
            WHERE a.finished>=? AND o.scope=? AND o.duration=1""",
            (start, scope),
        ):
            duration += max(0, fact["finished"] - max(fact["started"], start))
            incomplete |= not fact["complete"]
    open_cost = 0
    time_rate = 0
    for fact in conn.execute(
        """SELECT * FROM activity_open WHERE scope=? AND (?='*' OR session=?)""",
        (scope, session, session),
    ):
        if start is None or start <= now:
            open_cost += fact["model"]
        if fact["duration"]:
            duration += max(
                0,
                execution_now - max(fact["started"], start if start is not None else 0),
            )
            if execution_now == now and (start is None or start <= now):
                time_rate += 1
    first = conn.execute("SELECT MIN(started) FROM activity_sessions").fetchone()[0]
    complete = not incomplete and (
        not meta["legacy"]
        or since == "session"
        or (start is not None and first is not None and start >= first)
    )
    model, tool, cost, known, unknown, estimated, partial = values[:7]
    unknown += open_cost
    tokens: dict[str, int | None] = {}
    tokens_complete = complete
    for index, field in enumerate(TOKEN_FIELDS):
        quantity, known_tokens, missing_tokens = values[7 + index * 3 : 10 + index * 3]
        missing_tokens += open_cost
        tokens[field] = int(quantity) if known_tokens or not missing_tokens else None
        tokens_complete &= not bool(missing_tokens)
    return ActivityMetrics(
        model=int(model),
        tool=int(tool),
        cost=cost if known or not unknown else None,
        time=duration,
        time_rate=time_rate,
        estimated=bool(estimated),
        partial=bool(unknown or partial),
        complete=complete,
        input_tokens=tokens["input_tokens"],
        cached_tokens=tokens["cached_tokens"],
        output_tokens=tokens["output_tokens"],
        tokens_complete=tokens_complete,
    )


def _execution_clock(
    conn: sqlite3.Connection, meta: sqlite3.Row, now: float, live: bool | None
) -> tuple[sqlite3.Row | None, bool, float]:
    session = conn.execute(
        "SELECT * FROM activity_sessions WHERE id=?", (meta["session"],)
    ).fetchone()
    online = bool(session and session["ended"] is None) if live is None else live
    execution_now = now
    if session and (not online or session["ended"] is not None):
        execution_now = min(
            now,
            session["ended"] if session["ended"] is not None else session["checkpoint"],
        )
    return session, online, execution_now


def _read(
    conn: sqlite3.Connection,
    agent: str,
    query: ActivityQuery,
    now: float,
    *,
    live: bool | None = None,
) -> list[ActivitySnapshot]:
    meta = conn.execute("SELECT * FROM activity_meta").fetchone()
    if meta is None:
        raise ValueError("Activity migration is not available")
    session, online, execution_now = _execution_clock(conn, meta, now, live)
    memo: dict[tuple[str, str], ActivityMetrics] = {}

    def metrics(scope: str, since: str) -> ActivityMetrics:
        key = scope, since
        if key not in memo:
            memo[key] = _metrics(
                conn, scope, since, meta, now, execution_now=execution_now
            )
        return memo[key]

    def node(row: sqlite3.Row) -> ActivityNode:
        attempt = conn.execute(
            "SELECT session,complete FROM activity_attempts WHERE id=?",
            (row["attempt"],),
        ).fetchone()
        return ActivityNode(
            id=row["id"],
            kind=row["kind"],
            thread=row["thread"],
            root=row["root"],
            parent=row["parent"],
            title=row["title"],
            summary=row["summary"],
            status=row["status"],
            created=row["created"],
            changed=row["changed"],
            position=json.loads(row["position"]),
            stale=bool(
                attempt
                and (
                    not online
                    or attempt["session"] != meta["session"]
                    or not attempt["complete"]
                )
                and row["status"] in {"pending", "running"}
            ),
            stats=metrics(row["id"], query.since),
            total=metrics(row["id"], "all"),
        )

    recent = now - query.recent if query.recent is not None else 0
    eligible = conn.execute(
        """SELECT * FROM activity_nodes WHERE kind='run' AND parent IS NULL AND current=1
        AND (status IN ('pending','running') OR changed>=?) ORDER BY created,id""",
        (recent,),
    ).fetchall()
    text = query.text.casefold()
    agent_match = text in agent.casefold()
    counts: dict[str, list[int]] = {}
    matches: dict[str, list[str]] = {}
    selected = []
    for row in eligible:
        count = counts.setdefault(row["thread"], [0, 0])
        count[0] += row["status"] in {"pending", "running"}
        count[1] += row["status"] == "failed"
        if query.active and row["status"] not in {"pending", "running"}:
            continue
        if text and not agent_match and text not in row["thread"].casefold():
            ids = [
                child["id"]
                for child in conn.execute(
                    "SELECT ref AS id,title,summary,status FROM activity_search WHERE root=?",
                    (row["id"],),
                )
                if any(
                    text in str(child[field]).casefold()
                    for field in ("id", "title", "summary", "status")
                )
            ]
            if not ids:
                continue
            matches[row["id"]] = list(dict.fromkeys(ids))[:100]
        selected.append(row)
    roots = [node(row) for row in selected]
    paths: dict[str, list[ActivityNode]] = {}
    complete = bool(meta["migrated"])
    for root in roots:
        root.matches = matches.get(root.id, [])
        if root.status != "running" or root.stale:
            continue
        running = conn.execute(
            "SELECT * FROM activity_nodes WHERE root=? AND current=1 AND status IN ('pending','running') AND id!=? ORDER BY created,id LIMIT ?",
            (root.id, root.id, PATH_LIMIT + 1),
        ).fetchall()
        complete &= len(running) <= PATH_LIMIT
        keep: dict[str, sqlite3.Row] = {}
        for row in running[:PATH_LIMIT]:
            if row["status"] == "pending":
                continue
            while row and row["id"] != root.id and row["id"] not in keep:
                keep[row["id"]] = row
                row = conn.execute(
                    "SELECT * FROM activity_nodes WHERE id=? AND current=1",
                    (row["parent"],),
                ).fetchone()
        for current in [root, *(node(row) for row in keep.values())]:
            children = conn.execute(
                "SELECT status,COUNT(*) AS n,MAX(expected) AS expected FROM activity_nodes WHERE parent=? AND current=1 GROUP BY status",
                (current.id,),
            ).fetchall()
            current.children = max(
                sum(row["n"] for row in children),
                max((row["expected"] for row in children), default=0),
            )
            current.completed = sum(
                row["n"]
                for row in children
                if row["status"] not in {"pending", "running"}
            )
            current.pending = sum(
                row["n"] for row in children if row["status"] == "pending"
            )
            if current.kind == "run":
                current.pending = conn.execute(
                    "SELECT COUNT(*) FROM activity_nodes WHERE kind='run' AND current=1 AND status='pending' AND instr(parent,?)=1",
                    (current.id + ".",),
                ).fetchone()[0]
            if current is not root:
                paths.setdefault(root.id, []).append(current)
    thread_count = conn.execute(
        "SELECT COUNT(*) FROM activity_nodes WHERE kind='thread' AND current=1"
    ).fetchone()[0]
    thread_rows = conn.execute(
        """WITH active_threads AS (SELECT DISTINCT thread FROM activity_nodes
        WHERE kind='run' AND parent IS NULL AND current=1 AND status IN ('pending','running'))
        SELECT t.*,a.thread IS NOT NULL AS active_thread FROM activity_nodes t
        LEFT JOIN active_threads a ON a.thread=t.id WHERE t.kind='thread' AND t.current=1
        AND (t.changed>=? OR active_thread) ORDER BY active_thread DESC,t.changed DESC,t.id""",
        (recent,),
    )
    threads = []
    thread_eligible = thread_matched = 0
    matched_threads = {row["thread"] for row in selected}
    for row in thread_rows:
        active, failed = counts.get(row["id"], (0, 0))
        if not active and row["changed"] < recent:
            continue
        thread_eligible += 1
        if query.active and not active:
            continue
        if (
            text
            and not agent_match
            and text not in row["id"].casefold()
            and row["id"] not in matched_threads
        ):
            continue
        thread_matched += 1
        if len(threads) == THREAD_LIMIT:
            complete = False
            continue
        item = node(row)
        item.active, item.failed = active, failed
        threads.append(item)
    snapshot = ActivitySnapshot(
        agent=agent,
        revision=meta["revision"],
        session=meta["session"],
        session_start=session["started"] if session else None,
        observed=now,
        since=query.since,
        recent=query.recent,
        filter=query.text,
        active_only=query.active,
        presence="online" if online else "offline",
        stale=bool(not online and session and session["ended"] is None),
        complete=complete,
        coverage=""
        if complete
        else "Activity coverage incomplete: path/thread limit or backfill",
        stats=metrics("@agent", query.since),
        total=metrics("@agent", "all"),
        threads=threads,
        active=sum(value[0] for value in counts.values()),
        failed=sum(value[1] for value in counts.values()),
        thread_count=thread_count,
        thread_eligible=thread_eligible,
        thread_matched=thread_matched,
        eligible=len(eligible),
        matched=len(selected),
        available=len(selected),
    )
    # Project a boundary once, then paginate it. Smaller wire pages must not
    # multiply history searches, thread projection, or scope aggregation.
    pages = []
    for offset in range(0, max(1, len(roots)), PAGE_SIZE):
        batch = roots[offset : offset + PAGE_SIZE]
        pages.append(
            snapshot.model_copy(
                update={
                    "roots": batch,
                    "threads": threads if offset == 0 else [],
                    "paths": [
                        child for root in batch for child in paths.get(root.id, ())
                    ],
                    "offset": offset,
                    "next_offset": offset + PAGE_SIZE
                    if offset + PAGE_SIZE < len(roots)
                    else None,
                }
            )
        )
    return pages
