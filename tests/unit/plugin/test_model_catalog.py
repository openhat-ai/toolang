from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, cast

import pytest

import toolang.plugin.models.catalog as catalog_module
from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import (
    Model,
    ModelAlias,
    ModelCatalogSnapshot,
    Provider,
)
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.plugin.models.catalog import (
    MergedModelCatalog,
    PACKAGED_MODEL_CATALOG,
    catalog_json_dumps,
    read_model_catalog_snapshot,
    model_info_from_catalog,
    parse_model_catalog_data,
    query_catalog_models,
    resolve_model_catalog_path,
)
from toolang.plugin.models.adapters.chat_completions import (
    ChatCompletionsModelAdapter,
)
from toolang.plugin.models.adapters.messages import MessagesModelAdapter
from toolang.plugin.models.discovery import default_provider_base_url
from toolang.plugin.models.provider_resolver import resolve_provider
from toolang.plugin.models.resolution import (
    ModelTargetResolver,
    resolve_catalog_adapter,
    resolve_unique_model_query,
)


@dataclass(frozen=True, slots=True)
class _SnapshotCatalog(ModelCatalog):
    value: ModelCatalogSnapshot
    name: str = "models_dev"

    async def snapshot(self) -> ModelCatalogSnapshot:
        return self.value


def test_packaged_catalog_is_small_valid_and_covers_mainstream_providers() -> None:
    snapshot = read_model_catalog_snapshot(PACKAGED_MODEL_CATALOG)

    assert set(snapshot.providers) == {
        "anthropic",
        "deepseek",
        "google",
        "openai",
        "openrouter",
    }
    assert len(snapshot.models) >= 15
    assert PACKAGED_MODEL_CATALOG.stat().st_size < 64 * 1024


def test_merged_catalog_reuses_records_with_complete_origin() -> None:
    model = Model(
        provider_id="test",
        id="one",
        name="One",
        catalog="models.dev",
        catalog_revision="sha256:test",
    )
    provider = Provider(
        id="test",
        name="Test",
        env=(),
        npm="@ai-sdk/openai-compatible",
        models={model.id: model},
        catalog="models.dev",
        catalog_revision="sha256:test",
    )
    snapshot = ModelCatalogSnapshot(
        providers={provider.id: provider},
        models=(model,),
        revision="sha256:test",
    )

    merged = asyncio.run(MergedModelCatalog((_SnapshotCatalog(snapshot),)).snapshot())

    assert merged.models[0] is model
    assert merged.providers["test"] is provider


def test_catalog_reader_attaches_origin_without_rematerializing_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_catalog_data()), encoding="utf-8")

    def reject_replace(*args: object, **kwargs: object) -> None:
        raise AssertionError("catalog records must not be replaced after parsing")

    monkeypatch.setattr(catalog_module, "replace", reject_replace)

    snapshot = read_model_catalog_snapshot(path)
    model = snapshot.find("test", "one")

    assert model is not None
    assert model.catalog == "models.dev"
    assert model.catalog_revision == snapshot.revision
    assert snapshot.providers["test"].catalog == "models.dev"
    assert snapshot.providers["test"].catalog_revision == snapshot.revision


def test_catalog_import_preserves_unknown_fields_and_decimal_prices(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models.json"
    payload = _catalog_data()
    payload["test"]["future_provider_field"] = {"enabled": True}
    payload["test"]["models"]["one"]["future_model_field"] = ["value"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    snapshot = read_model_catalog_snapshot(path)
    model = snapshot.find("test", "one")

    assert model is not None
    assert model.cost == {"input": Decimal("1.25"), "output": 2}
    assert model.extra["future_model_field"] == ("value",)
    exported = cast(dict[str, Any], snapshot.to_data())
    assert exported["test"]["future_provider_field"] == {"enabled": True}
    assert exported["test"]["models"]["one"]["future_model_field"] == ["value"]


def test_catalog_values_are_deeply_immutable(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    payload = _catalog_data()
    payload["test"]["models"]["one"]["cost"]["tiers"] = [
        {"input": 3, "tier": {"type": "context", "size": 200}}
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")

    model = read_model_catalog_snapshot(path).find("test", "one")

    assert model is not None and model.cost is not None
    tiers = cast(tuple[object, ...], model.cost["tiers"])
    tier = cast(dict[str, object], tiers[0])
    nested = cast(dict[str, object], tier["tier"])
    with pytest.raises(TypeError):
        tier["input"] = 999
    with pytest.raises(TypeError):
        nested["size"] = 999
    exported = cast(dict[str, Any], read_model_catalog_snapshot(path).to_data())
    assert exported["test"]["models"]["one"]["cost"]["tiers"] == [
        {"input": 3, "tier": {"type": "context", "size": 200}}
    ]


def test_catalog_rejects_inconsistent_identity_as_a_complete_snapshot() -> None:
    payload = _catalog_data()
    payload["test"]["models"]["one"]["id"] = "other"

    with pytest.raises(ValueError, match="does not match id"):
        parse_model_catalog_data(payload)


def test_catalog_source_precedence_and_explicit_failure(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path / "root", "alice")
    layout.home.mkdir(parents=True)
    root = layout.root / "models.json"
    home = layout.home / "models.json"
    explicit = tmp_path / "explicit.json"
    for path in (root, home, explicit):
        path.write_text(json.dumps(_catalog_data()), encoding="utf-8")

    assert resolve_model_catalog_path(layout) == home.resolve()
    assert (
        resolve_model_catalog_path(layout, environ={"TOOLANG_MODEL_CATALOG": str(root)})
        == root.resolve()
    )
    assert (
        resolve_model_catalog_path(
            layout,
            explicit=explicit,
            environ={"TOOLANG_MODEL_CATALOG": str(root)},
        )
        == explicit.resolve()
    )

    with pytest.raises(FileNotFoundError, match="explicit model catalog"):
        resolve_model_catalog_path(layout, explicit=tmp_path / "missing.json")


def test_catalog_query_handles_nested_identity_schema_fields_and_nullable_boolean() -> (
    None
):
    provider = _provider(
        {
            "lab/model": _model(
                "lab/model",
                family="family",
                reasoning=True,
                temperature=None,
            ),
            "plain": _model(
                "plain",
                family="other",
                reasoning=False,
                temperature=False,
            ),
        }
    )
    snapshot = _snapshot(provider)

    assert [item.id for item in query_catalog_models(snapshot, ("test/lab/*",))] == [
        "lab/model"
    ]
    assert [
        item.id for item in query_catalog_models(snapshot, ("*[reasoning=false]",))
    ] == ["plain"]
    assert [
        item.id for item in query_catalog_models(snapshot, ("*[temperature=false]",))
    ] == ["plain"]
    assert [
        item.id for item in query_catalog_models(snapshot, ("*[family=family]",))
    ] == ["lab/model"]
    assert [item.id for item in query_catalog_models(snapshot, None)] == [
        "lab/model",
        "plain",
    ]
    with pytest.raises(ToolangError, match="query cannot be empty"):
        query_catalog_models(snapshot, ())


def test_filtered_export_round_trips_deterministically() -> None:
    provider = _provider({"one": _model("one"), "two": _model("two")})
    snapshot = _snapshot(provider)
    selected = query_catalog_models(snapshot, ("test/two",), include_local=False)

    first = catalog_json_dumps(snapshot.to_data(models=selected))
    second = catalog_json_dumps(snapshot.to_data(models=selected))
    imported = parse_model_catalog_data(json.loads(first, parse_float=Decimal))

    assert first == second
    assert tuple(imported) == ("test",)
    assert tuple(imported["test"].models) == ("two",)


def test_strict_export_rejects_local_only_models() -> None:
    local = _model("local", local=True)
    provider = Provider(
        id="test",
        name="Test",
        env=(),
        npm="@ai-sdk/openai-compatible",
        models={"local": local},
        local=True,
    )
    snapshot = _snapshot(provider)

    with pytest.raises(ValueError, match="local-only model cannot be exported"):
        snapshot.to_data()


def test_resolved_provider_adapter_ignores_model_protocol_hints() -> None:
    provider = _resolve(_provider({}), ChatCompletionsModelAdapter())
    model = Model(
        provider_id="test",
        id="one",
        name="One",
        provider={"npm": "@ai-sdk/anthropic"},
    )

    assert resolve_catalog_adapter(provider, model=model) == "chat_completions"


def test_anthropic_catalog_signal_resolves_messages_adapter() -> None:
    provider = _resolve(
        Provider(
            id="anthropic",
            name="Anthropic",
            env=("ANTHROPIC_API_KEY",),
            npm="@ai-sdk/anthropic",
            models={},
        ),
        MessagesModelAdapter(),
        environ={"ANTHROPIC_API_KEY": "secret"},
    )

    assert resolve_catalog_adapter(provider) == "messages"
    assert (
        default_provider_base_url(provider, environ={})
        == "https://api.anthropic.com/v1"
    )


def test_resolver_applies_advertised_mode_request_and_keeps_control_metadata() -> None:
    model = Model(
        provider_id="test",
        id="one",
        name="One",
        reasoning=True,
        structured_output=True,
        open_weights=False,
        release_date="2026-01-01",
        last_updated="2026-08-30",
        reasoning_options=({"type": "effort", "values": ["low", "high"]},),
        experimental={
            "modes": {
                "fast": {
                    "cost": {"input": 2, "output": 4},
                    "provider": {
                        "body": {"service_tier": "priority"},
                        "headers": {"X-Mode": "fast"},
                    },
                }
            }
        },
    )
    provider = _resolve(_provider({"one": model}), ChatCompletionsModelAdapter())
    info = model_info_from_catalog(
        model,
        adapter="chat_completions",
        revision="sha256:test",
    )
    resolver = ModelTargetResolver(
        providers={"test": provider},
        models=(info,),
        model_aliases={
            "fast-one": ModelAlias(
                name="fast-one",
                ref="test/one",
                provider="test",
                options={"mode": "fast", "reasoning": {"effort": "high"}},
            )
        },
        default_models=(),
        envs={"TEST_API_KEY": "secret"},
    )

    target = resolve_unique_model_query(resolver, query="*[alias=fast-one]")

    assert info.metadata["open_weights"] is False
    assert info.metadata["release_date"] == "2026-01-01"
    assert info.metadata["last_updated"] == "2026-08-30"
    assert target.mode == "fast"
    assert target.reasoning == {"effort": "high"}
    assert target.structured_output is True
    assert target.options == {"service_tier": "priority"}
    assert target.headers == {"X-Mode": "fast"}


def _catalog_data() -> dict[str, Any]:
    return {
        "test": {
            "id": "test",
            "name": "Test",
            "env": ["TEST_API_KEY"],
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                "one": {
                    "id": "one",
                    "name": "One",
                    "attachment": False,
                    "reasoning": True,
                    "tool_call": True,
                    "structured_output": True,
                    "temperature": False,
                    "release_date": "2026-01-01",
                    "last_updated": "2026-01-01",
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "open_weights": False,
                    "limit": {"context": 1000, "output": 100},
                    "cost": {"input": 1.25, "output": 2},
                }
            },
        }
    }


def _model(
    model_id: str,
    *,
    family: str | None = None,
    reasoning: bool | None = None,
    temperature: bool | None = True,
    local: bool = False,
) -> Model:
    return Model(
        provider_id="test",
        id=model_id,
        name=model_id,
        family=family,
        reasoning=reasoning,
        temperature=temperature,
        modalities={"input": ("text", "image"), "output": ("text",)},
        limit={"context": 1000},
        local=local,
    )


def _provider(models: dict[str, Model]) -> Provider:
    return Provider(
        id="test",
        name="Test",
        env=("TEST_API_KEY",),
        npm="@ai-sdk/openai-compatible",
        api="https://api.test/v1",
        models=models,
    )


def _resolve(
    provider: Provider,
    adapter: ChatCompletionsModelAdapter | MessagesModelAdapter,
    *,
    environ: dict[str, str] | None = None,
) -> Provider:
    return resolve_provider(
        provider,
        adapters={adapter.name: adapter},
        environ=environ or {"TEST_API_KEY": "secret"},
    )


def _snapshot(provider: Provider) -> ModelCatalogSnapshot:
    return ModelCatalogSnapshot(
        providers={provider.id: provider},
        models=tuple(provider.models.values()),
        revision="sha256:test",
    )
