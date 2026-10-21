"""Resident execution tree; no persistence, event consumption or recovery."""

from __future__ import annotations

from dataclasses import replace

from toolang.base.money import add_cost

from .accounting import selected_cost_is_approximate, selected_usd_cost
from .event_projection import step_info
from .events import ModelStepInfo, ToolStepInfo
from .records import StepRecord
from .schemas import LiveNode, LiveTreeSnapshot, RunNode, StepNode
from .types import (
    CallCounters,
    ModelStepReport,
    Occurrence,
    RunReport,
    RunStatus,
    StepKind,
    StepReport,
    StepRef,
    TokenUsage,
    ToolStepReport,
)

_TERMINAL = {"succeeded", "failed", "canceled"}


class LiveTree:
    """One root's bounded current structure, updated under its executor gate."""

    def __init__(self, run_id: str, thread_id: str) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self._nodes: dict[str, LiveNode] = {}
        self._children: dict[str, dict[str, None]] = {}

    @property
    def terminal(self) -> bool:
        return self._nodes[self.run_id].status in _TERMINAL

    def accept_run(
        self,
        run_id: str,
        *,
        parent: StepRef | None,
        runnable: str,
        runnable_kind: str = "",
        created_at: str,
        occurrence: Occurrence | None,
    ) -> None:
        if run_id in self._nodes:
            return  # Continuing the same resident compaction Run.
        parent_id = (
            str(parent) if parent is not None and run_id != self.run_id else None
        )
        self._add(
            RunNode(
                run_id,
                parent_id,
                runnable[:240],
                "pending",
                created_at,
                occurrence=occurrence,
                runnable_kind=runnable_kind,
            )
        )

    def begin_run(
        self, run_id: str, runnable: str, started_at: str, *, runnable_kind: str = ""
    ) -> None:
        node = self._run(run_id)
        self._nodes[run_id] = replace(
            node,
            runnable=runnable[:240],
            runnable_kind=runnable_kind or node.runnable_kind,
            status="running",
            started_at=node.started_at or started_at,
        )

    def begin_step(self, record: StepRecord) -> None:
        info = step_info(record.spec)
        name = info.model if isinstance(info, ModelStepInfo) else info.name
        target = (
            info.target
            if isinstance(info, ToolStepInfo)
            else ""
            if isinstance(info, ModelStepInfo)
            else info.runnable or info.handle
        )
        self._add(
            StepNode(
                record.id,
                str(record.parent) if record.parent else record.run_id,
                record.kind,
                getattr(info, "kind", record.kind),
                record.status,
                record.started_at,
                record.started_at,
                name=name[:240],
                plugin=info.plugin[:240] if isinstance(info, ToolStepInfo) else "",
                target=target[:240],
                summary=info.summary[:240] if isinstance(info, ToolStepInfo) else "",
                occurrence=record.occur,
                asynchronous=getattr(info, "asynchronous", False),
            )
        )

    def invoke(self, step: StepRef, kind: StepKind) -> None:
        node = self._step(step)
        if node.invoked or node.kind != kind or kind not in {"model", "tool"}:
            raise RuntimeError(f"invalid live invocation: {step}")
        self._nodes[node.id] = replace(node, invoked=True)
        for run in self._runs_above(str(step)):
            self._nodes[run.id] = replace(
                run,
                calls=CallCounters(
                    run.calls.model_calls + (kind == "model"),
                    run.calls.tool_calls + (kind == "tool"),
                ),
            )

    def note_model(self, step: StepRef, report: ModelStepReport) -> None:
        node = self._step(step)
        cost = selected_usd_cost(report.cost)
        approximate = selected_cost_is_approximate(report.cost)
        self._nodes[node.id] = replace(
            node, usage=report.usage, cost=cost, cost_approximate=approximate
        )
        if report.usage is None and report.cost is None:
            return
        assert report.usage is not None, "model cost requires provider token usage"
        for run in self._runs_above(node.id):
            # A missing initial total means no contributions. Thereafter None
            # represents an unknown contribution and remains unknown.
            first = run.usage is None
            self._nodes[run.id] = replace(
                run,
                usage=_add_usage(run.usage, report.usage),
                cost=cost
                if first
                else add_cost(run.cost, cost)
                if run.cost is not None and cost is not None
                else None,
                cost_approximate=run.cost_approximate or approximate,
            )

    def terminal_report(self, step: StepRef, report: StepReport) -> StepReport:
        node = self._nodes.get(str(step))
        if node is None:
            # A committed receipt or pre-restart compact Step adds no resident facts.
            return report
        assert isinstance(node, StepNode)
        if node.kind == "model":
            return replace(
                report if isinstance(report, ModelStepReport) else ModelStepReport(),
                invoked=node.invoked,
            )
        if node.kind == "tool":
            return replace(
                report
                if isinstance(report, ToolStepReport)
                else ToolStepReport(
                    node.summary.strip() or node.name or node.operation
                ),
                invoked=node.invoked,
            )
        return report

    def run_report(self, run_id: str) -> RunReport:
        node = self._run(run_id)
        return RunReport(node.calls, node.usage, node.cost, node.cost_approximate)

    def end_step(self, record: StepRecord) -> None:
        node = self._nodes.get(record.id)
        if node is None:
            return  # A repeated committed receipt is not another transition.
        self._nodes[record.id] = replace(
            node, status=record.status, finished_at=record.finished_at
        )
        self._prune(record.id)

    def end_run(self, run_id: str, status: RunStatus, finished_at: str) -> None:
        node = self._run(run_id)
        self._nodes[run_id] = replace(node, status=status, finished_at=finished_at)
        # Owned children have drained before Run termination. Retain no closed
        # branch even if an exceptional exit left a Step without its own end.
        for child in tuple(self._children[run_id]):
            self._discard(child)
        self._prune(run_id)

    def snapshot(self) -> LiveTreeSnapshot:
        nodes: list[LiveNode] = []

        def visit(identity: str) -> None:
            nodes.append(self._nodes[identity])
            for child in self._children[identity]:
                visit(child)

        visit(self.run_id)
        return LiveTreeSnapshot(self.run_id, self.thread_id, tuple(nodes))

    def _add(self, node: LiveNode) -> None:
        if node.id in self._nodes:
            raise RuntimeError(f"duplicate live node: {node.id}")
        if node.parent is not None:
            self._children[node.parent][node.id] = None
        self._nodes[node.id] = node
        self._children[node.id] = {}

    def _run(self, run_id: str) -> RunNode:
        node = self._nodes[run_id]
        if not isinstance(node, RunNode):
            raise RuntimeError(f"expected live Run: {run_id}")
        return node

    def _step(self, step: StepRef) -> StepNode:
        node = self._nodes[str(step)]
        if not isinstance(node, StepNode):
            raise RuntimeError(f"expected live Step: {step}")
        return node

    def _runs_above(self, identity: str) -> list[RunNode]:
        runs: list[RunNode] = []
        node = self._nodes[identity]
        while node.parent is not None:
            node = self._nodes[node.parent]
            if isinstance(node, RunNode):
                runs.append(node)
        return runs

    def _discard(self, identity: str) -> None:
        for child in tuple(self._children[identity]):
            self._discard(child)
        node = self._nodes.pop(identity)
        self._children.pop(identity)
        if node.parent is not None:
            self._children[node.parent].pop(identity)

    def _prune(self, identity: str) -> None:
        while identity != self.run_id:
            node = self._nodes[identity]
            if node.status not in _TERMINAL or self._children[identity]:
                return
            parent = node.parent
            self._discard(identity)
            if parent is None:
                return
            identity = parent


def _add_usage(left: TokenUsage | None, right: TokenUsage | None) -> TokenUsage | None:
    if left is None:
        return right
    if right is None:
        return left
    return TokenUsage(
        left.input_tokens + right.input_tokens,
        left.output_tokens + right.output_tokens,
        left.cache_read_tokens + right.cache_read_tokens
        if left.cache_read_tokens is not None and right.cache_read_tokens is not None
        else None,
        left.reasoning_tokens + right.reasoning_tokens
        if left.reasoning_tokens is not None and right.reasoning_tokens is not None
        else None,
    )
