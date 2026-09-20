"""Resolve one model call's output allowance and input admission budget."""

from __future__ import annotations

from toolang.base.types.model import Model, Reasoning


def output_budget(
    model: Model,
    *,
    demand: int | None = None,
    reasoning: Reasoning | None = None,
) -> int | None:
    """Resolve the inclusive output allowance for one model call.

    `None` means the call imposes no explicit allowance, so the provider applies
    its native maximum output allowance.
    """

    limit = model.limit.get("output")
    if demand is None:
        budget = limit
    elif limit is None:
        budget = demand
    else:
        budget = min(demand, limit)
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0
    ):
        raise ValueError("max_output must be a positive integer")
    thinking = reasoning.budget_tokens if reasoning is not None else None
    if thinking is not None and budget is not None and budget <= thinking:
        raise ValueError(
            f"max_output {budget} must exceed the reasoning budget {thinking}"
        )
    return budget


def context_capacity(model: Model) -> int | None:
    """Return the provider's joint input+output window, when known."""

    return model.limit.get("context")


def input_budget(model: Model) -> int | None:
    """Reserve a 5% (at least 1024 token) estimation margin inside capacity."""

    independent = model.limit.get("input")
    capacities: list[int] = []
    context = model.limit.get("context")
    if context is not None:
        capacities.append(context)
    if type(independent) is int and independent > 0:
        capacities.append(independent)
    if not capacities:
        return None
    capacity = min(capacities)
    return max(0, capacity - max(1024, (capacity + 19) // 20))
