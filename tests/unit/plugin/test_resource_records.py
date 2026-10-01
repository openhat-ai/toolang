"""Public records are the native TQ contract across resource owners."""

import json
from dataclasses import replace

import pytest
from tq import Query, QueryError

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelProvider,
    ModelRoute,
    ModelToolang,
    Provider,
)
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.plugin.adapters.chat_completions import ChatCompletionsModelAdapter
from toolang.plugin.models.collections import ModelCollection
from toolang.plugin.models.query import filter_models, order_and_allow_models
from toolang.plugin.models.records import model_record, provider_record
from toolang.plugin.toolsets.collections import ToolCollection, tool_record
from toolang.setup.catalog import merge_catalog_snapshots
from toolang.setup.config import resolve_setup_allow
from toolang.setup.routes import resolve_catalog_providers
from toolang.state.collections import cap_collection
from toolang.state.state import CapSource, StateCap

READY = ModelRoute(adapter="chat_completions", api="https://test/v1", env=())


def model(name="one", *, route=READY, allowed=True, local=False, **facts):
    return Model(
        id=name,
        name=name,
        _toolang=ModelToolang(
            provider="test", route=route, allowed=allowed, local=local
        ),
        **facts,
    )


@pytest.mark.parametrize(
    "route,allowed,expected",
    [
        (READY, True, ["ready"]),
        (READY, False, ["not_allowed"]),
        (replace(READY, env=None), True, ["no_env"]),
        (replace(READY, api=None), True, ["no_api"]),
        (replace(READY, adapter=None), True, ["no_adapter"]),
        (ModelRoute(), False, ["not_allowed", "no_env", "no_api", "no_adapter"]),
        (replace(READY, api=None, api_env_missing=True), True, ["no_env"]),
    ],
)
@pytest.mark.parametrize("local", [False, True])
def test_model_tags_express_independent_blockers_and_origin(
    route, allowed, expected, local
):
    value = model(route=route, allowed=allowed, local=local)
    tags = model_record(value)["tags"]
    assert tags == [*expected, "local" if local else "remote"]
    assert ("ready" in tags) == value._toolang.effective_ready
    assert len(tags) == len(set(tags))


@pytest.mark.parametrize(
    "models,expected",
    [
        ((), []),
        ((model(), model("two", route=ModelRoute())), ["ready"]),
        (
            (
                model(route=replace(READY, env=None)),
                model("two", route=replace(READY, api=None)),
            ),
            [],
        ),
        (
            (model(allowed=False), model("two", allowed=False, route=ModelRoute())),
            ["not_allowed"],
        ),
        (
            (model(route=ModelRoute()), model("two", route=ModelRoute())),
            ["no_env", "no_api", "no_adapter"],
        ),
    ],
)
def test_provider_status_aggregates_only_shared_blockers(models, expected):
    record = provider_record(Provider(id="test", name="Test"), models)
    assert record["tags"] == expected
    assert list(record["models"]) == [item.id for item in models]
    assert record["_toolang"]["model_count"] == len(models)
    assert record["_toolang"]["available_models"] == sum(
        item._toolang.effective_ready for item in models
    )


def test_public_model_shape_keeps_nested_data_and_removes_private_payloads():
    value = model(
        release_date="2025-04",
        reasoning_options=({"effort": "high", "budget": 100},),
        cost={"input": 1.2, "cache_read": 0.1, "future": {"tier": 2}},
        provider=ModelProvider(
            npm="sdk",
            api="https://model/v1",
            shape="chat_completions",
            mode="thinking",
            headers={"authorization": "secret"},
            body={"token": "secret"},
        ),
        experimental={
            "future": {
                "score": 42,
                "options": {"secret": True},
                "_toolang": {"secret": True},
            }
        },
        route=replace(
            READY,
            headers={"token": "secret"},
            options={"secret": True},
            env=(("FIRST_KEY", "SECOND_KEY"),),
        ),
    )
    record = json.loads(json.dumps(model_record(value)))
    assert record["ref"] == "test/one" and record["id"] == "one"
    assert record["provider"] == {
        "npm": "sdk",
        "api": "https://model/v1",
        "shape": "chat_completions",
        "mode": "thinking",
    }
    assert record["release_date"] == "2025-04"
    assert record["reasoning_options"] == [{"effort": "high", "budget": 100}]
    assert record["cost"] == {"input": 1.2, "cache_read": 0.1, "future": {"tier": 2}}
    assert record["experimental"] == {"future": {"score": 42}}
    assert record["_toolang"]["route"]["env"] == [["FIRST_KEY", "SECOND_KEY"]]
    assert "secret" not in json.dumps(record)
    assert not {
        "model",
        "available",
        "allowed",
        "routable",
        "ready",
        "catalog",
        "streaming",
        "parameters",
    }.intersection(record)
    expressions = (
        "*[experimental.future.score>=42;cost.future.tier=2]",
        "*[future.missing=value]",
        "*[release_date=2025-04]",
    )
    for expression in expressions:
        accepted = (
            Query.parse(expression).validate({"key": "ref"}).match(record) is not None
        )
        assert bool(filter_models((value,), (expression,))) == accepted
        assert resolve_setup_allow(({"allow": {"models": [expression]}},)).models == (
            expression,
        )


def test_local_origin_survives_merge_policy_route_and_provider_projection():
    snapshots = tuple(
        ModelCatalogSnapshot(
            providers={name: Provider(id=name, name=name)},
            models=(
                replace(model(), _toolang=ModelToolang(provider=name, route=READY)),
            ),
            revision=name,
            local=local,
        )
        for name, local in (("cloud", False), ("runtime", True))
    )
    merged = merge_catalog_snapshots(snapshots)
    for item in merged.models:
        local = item._toolang.provider == "runtime"
        updated = replace(
            item, _toolang=item._toolang.with_allowed(False).with_route(READY)
        )
        selected = ModelCollection((updated,)).subset((updated.ref,)).entries[0]
        assert selected._toolang.local is local
        provider = provider_record(
            merged.providers[item._toolang.provider], (selected,)
        )
        assert provider["models"][item.id]["tags"] == [
            "not_allowed",
            "local" if local else "remote",
        ]
        assert "local" not in provider["tags"] and "remote" not in provider["tags"]


@pytest.mark.parametrize(
    "environ", [{}, {"HOST": ""}, {"HOST": "   "}, {"HOST": "service.test"}]
)
def test_missing_endpoint_variables_are_environment_blockers(environ):
    provider = Provider(
        id="test",
        name="Test",
        npm="@ai-sdk/openai-compatible",
        api="https://${HOST}/v1",
    )
    source = ModelCatalogSnapshot(
        providers={"test": provider}, models=(model(),), revision="test"
    )
    resolved = resolve_catalog_providers(
        source,
        adapters={"chat_completions": ChatCompletionsModelAdapter()},
        environ=environ,
    )
    expected = (
        ["ready", "remote"] if environ.get("HOST", "").strip() else ["no_env", "remote"]
    )
    assert model_record(resolved.models[0])["tags"] == expected
    assert resolved.models[0]._toolang.effective_ready is (expected[0] == "ready")


def cap(name="reviewer", *, origin="local", scope="root"):
    return StateCap(
        kind="skill",
        name=name,
        shape="dir",
        ref=f"{scope}://skills/{name}",
        path=f"skills/{name}",
        source=CapSource(
            origin=origin,
            form="configured"
            if origin == "remote"
            else "inline"
            if scope == "here"
            else "authored",
            declared_ref="github://test/caps/reviewer@main"
            if origin == "remote"
            else None,
            path=f"agents/test/skills/{name}" if scope == "home" else f"skills/{name}",
            updated_at="now",
            fingerprint="0" * 64,
        ),
        meta={"description": "Review changes"},
    )


@tool(name="read", description="Read a file")
def read(path: str) -> str:
    return path


@pytest.mark.parametrize("expression", ["*[", "*[ref=x]"])
def test_empty_resource_collections_still_use_tq_validation(expression):
    consumers = (
        lambda: filter_models((), (expression,)),
        lambda: ToolCollection().query(expression),
        lambda: cap_collection((), agent_name="test").query(expression),
        lambda: resolve_setup_allow(({"allow": {"models": [expression]}},)),
    )
    with pytest.raises(QueryError):
        Query.parse(expression).validate({"key": "ref"})
    for consume in consumers:
        with pytest.raises(ValueError):
            consume()


def test_cap_sources_and_tool_parameters_use_native_json_membership():
    caps = cap_collection(
        (cap(), cap(scope="home", origin="remote"), cap()),
        agent_name="test",
        allowed=(),
    )
    assert len(caps.items) == 2
    assert [view.data["tags"] for view in caps.query("*[tags has not_allowed]")] == [
        ["not_allowed", "local", "root"],
        ["not_allowed", "remote", "home"],
    ]
    tools = ToolCollection.from_tools({"fs__read": create_function_tool(read)})
    for expression in (
        "*[tags has ready]",
        "*[parameters has path]",
        "*[unknown=value]",
        "fs/*",
    ):
        query = Query.parse(expression).validate({"key": "ref"})
        assert tools.query(expression) == tuple(
            view
            for view in tools.query()
            if query.match(json.loads(json.dumps(tool_record(view)))) is not None
        )
    assert tools.query("read") == ()
    assert tools.query("*[parameters=path]") == ()
    assert tools.query("*[parameters has path]") == tools.query()


def test_policy_tags_are_evaluated_once_without_status_restrictions():
    items = (model(), model("two", allowed=False))
    policy = resolve_setup_allow(({"allow": {"models": ["*[tags has not_allowed]"]}},))
    ordered, allowed = order_and_allow_models(items, policy.models)
    published = tuple(
        replace(item, _toolang=item._toolang.with_allowed(item.ref in allowed))
        for item in ordered
    )
    assert [(item.ref, model_record(item)["tags"]) for item in published] == [
        ("test/two", ["ready", "remote"]),
        ("test/one", ["not_allowed", "remote"]),
    ]


def test_inline_cap_has_here_scope_without_form_or_kind_tags():
    record = cap_collection((cap(scope="here"),), agent_name="test").items[0].data
    assert record["tags"] == ["ready", "local", "here"]
    assert record["source"] == "inline://skills/reviewer"
    assert not {"id", "kind", "form", "scope", "origin", "allowed"}.intersection(record)
