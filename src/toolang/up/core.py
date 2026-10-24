"""Process-local core services for one agent."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from toolang.base.types.model import ModelOverride
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor
from toolang.execution.inspection.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.setup import SetupWatcher
from toolang.state.watcher import StateWatcher
from toolang.state.state import AgentState
from .types import ALL_DRIVERS, ExecutionDrivers
from .lifecycle import ServerLifecycle


class AgentCore:
    """Own the core services shared by agent callers in one process."""

    __slots__ = (
        "drivers",
        "executor",
        "history",
        "ids",
        "layout",
        "lifecycle",
        "setup",
        "state",
        "store",
        "threads",
        "_owns_store",
        "_closed",
    )

    def __init__(
        self,
        layout: AgentLayout,
        *,
        sandbox: str = "host",
        model_catalog: Path | None = None,
        lifecycle: ServerLifecycle | None = None,
        drivers: ExecutionDrivers = ALL_DRIVERS,
        ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        default_overrides: Mapping[str, ModelOverride | str | None] | None = None,
        limit_overrides: Mapping[str, int | float | None] | None = None,
        compact_override: ModelOverride | None = None,
        workspace_additions: Mapping[str, str] | None = None,
        workdir: str | None = None,
        store: RunStore | None = None,
        ids: IdIssuer | None = None,
        initial_state: AgentState | None = None,
        start_executor: bool = True,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        from .source import restore_program_acquisition
        from .replay import require_complete_seed

        require_complete_seed(layout)
        layout = restore_program_acquisition(layout)
        self.layout = layout
        self.lifecycle = lifecycle
        self.drivers = drivers
        self._owns_store = store is None
        self._closed = False
        self.store = store or RunStore(layout.run_store)
        self.ids = ids or IdIssuer(layout.run_store, agent_name=layout.name)
        self.history = RunHistory(self.store)
        allow_overrides = dict(ceiling_overrides or {})
        self.setup = SetupWatcher(
            layout,
            environ=environ,
            model_catalog=model_catalog,
            sandbox=sandbox,
            allow_overrides={
                name: value
                for name, value in allow_overrides.items()
                if name in {"models", "tools"}
            },
            default_overrides=default_overrides,
            limit_overrides=limit_overrides,
            compact_override=compact_override,
        )
        self.state = StateWatcher(
            layout,
            initial_state=initial_state,
            workspace_additions=workspace_additions,
            allow_overrides={
                name: value
                for name, value in allow_overrides.items()
                if name in {"psyches", "skills", "services", "prompts"}
            },
        )
        self.executor = RunExecutor(
            self.store,
            self.ids,
            setup=lambda: self.setup.current(),
            state=lambda: self.state.current(),
            sync_state=self.state.sync,
            default_workdir=workdir,
            admission=lifecycle.admit if lifecycle is not None else None,
        )
        self.threads = ThreadManager(self.store, self.ids, stream=self.executor.stream)
        if start_executor:
            self.executor.start()

    def resource_counts(self) -> dict[str, object]:
        """Save non-sensitive summaries for inspection after source removal."""
        setup, state = self.setup.current(), self.state.current()
        tools = setup.tools()
        return {
            "models": len(setup.models_effective()),
            "providers": len(setup.providers_effective()),
            "tools": len(tools),
            "toolsets": len({ref.partition("/")[0] for ref in tools.refs()}),
            "caps": {
                kind + "s": sum(item.kind == kind for item in state.caps_for("agent"))
                for kind in ("psyche", "skill", "service", "prompt")
            },
            "chores": len(state.chores),
            "workspaces": list(setup.workspace_grants(state.workspaces)),
        }

    async def close(self, *, reason: str | None = None) -> None:
        """Stop local execution and close durable storage."""

        if self._closed:
            return
        self._closed = True
        if reason is None:
            await self.executor.stop()
        else:
            await self.executor.stop(reason=reason)
        if self._owns_store:
            self.store.close()
