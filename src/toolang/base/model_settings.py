"""Parse and apply the shared human-facing model setting body."""

from __future__ import annotations

from dataclasses import replace
import re
import shlex
from collections.abc import Sequence
from typing import cast

from toolang.base.types.model import (
    ModelEffort,
    ModelMaxOutput,
    ModelOverride,
    ModelRequest,
    Reasoning,
    ReasoningEffort,
)

_BUDGET_RE = re.compile(r"0|[1-9][0-9]*\Z")
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


def parse_model_body(body: str) -> ModelOverride:
    """Parse one canonical model identity and typed parameter assignment body."""

    if not isinstance(body, str):
        raise TypeError("model body must be a string")
    try:
        tokens = shlex.split(body, comments=False, posix=True)
    except ValueError as error:
        raise ValueError(str(error)) from error
    if not tokens:
        raise ValueError("model requires an identity or parameter assignment")

    identity: str | None = None
    effort: ModelEffort | None = None
    max_output: ModelMaxOutput | None = None
    for index, token in enumerate(tokens):
        if "=" not in token:
            if index != 0 or identity is not None:
                raise ValueError("model identity must be the first token")
            sentinel = token.lower()
            if sentinel == "none":
                raise ValueError(
                    "model identity 'none' was removed; use :model unset for "
                    "a model-free run"
                )
            identity = sentinel if sentinel in {"default", "unset"} else token
            if identity not in {"default", "unset"}:
                ModelRequest(identity)
            continue
        field, raw = _assignment(token)
        if field == "effort":
            if effort is not None:
                raise ValueError("duplicate model parameter: effort")
            effort = _effort_value(raw)
        elif field == "max_output":
            if max_output is not None:
                raise ValueError("duplicate model parameter: max_output")
            max_output = _max_output_value(raw)
        else:
            raise ValueError(f"unknown model parameter: {field}")
    return ModelOverride(identity=identity, effort=effort, max_output=max_output)


def apply_model_override(
    current: ModelRequest | None,
    default: ModelRequest | None,
    override: ModelOverride | None,
) -> ModelRequest | None:
    """Apply one sparse model operation to a concrete request."""

    if override is None:
        return current
    if override.identity == "default":
        model = default
    elif override.identity == "unset":
        model = None
    elif override.identity is not None:
        model = ModelRequest(override.identity)
    else:
        model = current
    if override.effort is None and override.max_output is None:
        return model
    if model is None:
        if override.effort is not None:
            raise ValueError("model effort requires an effective model")
        raise ValueError("model max_output requires an effective model")
    reasoning = model.reasoning
    max_output = model.max_output
    if override.effort is not None:
        if override.effort == "auto":
            reasoning = None
        elif isinstance(override.effort, int):
            reasoning = Reasoning(budget_tokens=override.effort)
        else:
            reasoning = Reasoning(effort=override.effort)
    if override.max_output is not None:
        max_output = None if override.max_output == "auto" else override.max_output
    return replace(model, reasoning=reasoning, max_output=max_output)


def compose_model_overrides(
    overrides: Sequence[ModelOverride],
) -> ModelOverride | None:
    """Compose ordered Setup-source operations relative to one lower request."""

    identity: str | None = None
    effort: ModelEffort | None = None
    max_output: ModelMaxOutput | None = None
    for override in overrides:
        if override.identity is not None:
            identity = override.identity
            effort = None
            max_output = None
        if override.effort is not None:
            effort = override.effort
        if override.max_output is not None:
            max_output = override.max_output
        if identity == "unset" and (effort is not None or max_output is not None):
            raise ValueError("model unset cannot combine with parameters")
    if identity is None and effort is None and max_output is None:
        return None
    return ModelOverride(identity=identity, effort=effort, max_output=max_output)


def format_model_body(override: ModelOverride) -> str:
    """Format one typed override as its canonical human-facing body."""

    tokens: list[str] = []
    if override.identity is not None:
        tokens.append(override.identity)
    if override.effort is not None:
        tokens.append(f"effort={override.effort}")
    if override.max_output is not None:
        tokens.append(f"max_output={override.max_output}")
    return shlex.join(tokens)


def _assignment(token: str) -> tuple[str, str]:
    field, separator, raw = token.partition("=")
    if not separator or not field or not raw:
        raise ValueError("model parameter expects name=value")
    return field, raw


def _max_output_value(raw: str) -> ModelMaxOutput:
    if raw == "auto":
        return "auto"
    if _BUDGET_RE.fullmatch(raw):
        value = int(raw)
        if value <= 0:
            raise ValueError("model max_output must be a positive integer")
        return value
    raise ValueError(f"unknown max output: {raw!r}")


def _effort_value(raw: str) -> ModelEffort:
    if raw == "auto":
        return "auto"
    if _BUDGET_RE.fullmatch(raw):
        return int(raw)
    if raw in _REASONING_EFFORTS:
        return cast(ReasoningEffort, raw)
    raise ValueError(f"unknown reasoning effort: {raw!r}")


__all__ = [
    "apply_model_override",
    "compose_model_overrides",
    "format_model_body",
    "parse_model_body",
]
