from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import pytest

from toolang.common.errors import ToolangError
from toolang.common.query import CollectionSchema, IdentitySpec, QueryDataset


@dataclass(frozen=True)
class CatalogModel:
    key: str
    provider: str
    model: str
    available: bool
    capabilities: tuple[str, ...]
    context: int


@dataclass(frozen=True)
class Route:
    provider: str
    adapter: str


@dataclass(frozen=True)
class RuntimeModel:
    key: str
    provider: str
    model: str
    alias: tuple[str, ...]
    route: Route
    streaming: bool


@dataclass(frozen=True)
class Provider:
    id: str
    local: bool
    available_models: int
    model_count: int
    adapters: tuple[str, ...]


@dataclass(frozen=True)
class InventoryItem:
    name: str
    source: str


@dataclass(frozen=True)
class Tool:
    key: str
    toolset: str
    name: str
    plugin: str
    parameters: tuple[str, ...]


@dataclass(frozen=True)
class Cap:
    key: str
    kind: Literal["psyche", "skill", "service", "prompt"]
    name: str
    scope: Literal["root", "home", "here"]
    editable: bool


@dataclass(frozen=True)
class Template:
    key: str
    kind: Literal["agent", "cap", "task", "chore"]
    name: str
    title: str
    path: str


@dataclass(frozen=True)
class Runnable:
    key: str
    kind: Literal["agic", "flow"]
    name: str
    module: str
    parameters: tuple[str, ...]
    route_actions: tuple[Literal["run", "execute"], ...]


@dataclass(frozen=True)
class PromptCompletion:
    name: str
    parameters: tuple[str, ...]
    required_parameters: tuple[str, ...]


@dataclass(frozen=True)
class Agent:
    name: str
    status: Literal["running", "stopped"]
    sandbox: str
    port: int | None
    api: bool


@dataclass(frozen=True)
class Job:
    id: str
    kind: Literal["task", "chore"]
    stage: str
    status: str
    title: str
    scheduled_at: datetime | None


@dataclass(frozen=True)
class Thread:
    id: str
    title: str
    origin: str
    status: str
    run_count: int


@dataclass(frozen=True)
class Run:
    id: str
    thread_id: str
    root_id: str
    runnable: str
    status: str
    step_count: int


@dataclass(frozen=True)
class Step:
    key: str
    path: str
    kind: str
    status: str
    index: int
    depth: int


@dataclass(frozen=True)
class Control:
    key: str
    pointer: str
    scope: str
    target: str
    index: int
    status: str


@dataclass(frozen=True)
class McpItem:
    key: str
    identity: str
    server: str
    description: str | None


@dataclass(frozen=True)
class FilesystemEntry:
    path: str
    name: str
    is_dir: bool


def _schema(
    name: str,
    item_type,
    *,
    key: str,
    paths: tuple[str, ...],
    labels: tuple[str, ...],
    separator: str | None = None,
) -> CollectionSchema:
    return CollectionSchema.from_type(
        name,
        item_type,
        key=key,
        identity=IdentitySpec(paths=paths, labels=labels, separator=separator),
    )


CATALOG_MODEL = _schema(
    "catalog models",
    CatalogModel,
    key="key",
    paths=("provider", "model"),
    labels=("provider", "model"),
    separator="/",
)
RUNTIME_MODEL = _schema(
    "runtime models",
    RuntimeModel,
    key="key",
    paths=("provider", "model"),
    labels=("provider", "model"),
    separator="/",
)
PROVIDER = _schema("providers", Provider, key="id", paths=("id",), labels=("provider",))
INVENTORY = _schema(
    "plugin inventories",
    InventoryItem,
    key="name",
    paths=("name",),
    labels=("plugin",),
)
TOOL = _schema(
    "tools",
    Tool,
    key="key",
    paths=("toolset", "name"),
    labels=("toolset", "tool"),
    separator="/",
)
CAP = _schema(
    "caps",
    Cap,
    key="key",
    paths=("kind", "name"),
    labels=("kind", "cap"),
    separator="/",
)
TEMPLATE = _schema(
    "templates",
    Template,
    key="key",
    paths=("kind", "name"),
    labels=("kind", "template"),
    separator="/",
)
RUNNABLE = _schema(
    "runnables",
    Runnable,
    key="key",
    paths=("kind", "name"),
    labels=("kind", "runnable"),
    separator=":",
)
PROMPT_COMPLETION = _schema(
    "prompt completions",
    PromptCompletion,
    key="name",
    paths=("name",),
    labels=("prompt",),
)
AGENT = _schema("agents", Agent, key="name", paths=("name",), labels=("agent",))
JOB = _schema("jobs", Job, key="id", paths=("id",), labels=("job",))
THREAD = _schema("threads", Thread, key="id", paths=("id",), labels=("thread",))
RUN = _schema("runs", Run, key="id", paths=("id",), labels=("run",))
STEP = _schema("steps", Step, key="key", paths=("path",), labels=("step",))
CONTROL = _schema(
    "controls",
    Control,
    key="key",
    paths=("pointer",),
    labels=("control",),
)
MCP_TOOL = _schema(
    "MCP tools",
    McpItem,
    key="key",
    paths=("identity",),
    labels=("tool",),
)
MCP_RESOURCE = _schema(
    "MCP resources",
    McpItem,
    key="key",
    paths=("identity",),
    labels=("URI",),
)
MCP_TEMPLATE = _schema(
    "MCP resource templates",
    McpItem,
    key="key",
    paths=("identity",),
    labels=("template",),
)
MCP_PROMPT = _schema(
    "MCP prompts",
    McpItem,
    key="key",
    paths=("identity",),
    labels=("prompt",),
)
FILESYSTEM = _schema(
    "filesystem entries",
    FilesystemEntry,
    key="path",
    paths=("name",),
    labels=("entry",),
)


NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("schema", "item", "query"),
    [
        (
            CATALOG_MODEL,
            CatalogModel("m", "openai", "gpt-5", True, ("tools",), 200_000),
            "openai/*[available;context>=200000]",
        ),
        (
            RUNTIME_MODEL,
            RuntimeModel(
                "r",
                "openai",
                "gpt-5",
                ("default",),
                Route("gateway", "responses"),
                True,
            ),
            "openai/gpt-5[alias=default;route.provider=gateway;streaming]",
        ),
        (
            PROVIDER,
            Provider("openai", False, 12, 20, ("responses",)),
            "openai[available_models>=10;adapters=responses]",
        ),
        (INVENTORY, InventoryItem("responses", "built-in"), "*[source=built-in]"),
        (
            TOOL,
            Tool("fs/read", "filesystem", "read", "core", ("path",)),
            "filesystem/*[plugin=core;parameters=path]",
        ),
        (
            CAP,
            Cap("skill/review", "skill", "review", "home", True),
            "skill/*[scope=home;editable]",
        ),
        (
            TEMPLATE,
            Template(
                "task/default", "task", "default", "Default task", "/templates/task"
            ),
            "task/*[title~=Default*]",
        ),
        (
            RUNNABLE,
            Runnable("agic:review", "agic", "review", "agent", ("input",), ("run",)),
            "agic:*[module=agent;route_actions=run]",
        ),
        (
            PROMPT_COMPLETION,
            PromptCompletion("review", ("topic", "depth"), ("topic",)),
            "review[parameters=depth;required_parameters=topic]",
        ),
        (
            AGENT,
            Agent("alice", "running", "host", 8000, True),
            "alice[status=running;api]",
        ),
        (
            JOB,
            Job("task_1", "task", "execute", "running", "Review", NOW),
            "task_1[scheduled_at>=2026-08-30T00:00:00Z]",
        ),
        (
            THREAD,
            Thread("thread_1", "Review", "chat", "active", 2),
            "thread_1[origin=chat;run_count>=2]",
        ),
        (
            RUN,
            Run("run_1", "thread_1", "run_1", "agic:review", "completed", 4),
            "run_1[runnable=agic:review;step_count>=4]",
        ),
        (
            STEP,
            Step("run_1/1.2", "1.2", "model", "completed", 2, 1),
            '"1.2"[kind=model;depth=1]',
        ),
        (
            CONTROL,
            Control(
                "c", "control://agent/run:1#step.2", "agent", "run:1", 2, "completed"
            ),
            '"control://agent/run:1#step.2"[index=2]',
        ),
        (
            MCP_TOOL,
            McpItem("t", "search", "docs", "Search docs"),
            "search[server=docs]",
        ),
        (
            MCP_RESOURCE,
            McpItem("r", "https://example.test/docs/a", "docs", None),
            '"https://example.test/docs/a"[description=null]',
        ),
        (MCP_TEMPLATE, McpItem("rt", "docs/{id}", "docs", None), '"docs/{id}"'),
        (
            MCP_PROMPT,
            McpItem("p", "summarize", "docs", "Summarize"),
            "summarize[server=docs]",
        ),
        (FILESYSTEM, FilesystemEntry("/workspace/src", "src", True), "src[is_dir]"),
    ],
    ids=lambda value: value.name if isinstance(value, CollectionSchema) else None,
)
def test_reviewed_collection_needs_only_typed_schema_data(
    schema: CollectionSchema,
    item: object,
    query: str,
) -> None:
    dataset = QueryDataset(schema, (item,))
    assert dataset.query(query) == (item,)


def test_mcp_query_requires_a_complete_snapshot() -> None:
    with pytest.raises(ToolangError, match="partial snapshot"):
        QueryDataset(
            MCP_RESOURCE,
            (McpItem("r", "https://example.test/a", "docs", None),),
            complete=False,
        )
