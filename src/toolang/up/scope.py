"""Compose Top membership independently of runtime liveness and read scheduling."""

from pathlib import Path

from toolang.common.layout import AgentLayout

from .discovery import agent_layouts
from .observation import RuntimeInventory, RuntimeSource, runtime_inventory
from .types import RuntimeInstance, RuntimeInstanceSnapshot


class ScopeReader:
    """Discover the complete roster; failed enumeration cannot prove departure."""

    def __init__(self, root: Path, *, agent: str | None) -> None:
        self.root = root.expanduser().resolve()
        self.agent = agent
        self._homes: tuple[AgentLayout, ...] = ()
        self._registered: dict[str, RuntimeSource] = {}

    def read(self) -> RuntimeInventory:
        diagnostics = []
        try:
            self._homes = agent_layouts(self.root, agent=self.agent)
        except OSError as exc:
            diagnostics.append(f"resident homes: {exc}")
        try:
            runtime = runtime_inventory(self.root, agent=self.agent, read_only=True)
        except (OSError, ValueError, RuntimeError) as exc:
            diagnostics.append(f"runtime registry: {exc}")
        else:
            current = {source.instance.identity: source for source in runtime.sources}
            diagnostics.extend(runtime.snapshot.diagnostics)
            if runtime.snapshot.diagnostics:
                # Coverage is unknown, so absence cannot retire an old binding.
                self._registered.update(current)
            else:
                self._registered = current
        scoped = {}
        for layout in self._homes:
            instance = RuntimeInstance(
                layout.name,
                "resident",
                layout.instance,
                layout.instance_name,
                "not running",
                (),
            )
            scoped[instance.identity] = RuntimeSource(
                instance, layout, None, None, None
            )
        scoped.update(self._registered)
        sources = tuple(
            sorted(
                scoped.values(),
                key=lambda source: (
                    source.instance.label.casefold(),
                    source.instance.identity,
                ),
            )
        )
        return RuntimeInventory(
            RuntimeInstanceSnapshot(
                tuple(source.instance for source in sources), tuple(diagnostics)
            ),
            sources,
        )
