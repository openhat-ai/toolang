"""File request trigger for inbox directories."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from collections.abc import Callable

from toolang.base.types.message import Message
from toolang.base.types.policy import RunBindings
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.records import RunRecord, execution_error_message
from toolang.lang.input import RunnableInput
from toolang.state.state import AgentState
from toolang.setup import AgentSetup
from toolang.work import files
from toolang.work.records import FileRequestRecord
from toolang.work.types import FileSnapshot

DEFAULT_INTERVAL_MS = 1_000.0
DEFAULT_STABLE_MS = 500.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FileSubmission:
    """One claimed file request ready for execution."""

    record: FileRequestRecord
    input: Message


def spawn(
    *,
    layout: AgentLayout,
    executor: RunExecutor,
    get_agent_setup: Callable[[], AgentSetup],
    get_agent_state: Callable[[], AgentState],
    inboxes: tuple[Path, ...],
    interval_ms: float,
    stable_ms: float,
    stop_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the file request trigger in one background task."""

    return asyncio.create_task(
        run(
            layout=layout,
            executor=executor,
            get_agent_setup=get_agent_setup,
            get_agent_state=get_agent_state,
            inboxes=inboxes,
            interval_ms=interval_ms,
            stable_ms=stable_ms,
            stop_signal=stop_signal,
        )
    )


async def run(
    *,
    layout: AgentLayout,
    executor: RunExecutor,
    get_agent_setup: Callable[[], AgentSetup],
    get_agent_state: Callable[[], AgentState],
    inboxes: tuple[Path, ...],
    interval_ms: float,
    stable_ms: float,
    stop_signal: asyncio.Event,
) -> None:
    """Scan inbox directories and start unseen file requests."""

    interval_timeout = interval_ms / 1000
    logger.debug(
        "files.started root=%s agent=%s interval_ms=%s inboxes=%s",
        layout.root,
        layout.name,
        int(interval_ms),
        ",".join(str(path) for path in inboxes) or "-",
    )
    active: dict[str, asyncio.Task[RunRecord]] = {}
    store = files.open_file_request_store(layout)
    try:
        while True:
            now = datetime.now(timezone.utc)
            _record_completed_runs(
                store,
                active,
                now=now,
            )
            for submission in collect_file_submissions(
                store, inboxes=inboxes, stable_ms=stable_ms, now=now
            ):
                if (
                    executor.store.get_thread(thread_id=submission.record.thread_id)
                    is None
                ):
                    executor.store.create_thread(
                        thread_id=submission.record.thread_id,
                        origin="file",
                        context={"file_request_id": submission.record.request_id},
                    )
                setup = get_agent_setup()
                state = get_agent_state()
                handle = executor.start(
                    RunSpec(
                        setup=setup,
                        state=state,
                        thread=submission.record.thread_id,
                        bindings=RunBindings(
                            runnable=(
                                "agic:file"
                                if state.program.find_agic("file") is not None
                                else "agic:default"
                            ),
                            model=setup.bindings.model,
                        ),
                        limits=setup.limits,
                        input=RunnableInput(primary=submission.input.percept),
                    )
                )
                try:
                    store.bind_run(
                        request_id=submission.record.request_id,
                        run_id=handle.run_id,
                        now=now,
                    )
                except Exception:
                    try:
                        handle.stop(reason="File run binding failed.")
                    except (RuntimeError, ValueError):
                        pass
                    raise
                active[handle.run_id] = handle.task
            try:
                await asyncio.wait_for(stop_signal.wait(), timeout=interval_timeout)
            except TimeoutError:
                continue
            else:
                return
    finally:
        if active:
            await asyncio.gather(*active.values(), return_exceptions=True)
            _record_completed_runs(store, active, now=datetime.now(timezone.utc))
        store.close()


def collect_file_submissions(
    store: files.FileRequestStore,
    *,
    inboxes: tuple[Path, ...],
    stable_ms: float,
    now: datetime | None = None,
) -> list[FileSubmission]:
    """Return unseen stable files claimed for processing."""

    current = now or datetime.now(timezone.utc)
    submissions: list[FileSubmission] = []
    for inbox in inboxes:
        for snapshot in _scan_inbox(inbox, now=current, stable_ms=stable_ms):
            try:
                message = files.file_input_message(Path(snapshot.absolute_path))
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug(
                    "files.input_skipped path=%s error=%s", snapshot.absolute_path, exc
                )
                continue
            thread_id = files.file_thread_id(snapshot.absolute_path)
            record = store.claim(snapshot, thread_id=thread_id, now=current)
            if record is None:
                continue
            submissions.append(
                FileSubmission(
                    record=record,
                    input=message,
                )
            )
    return submissions


def _scan_inbox(
    inbox: Path,
    *,
    now: datetime,
    stable_ms: float,
) -> tuple[FileSnapshot, ...]:
    try:
        root = inbox.expanduser().resolve()
    except OSError as exc:
        logger.warning("files.inbox_unreadable inbox=%s error=%s", inbox, exc)
        return ()
    if not root.is_dir():
        logger.warning("files.inbox_missing inbox=%s", root)
        return ()
    snapshots: list[FileSnapshot] = []
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
            fingerprint = files.fingerprint_file(path)
            relative_path = path.relative_to(root).as_posix()
        except OSError as exc:
            logger.debug("files.hash_skipped path=%s error=%s", path, exc)
            continue
        snapshots.append(
            FileSnapshot(
                watch_root=str(root),
                relative_path=relative_path,
                absolute_path=str(path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                fingerprint=fingerprint,
            )
        )
    return tuple(snapshots)


def _record_completed_runs(
    store: files.FileRequestStore,
    active: dict[str, asyncio.Task[RunRecord]],
    *,
    now: datetime,
) -> None:
    for run_id, task in tuple(active.items()):
        if not task.done():
            continue
        active.pop(run_id, None)
        try:
            run = task.result()
        except Exception as exc:
            status = "failed"
            error = str(exc) or type(exc).__name__
        else:
            status = run.status
            error = execution_error_message(run.error)
        store.finish_run(
            run_id=run_id,
            run_status=status,
            error=error,
            now=now,
        )


def _file_age_ms(mtime_ns: int, *, now: datetime) -> float:
    return max((now.timestamp() * 1_000_000_000 - mtime_ns) / 1_000_000, 0.0)
