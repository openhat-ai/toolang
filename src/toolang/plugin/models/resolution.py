"""Model query and target resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Protocol, cast

from toolang.base.errors import ToolangError
from toolang.base.types.model import (
    Model,
    ModelAlias,
    ModelInfo,
    ModelParameters,
    ModelRequest,
    ModelTarget,
    Provider,
    ReasoningEffort,
)
from toolang.common.query import (
    QueryDataset,
    SetOperator,
    format_identity,
    format_literal,
)
from toolang.plugin.models.collections import (
    MODEL_DEFINITION,
    MODEL_SCHEMA,
    ModelCostView,
    ModelLimitView,
    ModelModalitiesView,
    ModelParametersView,
    ModelQueryView,
    ModelReasoningParametersView,
    ModelRouteView,
    parse_model_query_date,
)
from toolang.plugin.models.config import ProviderConfig
from toolang.plugin.models.messages import (
    NO_AVAILABLE_MODELS_MESSAGE,
    NO_MATCHED_MODELS_MESSAGE,
)
from toolang.plugin.models.provider_resolver import (
    env_is_ready,
    selected_credential_value,
)

DEFAULT_MODEL_QUERY = "gpt-5"
CUSTOM_MODEL_PROVIDER = "custom"


def resolve_catalog_adapter(
    provider: Provider,
    *,
    model: Model | None = None,
) -> str | None:
    """Resolve a protocol adapter from explicit config and catalog signals."""

    if model is not None and model.resolved is not None:
        return model.resolved.adapter
    if provider.resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    return provider.resolved.adapter


def catalog_model_api(
    provider: Provider,
    model: Model,
    *,
    envs: Mapping[str, str],
) -> str | None:
    """Return the resolved API base for one model record."""

    del envs
    if model.resolved is not None:
        return model.resolved.api
    if provider.resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    return provider.resolved.api


class SupportsModelSelection(Protocol):
    """Minimal context shape needed to resolve model queries."""

    providers: Mapping[str, Provider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    exact_query: str
    target: ModelTarget
    aliases: tuple[str, ...] = ()
    info: ModelInfo | None = None


@dataclass(frozen=True, slots=True)
class ModelTargetResolver:
    """Resolve model catalog entries and runtime configuration into targets."""

    providers: Mapping[str, Provider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]
    provider_configs: Mapping[str, ProviderConfig] = field(default_factory=dict)

    def resolve(
        self,
        ref: str,
        *,
        allowed_queries: Sequence[str] | None = None,
    ) -> ModelTarget:
        return resolve_model(
            self,
            ref=ref,
            allowed_queries=allowed_queries,
        )

    def selectable(
        self,
        queries: Sequence[str] | None = None,
    ) -> tuple[tuple[str, ModelTarget], ...]:
        return selectable_model_targets(
            providers=self.providers,
            models=self.models,
            aliases=self.model_aliases,
            envs=self.envs,
            provider_configs=self.provider_configs,
            queries=queries,
        )


def resolve_model(
    context: SupportsModelSelection,
    *,
    ref: str,
    allowed_queries: Sequence[str] | None = None,
) -> ModelTarget:
    """Resolve one exact model ref against one uptime context."""

    return resolve_model_request(context, ref=ref, allowed_queries=allowed_queries)


def resolve_unique_model_query(
    context: SupportsModelSelection,
    *,
    query: str,
    allowed_queries: Sequence[str] | None = None,
) -> ModelTarget:
    """Require one target from a plural models collection query."""

    provider_configs = _context_provider_configs(context)
    matches = _resolve_query_targets(
        (query,),
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    if not matches:
        _raise_unavailable_alias_match(
            query,
            providers=context.providers,
            models=context.models,
            aliases=context.model_aliases,
            envs=context.envs,
            provider_configs=provider_configs,
        )
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
                provider_configs=provider_configs,
            )
        )
    if len(matches) > 1:
        joined = ", ".join(item.exact_query for item in matches)
        raise ToolangError(f"models query is ambiguous: {query} (matches {joined})")
    selected = matches[0]
    _require_allowed(
        selected.target,
        value=query,
        label="models query",
        allowed=_resolve_allowed_targets(
            allowed_queries,
            providers=context.providers,
            models=context.models,
            aliases=context.model_aliases,
            envs=context.envs,
            provider_configs=provider_configs,
        ),
    )
    return selected.target


def model_target_ref(target: ModelTarget) -> str:
    """Return the concrete public ref for one resolved model route."""

    return (
        target.ref
        if target.ref.partition("/")[0] == target.provider
        else f"{target.provider}/{target.ref}"
    )


def resolve_model_request(
    context: SupportsModelSelection,
    *,
    ref: str,
    allowed_queries: Sequence[str] | None = None,
) -> ModelTarget:
    """Resolve one concrete model route ref against one uptime context."""

    ModelRequest(ref)
    provider_configs = _context_provider_configs(context)
    candidates = _discover_available_candidates(
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    matches = tuple(
        candidate
        for candidate in candidates
        if model_target_ref(candidate.target) == ref or ref in candidate.aliases
    )
    if not matches:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
                provider_configs=provider_configs,
            )
        )
    if len(matches) > 1:
        joined = ", ".join(candidate.exact_query for candidate in matches)
        raise ToolangError(f"model ref is ambiguous: {ref} (matches {joined})")
    target = matches[0].target
    _require_allowed(
        target,
        value=ref,
        label="model ref",
        allowed=_resolve_allowed_targets(
            allowed_queries,
            providers=context.providers,
            models=context.models,
            aliases=context.model_aliases,
            envs=context.envs,
            provider_configs=provider_configs,
        ),
    )
    return target


def model_reasoning_efforts(
    context: SupportsModelSelection,
    target: ModelTarget,
) -> tuple[ReasoningEffort, ...]:
    """Return recognized catalog-advertised efforts in catalog order."""

    info = _find_model_info_by_ref(
        context.models,
        provider=target.provider,
        ref=target.ref,
    )
    if info is None:
        return ()
    raw_options = info.metadata.get("reasoning_options")
    options = (
        tuple(item for item in raw_options if isinstance(item, Mapping))
        if isinstance(raw_options, list | tuple)
        else ()
    )
    recognized = {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "default",
    }
    result: list[ReasoningEffort] = []
    for option in options:
        if option.get("type") != "effort":
            continue
        values = option.get("values")
        if not isinstance(values, list | tuple):
            continue
        for value in values:
            if isinstance(value, str) and value in recognized and value not in result:
                result.append(cast(ReasoningEffort, value))
    return tuple(result)


def apply_model_parameters(
    context: SupportsModelSelection,
    target: ModelTarget,
    parameters: ModelParameters,
) -> ModelTarget:
    """Validate and apply one request's typed parameters to a resolved target."""

    reasoning = parameters.reasoning
    effort = reasoning.effort if reasoning is not None else None
    if effort is None:
        return target
    allowed = model_reasoning_efforts(context, target)
    if effort not in allowed:
        joined = ", ".join(allowed) or "none"
        raise ToolangError(
            f"model {target.ref} does not advertise reasoning effort "
            f"{effort!r} (allowed: {joined})"
        )
    return replace(target, reasoning={"effort": effort})


def select_model_queries(
    context: SupportsModelSelection,
    *,
    directive_queries: Sequence[str] | None = None,
    allowed_queries: Sequence[str] | None = None,
    default_query: str | None = None,
) -> tuple[str, ...]:
    """Return effective precise model queries for one run resource set."""

    if directive_queries is not None and not directive_queries:
        return ()
    if allowed_queries is not None and not allowed_queries:
        return ()
    provider_configs = _context_provider_configs(context)
    directive_candidates = _resolve_query_targets(
        directive_queries,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    allowed_candidates = _resolve_query_targets(
        allowed_queries,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    if directive_queries and not directive_candidates:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
                provider_configs=provider_configs,
            )
        )
    if allowed_queries and not allowed_candidates:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
                provider_configs=provider_configs,
            )
        )
    if directive_candidates and allowed_candidates:
        directive_identities = {
            candidate.exact_query for candidate in directive_candidates
        }
        selected = tuple(
            candidate.exact_query
            for candidate in allowed_candidates
            if candidate.exact_query in directive_identities
        )
        if selected:
            return selected
        raise ToolangError(NO_MATCHED_MODELS_MESSAGE)

    if allowed_candidates:
        return tuple(candidate.exact_query for candidate in allowed_candidates)

    if directive_candidates:
        return tuple(candidate.exact_query for candidate in directive_candidates)

    available = _discover_available_candidates(
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    defaults = (
        (default_query,)
        if default_query and default_query.strip()
        else tuple(context.default_models)
    ) or (DEFAULT_MODEL_QUERY,)
    preferred = _resolve_first_unique_query(
        defaults,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    ordered: list[str] = []
    seen: set[str] = set()
    preferred_values = (preferred,) if preferred is not None else ()
    for candidate in (*preferred_values, *available):
        if candidate.exact_query in seen:
            continue
        seen.add(candidate.exact_query)
        ordered.append(candidate.exact_query)
    if ordered:
        return tuple(ordered)
    raise ToolangError(
        _empty_model_selection_message(
            providers=context.providers,
            models=context.models,
            aliases=context.model_aliases,
            envs=context.envs,
            provider_configs=provider_configs,
        )
    )


def _resolve_first_unique_query(
    queries: Sequence[str],
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> _Candidate | None:
    """Resolve ordered independent singular queries with fallback-on-zero."""

    for query in queries:
        matches = _resolve_query_targets(
            (query,),
            providers=providers,
            models=models,
            aliases=aliases,
            envs=envs,
            provider_configs=provider_configs,
        )
        if not matches:
            continue
        if len(matches) > 1:
            joined = ", ".join(item.exact_query for item in matches)
            raise ToolangError(f"models query is ambiguous: {query} (matches {joined})")
        return matches[0]
    return None


def _raise_unavailable_alias_match(
    query: str,
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> None:
    """Preserve route diagnostics for an explicit alias predicate."""

    parsed = MODEL_SCHEMA.parse(query)
    alias_names = {
        value
        for match in parsed.matches
        if match.identity_pattern == "*"
        for predicate in match.predicates
        if predicate.field.name == "alias" and predicate.operator == "="
        for value in predicate.values
        if isinstance(value, str)
    }
    if len(alias_names) != 1:
        return
    alias_name = next(iter(alias_names))
    alias = aliases.get(alias_name)
    if alias is None:
        return
    _target_from_alias(
        alias,
        providers=providers,
        models=models,
        envs=envs,
        provider_configs=provider_configs,
        strict=True,
    )


def selectable_model_targets(
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig] | None = None,
    queries: Sequence[str] | None = None,
) -> tuple[tuple[str, ModelTarget], ...]:
    """Return selectable model targets for CLI/API listing."""

    if queries is not None:
        return tuple(
            (candidate.exact_query, candidate.target)
            for candidate in _resolve_query_targets(
                queries,
                providers=providers,
                models=models,
                aliases=aliases,
                envs=envs,
                provider_configs=provider_configs or {},
            )
        )
    return tuple(
        (candidate.exact_query, candidate.target)
        for candidate in _discover_available_candidates(
            providers=providers,
            models=models,
            aliases=aliases,
            envs=envs,
            provider_configs=provider_configs or {},
        )
    )


def apply_model_query_operations(
    context: SupportsModelSelection,
    base_queries: Sequence[str],
    operations: Sequence[tuple[SetOperator, tuple[str, ...]]],
) -> tuple[str, ...]:
    """Apply model resource directives against one immutable candidate base."""

    provider_configs = _context_provider_configs(context)
    candidates = _resolve_query_targets(
        base_queries,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    dataset = _candidate_dataset(candidates)
    selected = dataset.apply(operations)
    return tuple(cast(_Candidate, item.record).exact_query for item in selected)


def _resolve_allowed_targets(
    queries: Sequence[str] | None,
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> tuple[ModelTarget, ...] | None:
    if queries is None:
        return None
    targets: list[ModelTarget] = []
    for candidate in _resolve_query_targets(
        queries,
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
        provider_configs=provider_configs,
    ):
        targets.append(candidate.target)
    return tuple(targets)


def _discover_available_candidates(
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    for name, alias in aliases.items():
        target = _target_from_alias(
            alias,
            providers=providers,
            models=models,
            envs=envs,
            provider_configs=provider_configs,
            strict=False,
        )
        if target is None:
            continue
        index = _candidate_target_index(candidates, target)
        if index is not None:
            candidate = candidates[index]
            candidates[index] = replace(
                candidate,
                aliases=(*candidate.aliases, name),
            )
            continue
        candidates.append(
            _Candidate(
                exact_query=_candidate_query(target, alias=name),
                target=target,
                aliases=(name,),
            )
        )
    for info in models:
        provider = providers.get(info.provider)
        if provider is None or _provider_id(provider) == CUSTOM_MODEL_PROVIDER:
            continue
        if not _model_info_ready(info):
            continue
        target = _target_from_info(
            provider,
            info,
            envs=envs,
            provider_configs=provider_configs,
        )
        for index, candidate in enumerate(candidates):
            if candidate.target.provider == target.provider and (
                candidate.target.ref == target.ref
            ):
                candidates[index] = replace(candidate, info=info)
        index = _candidate_target_index(candidates, target)
        if index is not None:
            candidates[index] = replace(candidates[index], info=info)
            continue
        candidates.append(
            _Candidate(
                exact_query=_candidate_query(target),
                target=target,
                info=info,
            )
        )
    return tuple(candidates)


def _candidate_target_index(
    candidates: Sequence[_Candidate],
    target: ModelTarget,
) -> int | None:
    return next(
        (
            index
            for index, candidate in enumerate(candidates)
            if candidate.target == target
        ),
        None,
    )


def _resolve_query_targets(
    queries: Sequence[str] | None,
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
    prefer_exact_route: bool = False,
) -> tuple[_Candidate, ...]:
    if not queries:
        return ()
    candidates = _discover_available_candidates(
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
        provider_configs=provider_configs,
    )
    exact_queries = {candidate.exact_query for candidate in candidates}
    if all(query in exact_queries for query in queries):
        selected = set(queries)
        return tuple(
            candidate for candidate in candidates if candidate.exact_query in selected
        )
    selected = _candidate_dataset(candidates).query(queries)
    return tuple(cast(_Candidate, item.record) for item in selected)


def _candidate_dataset(
    candidates: Sequence[_Candidate],
) -> QueryDataset[ModelQueryView]:
    return MODEL_DEFINITION.dataset(
        tuple(_candidate_view(candidate) for candidate in candidates)
    )


def _candidate_view(candidate: _Candidate) -> ModelQueryView:
    target = candidate.target
    provider, model = _runtime_identity_components(target)
    info = candidate.info
    metadata = info.metadata if info is not None else {}
    modalities = metadata.get("modalities")
    input_modalities = (
        modalities.get("input") if isinstance(modalities, Mapping) else None
    )
    output_modalities = (
        modalities.get("output") if isinstance(modalities, Mapping) else None
    )
    return ModelQueryView(
        key=candidate.exact_query,
        record=candidate,
        provider=provider,
        model=model,
        name=target.name,
        description=info.details if info is not None else None,
        family=_metadata_text(metadata, "family"),
        scope=target.scope,
        available=True,
        adapter=target.adapter,
        catalog=target.catalog,
        alias=candidate.aliases or None,
        route=ModelRouteView(
            provider=target.provider,
            adapter=target.adapter,
            scope=target.scope,
        ),
        tags=tuple(target.tags),
        streaming=target.streaming,
        attachment=_metadata_bool(metadata, "attachment"),
        reasoning=_metadata_bool(metadata, "reasoning"),
        tool_call=target.tools,
        temperature=_metadata_bool(metadata, "temperature"),
        structured_output=target.structured_output,
        open_weights=_metadata_bool(metadata, "open_weights"),
        status=_metadata_text(metadata, "status"),
        release_date=parse_model_query_date(_metadata_text(metadata, "release_date")),
        last_updated=parse_model_query_date(_metadata_text(metadata, "last_updated")),
        modalities=ModelModalitiesView(
            input=_string_values(input_modalities),
            output=_string_values(output_modalities),
        ),
        limit=ModelLimitView(
            context=info.context_window if info is not None else None,
            output=info.max_output_tokens if info is not None else None,
        ),
        cost=ModelCostView(
            input=_optional_decimal(info.input_price if info is not None else None),
            output=_optional_decimal(info.output_price if info is not None else None),
        ),
        parameters=ModelParametersView(
            reasoning=ModelReasoningParametersView(
                effort=_reasoning_effort_values(metadata)
            )
        ),
    )


def _optional_decimal(value: float | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _runtime_identity_components(target: ModelTarget) -> tuple[str, str]:
    provider, separator, model = target.ref.partition("/")
    if separator and provider and model:
        return provider, model
    return target.provider, target.model


def _candidate_query(target: ModelTarget, *, alias: str | None = None) -> str:
    provider, model = _runtime_identity_components(target)
    identity = format_identity(f"{provider}/{model}", exact=True)
    if alias is not None:
        return f"{identity}[alias={format_literal(alias)}]"
    return (
        f"{identity}[route.provider={format_literal(target.provider)};"
        f"route.adapter={format_literal(target.adapter)};alias=null]"
    )


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _reasoning_effort_values(metadata: Mapping[str, object]) -> tuple[str, ...]:
    raw_options = metadata.get("reasoning_options")
    if not isinstance(raw_options, list | tuple):
        return ()
    values: list[str] = []
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping):
            continue
        option = cast(Mapping[str, object], raw_option)
        raw_values = option.get("values", ())
        if option.get("type") != "effort" or not isinstance(raw_values, list | tuple):
            continue
        values.extend(value for value in raw_values if isinstance(value, str))
    return tuple(values)


def _model_info_ready(info: ModelInfo) -> bool:
    value = info.metadata.get("resolved_ready")
    return value if isinstance(value, bool) else info.adapter != "unavailable"


def _target_from_alias(
    alias: ModelAlias,
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
    strict: bool,
) -> ModelTarget | None:
    provider = providers.get(alias.provider)
    if provider is None:
        if strict:
            raise ToolangError(
                f"unknown model provider for alias {alias.name!r}: {alias.provider}"
            )
        return None
    missing = _missing_target_env_vars(provider, alias=alias, envs=envs)
    if missing:
        if strict:
            joined = ", ".join(missing)
            raise ToolangError(
                f"model alias {alias.name!r} is missing environment: {joined}"
            )
        return None
    if alias.provider == CUSTOM_MODEL_PROVIDER and not alias.endpoint:
        if strict:
            raise ToolangError(f"model alias {alias.name!r} is missing endpoint")
        return None
    info = _find_model_info_by_ref(
        models, provider=_provider_id(provider), ref=alias.ref
    )
    if info is not None:
        return _target_from_info(
            provider,
            info,
            envs=envs,
            provider_configs=provider_configs,
            alias=alias,
        )
    return _target_from_alias_only(
        provider,
        alias,
        envs=envs,
        provider_configs=provider_configs,
    )


def _find_model_info_by_ref(
    models: Sequence[ModelInfo],
    *,
    provider: str,
    ref: str,
) -> ModelInfo | None:
    for info in models:
        if info.provider == provider and info.ref == ref:
            return info
    return None


def _target_from_info(
    provider: Provider,
    info: ModelInfo,
    *,
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
    alias: ModelAlias | None = None,
) -> ModelTarget:
    config = provider_configs.get(provider.id)
    request_options = _target_options(
        config, dict(alias.options) if alias is not None else {}
    )
    reasoning = _reasoning_options(request_options)
    mode = _mode_option(request_options)
    _validate_reasoning_request(reasoning, info=info)
    mode_body, mode_headers = _model_mode_request(info, mode)
    mode_body.update(request_options)
    resolved = provider.resolved
    if resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    api_key = (
        envs.get(alias.key_env)
        if alias is not None and alias.key_env is not None
        else selected_credential_value(provider, environ=envs)
    )
    endpoint = (
        alias.endpoint
        if alias is not None and alias.endpoint is not None
        else _metadata_text(info.metadata, "resolved_api")
    )
    scope = _target_scope(
        provider,
        info=info,
        alias=alias,
        endpoint=endpoint,
        config=config,
    )
    return ModelTarget(
        ref=info.ref,
        provider=_provider_id(provider),
        name=alias.display_name
        if alias is not None and alias.display_name is not None
        else info.name,
        model=alias.model
        if alias is not None and alias.model is not None
        else info.model,
        adapter=alias.adapter
        if alias is not None and alias.adapter is not None
        else info.adapter,
        base_url=endpoint,
        api_key=api_key,
        scope=scope,
        tags=alias.tags if alias is not None and alias.tags else info.tags,
        headers=_target_headers(
            provider,
            mode_headers,
            dict(alias.headers) if alias is not None else {},
        ),
        options=mode_body,
        tools=alias.tools
        if alias is not None and alias.tools is not None
        else info.tools,
        streaming=alias.streaming
        if alias is not None and alias.streaming is not None
        else info.streaming,
        structured_output=_metadata_bool(info.metadata, "structured_output"),
        catalog=_metadata_text(info.metadata, "catalog"),
        catalog_revision=_metadata_text(info.metadata, "catalog_revision"),
        reasoning=reasoning,
        mode=mode,
    )


def _target_from_alias_only(
    provider: Provider,
    alias: ModelAlias,
    *,
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> ModelTarget:
    model_name = alias.model or _provider_model_name_from_ref(alias.provider, alias.ref)
    resolved = provider.resolved
    if resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    endpoint = alias.endpoint or resolved.api
    config = provider_configs.get(provider.id)
    scope = alias.scope or _configured_scope(config) or _scope_from_endpoint(endpoint)
    scope = scope or _provider_scope(alias.provider)
    request_options = _target_options(config, dict(alias.options))
    reasoning = _reasoning_options(request_options)
    mode = _mode_option(request_options)
    return ModelTarget(
        ref=alias.ref,
        provider=_provider_id(provider),
        name=alias.display_name or model_name,
        model=model_name,
        adapter=alias.adapter or resolve_catalog_adapter(provider) or "unavailable",
        base_url=endpoint,
        api_key=(
            envs.get(alias.key_env)
            if alias.key_env is not None
            else selected_credential_value(provider, environ=envs)
        ),
        scope=scope,
        tags=alias.tags,
        headers=_target_headers(provider, dict(alias.headers)),
        options=request_options,
        tools=True if alias.tools is None else alias.tools,
        streaming=True if alias.streaming is None else alias.streaming,
        reasoning=reasoning,
        mode=mode,
    )


def _provider_model_name_from_ref(provider: str, ref: str) -> str:
    if provider == "openrouter":
        return ref
    head, sep, tail = ref.partition("/")
    if sep:
        return tail.strip() or head.strip()
    return ref.strip()


def _provider_scope(provider: str) -> str:
    return "local" if provider in {"ollama", "llama_cpp"} else "remote"


def _scope_from_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    text = endpoint.strip().lower()
    if text.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return "local"
    if text.startswith(("http://", "https://")):
        return "remote"
    return None


def _missing_target_env_vars(
    provider: Provider,
    *,
    alias: ModelAlias | None,
    envs: Mapping[str, str],
) -> tuple[str, ...]:
    if alias is not None and alias.key_env is not None:
        return () if str(envs.get(alias.key_env, "")).strip() else (alias.key_env,)
    resolved = provider.resolved
    if resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    if env_is_ready(resolved.env, environ=envs):
        return ()
    return tuple(
        dict.fromkeys(
            name
            for alternative in resolved.env
            for name in (
                (alternative,) if isinstance(alternative, str) else alternative
            )
            if not str(envs.get(name, "")).strip()
        )
    )


def _provider_id(provider: Provider) -> str:
    return provider.id


def _target_headers(
    provider: Provider,
    *overrides: Mapping[str, str],
) -> dict[str, str]:
    defaults: dict[str, str] = {}
    if _provider_id(provider) == "openrouter":
        defaults = {
            "HTTP-Referer": "https://toolang.ai",
            "X-OpenRouter-Title": "Toolang",
            "X-OpenRouter-Categories": "cli-agent",
        }
    lowered = {key.lower(): key for key in defaults}
    for values in overrides:
        for key, value in values.items():
            existing = lowered.get(key.lower())
            if existing is not None and existing != key:
                defaults.pop(existing, None)
            defaults[key] = value
            lowered[key.lower()] = key
    return defaults


def _target_options(
    config: ProviderConfig | None,
    overrides: Mapping[str, object],
) -> dict[str, object]:
    options: dict[str, object] = {}
    if config is not None:
        options.update(config.options)
    options.update(overrides)
    return options


def _reasoning_options(options: dict[str, object]) -> dict[str, object]:
    value = options.pop("reasoning", None)
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _mode_option(options: dict[str, object]) -> str | None:
    value = options.pop("mode", None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolangError("model mode must be non-empty text")
    return value.strip()


def _model_mode_request(
    info: ModelInfo,
    mode: str | None,
) -> tuple[dict[str, object], dict[str, str]]:
    if mode is None:
        return {}, {}
    experimental = info.metadata.get("experimental")
    modes = experimental.get("modes") if isinstance(experimental, Mapping) else None
    selected = modes.get(mode) if isinstance(modes, Mapping) else None
    if not isinstance(selected, Mapping):
        raise ToolangError(f"model {info.ref} does not advertise mode {mode!r}")
    provider = selected.get("provider")
    body = provider.get("body") if isinstance(provider, Mapping) else None
    headers = provider.get("headers") if isinstance(provider, Mapping) else None
    return (
        dict(body) if isinstance(body, Mapping) else {},
        {
            str(key): value
            for key, value in headers.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if isinstance(headers, Mapping)
        else {},
    )


def _target_scope(
    provider: Provider,
    *,
    info: ModelInfo,
    alias: ModelAlias | None,
    endpoint: str | None,
    config: ProviderConfig | None,
) -> str:
    return (
        alias.scope
        if alias is not None and alias.scope is not None
        else _configured_scope(config)
        or _scope_from_endpoint(endpoint)
        or info.scope
        or _provider_scope(_provider_id(provider))
    )


def _configured_scope(config: ProviderConfig | None) -> str | None:
    return config.scope if config is not None else None


def _metadata_text(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) and value else None


def _metadata_bool(metadata: Mapping[str, object], key: str) -> bool | None:
    value = metadata.get(key)
    return value if isinstance(value, bool) else None


def _validate_reasoning_request(
    request: Mapping[str, object],
    *,
    info: ModelInfo,
) -> None:
    if not request:
        return
    raw_options = info.metadata.get("reasoning_options")
    options = (
        tuple(item for item in raw_options if isinstance(item, Mapping))
        if isinstance(raw_options, list | tuple)
        else ()
    )
    unknown = set(request) - {"enabled", "effort", "budget_tokens"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ToolangError(f"model {info.ref} has unknown reasoning controls: {joined}")
    enabled = request.get("enabled")
    if enabled is not None and (
        not isinstance(enabled, bool)
        or not any(option.get("type") == "toggle" for option in options)
    ):
        raise ToolangError(f"model {info.ref} does not advertise a reasoning toggle")
    effort = request.get("effort")
    if effort is not None:
        allowed = {
            value
            for option in options
            if option.get("type") == "effort"
            for value in option.get("values", ())
            if isinstance(value, str)
        }
        if not isinstance(effort, str) or effort not in allowed:
            joined = ", ".join(sorted(allowed)) or "none"
            raise ToolangError(
                f"model {info.ref} does not advertise reasoning effort "
                f"{effort!r} (allowed: {joined})"
            )
    budget = request.get("budget_tokens")
    if budget is not None:
        budget_options = tuple(
            option for option in options if option.get("type") == "budget_tokens"
        )
        if (
            isinstance(budget, bool)
            or not isinstance(budget, int)
            or budget < 0
            or not budget_options
        ):
            raise ToolangError(
                f"model {info.ref} does not advertise this reasoning token budget"
            )
        minimums = tuple(
            value
            for option in budget_options
            for value in (option.get("min"),)
            if isinstance(value, int) and not isinstance(value, bool)
        )
        if minimums and budget < min(minimums):
            raise ToolangError(
                f"model {info.ref} reasoning budget must be at least {min(minimums)}"
            )


def _require_allowed(
    target: ModelTarget,
    *,
    value: str,
    label: str,
    allowed: Sequence[ModelTarget] | None,
) -> None:
    if allowed is None:
        return
    if any(target == item for item in allowed):
        return
    allowed_text = (
        ", ".join(f"{item.ref}[{item.provider}]" for item in allowed) or "none"
    )
    raise ToolangError(
        f"{label} is outside the current resources: {value} (allowed: {allowed_text})"
    )


def _empty_model_selection_message(
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> str:
    available = _discover_available_candidates(
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
        provider_configs=provider_configs,
    )
    if available:
        return NO_MATCHED_MODELS_MESSAGE
    return NO_AVAILABLE_MODELS_MESSAGE


def _context_provider_configs(
    context: SupportsModelSelection,
) -> Mapping[str, ProviderConfig]:
    value = getattr(context, "provider_configs", {})
    if not isinstance(value, Mapping):
        return {}
    return {
        str(name): config
        for name, config in value.items()
        if isinstance(config, ProviderConfig)
    }
