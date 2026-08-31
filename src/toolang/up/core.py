"""Process-local core services for one agent."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.setup import SetupWatcher
from toolang.state.watcher import StateWatcher


class AgentCore:
    """Own the core services shared by agent callers in one process."""

    __slots__ = (
        "executor",
        "history",
        "ids",
        "layout",
        "setup",
        "state",
        "store",
        "threads",
    )

    def __init__(
        self,
        layout: AgentLayout,
        *,
        sandbox: str = "host",
        ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        default_overrides: Mapping[str, str | None] | None = None,
        limit_overrides: Mapping[str, int | Decimal | None] | None = None,
    ) -> None:
        self.layout = layout
        self.store = RunStore(layout.run_store)
        self.ids = IdIssuer(layout.id_state)
        self.threads = ThreadManager(self.store, self.ids)
        self.history = RunHistory(self.store)
        allow_overrides = dict(ceiling_overrides or {})
        self.setup = SetupWatcher(
            layout,
            sandbox=sandbox,
            allow_overrides={
                name: value
                for name, value in allow_overrides.items()
                if name in {"models", "tools"}
            },
            default_overrides=default_overrides,
            limit_overrides=limit_overrides,
        )
        self.state = StateWatcher(
            layout,
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
            load_state=lambda revision: self.state.load(revision),
            refresh_state=self.state.refresh_result,
        )
        self.executor.start()

    async def close(self) -> None:
        """Stop local execution and close durable storage."""

        await self.executor.stop()
        self.store.close()
