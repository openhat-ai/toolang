# Toolang Memory Plugin Model

This document defines how Toolang uses memory during runtime execution.

Memory is a plugin concern.

Toolang core does not define a separate memory-strategy system. If memory
behavior needs to change, that change should happen by:

- selecting a different memory plugin
- changing that plugin's configuration


## 1. Core Rule

Toolang has one external memory concept:

- `memory plugin`

Toolang does not define:

- `memory strategy`
- `memory policy`
- `memory backend` as a separate public runtime concept

Those are plugin-internal concerns.


## 2. Runtime Boundary

Toolang runtime is responsible for:

- deciding when memory is used
- passing explicit recall and write requests
- recording memory diagnostics in prompt traces and run traces

The memory plugin is responsible for:

- recalling memory
- writing memory
- reporting health

The memory plugin must not:

- mutate runtime scheduling
- rewrite prompt text directly
- decide turn admission
- append bus events by itself


## 3. Minimal Interface

```python
class MemoryPlugin(Protocol):
    def recall(self, request: MemoryRecallRequest) -> MemoryRecallResult: ...
    def remember(self, batch: MemoryWriteBatch) -> MemoryWriteResult: ...
    def health(self) -> PluginHealth: ...
```

Rules:

- the plugin returns structured data
- prompt rendering remains a Toolang runtime responsibility


## 4. Recall

Recall happens before the final prompt is built.

Suggested request shape:

```python
@dataclass
class MemoryRecallRequest:
    agent_uri: str
    agent_id: str
    thread_id: str
    turn_id: str
    origin: str
    sender: str
    query_text: str | None
    execution_strategy: str
    limits: MemoryLimits
    meta: dict[str, Any]
```

Suggested response shape:

```python
@dataclass
class MemoryRecallResult:
    items: list[MemoryItem]
    facts: list[MemoryFact]
    summaries: list[MemorySummary]
    provider: str
    diagnostics: dict[str, Any]
```


## 5. Remember

Remember happens after a turn completes and local state is saved.

Suggested write shape:

```python
@dataclass
class MemoryWriteBatch:
    agent_uri: str
    agent_id: str
    thread_id: str
    turn_id: str
    origin: str
    entries: list[MemoryEntry]
    meta: dict[str, Any]
```

Suggested result shape:

```python
@dataclass
class MemoryWriteResult:
    written: int
    provider: str
    diagnostics: dict[str, Any]
```

Rules:

- memory writes happen after local turn state is durable
- memory writes should not be required for turn success
- write failures should be recorded and may be retried later


## 6. Failure And Ordering

Memory is an augmentation layer, not the execution truth.

Rules:

- recall failure may degrade a turn, but should not crash the runtime by
  default
- write failure should not retroactively fail a completed turn
- failures should be visible in diagnostics and traces

Recommended order:

1. write local turn state
2. write prompt trace
3. perform memory write
4. append shared bus projection events
5. perform outbound delivery


## 7. Configuration

Memory configuration should identify:

- plugin name
- plugin instance settings

Example:

```toml
[memory]
plugin = "sqlite"

[memory.config]
path = ".toolang/agents/alice/memory.db"
max_recent_items = 50
```

Or:

```toml
[memory]
plugin = "remote-http"

[memory.config]
base_url = "https://memory.example.com"
namespace = "alice"
api_key_env = "ALICE_MEMORY_API_KEY"
```

Rules:

- runtime resolves environment variables before creating the plugin
- core modules receive explicit configuration values
- plugin-specific behavior belongs in plugin config, not in Toolang core


## 8. Diagnostics

Prompt traces and run traces should record:

- selected memory plugin
- recall request summary
- recall diagnostics
- write diagnostics
- degraded mode indicators


## 9. Example Plugin Types

Possible memory plugins:

- `sqlite`
- `sqlite-vector`
- `remote-http`
- `openmemory`
- vendor-specific cloud memory
