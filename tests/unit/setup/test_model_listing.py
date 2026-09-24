"""Persistent flat listing parity, invalidation, and environment boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import cast

import pytest

from toolang.base.types.model import (
    CatalogModel,
    CatalogProvider,
    CatalogSnapshot,
    ModelProvider,
    Model,
    ProviderToolang,
)
from toolang.common.cache import load_document, store_document
from toolang.common.layout import AgentLayout
from toolang.plugin.adapters.chat_completions import ChatCompletionsModelAdapter
from toolang.plugin.catalogs.models_dev.catalog import ModelsDevModelCatalog
from toolang.plugin.catalogs.models_dev.parsing import model_catalog_snapshot_from_data
from toolang.plugin.models.collections import MODEL_SCHEMA, catalog_model_dataset
from toolang.setup import watcher as watcher_module
from toolang.setup.cache_environment import environment_fingerprint
from toolang.setup.model_listing import ModelListing, build_model_listing
from toolang.setup.records import ModelListingCache
from toolang.setup.routes import catalog_environment_names, resolve_catalog_providers
from toolang.setup.watcher import SetupWatcher


def _catalog(*, provider="test", api="https://example.test/v1", env=()):
    return {
        provider: {
            "id": provider,
            "name": provider,
            "env": list(env),
            "npm": "@ai-sdk/openai-compatible",
            "api": api,
            "models": {
                name: {
                    "id": name,
                    "name": name.title(),
                    "tool_call": True,
                    "reasoning": name == "one",
                    "release_date": "2026-01",
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high"]}
                    ],
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                    "limit": {"context": 1000, "output": 100},
                    "cost": {"input": 0.12345678901234568, "output": 2},
                    "provider": {
                        "body": {"token": "public-metadata"},
                        "headers": {"X-Key": "public"},
                    },
                }
                for name in ("one", "two")
            },
        }
    }


class _Probe:
    name = "ollama"

    def __init__(self):
        self.endpoint = "http://fixed.test/v1"
        self.models = ()
        self.calls = 0
        self.hook = lambda: None

    async def snapshot(self):
        self.calls += 1
        self.hook()
        return CatalogSnapshot(
            providers={
                "local": CatalogProvider(
                    "local", "Local", api=self.endpoint, adapter="chat_completions"
                )
            },
            models=self.models,
            revision="constant-plugin-revision",
        )


class _Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.source = tmp_path / "catalog.json"
        self.source.write_text(json.dumps(_catalog()))
        self.layout = AgentLayout.resident(tmp_path, "alice")
        self.env = {}
        self.probe = _Probe()
        self.builds = 0
        self.default_api = None
        monkeypatch.setattr(
            watcher_module, "load_root_setup_envs", lambda _: dict(self.env)
        )
        monkeypatch.setattr(watcher_module, "load_setup_envs", lambda _: dict(self.env))
        monkeypatch.setattr(
            watcher_module,
            "load_model_catalogs",
            lambda config: {
                "models_dev": ModelsDevModelCatalog(Path(config["models_dev"]["path"])),
                self.probe.name: self.probe,
            },
        )
        monkeypatch.setattr(
            watcher_module,
            "load_model_adapters",
            lambda config: {
                "chat_completions": ChatCompletionsModelAdapter(
                    default_api=config.get("chat_completions", {}).get(
                        "default_api", self.default_api
                    )
                )
            },
        )
        original = watcher_module.build_model_listing

        def build(*args, **kwargs):
            self.builds += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(watcher_module, "build_model_listing", build)

    def load(self, *, agent=False, source=None):
        return asyncio.run(
            SetupWatcher(
                self.layout,
                agent_context=agent,
                model_catalog=source,
                validate_defaults=False,
            ).load_catalog_listing()
        )

    def write(self, **kwargs):
        self.source.write_text(json.dumps(_catalog(**kwargs)))

    def document(self):
        return load_document(
            self.layout.root_model_cache / "merged.json",
            kind="model_listing",
            key="merged",
            scan_content=False,
        )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return _Harness(tmp_path, monkeypatch)


def test_round_trip_preserves_every_query_field_export_and_order(tmp_path):
    data = _catalog()
    data.update(_catalog(provider="other"))
    source = model_catalog_snapshot_from_data(data, revision="source")
    source = replace(
        source,
        models=(
            replace(
                source.models[0],
                provider=ModelProvider(
                    api="https://model.test/v1",
                    body={"token": "public-metadata"},
                    _toolang=ProviderToolang(adapter="chat_completions"),
                ),
            ),
            *source.models[1:],
        ),
    )
    resolved = resolve_catalog_providers(
        source, adapters={"chat_completions": ChatCompletionsModelAdapter()}, environ={}
    )
    before = build_model_listing(resolved, allow_models=("test/two", "test/one"))
    cache = ModelListingCache(tmp_path)
    assert cache.store(before.records, inputs={}, environment_names=(), environ={})
    records = cache.load(inputs={}, environ={})
    assert records == before.records
    assert records is not None
    after = ModelListing(records)
    baseline = catalog_model_dataset(resolved)
    for model, view in zip(after.all.items, baseline.items, strict=True):
        for field in MODEL_SCHEMA.fields.values():
            assert after.all._field_values(
                model, (model.provider, model.id), field
            ) == baseline._field_values(view, view.key, field)
    for query in (
        None,
        "*",
        "test/one",
        "one",
        "*[available]",
        "*[reasoning;tool_call]",
        "*[release_date>=2026-01-01]",
        "*[modalities.input=image]",
        "*[cost.input<1]",
        "*[parameters.reasoning.effort=high]",
        ["test/two", "test/one"],
    ):
        selected = after.all.query(query)
        old = baseline.query(query)
        assert after.all.table(selected) == baseline.table(old)
        assert after.export(selected) == resolved.to_data(
            models=tuple(cast(Model, view.record) for view in old)
        )
    assert [model.id for model in after.default.items] == ["two", "one"]
    assert after.all is after.all and after.default is after.default
    assert after.providers is after.providers
    document = load_document(
        cache.path, kind="model_listing", key="merged", scan_content=False
    )
    assert set(document) == {"schema", "kind", "key", "metadata", "providers", "models"}
    assert isinstance(document["providers"], list)
    assert isinstance(document["models"], list)
    for model in document["models"]:
        assert isinstance(model, dict)
        assert isinstance(cast(dict[str, object], model)["provider"], str)


def test_warm_hit_skips_parsing_routes_build_and_scanning(harness, monkeypatch):
    first = harness.load()

    def unexpected(*args, **kwargs):
        raise AssertionError("warm listings must not rebuild or scan content")

    monkeypatch.setattr(
        "toolang.plugin.catalogs.models_dev.catalog.ModelCatalogSource.snapshot",
        unexpected,
    )
    monkeypatch.setattr(watcher_module, "_merge_catalogs", unexpected)
    monkeypatch.setattr(watcher_module, "_resolve_catalog", unexpected)
    monkeypatch.setattr(watcher_module, "load_model_adapters", unexpected)
    monkeypatch.setattr("toolang.common.cache._serialized_data_is_unsafe", unexpected)
    second = harness.load()
    assert second.records == first.records
    assert second.export(second.all.items) == first.export(first.all.items)
    assert harness.builds == 1
    assert harness.probe.calls == 2


@pytest.mark.parametrize(
    "kind", ["declared", "adapter", "bedrock", "local-template", "model-template"]
)
def test_effective_environment_changes_invalidate_missing_and_rotated_values(
    harness, kind
):
    variable = "TEST_API_KEY"
    if kind == "declared":
        harness.write(env=(variable,))
    elif kind == "adapter":
        variable = "DEFAULT_HOST"
        harness.write(api=None)
        harness.default_api = "https://${DEFAULT_HOST}/v1"
    elif kind == "bedrock":
        variable = "AWS_BEARER_TOKEN_BEDROCK"
        harness.write(provider="amazon-bedrock")
        harness.env["AWS_REGION"] = "test-region"
    elif kind == "local-template":
        variable = "OLLAMA_HOST"
        harness.write(api="https://${OLLAMA_HOST}/v1")
    else:
        variable = "MODEL_HOST"
        data = _catalog()
        for model in data["test"]["models"].values():
            model["provider"]["api"] = "https://${MODEL_HOST}/v1"
        harness.source.write_text(json.dumps(data))
    assert not harness.load().default.items
    harness.env[variable] = "synthetic-value-1"
    assert len(harness.load().default.items) == 2
    harness.env[variable] = "synthetic-value-2"
    assert len(harness.load().default.items) == 2
    harness.env["UNRELATED"] = "not-a-dependency"
    harness.load()
    assert harness.builds == 3
    document = harness.document()
    assert variable in document["metadata"]["environment_names"]
    assert [variable, sha256(b"synthetic-value-2").hexdigest()] in document["metadata"][
        "environment"
    ]
    content = (harness.layout.root_model_cache / "merged.json").read_text()
    assert "synthetic-value" not in content
    harness.env[variable] = ""
    empty_value_listing = harness.load()
    if kind in {"declared", "bedrock"}:
        assert not empty_value_listing.default.items
    del harness.env[variable]
    assert not harness.load().default.items
    assert harness.builds == 5


def test_environment_hashes_distinguish_missing_empty_and_ignore_order():
    assert environment_fingerprint(("A", "A", "B"), {"B": ""}) == (
        ("B", sha256(b"").hexdigest()),
    )
    assert environment_fingerprint(
        ("B", "A"), {"A": "one", "B": "two"}
    ) == environment_fingerprint(("A", "B"), {"A": "one", "B": "two"})


def test_dependency_extraction_uses_effective_rules_and_escaped_templates():
    snapshot = model_catalog_snapshot_from_data(
        _catalog(api="https://${HOST}/$$literal/$REGION", env=("IGNORED",)),
        revision="source",
    )
    provider = replace(
        snapshot.providers["test"],
        _toolang=ProviderToolang(env=(("PRIMARY", "ACCOUNT"), "FALLBACK")),
    )
    snapshot = replace(snapshot, providers={"test": provider})
    assert catalog_environment_names(snapshot, adapters={}) == (
        "ACCOUNT",
        "FALLBACK",
        "HOST",
        "PRIMARY",
        "REGION",
    )


def test_changed_adapter_config_recomputes_dependency_names(harness):
    harness.write(api=None)
    config = harness.layout.root_config
    config.write_text(
        '[plugin.model_adapter.chat_completions]\ndefault_api = "https://${OLD_HOST}/v1"\n'
    )
    harness.env["OLD_HOST"] = "old.test"
    assert harness.load().default.items
    config.write_text(
        '[plugin.model_adapter.chat_completions]\ndefault_api = "https://${NEW_HOST}/v1"\n'
    )
    assert not harness.load().default.items
    assert "NEW_HOST" in harness.document()["metadata"]["environment_names"]
    harness.env["NEW_HOST"] = "new.test"
    assert harness.load().default.items
    assert harness.builds == 3


def test_static_content_not_mtime_controls_invalidation(harness):
    first = harness.load()
    stat = harness.source.stat()
    os.utime(harness.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert harness.load().records == first.records
    assert harness.builds == 1
    stat = harness.source.stat()
    harness.source.write_text(harness.source.read_text().replace('"One"', '"Uno"'))
    os.utime(harness.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert harness.load().records.models[0].name == "Uno"
    assert harness.builds == 2


def test_catalog_switches_replace_the_same_cache_and_missing_source_errors(harness):
    alternate = harness.root / "alternate.json"
    alternate.write_text(json.dumps(_catalog(provider="alternate")))
    for path, expected, builds in (
        (harness.source, "test", 1),
        (alternate, "alternate", 2),
        (harness.source, "test", 3),
        (harness.source, "test", 3),
    ):
        assert harness.load(source=path).records.models[0].provider == expected
        assert harness.builds == builds
    assert sorted(p.name for p in harness.layout.root_model_cache.glob("*.json")) == [
        "merged.json"
    ]
    with pytest.raises(FileNotFoundError):
        harness.load(source=harness.root / "missing.json")


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("selector", ["default", "explicit", "environment"])
def test_cache_survives_sandbox_remounts(tmp_path, monkeypatch, agent, selector):
    host_root = tmp_path / "host"
    host_root.mkdir()
    harness = _Harness(host_root, monkeypatch)
    harness.layout.root_config.write_text("# shared root config\n")
    harness.layout.home.mkdir(parents=True)
    harness.layout.config.write_text('[allow]\nmodels = ["test/two"]\n')
    harness.write(env=("TEST_API_KEY",))
    harness.env["TEST_API_KEY"] = "same-credential"
    if selector != "default":
        harness.source = harness.source.rename(tmp_path / "external.json")
    if selector == "environment":
        harness.env["TOOLANG_MODEL_CATALOG"] = str(harness.source)
    source = harness.source if selector == "explicit" else None
    first = harness.load(agent=agent, source=source)

    def unexpected(*args, **kwargs):
        raise AssertionError("remounting unchanged inputs must hit the existing cache")

    monkeypatch.setattr(
        "toolang.plugin.catalogs.models_dev.catalog.ModelCatalogSource.snapshot",
        unexpected,
    )
    for name in ("sandbox-a", "sandbox-b"):
        guest_root = tmp_path / name
        shutil.copytree(host_root, guest_root)
        harness.layout = AgentLayout.resident(guest_root, "alice")
        guest_source = guest_root / "catalog.json"
        if selector != "default":
            guest_source = guest_root / ".inputs" / "models" / "catalog-remounted.json"
            guest_source.parent.mkdir(parents=True)
            shutil.copyfile(harness.source, guest_source)
        guest_source.touch()
        harness.env.update(
            HOME=f"/home/{name}",
            TOOLANG_ROOT=str(guest_root),
            TOOLANG_SANDBOX=f"docker:{name}",
        )
        if selector == "environment":
            harness.env["TOOLANG_MODEL_CATALOG"] = str(guest_source)
        listing = harness.load(
            agent=agent, source=guest_source if selector == "explicit" else None
        )
        assert listing.records == first.records
        assert listing.export(listing.default.items) == first.export(
            first.default.items
        )
    assert harness.builds == 1
    assert harness.probe.calls == 3


def test_root_agent_scope_and_complete_config_bytes(harness):
    harness.layout.home.mkdir(parents=True)
    harness.layout.config.write_text('[allow]\nmodels = ["test/two"]\n')
    assert len(harness.load().default.items) == 2
    assert [model.id for model in harness.load(agent=True).default.items] == ["two"]
    harness.load()
    harness.load(agent=True)
    assert harness.builds == 2
    harness.layout.root_config.write_text("# byte-only change\n")
    harness.load()
    harness.load(agent=True)
    assert harness.builds == 4
    assert (harness.layout.home_model_cache / "merged.json").is_file()


def test_dynamic_snapshot_endpoint_and_facts_invalidate(harness):
    harness.load()
    harness.probe.endpoint = "http://changed.test/v1"
    harness.load()
    harness.probe.models = (
        CatalogModel(
            "dynamic", "Dynamic", provider_id="local", limit={"context": 1000}
        ),
    )
    listing = harness.load()
    assert any(model.id == "dynamic" for model in listing.records.models)
    harness.probe.models = (replace(harness.probe.models[0], limit={"context": 2000}),)
    listing = harness.load()
    assert next(
        model for model in listing.records.models if model.id == "dynamic"
    ).limit == {"context": 2000}
    harness.load()
    assert harness.builds == 4


def test_snapshot_failure_does_not_replace_valid_listing(harness):
    harness.load()
    before = (harness.layout.root_model_cache / "merged.json").read_bytes()

    def fail():
        raise RuntimeError("required probe failed")

    harness.probe.hook = fail
    with pytest.raises(RuntimeError, match="required probe failed"):
        harness.load()
    assert (harness.layout.root_model_cache / "merged.json").read_bytes() == before


def test_config_hash_and_parse_use_same_capture(harness):
    original = '[allow]\nmodels = ["test/one"]\n'
    harness.layout.root_config.write_text(original)
    harness.probe.hook = lambda: harness.layout.root_config.write_text(
        '[allow]\nmodels = ["test/two"]\n'
    )
    assert [model.id for model in harness.load().default.items] == ["one"]
    assert harness.document()["metadata"]["inputs"]["config_files"] == [
        sha256(original.encode()).hexdigest()
    ]
    assert [model.id for model in harness.load().default.items] == ["two"]


def test_static_parse_uses_captured_bytes(harness):
    original = harness.source.read_bytes()
    harness.probe.hook = lambda: harness.write(provider="changed")
    assert harness.load().records.models[0].provider == "test"
    assert (
        harness.document()["metadata"]["inputs"]["sources"][0][1]
        == "sha256:" + sha256(original).hexdigest()
    )
    assert harness.load().records.models[0].provider == "changed"


@pytest.mark.parametrize(
    "damage",
    ["json", "checksum", "schema", "duplicate", "dangling", "rank", "type", "plugin"],
)
def test_invalid_or_changed_cache_rebuilds(harness, damage):
    expected = harness.load().records
    path = harness.layout.root_model_cache / "merged.json"
    if damage == "json":
        path.write_text("{broken")
    elif damage == "checksum":
        path.write_text(path.read_text().replace('"One"', '"Uno"'))
    else:
        document = harness.document()
        if damage == "schema":
            document["metadata"]["version"] = -1
        elif damage == "duplicate":
            document["models"].append(document["models"][0])
        elif damage == "dangling":
            document["models"][0]["provider"] = "unknown"
        elif damage == "rank":
            document["models"][0]["allowed_order"] = 999
        elif damage == "type":
            document["models"][0]["limit"]["context"] = -1
        elif damage == "plugin":
            document["metadata"]["inputs"]["plugins"] = []
        assert store_document(
            path,
            kind="model_listing",
            key="merged",
            document=document,
            scan_content=False,
        )
    assert harness.load().records == expected
    assert harness.builds == 2


def test_no_scanning_on_write_and_failed_write_keeps_valid_result(harness, monkeypatch):
    def no_scan(*args, **kwargs):
        raise AssertionError("catalog declarations must not be scanned")

    monkeypatch.setattr("toolang.common.cache._serialized_data_is_unsafe", no_scan)
    expected = harness.load()
    assert (
        "public-metadata"
        in (harness.layout.root_model_cache / "merged.json").read_text()
    )

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("toolang.common.cache.atomic_write_text", fail)
    harness.layout.root_config.write_text("# invalidate\n")
    assert harness.load().records == expected.records
    assert not [
        path
        for path in harness.layout.root_model_cache.glob(".merged.json.*")
        if path.suffix != ".lock"
    ]


def test_runtime_source_named_merged_has_separate_namespace(harness, monkeypatch):
    harness.probe.name = "merged"
    expected = harness.load().records
    monkeypatch.setattr(watcher_module, "load_tools", lambda **kwargs: {})
    asyncio.run(
        SetupWatcher(
            harness.layout, agent_context=False, validate_defaults=False
        ).refresh()
    )
    assert (harness.layout.root_model_cache / "sources" / "merged.json").is_file()
    assert harness.load().records == expected
    assert harness.builds == 1
