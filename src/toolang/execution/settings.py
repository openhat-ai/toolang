"""Immutable runnable configuration inherited independently of resource ceilings."""

from __future__ import annotations

from .types import PromptSetting, RunnableSettings

from toolang.lang.ast import AgicDecl, FlowDecl


def resolve_settings(
    runnable: AgicDecl | FlowDecl,
    module: str,
    parent: RunnableSettings | None = None,
) -> RunnableSettings:
    """Override explicit settings; retain the declaring module of inherited text."""
    base = parent or RunnableSettings()
    directives = {item.name: item.values for item in runnable.directives}

    def prompt(kind: str) -> PromptSetting:
        value = getattr(runnable, kind)
        inherited = getattr(base, kind)
        return (
            (inherited or PromptSetting(module))
            if value is None
            else PromptSetting(module, value)
        )

    return RunnableSettings(
        lanes=int(directives["lanes"][0]) if "lanes" in directives else base.lanes,
        recall=directives.get("recall", base.recall),
        hands=directives.get("hands", base.hands),
        handoffs=directives.get("handoffs", base.handoffs),
        instruct=prompt("instruct"),
        context=prompt("context"),
    )
