"""Model selection helpers over resolved catalog models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from toolang.base.errors import ToolangError
from toolang.base.types.model import Model, Reasoning
from toolang.plugin.models.collections import ModelCollection


def build_model_collection(models: Sequence[Model]) -> ModelCollection:
    """Compile resolved catalog models into one immutable effective collection."""

    return ModelCollection(tuple(models))


def model_reasoning_controls(model: Model) -> tuple[Mapping[str, object], ...]:
    """Return one model's advertised reasoning options in catalog order."""

    return tuple(model.reasoning_options or ())


def model_reasoning_efforts(model: Model) -> tuple[str, ...]:
    """Return every advertised reasoning effort level in catalog order."""

    values: list[str] = []
    for option in model_reasoning_controls(model):
        if option.get("type") != "effort":
            continue
        raw = option.get("values")
        if not isinstance(raw, list | tuple):
            continue
        for value in raw:
            if isinstance(value, str) and value not in values:
                values.append(value)
    return tuple(values)


def model_reasoning_effort_exhaustive(model: Model) -> bool:
    """Return whether the source explicitly closes its effort enumeration."""

    return any(
        option.get("type") == "effort" and option.get("exhaustive") is True
        for option in model_reasoning_controls(model)
    )


def model_reasoning_effort_applicable(model: Model) -> bool | None:
    """Report known reasoning applicability without promoting unknown facts."""

    if any(
        option.get("type") in {"effort", "budget_tokens", "toggle"}
        for option in model_reasoning_controls(model)
    ):
        return True
    return model.reasoning


def resolve_model_reasoning(
    model: Model,
    reasoning: Reasoning | None,
) -> Reasoning | None:
    """Validate one request's reasoning demand and return the effective control."""

    if reasoning is None:
        return None
    effort = reasoning.effort
    budget = reasoning.budget_tokens
    if effort is None and budget is None:
        return None
    if model.reasoning is False and not model_reasoning_controls(model):
        raise ToolangError(f"model {model.ref} does not advertise reasoning controls")
    request: dict[str, object] = {}
    if effort is not None:
        request["effort"] = effort
    if budget is not None:
        request["budget_tokens"] = budget
    _validate_reasoning_request(request, model=model)
    return reasoning


def _validate_reasoning_request(
    request: Mapping[str, object],
    *,
    model: Model,
) -> None:
    options = model_reasoning_controls(model)
    unknown = set(request) - {"effort", "budget_tokens"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ToolangError(
            f"model {model.ref} has unknown reasoning controls: {joined}"
        )
    effort = request.get("effort")
    if effort is not None:
        if not isinstance(effort, str):
            raise ToolangError(f"model {model.ref} reasoning effort must be a level")
        effort_options = tuple(
            option for option in options if option.get("type") == "effort"
        )
        allowed: list[str] = []
        for option in effort_options:
            values = option.get("values")
            if not isinstance(values, list | tuple):
                continue
            for value in values:
                if isinstance(value, str) and value not in allowed:
                    allowed.append(value)
        # Catalog enumerations are evidence: they reject locally only when the
        # source marks them exhaustive. Otherwise the provider decides.
        exhaustive = model_reasoning_effort_exhaustive(model)
        if effort != "none" and exhaustive and effort not in allowed:
            joined = ", ".join(allowed) or "none"
            raise ToolangError(
                f"model {model.ref} does not advertise reasoning effort "
                f"{effort!r} (allowed: {joined})"
            )
    budget = request.get("budget_tokens")
    if budget is not None:
        budget_options = tuple(
            option for option in options if option.get("type") == "budget_tokens"
        )
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ToolangError(
                f"model {model.ref} does not advertise this reasoning token budget"
            )
        minimums = tuple(
            value
            for option in budget_options
            for value in (option.get("min"),)
            if isinstance(value, int) and not isinstance(value, bool)
        )
        if minimums and budget < min(minimums):
            raise ToolangError(
                f"model {model.ref} reasoning budget must be at least {min(minimums)}"
            )
        maximums = tuple(
            value
            for option in budget_options
            for value in (option.get("max"),)
            if type(value) is int
        )
        if maximums and budget > max(maximums):
            raise ToolangError(
                f"model {model.ref} reasoning budget must be at most {max(maximums)}"
            )
