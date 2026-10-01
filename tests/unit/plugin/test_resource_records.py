"""Public records are the native TQ contract across resource owners."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from tq import Query, QueryError

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelProvider,
    ModelRoute,
    ModelToolang,
    Provider,
    ProviderToolang,
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
        _toolang=ModelToolang(route=route, allowed=allowed, local=local),
        **facts,
        provider="test",
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


@pytest.mark.parametrize("ready,total", [(0, 0), (0, 5), (3, 5)])
def test_provider_inspection_only_formats_stored_setup_facts(ready, total):
    provider = Provider(
        id="test",
        name="Test",
        api="https://catalog/v1",
        env=("RAW_KEY",),
        _toolang=ProviderToolang(model_count=total, ready_count=ready, route=READY),
    )
    canonical = provider.to_data()
    record = provider_record(provider)
    assert record["models"] == f"{ready}/{total}"
    assert record["api"] == READY.api
    assert record["env"] == []
    assert record["adapter"] == READY.adapter
    assert record["_toolang"] == canonical["_toolang"]
    assert set(record["_toolang"]) == {"model_count", "ready_count", "route"}
    assert not {"tags", "ref", "adapters"}.intersection(record)
    assert "models" not in canonical
    assert provider.to_data() == canonical
    assert canonical["api"] == "https://catalog/v1"
    assert canonical["env"] == ["RAW_KEY"]


def test_public_model_shape_keeps_catalog_data_and_excludes_runtime_payloads():
    value = model(
        release_date="2025-04",
        reasoning_options=({"effort": "high", "budget": 100},),
        cost={"input": 1.2, "cache_read": 0.1, "future": {"tier": 2}},
        override=ModelProvider(
            npm="sdk",
            api="https://model/v1",
            shape="chat_completions",
            mode="thinking",
            headers={"authorization": "declared-header"},
            body={"token": "declared-body"},
        ),
        experimental={
            "future": {
                "score": 42,
                "options": {"public": True},
                "_toolang": {"public": True},
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
    assert record["provider"] == "test"
    assert record["override"] == {
        "npm": "sdk",
        "api": "https://model/v1",
        "shape": "chat_completions",
        "mode": "thinking",
        "headers": {"authorization": "declared-header"},
        "body": {"token": "declared-body"},
    }
    assert record["release_date"] == "2025-04"
    assert record["reasoning_options"] == [{"effort": "high", "budget": 100}]
    assert record["cost"] == {"input": 1.2, "cache_read": 0.1, "future": {"tier": 2}}
    assert record["experimental"] == {
        "future": {
            "score": 42,
            "options": {"public": True},
            "_toolang": {"public": True},
        }
    }
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
                replace(model(), _toolang=ModelToolang(route=READY), provider=name),
            ),
            revision=name,
            local=local,
        )
        for name, local in (("cloud", False), ("runtime", True))
    )
    merged = merge_catalog_snapshots(snapshots)
    for item in merged.models:
        local = item.provider == "runtime"
        updated = replace(
            item, _toolang=item._toolang.with_allowed(False).with_route(READY)
        )
        selected = ModelCollection((updated,)).subset((updated.ref,)).entries[0]
        assert selected._toolang.local is local
        provider = provider_record(merged.providers[item.provider])
        assert model_record(selected)["tags"] == [
            "not_allowed",
            "local" if local else "remote",
        ]
        assert "tags" not in provider


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
            line=4 if scope == "here" else None,
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
        lambda: cap_collection((), root=Path("/toolang"), agent_name="test").query(
            expression
        ),
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
        root=Path("/toolang"),
        agent_name="test",
        allowed=(),
    )
    assert len(caps.items) == 2
    assert [view.data["tags"] for view in caps.query("*[tags has not_allowed]")] == [
        ["not_allowed", "local", "root", "authored"],
        ["not_allowed", "remote", "home", "configured"],
    ]
    tools = ToolCollection.from_tools({"fs__read": create_function_tool(read)})
    assert set(tool_record(tools.query()[0])) == {
        "ref",
        "toolset",
        "name",
        "description",
        "parameters",
        "tags",
    }
    assert tool_record(tools.query()[0], allowed=False)["tags"] == ["not_allowed"]
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


def test_inline_cap_has_here_scope_and_form_without_kind_tags():
    record = (
        cap_collection((cap(scope="here"),), root=Path("/toolang"), agent_name="test")
        .items[0]
        .data
    )
    assert record["tags"] == ["ready", "local", "here", "inline"]
    assert record["location"] == "/toolang/skills/reviewer:4"
    assert not {"id", "kind", "form", "scope", "origin", "allowed"}.intersection(record)


@pytest.mark.parametrize(
    "input_price,output_price,expected",
    [
        (1, 2, "  1.00 /  2.00"),
        (12, 34, " 12.00 / 34.00"),
        (123, 456, "123.00 /456.00"),
        (1234, 0.003, "1234.00 /  0.00"),
        (None, 0, "     - /  0.00"),
        (None, None, "     - /     -"),
    ],
)
def test_inspection_price_preserves_padding_and_canonical_precision(
    input_price, output_price, expected
):
    cost = {
        key: value
        for key, value in (("input", input_price), ("output", output_price))
        if value is not None
    }
    value = model(cost=cost)
    canonical = value.to_data()
    inspected = model_record(value)
    assert inspected["price"] == expected
    assert inspected["cost"] == canonical["cost"] == cost
    assert "price" not in canonical and "tags" not in canonical
    assert canonical["_toolang"]["tags"] == inspected["tags"]
    assert set(canonical["_toolang"]) == {"tags", "route"}


@pytest.mark.parametrize(
    "query",
    [
        "*[context>=200000]",
        "*[limit.context>=200000]",
        "*[tags has ready]",
        "*[_toolang.tags has ready]",
        "*[features has tool_call]",
        "*[input has image]",
        "*[output has text]",
    ],
)
def test_inspection_shortcuts_are_real_query_fields_on_every_model_selection(query):
    value = model(
        limit={"context": 200000, "output": 32768},
        modalities={"input": ("text", "image"), "output": ("text",)},
        reasoning=True,
        tool_call=True,
        temperature=False,
    )
    record = model_record(value)
    assert record["features"] == ["reasoning", "tool_call"]
    assert record["max_output"] == 32768
    assert Query.parse(query).validate({"key": "ref"}).match(record) is not None
    assert filter_models((value,), (query,)) == (value,)
    ordered, allowed = order_and_allow_models((value,), (query,))
    assert ordered == (value,) and allowed == frozenset({value.ref})


@pytest.mark.parametrize("form", ["authored", "inline", "configured", "referenced"])
@pytest.mark.parametrize("allowed", [True, False])
def test_cap_location_addresses_content_and_tags_preserve_form_and_allow(form, allowed):
    remote = form in {"configured", "referenced"}
    source = CapSource(
        origin="remote" if remote else "local",
        form=form,
        path="agents/test/agent.too"
        if form != "authored"
        else "skills/reviewer/SKILL.md",
        line=7,
        declared_ref="github://test/caps/skills/reviewer@main" if remote else None,
        updated_at="now",
        fingerprint="0" * 64,
    )
    entry = replace(
        cap(), source=source, ref=source.declared_ref if remote else cap().ref
    )
    collection = cap_collection(
        (entry,),
        root=Path("/toolang"),
        agent_name="test",
        allowed=None if allowed else (),
    )
    record = collection.items[0].data
    expected = (
        "https://github.com/test/caps/tree/main/skills/reviewer"
        if remote
        else "/toolang/agents/test/agent.too:7"
        if form == "inline"
        else "/toolang/skills/reviewer/SKILL.md"
    )
    assert record["location"] == expected
    assert set(record) == {"ref", "name", "description", "location", "tags"}
    tags = record["tags"]
    assert isinstance(tags, list)
    assert form in tags
    status = "ready" if allowed else "not_allowed"
    assert status in tags
    assert collection.query(f"*[tags has {status}]") == collection.items
    # Display locations never replace the canonical source identity.
    assert collection.items[0].source_ref == (
        source.declared_ref
        if remote
        else "inline://skills/reviewer"
        if form == "inline"
        else "root://skills/reviewer"
    )
