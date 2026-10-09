"""Transaction-owned activity indexes and consumption facts.

Only compact metadata is indexed. Attempts and ownership survive record deletion;
minute buckets are updated by differences, never by summing an execution tree.
"""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import TYPE_CHECKING, Any, cast

from toolang.common.time import utc_now
from toolang.lang.types import Array
from .accounting import selected_usd_cost
from .inspection.types import step_operation
from .records import RunControlPayload, StoredModelStepGiven, output_from_data
from .types import (
    ContentRef,
    ModelStepNoted,
    StepRef,
    ToolStepGiven,
    ToolStepNoted,
)

if TYPE_CHECKING:
    from .store import RunStore


DDL = (
    """CREATE TABLE IF NOT EXISTS activity_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
        session TEXT, legacy INTEGER NOT NULL, migrated INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS activity_sessions (
        id TEXT PRIMARY KEY, started REAL NOT NULL, checkpoint REAL NOT NULL,
        ended REAL, complete INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS activity_dirty (
        kind TEXT NOT NULL, id TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,id))""",
    """CREATE TABLE IF NOT EXISTS activity_nodes (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, thread TEXT NOT NULL,
        root TEXT NOT NULL, parent TEXT, title TEXT NOT NULL, summary TEXT NOT NULL,
        status TEXT NOT NULL, created REAL NOT NULL, changed REAL NOT NULL,
        started REAL, finished REAL, attempt INTEGER, current INTEGER NOT NULL,
        revision INTEGER NOT NULL, position TEXT NOT NULL, expected INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS activity_search (
        root TEXT NOT NULL, ref TEXT NOT NULL, attempt INTEGER NOT NULL,
        title TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY(root,ref,attempt))""",
    "CREATE INDEX IF NOT EXISTS idx_activity_root ON activity_nodes(root,current,status)",
    "CREATE INDEX IF NOT EXISTS idx_activity_parent ON activity_nodes(parent,current)",
    "CREATE INDEX IF NOT EXISTS idx_activity_changed ON activity_nodes(kind,current,changed)",
    """CREATE TABLE IF NOT EXISTS activity_attempts (
        id INTEGER PRIMARY KEY, ref TEXT NOT NULL, session TEXT NOT NULL,
        kind TEXT NOT NULL, started REAL NOT NULL, finished REAL,
        cost REAL, estimated INTEGER NOT NULL DEFAULT 0,
        partial INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 1,
        interrupted INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS activity_owners (
        scope TEXT NOT NULL, attempt INTEGER NOT NULL, duration INTEGER NOT NULL,
        PRIMARY KEY(scope,attempt))""",
    "CREATE INDEX IF NOT EXISTS idx_activity_attempt_ref ON activity_attempts(ref,started)",
    "CREATE INDEX IF NOT EXISTS idx_activity_owner_attempt ON activity_owners(attempt)",
    """CREATE TABLE IF NOT EXISTS activity_durations (
        scope TEXT NOT NULL, session TEXT NOT NULL, total REAL NOT NULL,
        incomplete INTEGER NOT NULL, PRIMARY KEY(scope,session))""",
    """CREATE TABLE IF NOT EXISTS activity_open (
        scope TEXT NOT NULL, attempt INTEGER NOT NULL, session TEXT NOT NULL,
        started REAL NOT NULL, model INTEGER NOT NULL, duration INTEGER NOT NULL,
        PRIMARY KEY(scope,attempt))""",
    "CREATE INDEX IF NOT EXISTS idx_activity_finished ON activity_attempts(finished)",
    "CREATE INDEX IF NOT EXISTS idx_activity_started ON activity_attempts(started)",
    "CREATE INDEX IF NOT EXISTS idx_activity_duration_owner ON activity_owners(scope,duration,attempt)",
    """CREATE TABLE IF NOT EXISTS activity_buckets (
        scope TEXT NOT NULL, session TEXT NOT NULL, minute INTEGER NOT NULL,
        model INTEGER NOT NULL, tool INTEGER NOT NULL, cost REAL NOT NULL,
        known INTEGER NOT NULL, unknown INTEGER NOT NULL, estimated INTEGER NOT NULL,
        partial INTEGER NOT NULL, PRIMARY KEY(scope,session,minute))""",
)


def stamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        # Historical/imported records allow opaque timestamps. Do not make
        # optional observation reject records accepted by the store contract.
        return float(value) if value.isdecimal() else None


def initialize(conn: sqlite3.Connection) -> None:
    """Install a resumable queue once; triggers capture concurrent live mutations."""
    for sql in DDL:
        conn.execute(sql)
    if conn.execute("SELECT 1 FROM activity_meta").fetchone() is None:
        legacy = bool(conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone())
        conn.execute("INSERT INTO activity_meta VALUES (1,0,NULL,?,0)", (legacy,))
        for table, kind in (("threads", "thread"), ("runs", "run"), ("steps", "step")):
            conn.execute(
                f"INSERT INTO activity_dirty SELECT ?,id,1 FROM {table}", (kind,)
            )
    # Exclude cursor stamps and nonstructural writes. A mutation costs one queue
    # entry irrespective of how many fields change or how many observers exist.
    for table, kind, columns in (
        ("threads", "thread", "updated_at,horizon,peer"),
        ("runs", "run", "status,started_at,finished_at,output,error,control"),
        ("steps", "step", "status,finished_at,noted,output,error"),
    ):
        for operation, ref in (
            ("INSERT", "NEW"),
            (f"UPDATE OF {columns}", "NEW"),
            ("DELETE", "OLD"),
        ):
            name = operation.split()[0].lower()
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS activity_{table}_{name}
                AFTER {operation} ON {table} BEGIN
                INSERT OR IGNORE INTO activity_dirty VALUES ('{kind}',{ref}.id,0);
                END""")


def backfill(store: RunStore) -> None:
    """Commit bounded batches so an interrupted migration resumes without recounting."""
    conn = store._conn
    while conn.execute("SELECT 1 FROM activity_dirty LIMIT 1").fetchone():
        with store._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                flush(store, limit=256)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    with store.write_transaction():
        conn.execute("UPDATE activity_meta SET migrated=1")


def _preview(value: object) -> str:
    if isinstance(value, (tuple, list, Array)):
        return _preview(" ".join(filter(None, (_preview(item) for item in value))))
    if isinstance(value, dict):
        return _preview(cast(dict[str, object], value).get("text", ""))
    if hasattr(value, "text"):
        return _preview(getattr(value, "text"))
    if isinstance(value, str):
        return " ".join(value.split())[:240]
    return ""


def _summary(store: RunStore, kind: str, ref: str, row: sqlite3.Row) -> tuple[str, str]:
    if kind == "run":
        control = store.get_run_control(run_id=ref, index=0)
        title = (
            control.payload.runnable
            if control and isinstance(control.payload, RunControlPayload)
            else ref
        )
        error = json.loads(row["error"]) if row["error"] else None
        if error and error.get("type") == "message":
            return title, _preview(error["message"])
        if row["output"]:
            return title, _preview(output_from_data(json.loads(row["output"])).value)
        return title, ""
    step = store.get_step(ref=StepRef.parse(ref))
    assert step is not None
    summary = ""
    if isinstance(step.given, ToolStepGiven):
        summary = (
            step.noted.summary
            if isinstance(step.noted, ToolStepNoted)
            else step.given.summary
        )
    elif isinstance(step.given, StoredModelStepGiven):
        # Capture once at begin. Do not ship prompts or resolve full message history.
        for message in reversed(step.given.call.messages.delta):
            if message.role != "user":
                continue
            for segment in message.content:
                if isinstance(segment, str):
                    summary = _preview(segment)
                elif isinstance(segment, ContentRef):
                    raw = store.get_content(segment)
                    if raw:
                        try:
                            data = json.loads(raw)
                            summary = _preview(data)
                        except (ValueError, UnicodeDecodeError):
                            pass
                else:
                    summary = _preview(getattr(segment, "text", ""))
                if summary:
                    break
            if summary:
                break
        if not summary:
            raw = store._conn.execute(
                "SELECT substr(value,1,1024) FROM contents WHERE id=?",
                (step.given.call.instructions,),
            ).fetchone()
            if raw and raw[0]:
                summary = _preview(bytes(raw[0]).decode("utf-8", errors="ignore"))
        summary = f"preview: {summary}" if summary else ""
    return step_operation(step), _preview(summary)


def _ownership(
    conn: sqlite3.Connection, kind: str, ref: str, row: sqlite3.Row
) -> tuple[str, str, str | None, list[str]]:
    if kind == "thread":
        return ref, "", None, [ref, "@agent"]
    scopes = [ref]
    parent = (
        row["parent"] if kind == "run" else str(StepRef.parse(ref).parent or row["run"])
    )
    current = parent
    while current:
        scopes.append(current)
        if "." in current:
            step = StepRef.parse(current)
            current = str(step.parent or step.run_id)
        else:
            owner = conn.execute(
                "SELECT parent,thread FROM runs WHERE id=?", (current,)
            ).fetchone()
            if owner is None:
                break
            current = owner["parent"]
    root = scopes[-1]
    run = (
        row
        if kind == "run"
        else conn.execute("SELECT * FROM runs WHERE id=?", (row["run"],)).fetchone()
    )
    thread = run["thread"] if run else ""
    return thread, root, parent, [*scopes, thread, "@agent"]


def _contributions(
    attempt: sqlite3.Row | dict[str, Any],
) -> list[tuple[float, tuple[float, ...]]]:
    kind, end = attempt["kind"], attempt["finished"]
    result: list[tuple[float, tuple[float, ...]]] = [
        (attempt["started"], (int(kind == "model"), int(kind == "tool"), 0, 0, 0, 0, 0))
    ]
    if kind == "model" and end is not None:
        cost = attempt["cost"]
        result.append(
            (
                end,
                (
                    0,
                    0,
                    cost or 0,
                    int(cost is not None),
                    int(cost is None),
                    attempt["estimated"],
                    attempt["partial"],
                ),
            )
        )
    return result


def _buckets(
    conn: sqlite3.Connection, attempt: sqlite3.Row | dict[str, Any], sign: int
) -> None:
    scopes = conn.execute(
        "SELECT scope FROM activity_owners WHERE attempt=?", (attempt["id"],)
    ).fetchall()
    for owner in conn.execute(
        "SELECT scope,duration FROM activity_owners WHERE attempt=?", (attempt["id"],)
    ).fetchall():
        if attempt["finished"] is None:
            if sign == 1:
                conn.execute(
                    "INSERT OR REPLACE INTO activity_open VALUES (?,?,?,?,?,?)",
                    (
                        owner["scope"],
                        attempt["id"],
                        attempt["session"],
                        attempt["started"],
                        attempt["kind"] == "model",
                        owner["duration"],
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM activity_open WHERE scope=? AND attempt=?",
                    (owner["scope"], attempt["id"]),
                )
        elif owner["duration"]:
            duration = max(0, attempt["finished"] - attempt["started"])
            for session in (attempt["session"], "*"):
                conn.execute(
                    """INSERT INTO activity_durations VALUES (?,?,?,?)
                    ON CONFLICT(scope,session) DO UPDATE SET total=total+excluded.total,
                    incomplete=incomplete+excluded.incomplete""",
                    (
                        owner["scope"],
                        session,
                        sign * duration,
                        sign * (not attempt["complete"]),
                    ),
                )
    for at, values in _contributions(attempt):
        if not any(values):
            continue
        for scope in scopes:
            for session in (attempt["session"], "*"):
                for minute in (-1, int(at // 60)):
                    conn.execute(
                        """INSERT INTO activity_buckets VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(scope,session,minute) DO UPDATE SET
                        model=model+excluded.model, tool=tool+excluded.tool, cost=cost+excluded.cost,
                        known=known+excluded.known, unknown=unknown+excluded.unknown,
                        estimated=estimated+excluded.estimated, partial=partial+excluded.partial""",
                        (
                            scope["scope"],
                            session,
                            minute,
                            *(sign * value for value in values),
                        ),
                    )


def _interrupt(conn: sqlite3.Connection, attempt: sqlite3.Row, at: float) -> None:
    """Freeze an unfinished attempt without losing its consumed calls or ownership."""
    _buckets(conn, attempt, -1)
    conn.execute(
        "UPDATE activity_attempts SET finished=MAX(started,?),complete=0,interrupted=1 WHERE id=?",
        (at, attempt["id"]),
    )
    _buckets(
        conn,
        conn.execute(
            "SELECT * FROM activity_attempts WHERE id=?", (attempt["id"],)
        ).fetchone(),
        1,
    )


def flush(store: RunStore, *, limit: int | None = None) -> None:
    conn = store._conn
    queued = conn.execute(
        "SELECT * FROM activity_dirty ORDER BY CASE kind WHEN 'thread' THEN 0 WHEN 'run' THEN 1 ELSE 2 END,id LIMIT ?",
        (limit or -1,),
    ).fetchall()
    if not queued:
        return
    conn.execute("UPDATE activity_meta SET revision=revision+1")
    meta = conn.execute("SELECT * FROM activity_meta").fetchone()
    revision, session = meta["revision"], meta["session"] or "legacy"
    for item in queued:
        kind, ref = item["kind"], item["id"]
        legacy = bool(item["legacy"])
        attempt_session = "legacy" if legacy else session
        table = {"thread": "threads", "run": "runs", "step": "steps"}[kind]
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (ref,)).fetchone()
        old = conn.execute("SELECT * FROM activity_nodes WHERE id=?", (ref,)).fetchone()
        start = stamp(row["started_at"]) if row and kind != "thread" else None
        if old and (row is None or old["started"] != start):
            abandoned = conn.execute(
                """SELECT a.*,s.checkpoint FROM activity_attempts a
                JOIN activity_sessions s ON s.id=a.session
                WHERE a.id=? AND a.finished IS NULL""",
                (old["attempt"],),
            ).fetchone()
            if abandoned:
                _interrupt(conn, abandoned, abandoned["checkpoint"])
        if row is None:
            conn.execute(
                "UPDATE activity_nodes SET current=0,revision=? WHERE id=?",
                (revision, ref),
            )
            conn.execute(
                "DELETE FROM activity_dirty WHERE kind=? AND id=?", (kind, ref)
            )
            continue
        thread, root, parent, scopes = _ownership(conn, kind, ref, row)
        finish = stamp(row["finished_at"]) if kind != "thread" else None
        changed = max(
            filter(
                lambda x: x is not None,
                (
                    stamp(row["created_at"]),
                    start,
                    finish,
                    stamp(row["updated_at"]) if kind == "thread" else None,
                ),
            ),
            default=0,
        )
        if kind == "run":
            control = conn.execute(
                "SELECT created_at FROM controls WHERE id=?", (row["control"],)
            ).fetchone()
            if control:
                changed = max(changed, stamp(control[0]) or 0)
        child = conn.execute(
            "SELECT MAX(changed) FROM activity_nodes WHERE parent=? AND current=1",
            (ref,),
        ).fetchone()
        changed = max(changed, child[0] or 0)
        attempt_id = (
            old["attempt"]
            if old and old["current"] and old["started"] == start
            else None
        )
        attempt = (
            conn.execute(
                "SELECT * FROM activity_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if attempt_id
            else None
        )
        if start is not None and kind != "thread":
            call_kind = row["kind"] if kind == "step" else "run"
            if attempt is None:
                cursor = conn.execute(
                    "INSERT INTO activity_attempts(ref,session,kind,started,complete) VALUES (?,?,?,?,?)",
                    (
                        ref,
                        attempt_session,
                        call_kind,
                        start,
                        not legacy and bool(thread),
                    ),
                )
                attempt_id = cursor.lastrowid
                for scope in scopes:
                    duration = scope == ref or (
                        kind == "run" and parent is None and scope in {thread, "@agent"}
                    )
                    conn.execute(
                        "INSERT INTO activity_owners VALUES (?,?,?)",
                        (scope, attempt_id, duration),
                    )
            else:
                _buckets(conn, attempt, -1)
            cost, estimated, partial = None, False, False
            if call_kind == "model" and finish is not None:
                step = store.get_step(ref=StepRef.parse(ref))
                accounting = (
                    step.noted.accounting
                    if step and isinstance(step.noted, ModelStepNoted)
                    else None
                )
                cost = selected_usd_cost(accounting)
                estimated = bool(accounting and accounting.selected == "estimated")
                selected = (
                    accounting.estimate
                    if estimated and accounting
                    else accounting.reported
                    if accounting
                    else None
                )
                partial = bool(selected and not selected.complete)
            conn.execute(
                "UPDATE activity_attempts SET finished=CASE WHEN interrupted=0 THEN ? ELSE finished END,cost=?,estimated=?,partial=? WHERE id=?",
                (
                    start if legacy and finish is None else finish,
                    cost,
                    estimated,
                    partial,
                    attempt_id,
                ),
            )
            _buckets(
                conn,
                conn.execute(
                    "SELECT * FROM activity_attempts WHERE id=?", (attempt_id,)
                ).fetchone(),
                1,
            )
        title, summary = (
            (ref, "") if kind == "thread" else _summary(store, kind, ref, row)
        )
        status = "idle" if kind == "thread" else row["status"]
        occur = json.loads(row["occur"]) if kind != "thread" and row["occur"] else {}
        item, lane, iteration = (
            occur.get(key) or {} for key in ("item", "lane", "iteration")
        )
        position = (
            json.dumps([int(index) for index in row["path"].split(".")])
            if kind == "step"
            else json.dumps(
                [
                    iteration.get("index", 0),
                    1 if iteration.get("phase") == "until" else 0,
                    item.get("index", 0),
                    lane.get("index", 0),
                ]
            )
        )
        expected = item.get("count", 1) * lane.get("count", 1) if item or lane else 0
        if root:
            conn.execute(
                "INSERT OR REPLACE INTO activity_search VALUES (?,?,?,?,?,?)",
                (root, ref, attempt_id or 0, title, summary, status),
            )
        conn.execute(
            """INSERT INTO activity_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,thread=excluded.thread,
            root=excluded.root,parent=excluded.parent,title=excluded.title,summary=excluded.summary,
            status=excluded.status,changed=MAX(changed,excluded.changed),started=excluded.started,
            finished=excluded.finished,attempt=excluded.attempt,current=1,revision=excluded.revision,
            position=excluded.position,expected=excluded.expected""",
            (
                ref,
                kind,
                thread,
                root,
                parent,
                title,
                summary,
                status,
                stamp(row["created_at"]) or 0,
                changed,
                start,
                finish,
                attempt_id,
                1,
                revision,
                position,
                expected,
            ),
        )
        for scope in scopes[1:]:
            conn.execute(
                "UPDATE activity_nodes SET changed=MAX(changed,?),revision=? WHERE id=?",
                (changed, revision, scope),
            )
        conn.execute("DELETE FROM activity_dirty WHERE kind=? AND id=?", (kind, ref))
    if session != "legacy":
        conn.execute(
            "UPDATE activity_sessions SET checkpoint=? WHERE id=?",
            (stamp(utc_now()), session),
        )


def start_session(store: RunStore, session: str, at: str) -> None:
    conn = store._conn
    with store.write_transaction():
        for previous in conn.execute(
            "SELECT * FROM activity_sessions WHERE ended IS NULL"
        ).fetchall():
            for attempt in conn.execute(
                "SELECT * FROM activity_attempts WHERE session=? AND finished IS NULL",
                (previous["id"],),
            ).fetchall():
                _interrupt(conn, attempt, previous["checkpoint"])
            conn.execute(
                "UPDATE activity_sessions SET ended=checkpoint,complete=0 WHERE id=?",
                (previous["id"],),
            )
        now = stamp(at)
        conn.execute(
            "INSERT INTO activity_sessions(id,started,checkpoint) VALUES (?,?,?)",
            (session, now, now),
        )
        conn.execute(
            "UPDATE activity_meta SET session=?,revision=revision+1", (session,)
        )


def checkpoint(store: RunStore, session: str, at: str, *, end: bool = False) -> None:
    with store.write_transaction():
        store._conn.execute(
            "UPDATE activity_sessions SET checkpoint=?,ended=CASE WHEN ? THEN ? ELSE ended END WHERE id=?",
            (stamp(at), end, stamp(at), session),
        )
        if end:
            for attempt in store._conn.execute(
                "SELECT * FROM activity_attempts WHERE session=? AND finished IS NULL",
                (session,),
            ).fetchall():
                _interrupt(store._conn, attempt, stamp(at) or attempt["started"])
            store._conn.execute("UPDATE activity_meta SET revision=revision+1")
