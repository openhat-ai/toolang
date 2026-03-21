# Toolang Plugin Model

This document defines the plugin boundary for Toolang runtime integrations.

Toolang currently treats these areas as pluggable:

- `memory`
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

### 3.1 Memory

Memory plugins provide:

- `recall`
- `remember`
- `health`

Detailed memory behavior lives in [memory.md](./memory.md).

### 3.2 Channel

Channel plugins handle message ingress and egress.

They may support:

- polling
- hook decoding
- outbound delivery
- health checks

Conceptual interface:

```python
class ChannelPlugin(Protocol):
    def poll(self, state: ChannelState) -> list[InboundDelivery]: ...
    def decode_hook(self, request: HookRequest) -> InboundDelivery | None: ...
    def deliver(self, target: ReplyTarget, message: OutboundMessage) -> DeliveryResult: ...
    def health(self) -> PluginHealth: ...
```

### 3.3 Sandbox

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
- runtime converts that input into `Message` and turn requests
- scheduler admits work
- execution strategy completes the turn
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
- channel behavior changes by changing the channel plugin or plugin config
- sandbox behavior changes by changing the sandbox plugin or plugin config

Toolang core should not grow extra strategy concepts when plugin replacement is
enough.


## 9. Recommended First Implementations

Reasonable first-party plugins:

- memory
  - `sqlite`
  - `remote-http`
- channel
  - `telegram`
  - `webhook`
- sandbox
  - `host`
  - `docker`
