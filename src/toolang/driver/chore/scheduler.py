"""Event-driven chore scheduling on a dedicated owner loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import itertools
import logging
import threading
from typing import Literal, TypeAlias

from toolang.base.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.input import parse_call
from toolang.execution.calls import resolve_spec
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.errors import ThreadBusy
from toolang.execution.records import RunRecord
from toolang.lang.includes import resolve_file_include
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

from .records import ChoreRecord
from toolang.catalog.chores import Chore
from .store import ChoreStore, next_activation

DEFAULT_SAFETY_REFRESH_SECONDS = 30.0
DEFAULT_STATE_POLL_SECONDS = 1.0
_LOGGER = logging.getLogger(__name__)

_CommandKind: TypeAlias = Literal[
    "refresh",
    "run_chore",
    "cancel_chore",
    "get",
    "pause",
    "stop",
]


@dataclass(frozen=True, slots=True)
class _Command:
    kind: _CommandKind
    value: object = None
    result: Future[object] | None = None


class ChoreDriver:
    """Own chore checkpoints and timers outside the execution event loop."""

    def __init__(
        self,
        *,
        layout: AgentLayout,
        executor: RunExecutor,
        ids: IdIssuer,
        get_agent_setup: Callable[[], AgentSetup],
        get_agent_state: Callable[[], AgentState],
        safety_refresh_seconds: float = DEFAULT_SAFETY_REFRESH_SECONDS,
        state_poll_seconds: float = DEFAULT_STATE_POLL_SECONDS,
    ) -> None:
        self.layout = layout
        self.executor = executor
        self.ids = ids
        self.get_agent_setup = get_agent_setup
        self.get_agent_state = get_agent_state
        self.safety_refresh_seconds = safety_refresh_seconds
        self.state_poll_seconds = state_poll_seconds
        self._execution_loop: asyncio.AbstractEventLoop | None = None
        self._scheduler_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started: Future[object] | None = None
        self._commands: asyncio.Queue[_Command] | None = None
        self._store: ChoreStore | None = None
        self._chores: dict[str, Chore] = {}
        self._records: dict[str, ChoreRecord] = {}
        self._tokens: dict[str, int] = {}
        self._heap: list[tuple[float, int, str, int]] = []
        self._sequence = itertools.count()
        self._active: dict[str, asyncio.Task[None]] = {}
        self._state_revision = ""
        self._paused = False
        self._stopping = False

    async def start(self) -> None:
        """Start the dedicated scheduler thread from the execution loop."""

        if self._thread is not None:
            raise RuntimeError("chore scheduler is already started")
        self._execution_loop = asyncio.get_running_loop()
        self._started = Future()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"toolang-chores-{self.layout.name}",
            daemon=True,
        )
        self._thread.start()
        await asyncio.wrap_future(self._started)

    async def refresh(self) -> None:
        """Refresh authored and program chores on the scheduler loop."""

        await self._request("refresh")

    def refresh_sync(self) -> None:
        """Refresh chores from a blocking API or CLI worker thread."""

        self._request_sync("refresh")

    async def run_chore(self, chore_id: str) -> ChoreRecord:
        """Request one coalesced manual chore occurrence."""

        return _as_record(await self._request("run_chore", chore_id))

    def run_chore_sync(self, chore_id: str) -> ChoreRecord:
        """Request a manual chore run from a blocking caller thread."""

        return _as_record(self._request_sync("run_chore", chore_id))

    async def cancel_chore(self, chore_id: str) -> ChoreRecord:
        """Cancel a pending chore or return its active checkpoint."""

        return _as_record(await self._request("cancel_chore", chore_id))

    def cancel_chore_sync(self, chore_id: str) -> ChoreRecord:
        """Cancel or inspect a chore from a blocking caller thread."""

        return _as_record(self._request_sync("cancel_chore", chore_id))

    def get_sync(self, chore_id: str) -> ChoreRecord | None:
        """Return one in-memory checkpoint to a blocking caller thread."""

        value = self._request_sync("get", chore_id)
        if value is not None and not isinstance(value, ChoreRecord):
            raise TypeError("scheduler get did not return a ChoreRecord")
        return value

    async def get(self, chore_id: str) -> ChoreRecord | None:
        """Return one in-memory checkpoint without blocking the caller loop."""

        value = await self._request("get", chore_id)
        if value is not None and not isinstance(value, ChoreRecord):
            raise TypeError("scheduler get did not return a ChoreRecord")
        return value

    async def pause(self) -> None:
        """Stop new dispatch while retaining completion handling."""

        thread = self._thread
        loop = self._scheduler_loop
        if thread is None or not thread.is_alive() or loop is None:
            return
        try:
            await self._request("pause")
        except RuntimeError:
            if thread.is_alive() and loop.is_running():
                raise

    async def stop(self) -> None:
        """Stop and join the scheduler owner thread."""

        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            loop = self._scheduler_loop
            if loop is not None and loop.is_running():
                try:
                    await self._request("stop")
                except RuntimeError:
                    if thread.is_alive() and loop.is_running():
                        raise
            await asyncio.to_thread(thread.join)
        self._thread = None
        self._scheduler_loop = None

    async def _request(self, kind: _CommandKind, value: object = None) -> object:
        return await asyncio.wrap_future(self._submit_request(kind, value))

    def _request_sync(self, kind: _CommandKind, value: object = None) -> object:
        return self._submit_request(kind, value).result()

    def _submit_request(
        self,
        kind: _CommandKind,
        value: object = None,
    ) -> Future[object]:
        loop = self._scheduler_loop
        queue = self._commands
        if loop is None or queue is None or not loop.is_running():
            raise RuntimeError("chore scheduler is not running")
        result: Future[object] = Future()
        command = _Command(kind=kind, value=value, result=result)
        loop.call_soon_threadsafe(queue.put_nowait, command)
        return result

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._scheduler_loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:
            started = self._started
            if started is not None and not started.done():
                started.set_exception(exc)
            else:
                _LOGGER.exception("chore scheduler stopped unexpectedly")
        finally:
            loop.close()

    async def _serve(self) -> None:
        self._commands = asyncio.Queue()
        self._store = ChoreStore(self.layout.chore_store)
        try:
            self._reconcile()
            await self._recover_active()
            started = self._started
            if started is not None and not started.done():
                started.set_result(None)
            loop = asyncio.get_running_loop()
            next_safety = loop.time() + self.safety_refresh_seconds
            next_state = loop.time() + self.state_poll_seconds
            while not self._stopping:
                if not self._paused:
                    self._dispatch_due()
                now = loop.time()
                timeout = min(
                    self._heap_timeout(),
                    max(0.0, next_safety - now),
                    max(0.0, next_state - now),
                )
                try:
                    command = await asyncio.wait_for(
                        self._commands.get(),
                        timeout=timeout,
                    )
                except TimeoutError:
                    now = loop.time()
                    if now >= next_state:
                        next_state = now + self.state_poll_seconds
                        state = self.get_agent_state()
                        if state.revision != self._state_revision:
                            self._reconcile()
                    if now >= next_safety:
                        next_safety = now + self.safety_refresh_seconds
                        self._reconcile()
                    continue
                await self._handle(command)
        finally:
            if self._active:
                await asyncio.gather(
                    *tuple(self._active.values()),
                    return_exceptions=True,
                )
            if self._store is not None:
                self._store.close()
            self._store = None

    async def _handle(self, command: _Command) -> None:
        try:
            value = await self._apply(command)
        except BaseException as exc:
            if command.result is not None and not command.result.done():
                command.result.set_exception(exc)
            elif isinstance(exc, Exception):
                _LOGGER.error(
                    "chores.command_failed kind=%s error=%s",
                    command.kind,
                    str(exc) or type(exc).__name__,
                )
            else:
                raise
        else:
            if command.result is not None and not command.result.done():
                command.result.set_result(value)

    async def _apply(self, command: _Command) -> object:
        assert self._store is not None
        if command.kind == "refresh":
            self._reconcile()
            return None
        if command.kind in {"run_chore", "cancel_chore"}:
            self._reconcile()
            reference = str(command.value)
            chore_id = next(
                (
                    item.id
                    for item in self._records.values()
                    if reference in (item.id, item.name, f"chore/{item.name}")
                ),
                reference,
            )
            if command.kind == "run_chore" and chore_id not in self._chores:
                raise FileNotFoundError(f"chore not found: {chore_id}")
            if command.kind == "run_chore":
                record = self._store.request_manual_chore(chore_id=chore_id)
            else:
                current = self._records.get(chore_id)
                if current is None:
                    raise FileNotFoundError(f"chore not found: {chore_id}")
                record = (
                    current
                    if current.active_run is not None
                    else self._store.cancel_pending_chore(chore_id=chore_id)
                )
            self._replace_record(record)
            return record
        if command.kind == "get":
            reference = str(command.value)
            chore_id = next(
                (
                    item.id
                    for item in self._records.values()
                    if reference in (item.id, item.name, f"chore/{item.name}")
                ),
                reference,
            )
            return self._records.get(chore_id)
        if command.kind == "pause":
            self._paused = True
            return None
        if command.kind == "stop":
            self._paused = True
            self._stopping = True
            return None
        raise ValueError(f"unsupported scheduler command: {command.kind}")

    def _reconcile(self, *, state: AgentState | None = None) -> None:
        assert self._store is not None
        state = self.get_agent_state() if state is None else state
        chores = {chore.id: chore for chore in state.chores}
        records = self._store.reconcile(chores=chores)
        self._chores = chores
        self._state_revision = state.revision
        self._set_records(records)

    def _set_records(self, records: tuple[ChoreRecord, ...]) -> None:
        previous_ids = set(self._records)
        self._records = {record.id: record for record in records}
        for chore_id in previous_ids | set(self._records):
            self._invalidate(chore_id)
        for chore_id in self._records:
            self._push(chore_id)

    async def _recover_active(self) -> None:
        assert self._store is not None
        for record in tuple(self._records.values()):
            run_id = record.active_run
            if run_id is None:
                continue
            run = await self._get_run(run_id)
            if run is None:
                restored = self._store.release_claim(run_id=run_id)
                if restored is not None:
                    self._replace_record(restored)
                continue
            if run.status in {"pending", "running"}:
                blocked = self._store.mark_recovery_blocked(
                    run_id=run_id,
                    error="accepted run was interrupted outside this executor",
                )
                if blocked is not None:
                    self._replace_record(blocked)
                _LOGGER.error("chores.recovery_blocked run_id=%s", run_id)
                continue
            repaired = self._store.finish_run(
                chores=self._chores, run_id=run_id, run_status=run.status
            )
            if repaired is None:
                self._drop_record(record.id)
            else:
                self._replace_record(repaired)
        self._set_records(self._store.reconcile(chores=self._chores))

    async def _get_run(self, run_id: str) -> RunRecord | None:
        loop = self._execution_loop
        if loop is None:
            raise RuntimeError("execution loop is unavailable")

        async def get() -> RunRecord | None:
            return self.executor.store.get_run(run_id=run_id)

        future = asyncio.run_coroutine_threadsafe(get(), loop)
        return await asyncio.wrap_future(future)

    def _dispatch_due(self) -> None:
        assert self._store is not None
        state = self.get_agent_state()
        setup = self.get_agent_setup()
        if state.revision != self._state_revision:
            self._reconcile(state=state)
        now = datetime.now(timezone.utc)
        while self._heap:
            timestamp, _sequence, chore_id, token = self._heap[0]
            if token != self._tokens.get(chore_id):
                heapq.heappop(self._heap)
                continue
            if timestamp > now.timestamp():
                return
            heapq.heappop(self._heap)
            record = self._records.get(chore_id)
            chore = self._chores.get(chore_id)
            activation = next_activation(record) if record is not None else None
            if record is None or chore is None or activation is None:
                continue
            due_at, trigger = activation
            if due_at > now:
                self._push(chore_id)
                continue
            try:
                spec = self._build_spec(chore, setup=setup, state=state)
            except (OSError, ToolangError, ValueError) as exc:
                error = str(exc) or type(exc).__name__
                _LOGGER.warning(
                    "chores.submission_rejected kind=%s chore_id=%s source=%s error=%s",
                    "chore",
                    chore.id,
                    chore.location,
                    error,
                )
                rejected = self._store.reject_activation(
                    chore=chore,
                    trigger=trigger,
                    error=error,
                    now=now,
                )
                self._replace_record(rejected)
                continue
            run_id = self.ids.issue_run()
            claimed = self._store.claim(
                chore=chore,
                trigger=trigger,
                run_id=run_id,
                now=now,
            )
            self._replace_record(claimed)
            self._submit_run(run_id, chore, spec)

    def _build_spec(
        self,
        chore: Chore,
        *,
        setup: AgentSetup | None = None,
        state: AgentState | None = None,
    ) -> RunSpec:
        setup = self.get_agent_setup() if setup is None else setup
        state = self.get_agent_state() if state is None else state
        override, input = parse_call(chore.body)
        base = chore.path.parent
        spec = resolve_spec(
            override,
            input,
            setup=setup,
            state=state,
            thread=chore.thread_id,
            driver="chore",
            include=lambda reference: resolve_file_include(reference, base=base),
        )
        return spec

    def _submit_run(self, run_id: str, chore: Chore, spec: RunSpec) -> None:
        execution_loop = self._execution_loop
        if execution_loop is None:
            raise RuntimeError("scheduler event loops are unavailable")

        async def execute() -> RunRecord:
            thread = self.executor.store.get_thread(thread_id=chore.thread_id)
            if thread is None:
                self.executor.store.create_thread(
                    thread_id=chore.thread_id,
                    origin="chore",
                )
            return await self.executor.run(spec, run_id=run_id)

        future = asyncio.run_coroutine_threadsafe(execute(), execution_loop)
        waiter = asyncio.create_task(
            self._wait_run(run_id, future),
            name=f"toolang-chore-run-{run_id}",
        )
        self._active[run_id] = waiter

    async def _wait_run(
        self,
        run_id: str,
        future: Future[RunRecord],
    ) -> None:
        assert self._store is not None
        try:
            run = await asyncio.wrap_future(future)
        except ThreadBusy:
            # Keep the claimed trigger while backing off; other chores continue.
            await asyncio.sleep(DEFAULT_STATE_POLL_SECONDS)
            record = self._store.release_claim(run_id=run_id)
        except Exception as exc:
            record = self._store.reject_claim(
                run_id=run_id,
                error=str(exc) or type(exc).__name__,
            )
        else:
            record = self._store.finish_run(
                chores=self._chores,
                run_id=run_id,
                run_status=run.status,
            )
        finally:
            self._active.pop(run_id, None)
        if record is None:
            previous = next(
                (item for item in self._records.values() if item.active_run == run_id),
                None,
            )
            if previous is not None:
                self._drop_record(previous.id)
        else:
            self._replace_record(record)

    def _replace_record(self, record: ChoreRecord) -> None:
        self._records[record.id] = record
        self._invalidate(record.id)
        self._push(record.id)

    def _drop_record(self, chore_id: str) -> None:
        self._records.pop(chore_id, None)
        self._invalidate(chore_id)

    def _invalidate(self, chore_id: str) -> None:
        self._tokens[chore_id] = self._tokens.get(chore_id, 0) + 1

    def _push(self, chore_id: str) -> None:
        record = self._records.get(chore_id)
        activation = next_activation(record) if record is not None else None
        if activation is None or chore_id not in self._chores:
            return
        due_at, _trigger = activation
        heapq.heappush(
            self._heap,
            (
                due_at.timestamp(),
                next(self._sequence),
                chore_id,
                self._tokens[chore_id],
            ),
        )

    def _heap_timeout(self) -> float:
        while self._heap:
            timestamp, _sequence, chore_id, token = self._heap[0]
            if token != self._tokens.get(chore_id):
                heapq.heappop(self._heap)
                continue
            return max(0.0, timestamp - datetime.now(timezone.utc).timestamp())
        return max(self.safety_refresh_seconds, self.state_poll_seconds)


def _as_record(value: object) -> ChoreRecord:
    if not isinstance(value, ChoreRecord):
        raise TypeError("scheduler command did not return a ChoreRecord")
    return value
