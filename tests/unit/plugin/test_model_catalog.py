from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, cast

import pytest

import toolang.plugin.catalogs.models_dev.path as catalog_path_module
from toolang.base.protocols.model import ModelCatalog
from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelToolang,
    Provider,
)
from toolang.common.json import dumps
from toolang.common.layout import AgentLayout
from toolang.plugin.adapters.chat_completions import (
    ChatCompletionsModelAdapter,
)
from toolang.plugin.adapters.messages import MessagesModelAdapter
from toolang.plugin.catalogs.models_dev.catalog import read_model_catalog_snapshot
from toolang.plugin.catalogs.models_dev.parsing import parse_model_catalog_data
from toolang.plugin.catalogs.models_dev.path import (
    PACKAGED_MODEL_CATALOG,
    resolve_model_catalog_path,
)
from toolang.setup.catalog import MergedModelCatalog
from toolang.plugin.models.provider_resolver import (
    model_adapter,
    provider_adapter,
    resolve_provider,
)


@dataclass(frozen=True, slots=True)
class _SnapshotCatalog(ModelCatalog):
    value: ModelCatalogSnapshot
    name: str = "models_dev"

    async def snapshot(self) -> ModelCatalogSnapshot:
        return self.value


def test_packaged_catalog_is_small_valid_and_covers_mainstream_providers() -> None:
    snapshot = read_model_catalog_snapshot(PACKAGED_MODEL_CATALOG)

    assert PACKAGED_MODEL_CATALOG.name == "catalog.json"
    assert not PACKAGED_MODEL_CATALOG.with_name("models.json").exists()
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
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test", ready=True),
    )
    provider = Provider(
        id="test",
        name="Test",
        env=(),
        npm="@ai-sdk/openai-compatible",
        models={model.id: model},
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

    monkeypatch.setattr(dataclasses, "replace", reject_replace)

    snapshot = read_model_catalog_snapshot(path)
    model = snapshot.find("test", "one")

    assert model is not None
    assert model is snapshot.find("test", "one")
    assert snapshot.local is False


def test_catalog_import_drops_unknown_fields_and_keeps_decimal_prices(
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
    exported = cast(dict[str, Any], snapshot.to_data())
    assert "future_provider_field" not in exported["test"]
    assert "future_model_field" not in exported["test"]["models"]["one"]


def test_catalog_import_accepts_combined_models_dev_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    providers = _catalog_data()
    canonical_models = {
        "test/one": {
            "id": "test/one",
            "name": "One",
            "description": "Provider-agnostic metadata",
        }
    }
    path.write_text(
        json.dumps({"models": canonical_models, "providers": providers}),
        encoding="utf-8",
    )

    combined = read_model_catalog_snapshot(path)
    first_revision = combined.revision
    api_path = tmp_path / "api.json"
    api_path.write_text(json.dumps(providers), encoding="utf-8")
    direct = read_model_catalog_snapshot(api_path)
    canonical_models["test/one"]["description"] = "Updated metadata"
    path.write_text(
        json.dumps({"models": canonical_models, "providers": providers}),
        encoding="utf-8",
    )
    updated = read_model_catalog_snapshot(path)

    assert combined.to_data() == updated.to_data()
    assert combined.to_data() == direct.to_data()
    assert updated.revision != first_revision


@pytest.mark.parametrize(
    ("field", "value"),
    (("models", []), ("providers", [])),
)
def test_catalog_import_validates_combined_top_level_members(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {"models": {}, "providers": _catalog_data()}
    payload[field] = value
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=rf"combined model catalog {field}"):
        read_model_catalog_snapshot(path)


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


def test_catalog_reasoning_options_are_deeply_immutable(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    payload = _catalog_data()
    payload["test"]["models"]["one"]["reasoning_options"] = [
        {"type": "effort", "values": ["low", "high"], "exhaustive": True}
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    model = read_model_catalog_snapshot(path).models[0]
    assert model.reasoning_options is not None
    option = cast(dict[str, Any], model.reasoning_options[0])

    with pytest.raises(TypeError):
        option["exhaustive"] = False
    with pytest.raises(TypeError):
        option["values"][0] = "injected"
    resolved = model.with_readiness(True)
    assert resolved.reasoning_options is model.reasoning_options
    assert model.to_data()["reasoning_options"] == [
        {"type": "effort", "values": ["low", "high"], "exhaustive": True}
    ]


def test_catalog_rejects_inconsistent_identity_as_a_complete_snapshot() -> None:
    payload = _catalog_data()
    payload["test"]["models"]["one"]["id"] = "other"

    with pytest.raises(ValueError, match="does not match id"):
        parse_model_catalog_data(payload)


def test_catalog_source_precedence_and_explicit_failure(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path / "root", "alice")
    layout.home.mkdir(parents=True)
    root = layout.root / "catalog.json"
    home = layout.home / "catalog.json"
    configured = tmp_path / "models.json"
    explicit = tmp_path / "explicit-models.json"
    for path in (root, home, configured, explicit):
        path.write_text(json.dumps(_catalog_data()), encoding="utf-8")

    assert resolve_model_catalog_path(layout) == home.resolve()
    assert resolve_model_catalog_path(layout, include_agent=False) == root.resolve()
    assert (
        resolve_model_catalog_path(
            layout,
            environ={"TOOLANG_MODEL_CATALOG": str(configured)},
        )
        == configured.resolve()
    )
    assert (
        resolve_model_catalog_path(
            layout,
            explicit=explicit,
            environ={"TOOLANG_MODEL_CATALOG": str(configured)},
        )
        == explicit.resolve()
    )

    with pytest.raises(FileNotFoundError, match="explicit model catalog"):
        resolve_model_catalog_path(layout, explicit=tmp_path / "missing.json")


def test_catalog_source_ignores_implicit_models_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path / "root", "alice")
    layout.home.mkdir(parents=True)
    for path in (layout.home / "models.json", layout.root / "models.json"):
        path.write_text(json.dumps(_catalog_data()), encoding="utf-8")
    packaged = tmp_path / "packaged-catalog.json"
    packaged.write_text(json.dumps(_catalog_data()), encoding="utf-8")
    monkeypatch.setattr(catalog_path_module, "PACKAGED_MODEL_CATALOG", packaged)

    assert resolve_model_catalog_path(layout) == packaged.resolve()
    assert resolve_model_catalog_path(layout, include_agent=False) == packaged.resolve()


def test_root_catalog_is_selected_despite_unrecognized_models_file(
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path / "root", "alice")
    layout.home.mkdir(parents=True)
    legacy = layout.home / "models.json"
    root = layout.root / "catalog.json"
    legacy.write_text(json.dumps(_catalog_data()), encoding="utf-8")
    root.write_text(json.dumps(_catalog_data()), encoding="utf-8")

    assert resolve_model_catalog_path(layout) == root.resolve()


def test_filtered_export_round_trips_deterministically() -> None:
    provider = _provider({"one": _model("one"), "two": _model("two")})
    snapshot = _snapshot(provider)
    selected = tuple(model for model in snapshot.models if model.id == "two")

    first = dumps(snapshot.to_data(models=selected))
    second = dumps(snapshot.to_data(models=selected))
    imported = parse_model_catalog_data(json.loads(first, parse_float=Decimal))

    assert first == second
    assert tuple(imported) == ("test",)
    assert tuple(imported["test"].models) == ("two",)


def test_strict_export_rejects_local_only_models() -> None:
    provider = _provider({"local": _model("local")})
    snapshot = dataclasses.replace(_snapshot(provider), local=True)

    with pytest.raises(ValueError, match="local-only catalog cannot be exported"):
        snapshot.to_data()


def test_resolved_provider_adapter_ignores_model_protocol_hints() -> None:
    provider = _resolve(_provider({}), ChatCompletionsModelAdapter())
    model = Model(
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test", ready=True),
        provider={"npm": "@ai-sdk/anthropic"},
    )

    assert model_adapter(provider, model) == "chat_completions"


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

    assert provider_adapter(provider) == "messages"
    assert provider.api is None


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
) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        _toolang=ModelToolang(provider="test", ready=True),
        family=family,
        reasoning=reasoning,
        temperature=temperature,
        modalities={"input": ("text", "image"), "output": ("text",)},
        limit={"context": 1000},
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
