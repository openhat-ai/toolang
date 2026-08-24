"""Side-effect-free preparation of one agic's first model call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from toolang.base.types.message import Message
from toolang.base.types.model import ModelInfo, ModelTarget, Provider
from toolang.base.types.run import ModelCall
from toolang.lang.ast import AgicDecl
from toolang.plugin.models.config import (
    ProviderConfig,
    parse_default_models,
    parse_model_aliases,
)
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

from .executor import RunSpec, _bind_run, _prepare_start_spec
from .prepare import build_model_call, prepare_agic


@dataclass(frozen=True, slots=True)
class ModelCallPreview:
    """One normalized first call and the exact basis used to prepare it."""

    call: ModelCall
    model: ModelTarget
    prompt_context: str
    run_id: str
    thread_id: str
    created_at: str


class _PreviewContext:
    """Read-only preparation context with the model-selection executor shape."""

    def __init__(
        self,
        setup: AgentSetup,
        state: AgentState,
        *,
        created_at: str,
    ) -> None:
        self.setup = setup
        self.state = state
        self.layout = setup.layout
        config_layers = (state.root_config, state.home_config)
        self.model_aliases = parse_model_aliases(config_layers)
        self.default_models = parse_default_models(config_layers)
        self.date = created_at.partition("T")[0]
        self.timezone = "UTC"

    @property
    def providers(self) -> Mapping[str, Provider]:
        return self.setup.providers

    @property
    def models(self) -> tuple[ModelInfo, ...]:
        return self.setup.models

    @property
    def envs(self) -> Mapping[str, str]:
        return self.setup.envs

    @property
    def provider_configs(self) -> Mapping[str, ProviderConfig]:
        return cast(Mapping[str, ProviderConfig], self.setup.provider_configs)


def prepare_model_call(
    spec: RunSpec,
    *,
    run_id: str,
    history: Sequence[Message] = (),
) -> ModelCallPreview:
    """Prepare the exact first normalized call without accepting a run."""

    executable, input, agent_resources, resources = _prepare_start_spec(spec)
    if not isinstance(executable, AgicDecl):
        raise ValueError(f"runnable is not an agic: {executable.name}")
    bound = _bind_run(
        spec,
        executable=executable,
        run_id=run_id,
        input=input,
        agent_resources=agent_resources,
        resources=resources,
    )
    context = _PreviewContext(
        spec.setup,
        spec.state,
        created_at=bound.created_at,
    )
    prepared = prepare_agic(
        cast(Any, context),
        bound,
        executable,
        variables={
            **({"_": input.primary} if input.primary is not None else {}),
            **dict(input.named),
        },
        history=history,
    )
    return ModelCallPreview(
        call=build_model_call(prepared),
        model=prepared.model,
        prompt_context=prepared.prompt_context,
        run_id=bound.run_id,
        thread_id=bound.thread,
        created_at=bound.created_at,
    )
