# Toolang Plugin Model

This document defines the plugin boundary for Toolang runtime integrations.

Toolang currently treats these areas as plugin families:

- `model`
- `run_strategy`
- `tool`
- `channel`
- `sandbox`


## 1. Design Goal

Plugins make Toolang open to:

- local implementations
- remote managed implementations
- first-party integrations
- third-party integrations

The boundary should remain:

- small
- explicit
- easy to diagnose
- easy to replace


## 2. Core Principle

Toolang core owns:

- execution lifecycle
- scheduling
- local truth-layer state
- prompt and run traces
- shared bus projection

Plugins own only their domain-specific input/output operations.

Rules:

- plugins return structured data
- plugins do not mutate Toolang core state directly
- plugins do not decide scheduling policy
- plugins do not become the primary execution truth


## 3. Plugin Families

### 3.1 Model

Model plugins provide one model-turn integration.

They may support:

- one-shot invoke
- streaming text and tool-call events
- provider-managed continuation state
- provider-specific reasoning features

Detailed model-selection behavior lives in [models.md](./models.md).

Conceptual interface:

```python
class ModelPlugin(Protocol):
    name: str
    description: str | None

    def capabilities(self) -> ModelCapabilities: ...
    def resolve_selector(self, selector: str, *, environ: Mapping[str, str]) -> ResolvedModel | None: ...
    def invoke(self, target: ResolvedModel, request: ModelCall) -> ModelCallResult: ...
    def stream(
        self,
        target: ResolvedModel,
        request: ModelCall,
        *,
        on_event: ModelEventHandler,
    ) -> ModelCallResult: ...
```

### 3.2 Run Strategy

Run-strategy plugins provide high-level agent loop behavior.

They consume one bound run context and decide how model calls and tool calls
should be sequenced.

Conceptual interface:

```python
class StrategyPlugin(Protocol):
    name: str

    def run(self, context: RunContext) -> RunResult: ...
```

`RunContext` provides strategy-facing operations such as:

- `call_model()`
- `call_tool(...)`
- `call_tools(...)`
- `finish()`

Strategy code should treat `toolang.base.types.message.Message` as the
canonical shared content model. Message parts such as `text`, `tool_call`, and
`tool_result` belong to `base`, not to execution-only view or storage modules.

### 3.3 Tool

Tool plugins provide callable tool definitions and local invocation.

Detailed tool behavior lives in [tools.md](./tools.md).

Conceptual interface:

```python
class ToolPlugin(Protocol):
    name: str
    description: str | None

    def tools(self) -> Mapping[str, Tool]: ...
```

### 3.4 Channel

Channel plugins handle message ingress and egress.

They may support:

- polling
- hook decoding
- outbound delivery
- health checks

Conceptual interface:

```python
class ChannelPlugin(Protocol):
    def poll(self, state: ChannelState, context: ChannelContext) -> PollResult: ...
    def decode_hook(self, request: HookRequest, context: ChannelContext) -> InboundDelivery | None: ...
    def deliver(self, target: ReplyTarget, message: OutboundMessage, context: ChannelContext) -> DeliveryResult: ...
    def health(self, context: ChannelContext) -> PluginHealth: ...
```

`PollResult` carries:

- zero or more `InboundDelivery` values
- the next plugin-owned poll cursor and metadata snapshot

### 3.5 Sandbox

Sandbox plugins provide execution environments.

They may support:

- one-shot invoke execution
- long-lived runtime spawning
- liveness probing
- stop and cleanup

Conceptual interface:

```python
class SandboxPlugin(Protocol):
    def resolve_selector(self, raw_selector: str | None, *, configured_selector: SandboxSelector | None = None) -> SandboxSelector: ...
    def prepare(self, request: SandboxStartRequest) -> SandboxPlan: ...
    def start(self, plan: SandboxPlan) -> SandboxStartResult: ...
    def alive(self, state: SandboxState) -> bool: ...
    def stop(self, state: SandboxState, *, force: bool = False) -> None: ...
```


## 4. Loading Model

Plugins should be selected by explicit config and loaded by name.

Recommended discovery mechanism:

- Python package entry points
  - `toolang.model`
  - `toolang.run_strategy`
  - `toolang.tool`
  - `toolang.channel`
  - `toolang.sandbox`

All plugin entry points currently follow the same factory convention:

```python
def create_plugin(config: Mapping[str, Any]) -> Plugin: ...
```

Toolang does not require one shared base factory protocol for these entry
points. Loaders only rely on the callable shape above. That includes run
strategies. Strategies that do not currently use config should still accept a
mapping and ignore it.

Recommended loading flow:

1. read source config
2. resolve environment variables and relative paths at the call site
3. build an explicit plugin spec
4. load the named plugin factory
5. construct the plugin instance with explicit config

Rules:

- core modules should not infer plugin config
- lower-level runtime code should receive explicit plugin instances or explicit
  specs


## 5. Configuration

Each plugin family should use:

- one plugin name
- one config object

Examples:

```toml
[channels.telegram]
plugin = "telegram"

[channels.telegram.config]
token_env = "TELEGRAM_BOT_TOKEN"
chat_id = "12345678"
owner_chat_id = "87654321"
```

```toml
[tools.web_search]
plugin = "default"

[tools.web_search.config]
timeout_sec = 15
```

```toml
[sandbox]
plugin = "docker"

[sandbox.config]
image = "python:3.13"
network = "bridge"
```


## 6. Runtime Boundary

Plugins may provide data and effects, but Toolang runtime stays in control.

Responsibility split:

- runtime loop receives or generates input
- runtime converts that input into `Message` and run submissions
- scheduler admits work
- execution strategy completes the run
- runtime persists local state
- runtime appends bus events
- plugins perform only their domain-specific operations

The formal shared plugin boundary lives under
`toolang.base`.
Run-strategy plugins should depend on
`toolang.base.protocols.strategy` rather than importing concrete
execution modules.


## 7. Diagnostics

Every plugin interaction should be traceable.

Recommended diagnostics fields:

- plugin family
- plugin name
- request summary
- provider response summary
- degraded mode details
- error details

These diagnostics should appear in:

- prompt traces
- run traces
- optional bus events when useful


## 8. Replaceability

Toolang should treat plugins as replaceable instances, not architectural modes.

This means:

- memory behavior changes by changing the memory plugin or plugin config
- tool behavior changes by changing the tool plugin or plugin config
- channel behavior changes by changing the channel plugin or plugin config
- sandbox behavior changes by changing the sandbox plugin or plugin config

Toolang core should not grow extra strategy concepts when plugin replacement is
enough.


## 9. Recommended First Implementations

Reasonable first-party plugins:

- model
  - `openai`
  - `anthropic`
  - `openrouter`
  - `ollama`
- memory
  - `sqlite`
  - `remote-http`
- tools
  - `filesystem:default`
  - `shell:default`
  - `service_use:mcat`
  - `web_search:default`
- channels
  - `telegram`
  - `webhook`
- sandbox
  - `host`
  - `docker`


## 10. Loop-Owned Source Files

Channel-facing runtime loops should use explicit agent-home source files:

- `channels.toml`
  - named channel bindings with one plugin name and one config object
- `hooks.toml`
  - named hook bindings with request-path matching and plugin config

The current first-party channel plugins include:

- `telegram`
  - long polling ingress
  - outbound text replies
- `webhook`
  - hook decoding only

Plugins decode or deliver channel traffic, but runtime still owns:

- `Message` creation
- thread and run persistence
- run lifecycle
- bus projection writes


## 11. Package Boundary

Rules:

- `toolang.base` owns the shared plugin contracts
- `toolang.models` owns the built-in model plugins
- `toolang.tools` owns the built-in tool plugins
- `toolang.channels` owns the built-in channel plugins
- `toolang.sandboxes` owns the built-in sandbox plugins
- `toolang.strategies` owns the built-in run strategies
- first-party default implementations should use the same plugin contracts as
  external plugin packages
