"""Display-oriented model catalog views."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelAlias, ModelInfo, ModelTarget
from toolang.models.discovery import (
    default_provider_api_key_env,
    default_provider_base_url,
    missing_provider_env_vars,
    model_infos,
    required_provider_env_vars,
)
from toolang.models.resolution import selectable_model_targets


def model_list_rows(
    *,
    providers: Mapping[str, ModelProvider],
    aliases: Mapping[str, ModelAlias],
    environ: Mapping[str, str],
    selectors: Sequence[str] = (),
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> list[tuple[str, str, str]]:
    """Return table rows for selectable model listings."""

    rows: list[tuple[str, str, str]] = []
    for _selector, target in selectable_model_targets(
        providers=providers,
        aliases=aliases,
        environ=environ,
        selectors=selectors,
        cache_dir=cache_dir,
        refresh=refresh,
    ):
        rows.append(
            (
                target.ref,
                target.provider,
                model_target_profile(
                    target,
                    provider=providers.get(target.provider),
                    environ=environ,
                    cache_dir=cache_dir,
                    refresh=refresh,
                ),
            )
        )
    return rows


def model_provider_rows(
    *,
    providers: Mapping[str, ModelProvider],
    aliases: Mapping[str, ModelAlias],
    provider_configs: Mapping[str, object],
    environ: Mapping[str, str],
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> list[tuple[str, str, str]]:
    """Return table rows for model provider configuration status."""

    available_counts = _available_model_counts_by_provider(
        providers=providers,
        environ=environ,
        cache_dir=cache_dir,
        refresh=refresh,
    )
    rows: list[tuple[str, str, str]] = []
    for name, provider in sorted(providers.items()):
        if name == "custom":
            continue
        models = model_infos(provider, environ=environ, cache_dir=cache_dir, refresh=refresh)
        adapter = model_provider_adapter_summary(models)
        model_count = _model_provider_model_count(
            models=models,
            available_count=available_counts.get(name, 0),
        )
        details = model_provider_config(
            provider,
            environ=environ,
            adapter=adapter,
            model_count=len(models),
            available_count=available_counts.get(name, 0),
            configured=bool(provider_configs.get(name)),
        )
        if name in provider_configs:
            details = f"config=models.providers.{name}, {details}"
        rows.append((name, model_count, details))
    for name, alias in sorted(aliases.items()):
        adapter, details = model_alias_status(alias, providers=providers, environ=environ)
        rows.append((alias.provider, "-", f"alias={name}, {details}, adapter={adapter}"))
    return rows


def available_model_adapters() -> tuple[str, ...]:
    """Return available model adapter names."""

    from toolang.plugin import list_plugin_names

    return tuple(list_plugin_names(group="toolang.model_adapter"))


def model_target_profile(
    target: ModelTarget,
    *,
    provider: ModelProvider | None,
    environ: Mapping[str, str],
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> str:
    """Return a compact profile string for one selectable model target."""

    info = _find_model_info(target, provider=provider, environ=environ, cache_dir=cache_dir, refresh=refresh)
    parts: list[str] = []
    parts.append(f"streaming={'y' if target.streaming else 'n'}")
    parts.append(f"tools={'y' if target.tools else 'n'}")
    if info is None:
        return ", ".join(parts)
    if info.context_window is not None:
        parts.append(f"ctx={_format_k(info.context_window)}")
    if info.max_output_tokens is not None:
        parts.append(f"max_out={_format_k(info.max_output_tokens)}")
    if info.input_price is not None or info.output_price is not None:
        in_price = "-" if info.input_price is None else _format_price_per_million(info.input_price)
        out_price = "-" if info.output_price is None else _format_price_per_million(info.output_price)
        parts.append(f"price=${in_price}/${out_price}")
    return ", ".join(parts)


def model_alias_status(
    alias: ModelAlias,
    *,
    providers: Mapping[str, ModelProvider],
    environ: Mapping[str, str],
) -> tuple[str, str]:
    """Return adapter and config status for one alias."""

    adapter = alias.adapter or "responses"
    provider = providers.get(alias.provider)
    if provider is None:
        return (adapter, f"ref={alias.ref}, configured=false, missing_provider={alias.provider}")
    missing = _model_alias_missing_env(alias, provider=provider, environ=environ)
    details = _model_alias_details(alias, provider=provider, environ=environ)
    if alias.provider == "custom" and alias.endpoint is None:
        return (adapter, f"{details}, configured=false, missing_endpoint=true")
    if missing:
        return (adapter, f"{details}, configured=false, missing_env={'+'.join(missing)}")
    return (adapter, f"{details}, configured=true")


def model_provider_config(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
    adapter: str,
    model_count: int | None = None,
    available_count: int | None = None,
    configured: bool = False,
) -> str:
    """Return compact provider configuration details."""

    missing = missing_provider_env_vars(provider, environ=environ)
    required = required_provider_env_vars(provider)
    base_url = default_provider_base_url(provider, environ=environ)
    api_key_env = default_provider_api_key_env(provider)
    offline = _provider_url_offline(provider, model_count=model_count, available_count=available_count)
    parts: list[str] = []
    if base_url is not None:
        parts.append(f"url={base_url}{'(offline)' if offline else ''}")
    parts.append(f"adapter={adapter}")
    if required:
        parts.append(f"env={_env_status(required, missing)}")
    elif api_key_env is not None:
        parts.append(f"env={api_key_env}")
    if configured:
        parts.append("configured=true")
    return ", ".join(parts)


def model_provider_adapter_summary(models: tuple[ModelInfo, ...]) -> str:
    """Return one compact adapter summary for provider-exposed models."""

    adapters = sorted({info.adapter for info in models if info.adapter.strip()})
    if not adapters:
        return "responses"
    return "+".join(adapters)


def _available_model_counts_by_provider(
    *,
    providers: Mapping[str, ModelProvider],
    environ: Mapping[str, str],
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _selector, target in selectable_model_targets(
        providers=providers,
        aliases={},
        environ=environ,
        cache_dir=cache_dir,
        refresh=refresh,
    ):
        counts[target.provider] = counts.get(target.provider, 0) + 1
    return counts


def _model_provider_model_count(
    *,
    models: tuple[ModelInfo, ...],
    available_count: int | None,
) -> str:
    if available_count is None:
        return str(len(models))
    return f"{available_count}/{len(models)}"


def _env_status(required: tuple[str, ...], missing: tuple[str, ...]) -> str:
    missing_set = set(missing)
    return "+".join(f"{name}(missing)" if name in missing_set else name for name in required)


def _provider_url_offline(
    provider: ModelProvider,
    *,
    model_count: int | None,
    available_count: int | None,
) -> bool:
    if provider.name != "ollama":
        return False
    if model_count != 0 or available_count != 0:
        return False
    return True


def _model_alias_missing_env(
    alias: ModelAlias,
    *,
    provider: ModelProvider,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    required = list(required_provider_env_vars(provider))
    default_key_env = default_provider_api_key_env(provider)
    if alias.key_env is not None:
        required = [alias.key_env if name == default_key_env else name for name in required]
        if default_key_env is None or alias.key_env not in required:
            required.append(alias.key_env)
    seen: set[str] = set()
    missing: list[str] = []
    for name in required:
        if name in seen:
            continue
        seen.add(name)
        if not str(environ.get(name, "")).strip():
            missing.append(name)
    return tuple(missing)


def _model_alias_details(
    alias: ModelAlias,
    *,
    provider: ModelProvider,
    environ: Mapping[str, str],
) -> str:
    endpoint = alias.endpoint or default_provider_base_url(provider, environ=environ)
    key_env = alias.key_env or default_provider_api_key_env(provider)
    parts = [f"ref={alias.ref}"]
    if endpoint is not None:
        parts.append(f"endpoint={endpoint}")
    if key_env is not None:
        parts.append(f"key_env={key_env}")
    if alias.scope is not None:
        parts.append(f"scope={alias.scope}")
    return ", ".join(parts)


def _find_model_info(
    target: ModelTarget,
    *,
    provider: ModelProvider | None,
    environ: Mapping[str, str],
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> ModelInfo | None:
    if provider is None:
        return None
    for info in model_infos(provider, environ=environ, cache_dir=cache_dir, refresh=refresh):
        if info.ref == target.ref:
            return info
    return None


def _format_k(value: int) -> str:
    if value >= 1_000_000:
        return f"{_format_decimal_unit(value / 1_000_000)}M"
    if value >= 1_000:
        return f"{_format_decimal_unit(value / 1_000)}k"
    return str(value)


def _format_decimal_unit(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_price_per_million(value: float) -> str:
    return f"{value * 1_000_000:g}"
