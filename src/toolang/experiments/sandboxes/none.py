"""Direct local sandbox plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..base.protocols.sandbox import SandboxPlugin
from ..base.types.sandbox import (
    SandboxPlan,
    SandboxSelector,
    SandboxStartRequest,
    SandboxStartResult,
    SandboxState,
)


@dataclass(slots=True)
class NoneSandbox:
    """Sandbox plugin that runs directly on the current host."""

    config: dict[str, Any]
    name: str = "none"

    def resolve_selector(
        self,
        raw_selector: str | None,
        *,
        configured_selector: SandboxSelector | None = None,
    ) -> SandboxSelector:
        if raw_selector is not None:
            selector = SandboxSelector.parse(raw_selector)
            if selector.driver != self.name:
                raise ValueError(f"sandbox selector does not match plugin {self.name}: {raw_selector}")
            return SandboxSelector(driver=self.name)
        if configured_selector is not None:
            if configured_selector.driver != self.name:
                raise ValueError(
                    f"configured sandbox selector does not match plugin {self.name}: "
                    f"{configured_selector.render()}"
                )
            return SandboxSelector(driver=self.name)
        return SandboxSelector(driver=self.name)

    def prepare(self, request: SandboxStartRequest) -> SandboxPlan:
        return SandboxPlan(
            selector=request.selector,
            start_mode="direct",
            sandbox_root=request.sandbox_root,
            sandbox_home=request.sandbox_home,
            sandbox_working_directory=request.sandbox_home,
            run_command=request.run_command,
            run_shell_command=request.run_shell_command,
            env_vars=dict(request.env_vars),
            state=SandboxState(
                selector=request.selector,
                meta={
                    "endpoint": request.endpoint,
                    "local_host": request.local_host,
                    "port": request.port,
                },
            ),
        )

    def start(self, plan: SandboxPlan) -> SandboxStartResult:
        return SandboxStartResult(
            state=plan.state
            or SandboxState(selector=plan.selector),
            endpoint=(plan.state.meta.get("endpoint") if plan.state is not None else None),
            meta={"start_mode": plan.start_mode},
        )

    def alive(self, state: SandboxState) -> bool:
        del state
        return True

    def stop(self, state: SandboxState, *, force: bool = False) -> None:
        del state, force
        return None


def create_sandbox(config: Mapping[str, Any]) -> SandboxPlugin:
    """Create the built-in direct sandbox plugin."""

    return NoneSandbox(dict(config))
