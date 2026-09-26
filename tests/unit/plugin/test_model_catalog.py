from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
import json
from hashlib import sha256
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
from toolang.setup.routes import (
    model_adapter,
    provider_adapter,
    resolve_provider,
)

from toolang.base.types.model import ModelProvider


@dataclass(frozen=True, slots=True)
class _SnapshotCatalog(ModelCatalog):
    value: ModelCatalogSnapshot
    name: str = "models_dev"

    async def snapshot(self) -> ModelCatalogSnapshot:
        return self.value


def test_packaged_catalog_matches_pinned_release_and_keeps_model_overrides() -> None:
    snapshot = read_model_catalog_snapshot(PACKAGED_MODEL_CATALOG)

    assert PACKAGED_MODEL_CATALOG.name == "catalog.json"
    assert not PACKAGED_MODEL_CATALOG.with_name("models.json").exists()
    assert sha256(PACKAGED_MODEL_CATALOG.read_bytes()).hexdigest() == (
        "a9594734ccc502052dd76cf2eff5127e94ffc077d645920dd60e04db6c8ddebe"
    )
    assert len(snapshot.providers) == 223
    assert len(snapshot.models) == 2134
    assert {"anthropic", "deepseek", "google", "openai", "openrouter"} <= set(
        snapshot.providers
    )
    assert snapshot.models[0].ref == "302ai/deepseek-flash"
    model = snapshot.find("agentrouter", "claude-opus-5")
    assert model is not None and model.provider is not None
    assert model.provider.npm == "@ai-sdk/anthropic"


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
    )
    snapshot = ModelCatalogSnapshot(
        providers={provider.id: provider},
        models=(model,),
        revision="sha256:test",
    )

    merged = asyncio.run(MergedModelCatalog((_SnapshotCatalog(snapshot),)).snapshot())

    assert merged.models[0] is model
    assert merged.providers["test"] is provider


def test_flat_catalog_preserves_provider_and_model_file_order(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    data = {
        "providers": [
            {"id": "z-provider", "name": "Z", "env": [], "npm": "z"},
            {"id": "a-provider", "name": "A", "env": [], "npm": "a"},
        ],
        "models": [
            {
                "id": "later",
                "provider": "a-provider",
                "name": "Later",
                "modalities": {},
                "limit": {},
            },
            {
                "id": "first",
                "provider": "z-provider",
                "name": "First",
                "modalities": {},
                "limit": {},
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    snapshot = read_model_catalog_snapshot(path)

    assert tuple(snapshot.providers) == ("z-provider", "a-provider")
    assert tuple(model.ref for model in snapshot.models) == (
        "a-provider/later",
        "z-provider/first",
    )


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        ({"provider": "missing"}, "does not name a catalog provider"),
        ({"provider": 42}, "provider must be text"),
        ({"provider": ""}, "provider is required"),
        ({"provider": {"shape": "messages"}}, "provider must be text"),
        ({"ref": "test/one"}, "must have provider and id, not ref"),
        ({"provider_override": {}}, "must use override, not provider_override"),
    ),
)
def test_flat_model_requires_an_existing_provider_and_rejects_legacy_refs(
    mutation: dict[str, object], error: str
) -> None:
    payload = _flat_catalog_data(_catalog_data())
    payload["models"][0].update(mutation)
    with pytest.raises((TypeError, ValueError), match=error):
        parse_model_catalog_data(payload)


def test_flat_catalog_rejects_missing_provider_and_invalid_override() -> None:
    payload = _flat_catalog_data(_catalog_data())
    row = payload["models"][0]
    row.pop("provider")
    with pytest.raises(ValueError, match="model at index 0 provider is required"):
        parse_model_catalog_data(payload)
    row["provider"] = "test"
    row["override"] = "not a connection override"
    with pytest.raises(TypeError, match="override must be an object"):
        parse_model_catalog_data(payload)


def test_flat_catalog_composes_refs_and_keeps_connection_overrides() -> None:
    payload = {
        "providers": [
            {"id": "with/slash", "name": "Slash", "env": [], "npm": "@ai-sdk/openai"},
        ],
        "models": [
            {
                "provider": "with/slash",
                "id": "nested/model",
                "name": "Nested",
                "modalities": {},
                "limit": {},
                "override": {
                    "shape": "messages",
                    "api": "https://test.invalid/v1",
                },
            }
        ],
    }
    providers, models = parse_model_catalog_data(payload)
    assert tuple(providers) == ("with/slash",)
    assert models[0].ref == "with/slash/nested/model"
    assert models[0].provider == ModelProvider(
        shape="messages", api="https://test.invalid/v1"
    )
    assert models[0]._toolang.provider == "with/slash"


def test_catalog_reader_attaches_origin_without_rematerializing_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models.json"
    _write_catalog(path, _catalog_data())

    def reject_replace(*args: object, **kwargs: object) -> None:
        raise AssertionError("catalog records must not be replaced after parsing")

    monkeypatch.setattr(dataclasses, "replace", reject_replace)

    snapshot = read_model_catalog_snapshot(path)
    model = snapshot.find("test", "one")

    assert model is not None
    assert model is snapshot.find("test", "one")
    assert snapshot.local is False


def test_catalog_import_drops_unknown_fields_and_keeps_float_prices(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models.json"
    payload = _catalog_data()
    payload["test"]["future_provider_field"] = {"enabled": True}
    payload["test"]["models"]["one"]["future_model_field"] = ["value"]
    _write_catalog(path, payload)

    snapshot = read_model_catalog_snapshot(path)
    model = snapshot.find("test", "one")

    assert model is not None
    assert model.cost == {"input": 1.25, "output": 2}
    exported = cast(dict[str, Any], snapshot.to_data())
    assert "future_provider_field" not in exported["test"]
    assert "future_model_field" not in exported["test"]["models"]["one"]


def test_catalog_import_rejects_models_dev_provider_map_and_combined_data(
    tmp_path: Path,
) -> None:
    provider_map = _catalog_data()
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(provider_map), encoding="utf-8")
    with pytest.raises(ValueError, match="flat cata format"):
        read_model_catalog_snapshot(path)

    path.write_text(
        json.dumps(
            {"models": {"test/one": {"id": "test/one"}}, "providers": provider_map}
        ),
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="providers must be an array"):
        read_model_catalog_snapshot(path)


@pytest.mark.parametrize(
    ("field", "value"),
    (("models", {}), ("providers", {})),
)
def test_catalog_import_validates_combined_top_level_members(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {"models": [], "providers": []}
    payload[field] = value
    path = tmp_path / "catalog.json"
    _write_catalog(path, payload)

    with pytest.raises(
        TypeError, match=rf"flat model catalog {field} must be an array"
    ):
        read_model_catalog_snapshot(path)


@pytest.mark.parametrize(
    ("limit", "expected"),
    (
        ({"context": 0, "output": 8192}, {"output": 8192}),
        ({"context": 200_000, "output": 0}, {"context": 200_000}),
        ({"context": 0, "output": 0}, {}),
    ),
)
def test_catalog_import_treats_zero_limits_as_unknown(
    tmp_path: Path,
    limit: dict[str, int],
    expected: dict[str, int],
) -> None:
    path = tmp_path / "models.json"
    payload = _catalog_data()
    payload["test"]["models"]["one"]["limit"] = limit
    _write_catalog(path, payload)

    model = read_model_catalog_snapshot(path).find("test", "one")

    assert model is not None
    assert model.limit == expected


@pytest.mark.parametrize("value", ["8192", 1.5, True, -1])
def test_catalog_import_rejects_limits_that_are_not_counts(
    tmp_path: Path,
    value: object,
) -> None:
    path = tmp_path / "models.json"
    payload = _catalog_data()
    payload["test"]["models"]["one"]["limit"] = {"context": value}
    _write_catalog(path, payload)

    with pytest.raises(TypeError, match=r"model test/one limit\.context"):
        read_model_catalog_snapshot(path)


def test_catalog_values_are_deeply_immutable(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    payload = _catalog_data()
    payload["test"]["models"]["one"]["cost"]["tiers"] = [
        {"input": 3, "tier": {"type": "context", "size": 200}}
    ]
    _write_catalog(path, payload)

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
    _write_catalog(path, payload)
    model = read_model_catalog_snapshot(path).models[0]
    assert model.reasoning_options is not None
    option = cast(dict[str, Any], model.reasoning_options[0])

    with pytest.raises(TypeError):
        option["exhaustive"] = False
    with pytest.raises(TypeError):
        option["values"][0] = "injected"
    resolved = model.with_route(model._toolang.route)
    assert resolved.reasoning_options is model.reasoning_options
    assert model.to_data()["reasoning_options"] == [
        {"type": "effort", "values": ["low", "high"], "exhaustive": True}
    ]


def test_catalog_rejects_duplicate_model_identity_as_a_complete_snapshot() -> None:
    payload = _flat_catalog_data(_catalog_data())
    payload["models"].append(dict(payload["models"][0]))

    with pytest.raises(ValueError, match="duplicate catalog model: test/one"):
        parse_model_catalog_data(payload)


def test_catalog_source_precedence_and_explicit_failure(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path / "root", "alice")
    layout.home.mkdir(parents=True)
    root = layout.root / "catalog.json"
    home = layout.home / "catalog.json"
    configured = tmp_path / "models.json"
    explicit = tmp_path / "explicit-models.json"
    for path in (root, home, configured, explicit):
        _write_catalog(path, _catalog_data())

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
        _write_catalog(path, _catalog_data())
    packaged = tmp_path / "packaged-catalog.json"
    _write_catalog(packaged, _catalog_data())
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
    _write_catalog(legacy, _catalog_data())
    _write_catalog(root, _catalog_data())

    assert resolve_model_catalog_path(layout) == root.resolve()


def test_filtered_export_round_trips_deterministically() -> None:
    snapshot = _snapshot(_provider(), (_model("one"), _model("two")))
    selected = tuple(model for model in snapshot.models if model.id == "two")

    first = dumps(snapshot.to_data(models=selected))
    second = dumps(snapshot.to_data(models=selected))
    imported, models = parse_model_catalog_data(
        _flat_catalog_data(json.loads(first, parse_float=float))
    )

    assert first == second
    assert tuple(imported) == ("test",)
    assert tuple(model.id for model in models) == ("two",)


def test_strict_export_rejects_local_only_models() -> None:
    snapshot = dataclasses.replace(
        _snapshot(_provider(), (_model("local"),)), local=True
    )

    with pytest.raises(ValueError, match="local-only catalog cannot be exported"):
        snapshot.to_data()


def test_model_protocol_hints_override_the_provider_default() -> None:
    provider = _resolve(_provider(), ChatCompletionsModelAdapter())
    model = Model(
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test", ready=True),
        provider=ModelProvider(npm="@ai-sdk/anthropic"),
    )

    assert model_adapter(provider, model) == "messages"


def test_anthropic_catalog_signal_resolves_messages_adapter() -> None:
    provider = _resolve(
        Provider(
            id="anthropic",
            name="Anthropic",
            env=("ANTHROPIC_API_KEY",),
            npm="@ai-sdk/anthropic",
        ),
        MessagesModelAdapter(),
        environ={"ANTHROPIC_API_KEY": "secret"},
    )

    assert provider_adapter(provider) == "messages"
    assert provider.api is None


def _flat_catalog_data(data: dict[str, Any]) -> dict[str, Any]:
    """Test-side external conversion from fixture provider maps to cata arrays."""

    if set(data) == {"providers", "models"}:
        return data
    providers: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    for provider_id, provider in data.items():
        providers.append(
            {key: value for key, value in provider.items() if key != "models"}
        )
        for model_id, model in provider.get("models", {}).items():
            facts = dict(model)
            if "provider" in facts:
                facts["override"] = facts.pop("provider")
            models.append({**facts, "provider": provider_id})
    return {"providers": providers, "models": models}


def _write_catalog(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(_flat_catalog_data(data)), encoding="utf-8")


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


def _provider() -> Provider:
    return Provider(
        id="test",
        name="Test",
        env=("TEST_API_KEY",),
        npm="@ai-sdk/openai-compatible",
        api="https://api.test/v1",
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


def _snapshot(provider: Provider, models: tuple[Model, ...]) -> ModelCatalogSnapshot:
    return ModelCatalogSnapshot(
        providers={provider.id: provider},
        models=models,
        revision="sha256:test",
    )


def test_flat_snapshot_joins_same_local_ids_by_provider_and_exports_selection():
    providers = {
        name: Provider(id=name, name=name) for name in ("first", "second", "empty")
    }
    models = tuple(
        Model(id="same", name=name, _toolang=ModelToolang(provider=name))
        for name in ("first", "second")
    )
    snapshot = ModelCatalogSnapshot(providers=providers, models=models, revision="test")
    assert all(
        not hasattr(provider, "models") for provider in snapshot.providers.values()
    )
    assert snapshot.find("first", "same") is models[0]
    assert snapshot.find("second", "same") is models[1]
    assert snapshot.find("empty", "same") is None
    assert snapshot.find("missing", "same") is None
    assert snapshot.to_data(models=(models[1],)) == {
        "second": providers["second"].to_data(models={"same": models[1]})
    }
    assert providers["empty"].to_data()["models"] == {}
    assert snapshot.to_data(models=()) == {}
    with pytest.raises(ValueError, match="unknown providers"):
        dataclasses.replace(snapshot, providers={"first": providers["first"]})
    with pytest.raises(ValueError, match="unique provider/model identity"):
        dataclasses.replace(snapshot, models=(models[0], models[0]))
