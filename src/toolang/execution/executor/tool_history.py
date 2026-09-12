"""Read one agent's records without sharing the executor's Store connection."""

from contextlib import closing
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import TypeAdapter

from toolang.base.protocols.tool import ToolHistory

from ..inspection.history import RunHistory
from toolang.lang.types import Value

from ..records import (
    local_value_to_data,
    CancelControlPayload,
    ControlRecord,
    ExecuteControlPayload,
    RunControlPayload,
    SteerControlPayload,
    StepRecord,
)
from ..inspection.views import RunView
from ..schemas import HistoryToolCursor, Record, record_to_data
from ..store import RunStore
from ..inspection.views import ThreadView
from ..types import ContentRef, RunRef, StepRef, ThreadRef, output_to_protocol_data


_CURSOR = TypeAdapter(HistoryToolCursor)


class _ToolHistory(ToolHistory):
    def __init__(self, db_path: Path, thread: str) -> None:
        self._db_path = db_path.resolve()
        self._thread = thread

    def read_threads(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        saved = self._decode("read_threads", cursor)
        with closing(RunStore(self._db_path, read_only=True)) as store:
            page = RunHistory(store).thread_page(limit=limit, cursor=saved)
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
        with closing(RunStore(self._db_path, read_only=True)) as store:
            history = RunHistory(store)
            page = (
                history.next_page(saved)
                if saved is not None
                else history.thread_view(
                    ThreadRef.parse(thread) if thread is not None else self._thread,
                    begin=RunRef.parse(begin) if begin is not None else None,
                    end=RunRef.parse(end) if end is not None else None,
                    limit=limit,
                    reverse=from_end,
                    include_children=False,
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
        with (
            closing(RunStore(self._db_path, read_only=True)) as store,
            store.read_transaction(),
        ):
            history = RunHistory(store)
            if saved is not None:
                page = history.next_page(saved)
            else:
                if run is None:
                    raise ValueError("history requires a run reference")
                page = history.run_view(
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
                "entries": [_record_data(store, r) for r in page.entries],
                "dependencies": [_record_data(store, r) for r in page.dependencies],
                "cursor": self._encode("read_steps", page.cursor),
            }

    def read_output(self, *, run: str) -> dict[str, Any]:
        target = RunRef.parse(run)
        with (
            closing(RunStore(self._db_path, read_only=True)) as store,
            store.read_transaction(),
        ):
            record = store.get_run(run_id=str(target))
            if record is None:
                raise KeyError(str(target))
            output = RunHistory(store).get_output(target)
            return {
                "run": str(target),
                "status": record.status,
                "output": output_to_protocol_data(output)
                if output is not None
                else None,
            }

    def _decode(self, tool: str, cursor: str | None) -> str | None:
        if cursor is None:
            return None
        # Existing tool results may still hold a self-contained cursor.
        data: bytes | str | None = cursor
        if not cursor.startswith("{"):
            with closing(RunStore(self._db_path, read_only=True)) as store:
                data = store.get_content(ContentRef.parse(cursor))
        if data is None:
            raise ValueError("history cursor is unavailable for this agent")
        saved = _CURSOR.validate_json(data)
        if saved.store != str(self._db_path) or saved.tool != tool:
            raise ValueError("history cursor belongs to another agent or tool")
        return saved.cursor

    def _encode(
        self,
        tool: Literal["read_threads", "read_runs", "read_steps"],
        cursor: str | None,
    ) -> str | None:
        if cursor is None:
            return None
        data = _CURSOR.dump_json(HistoryToolCursor(str(self._db_path), tool, cursor))
        # Cache the immutable snapshot, not execution facts. Models copy a short
        # reference; continuations do not depend on process-local reader state.
        with closing(RunStore(self._db_path)) as cache:
            return str(cache.put_content(data))


def _record_data(store: RunStore, record: Record) -> dict[str, object]:
    data = record_to_data(record)
    if isinstance(record, StepRecord) and record.output is not None:
        data["output"] = output_to_protocol_data(store.resolve_output(record.output))
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
            "input": {
                name: local_value_to_data(cast(Value, store.resolve_value(value)))
                for name, value in record.payload.input.items()
            },
        }
    return data
