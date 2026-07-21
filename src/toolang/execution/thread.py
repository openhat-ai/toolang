"""Thread lifecycle operations over durable execution state."""

from __future__ import annotations

from dataclasses import dataclass
import json

from toolang.base.types.message import Message

from .binding import allocate_thread_id
from .executor import Executor
from .records import RunRecord, ThreadPeer, ThreadRecord


@dataclass(frozen=True, slots=True)
class ThreadChange:
    """Internal thread mutation result needed by runtime orchestration."""

    run_id: str | None
    thread_id: str


class ThreadOperations:
    """Create and branch threads using one process's run executor."""

    def __init__(self, executor: Executor) -> None:
        self.executor = executor

    def create(
        self, *, kind: str = "chat", peer: ThreadPeer | None = None
    ) -> ThreadRecord:
        """Allocate and persist one empty chat thread."""

        return self.executor.store.ensure_thread(
            thread_id=allocate_thread_id(self.executor.id_state_path, kind),
            origin="chat",
            peer=peer,
        )

    async def rewind(
        self,
        *,
        run_id: str,
        message: Message | None = None,
    ) -> ThreadChange:
        """Supersede an eligible chat thread from one anchor run."""

        anchor = self._branchable_run(run_id)
        new_run_id = self.executor.allocate_run_id() if message is not None else None
        await self._cancel_replaced_runs(anchor, reason="Run was rewound.")
        superseded = self.executor.store.supersede_thread_from_run(
            run_id=anchor.run_id,
            superseded={
                "type": "rewound",
                "by": new_run_id,
                "from_run_id": anchor.run_id,
            },
        )
        result = ThreadChange(
            run_id=new_run_id,
            thread_id=anchor.thread_id,
        )
        self.executor.store.append_event(
            domain="thread",
            domain_id=anchor.thread_id,
            type="thread_rewind",
            payload={
                "from_run_id": anchor.run_id,
                "new_run_id": result.run_id,
                "thread_id": result.thread_id,
                "superseded_run_ids": [item.run_id for item in superseded],
                **({"message": message.to_data()} if message is not None else {}),
            },
        )
        return result

    def fork(
        self,
        *,
        run_id: str,
        message: Message | None = None,
        include_anchor: bool = False,
    ) -> ThreadChange:
        """Create one chat thread branched from an eligible anchor run."""

        anchor = self._branchable_run(run_id)
        prefix = anchor.thread_id.split("_", 1)[0].strip() or "thread"
        thread_id = allocate_thread_id(self.executor.id_state_path, prefix)
        new_run_id = self.executor.allocate_run_id() if message is not None else None
        self.executor.store.ensure_thread(
            thread_id=thread_id,
            origin="chat",
            parent=json.dumps(
                {
                    "type": "fork",
                    "thread_id": anchor.thread_id,
                    "from_run_id": anchor.run_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        source_runs = self.executor.store.list_thread_runs_before(run_id=anchor.run_id)
        if include_anchor:
            source_runs = (*source_runs, anchor)
        copied = self.executor.store.copy_runs_to_thread(
            source_run_ids=tuple(run.run_id for run in source_runs),
            target_thread_id=thread_id,
            target_run_ids=tuple(self.executor.allocate_run_id() for _ in source_runs),
        )
        result = ThreadChange(
            run_id=new_run_id,
            thread_id=thread_id,
        )
        payload: dict[str, object] = {
            "run_id": result.run_id,
            "thread_id": result.thread_id,
            "source_thread_id": anchor.thread_id,
            "from_run_id": anchor.run_id,
            "include_anchor": include_anchor,
            "copied_run_ids": [run.run_id for run in copied],
            "message": message.to_data() if message is not None else None,
        }
        self.executor.store.append_event(
            domain="thread",
            domain_id=anchor.thread_id,
            type="thread_fork",
            payload=payload,
        )
        self.executor.store.append_event(
            domain="thread",
            domain_id=thread_id,
            type="thread_forked",
            payload=payload,
        )
        return result

    def _branchable_run(self, run_id: str) -> RunRecord:
        run = self.executor.store.get_run(run_id=run_id)
        if run is None:
            raise FileNotFoundError(f"run not found: {run_id}")
        thread = self.executor.store.get_thread(thread_id=run.thread_id)
        origin = thread.origin if thread is not None else run.origin
        if run.thread_id.startswith(("task_", "chore_")) or origin != "chat":
            raise ValueError(f"thread cannot be branched: {run.thread_id}")
        return run

    async def _cancel_replaced_runs(self, anchor: RunRecord, *, reason: str) -> None:
        runs = [
            item
            for item in sorted(
                self.executor.store.list_runs(thread_id=anchor.thread_id, limit=None),
                key=lambda run: run.created_at,
            )
            if item.created_at >= anchor.created_at and item.status == "running"
        ]
        for run in runs:
            await self.executor.stop(run_id=run.run_id, reason=reason)
