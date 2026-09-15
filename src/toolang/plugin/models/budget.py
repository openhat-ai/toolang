"""Resolve one model call's output allowance and input admission budget."""

from collections.abc import Mapping

from toolang.base.types.model import ModelInfo, ModelTarget


def output_budget(target: ModelTarget, info: ModelInfo) -> int | None:
    """Resolve the inclusive output allowance for one model call.

    `None` means the call imposes no explicit allowance, so the provider applies
    its native maximum output allowance.
    """

    budget = target.max_output
    if budget is None:
        budget = info.max_output_tokens
    elif info.max_output_tokens is not None:
        budget = min(budget, info.max_output_tokens)
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0
    ):
        raise ValueError("max_output must be a positive integer")
    thinking = target.reasoning.get("budget_tokens")
    if (
        isinstance(thinking, int)
        and not isinstance(thinking, bool)
        and budget is not None
        and budget <= thinking
    ):
        raise ValueError(
            f"max_output {budget} must exceed the reasoning budget {thinking}"
        )
    return budget


def context_capacity(info: ModelInfo) -> int | None:
    """Return the provider's joint input+output window, when known."""

    return info.context_window


def input_budget(info: ModelInfo) -> int | None:
    """Reserve a 5% (at least 1024 token) estimation margin inside capacity."""

    limits = info.metadata.get("limit", {})
    independent = limits.get("input") if isinstance(limits, Mapping) else None
    capacities = []
    if info.context_window is not None:
        capacities.append(info.context_window)
    if type(independent) is int and independent > 0:
        capacities.append(independent)
    if not capacities:
        return None
    capacity = min(capacities)
    return max(0, capacity - max(1024, (capacity + 19) // 20))
