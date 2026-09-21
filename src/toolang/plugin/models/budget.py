"""Resolve one model call's output allowance and input admission budget."""

from __future__ import annotations

from collections.abc import Mapping

from toolang.base.types.model import Reasoning


def output_budget(
    limits: Mapping[str, int],
    *,
    demand: int | None = None,
    reasoning: Reasoning | None = None,
    automatic_target: int = 4096,
    context_fraction: int = 4,
    reasoning_headroom: int = 1024,
) -> int:
    """Resolve an output allowance from normalized facts and consumer policy."""

    if demand is not None and (type(demand) is not int or demand <= 0):
        raise ValueError("max_output must be a positive integer")
    for name, value in limits.items():
        if type(value) is not int or value <= 0:
            raise ValueError(f"model limit.{name} must be a positive integer")
    for value in (automatic_target, context_fraction, reasoning_headroom):
        if type(value) is not int or value <= 0:
            raise ValueError("output policy values must be positive integers")
    thinking = reasoning.budget_tokens if reasoning is not None else None
    budget = demand
    if budget is None:
        budget = automatic_target
        if (context := limits.get("context")) is not None:
            budget = min(budget, context // context_fraction)
        if thinking is not None:
            budget = max(budget, thinking + reasoning_headroom)
    if (limit := limits.get("output")) is not None:
        budget = min(budget, limit)
    if budget <= 0:
        raise ValueError("max_output must be a positive integer")
    if thinking is not None and budget <= thinking:
        raise ValueError(
            f"max_output {budget} must exceed the reasoning budget {thinking}"
        )
    return budget


def context_capacity(limits: Mapping[str, int]) -> int | None:
    """Return the provider's joint input+output window, when known."""

    return limits.get("context")


def input_budget(limits: Mapping[str, int], output: int | None) -> int | None:
    """Reserve inclusive output and a 5% (at least 1024 token) input margin."""

    independent = limits.get("input")
    capacities: list[int] = []
    available: list[int] = []
    context = limits.get("context")
    if context is not None:
        if output is None:
            raise ValueError(
                "known context requires max_output or a model output limit"
            )
        capacities.append(context)
        available.append(context - output)
    if type(independent) is int and independent > 0:
        capacities.append(independent)
        available.append(independent)
    if not capacities:
        return None
    capacity = min(capacities)
    budget = min(available) - max(1024, (capacity + 19) // 20)
    if budget <= 0:
        raise ValueError(
            "model output allowance and estimation margin leave no input budget"
        )
    return budget
