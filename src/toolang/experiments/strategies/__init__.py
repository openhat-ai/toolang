"""Run strategy loading and built-in strategies."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import cache
from importlib.metadata import entry_points
from typing import Any, cast

from ..base.error import ToolangError
from ..base.protocols.strategy import StrategyPlugin


_BASIC_STRATEGY_NAME = "basic"


@cache
def load_run_strategies() -> dict[str, StrategyPlugin]:
    """Load installed run strategies plus the built-in baseline."""

    from .basic import STRATEGY as BASIC_STRATEGY

    strategies: dict[str, StrategyPlugin] = {BASIC_STRATEGY.name: BASIC_STRATEGY}
    for entry_point in entry_points(group="toolang.run_strategy"):
        try:
            factory = cast(Callable[[Mapping[str, Any]], StrategyPlugin], entry_point.load())
        except ModuleNotFoundError:
            continue
        strategy = factory({})
        if strategy.name in strategies:
            continue
        strategies[strategy.name] = strategy
    return strategies


def normalize_run_strategy_name(name: str) -> str:
    """Normalize one user-facing run-strategy name."""

    text = name.strip()
    if not text:
        return _BASIC_STRATEGY_NAME
    return text


def load_run_strategy(name: str) -> StrategyPlugin:
    """Load one installed run strategy by name."""

    normalized = normalize_run_strategy_name(name)
    strategy = load_run_strategies().get(normalized)
    if strategy is not None:
        return strategy
    raise ToolangError(f"unknown run strategy: {name}")


__all__ = [
    "StrategyPlugin",
    "load_run_strategy",
    "load_run_strategies",
    "normalize_run_strategy_name",
]
