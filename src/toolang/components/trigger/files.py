"""File request trigger for inbox directories."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ... import file_requests
from ...execution.input import allocate_run_id
from ...execution.records import RunStatus
from ...execution.runner import RunRequest

if TYPE_CHECKING:
    from ...up import UptimeContext
    from ...execution.runner import RunOutcome

DEFAULT_INTERVAL_MS = 1_000.0
DEFAULT_STABLE_MS = 500.0
logger = logging.getLogger("toolang.files")


@dataclass(frozen=True, slots=True)
class FileSubmission:
    """One claimed file request ready for the runtime queue."""

    record: file_requests.FileRequestRecord
    run_id: str
    text: str
    parts: list[dict[str, str]]


def spawn(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the file request trigger in one background task."""

    return asyncio.create_task(run(context, stop_signal=stop_signal))


async def run(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
) -> None:
    """Scan inbox directories and enqueue unseen file requests."""

    interval_value = context.config.require("components.trigger.file.interval_ms")
    if not isinstance(interval_value, int | float):
        raise TypeError("invalid config: components.trigger.file.interval_ms")
    interval_timeout = float(interval_value) / 1000
    inboxes = _configured_inboxes(context)
    logger.debug(
        "files.started root=%s agent=%s interval_ms=%s inboxes=%s",
        context.root,
        context.name,
        int(float(interval_value)),
        ",".join(str(path) for path in inboxes) or "-",
    )
    seen_completed: set[str] = set()
    store = file_requests.open_file_request_store(context.root, context.name)
    try:
        while True:
            now = datetime.now(timezone.utc)
            _record_completed_runs(
                context,
                store,
                context.runner.completed(),
                seen_completed=seen_completed,
                now=now,
            )
            for submission in collect_file_submissions(context, store, now=now):
                context.runner.enqueue(
                    RunRequest(
                        group="file",
                        origin="file",
                        run_id=submission.run_id,
                        thread_id=submission.record.thread_id,
                        thunk=submission.text,
                        thunk_name="file",
                        metadata={
                            "invoke_parts": submission.parts,
                            "file_request": {
                                "id": submission.record.request_id,
                                "watch_root": submission.record.watch_root,
                                "relative_path": submission.record.relative_path,
                                "path": submission.record.absolute_path,
                                "size": submission.record.size,
                                "mtime_ns": submission.record.mtime_ns,
                                "fingerprint": submission.record.fingerprint,
                            },
                        },
                    )
                )
            try:
                await asyncio.wait_for(stop_signal.wait(), timeout=interval_timeout)
            except TimeoutError:
                continue
            else:
                return
    finally:
        store.close()


def collect_file_submissions(
    context: UptimeContext,
    store: file_requests.FileRequestStore,
    *,
    now: datetime | None = None,
) -> list[FileSubmission]:
    """Return unseen stable files claimed for processing."""

    current = now or datetime.now(timezone.utc)
    stable_ms = _stable_ms(context)
    submissions: list[FileSubmission] = []
    for inbox in _configured_inboxes(context):
        for snapshot in _scan_inbox(inbox, now=current, stable_ms=stable_ms):
            try:
                text, parts = file_requests.render_file_input(Path(snapshot.absolute_path))
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug("files.input_skipped path=%s error=%s", snapshot.absolute_path, exc)
                continue
            run_id = allocate_run_id(context)
            thread_id = file_requests.file_thread_id(snapshot.absolute_path)
            record = store.claim(snapshot, run_id=run_id, thread_id=thread_id, now=current)
            if record is None:
                continue
            submissions.append(
                FileSubmission(
                    record=record,
                    run_id=run_id,
                    text=text,
                    parts=parts,
                )
            )
    return submissions


def _scan_inbox(
    inbox: Path,
    *,
    now: datetime,
    stable_ms: float,
) -> tuple[file_requests.FileSnapshot, ...]:
    try:
        root = inbox.expanduser().resolve()
    except OSError as exc:
        logger.warning("files.inbox_unreadable inbox=%s error=%s", inbox, exc)
        return ()
    if not root.is_dir():
        logger.warning("files.inbox_missing inbox=%s", root)
        return ()
    snapshots: list[file_requests.FileSnapshot] = []
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError as exc:
            logger.debug("files.path_skipped path=%s error=%s", path, exc)
            continue
        if _file_age_ms(stat.st_mtime_ns, now=now) < stable_ms:
            continue
        try:
            fingerprint = file_requests.fingerprint_file(path)
            relative_path = path.relative_to(root).as_posix()
        except OSError as exc:
            logger.debug("files.hash_skipped path=%s error=%s", path, exc)
            continue
        snapshots.append(
            file_requests.FileSnapshot(
                watch_root=str(root),
                relative_path=relative_path,
                absolute_path=str(path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                fingerprint=fingerprint,
            )
        )
    return tuple(snapshots)


def _stable_ms(context: UptimeContext) -> float:
    value = context.config.require("components.trigger.file.stable_ms")
    if not isinstance(value, int | float):
        raise TypeError("invalid config: components.trigger.file.stable_ms")
    return float(value)


def _configured_inboxes(context: UptimeContext) -> tuple[Path, ...]:
    value = context.config.require("components.trigger.file.inboxes")
    if not isinstance(value, tuple):
        raise TypeError("invalid config: components.trigger.file.inboxes")
    inboxes: list[Path] = []
    for item in value:
        if isinstance(item, Path):
            inboxes.append(item)
            continue
        if isinstance(item, str):
            inboxes.append(Path(item))
            continue
        raise TypeError("invalid config: components.trigger.file.inboxes")
    return tuple(inboxes)


def _record_completed_runs(
    context: UptimeContext,
    store: file_requests.FileRequestStore,
    results: list[RunOutcome],
    *,
    seen_completed: set[str],
    now: datetime,
) -> None:
    for result in results:
        if result.run_id in seen_completed:
            continue
        seen_completed.add(result.run_id)
        if result.origin != "file":
            continue
        stored = context.store.get_run(run_id=result.run_id)
        status: RunStatus = stored.status if stored is not None else _outcome_status(result.status)
        store.finish_run(
            run_id=result.run_id,
            run_status=status,
            error=result.error,
            now=now,
        )


def _outcome_status(status: str) -> RunStatus:
    return "finished" if status == "finished" else "failed"


def _file_age_ms(mtime_ns: int, *, now: datetime) -> float:
    return max((now.timestamp() * 1_000_000_000 - mtime_ns) / 1_000_000, 0.0)
