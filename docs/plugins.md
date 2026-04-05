# Toolang Plugin Model

This document defines the plugin boundary for Toolang runtime integrations.

Toolang currently treats these areas as plugin families:

- `memory`
- `tools`
- `channels`
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

### 3.1 Memory

Memory plugins provide:

- `recall`
- `remember`
- `health`

Detailed memory behavior lives in [memory.md](./memory.md).

### 3.2 Tools

Tool plugins provide callable tool definitions and local invocation.

Detailed tool behavior lives in [tools.md](./tools.md).

### 3.3 Channels

Channel plugins handle message ingress and egress.

They may support:

- polling
- hook decoding
- outbound delivery
- health checks

Conceptual interface:

```python
class ChannelPlugin(Protocol):
    def poll(self, state: ChannelState) -> PollResult: ...
    def decode_hook(self, request: HookRequest) -> InboundDelivery | None: ...
    def deliver(self, target: ReplyTarget, message: OutboundMessage) -> DeliveryResult: ...
    def health(self) -> PluginHealth: ...
```

`PollResult` carries:

- zero or more `InboundDelivery` values
- the next plugin-owned poll cursor and metadata snapshot

### 3.4 Sandbox

Sandbox plugins provide execution environments.

They may support:

- one-shot invoke execution
- long-lived runtime spawning
- liveness probing
- stop and cleanup

Conceptual interface:

```python
class SandboxPlugin(Protocol):
    def run_invoke(self, request: SandboxInvokeRequest) -> SandboxInvokeResult: ...
    def spawn_runtime(self, request: SandboxRuntimeRequest) -> SandboxHandle: ...
    def probe(self, handle: SandboxHandle) -> SandboxStatus: ...
    def stop(self, handle: SandboxHandle) -> None: ...
    def health(self) -> PluginHealth: ...
```


## 4. Loading Model

Plugins should be selected by explicit config and loaded by name.

Recommended discovery mechanism:

- Python package entry points
  - `toolang.memory`
  - `toolang.tool`
  - `toolang.channel`
  - `toolang.sandbox`

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
[memory]
plugin = "sqlite"

[memory.config]
path = ".toolang/agents/alice/memory.db"
```

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

- `toolang.plugins` owns generic plugin discovery and loading
- `toolang.memory` owns the memory family contract
- `toolang.tools` owns the tool family contract
- `toolang.channels` owns the channel family contract
- `toolang.sandbox` owns the sandbox family contract
- first-party default implementations should use the same plugin contracts as
  external plugin packages
