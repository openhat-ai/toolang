"""Bind record readers to one agent Store and the caller's default Thread."""

from typing import Any, Literal, cast

from pydantic import TypeAdapter

from toolang.base.protocols.tool import ToolHistory

from ..history import RunHistory
from ..records import (
    CancelControlPayload,
    ControlRecord,
    ExecuteControlPayload,
    RunControlPayload,
    SteerControlPayload,
    StepRecord,
)
from ..run_view import RunView
from ..schemas import HistoryToolCursor, Record, record_to_data
from ..store import RunStore
from ..thread_view import ThreadView
from ..types import RunRef, StepRef, ThreadRef, local_to_protocol_data


_CURSOR = TypeAdapter(HistoryToolCursor)


class _ToolHistory(ToolHistory):
    def __init__(self, store: RunStore, thread: str) -> None:
        self._store = store
        self._thread = thread
        self._history = RunHistory(store)
        self._identity = str(store.db_path.resolve())

    def read_threads(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        page = self._history.thread_page(
            limit=limit, cursor=self._decode("read_threads", cursor)
        )
        return {
            "threads": [record_to_data(r) for r in page.threads],
            "cursor": self._encode("read_threads", page.cursor),
        }

    def read_runs(
        self,
        *,
        thread: str | None = None,
        begin: str | None = None,
        end: str | None = None,
        limit: int = 20,
        from_end: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        saved = self._decode("read_runs", cursor)
        page = (
            self._history.next_page(saved)
            if saved is not None
            else self._history.thread_view(
                ThreadRef.parse(thread) if thread is not None else self._thread,
                begin=RunRef.parse(begin) if begin is not None else None,
                end=RunRef.parse(end) if end is not None else None,
                limit=limit,
                reverse=from_end,
            )
        )
        if not isinstance(page, ThreadView):
            raise ValueError("history cursor does not select Runs")
        return {
            "thread": page.record.id,
            "head": str(page.head),
            "runs": [record_to_data(run) for run in page.runs()],
            "cursor": self._encode("read_runs", page.cursor),
        }

    def read_steps(
        self,
        *,
        run: str | None = None,
        begin: str | None = None,
        end: str | None = None,
        limit: int = 20,
        from_end: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        saved = self._decode("read_steps", cursor)
        with self._store.read_transaction():
            if saved is not None:
                page = self._history.next_page(saved)
            else:
                if run is None:
                    raise ValueError("history requires a run reference")
                page = self._history.run_view(
                    RunRef.parse(run),
                    begin=StepRef.parse(begin) if begin is not None else None,
                    end=StepRef.parse(end) if end is not None else None,
                    limit=limit,
                    reverse=from_end,
                )
            if not isinstance(page, RunView):
                raise ValueError("history cursor does not select Steps")
            return {
                "run": record_to_data(page.record),
                "entries": [self._record_data(r) for r in page.entries],
                "dependencies": [self._record_data(r) for r in page.dependencies],
                "cursor": self._encode("read_steps", page.cursor),
            }

    def read_output(self, *, run: str) -> dict[str, Any]:
        target = RunRef.parse(run)
        with self._store.read_transaction():
            record = self._store.get_run(run_id=str(target))
            if record is None:
                raise KeyError(str(target))
            output = self._history.get_output(target)
            return {
                "run": str(target),
                "status": record.status,
                "output": local_to_protocol_data(output)
                if output is not None
                else None,
            }

    def _record_data(self, record: Record) -> dict[str, object]:
        data = record_to_data(record)
        if isinstance(record, StepRecord) and record.output is not None:
            data["output"] = local_to_protocol_data(
                self._store.resolve_local(record.output)
            )
        elif isinstance(record, ControlRecord) and isinstance(
            record.payload,
            (
                RunControlPayload,
                ExecuteControlPayload,
                SteerControlPayload,
                CancelControlPayload,
            ),
        ):
            data["payload"] = {
                **cast(dict[str, object], data["payload"]),
                "input": [
                    local_to_protocol_data(self._store.resolve_local(local))
                    for local in record.payload.input
                ],
            }
        return data

    def _decode(self, tool: str, cursor: str | None) -> str | None:
        if cursor is None:
            return None
        saved = _CURSOR.validate_json(cursor)
        if saved.store != self._identity or saved.tool != tool:
            raise ValueError("history cursor belongs to another agent or tool")
        return saved.cursor

    def _encode(
        self,
        tool: Literal["read_threads", "read_runs", "read_steps"],
        cursor: str | None,
    ) -> str | None:
        return (
            _CURSOR.dump_json(HistoryToolCursor(self._identity, tool, cursor)).decode()
            if cursor is not None
            else None
        )
