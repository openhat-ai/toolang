# Hub namespaces selected by the client root

Status: proposed; implementation requires human confirmation.

## Goal and scope

One Hub must serve Brice's and Bryan's Toolang roots independently, even with
same-name agents. Currently HTTP requests use the Hub startup root, causing
shared conversations and `Agent owner mismatch` during registration.

Cover the six paths below. Preserve commands, flags, configuration defaults, and
local process ownership. Exclude authentication, cross-root messaging, migration,
custom namespace overrides, automatic startup, and changes to `cd`.
Implement root propagation and backend selection centrally; all six paths reuse it.

## Contract

1. Resolve `--root` > `TOOLANG_ROOT` > `~/.toolang` at CLI/setup boundaries.
   Canonicalize the host root there and carry it through visiting, roaming, and
   guest launches independently of HOME, PLACE, and mounted paths. Equal canonical
   paths share a dataset, including across machines; different roots do not.
2. Add the concrete root to `HubConnection`; send percent-encoded UTF-8 in
   `X-Toolang-Root` on metadata, application, and SSE requests. Validate absolute,
   normalized paths without resolving them on the Hub filesystem. Reject missing
   or invalid application selectors before storage access; never use a default.
3. Scoped `/info` adds the selected root to the existing metadata response without
   creating keys or participants. Verify the echo and include root in connection
   cache matching. Reuse existing dataset identity, `X-Toolang-Backend` checks,
   lease fencing, batch pinning, and uncertain-send handling.
4. Reuse `toolang:<root_code>:v0:*`, store schema 5, full-root ownership records,
   and collision checks. The selected backend owns all participant, conversation,
   lease, cursor, statistics, staging, and event operations.
5. Keep one Hub lifecycle, connection pool, and presence worker. Bind each request
   or subscription to its namespace without mutating a shared current-root field.
   Reuse atomic namespace initialization and preserve its integrity checks per
   namespace. For background expiry, traverse existing root ownership keys with
   bounded SCAN and apply the existing expiry logic within each namespace; isolate
   namespace errors and preserve cancellation. No per-root lifecycle map is needed.
6. Preserve startup-root configuration, process ownership, and rootless
   `/info`/`/healthz` behavior and formats; scoped probes select their root.
   Require root-aware peers: new clients reject missing root echoes, and new
   servers reject unscoped application requests. Keep API/event version 0; upgrade
   Hub and clients together without migration or compatibility adapters.

## Affected paths

All source paths below are relative to `src/toolang/`.

| Path | Main touchpoints |
| --- | --- |
| Human `talk` / `convo` | `cli/common/messaging.py`, `teaming/{config,schemas,client}.py` |
| Registration, leases, incoming Talk | `up/server.py`, `teaming/{api,agent_api,agent_client,lifecycle}.py` |
| Conversation tools and rules/preflight | `plugin/toolsets/convo.py`, `execution/executor/conversations.py` |
| Event publication, SSE, conversation statistics | Selected backend dependencies in `teaming/` |
| Visiting, roaming, guest root propagation | `setup/{teaming,watcher,tools}.py`, `up/{server,sandbox,mounts}.py` |
| Scoped Hub availability | `cli/toolang/commands/ps.py`, existing Hub metadata/health routes |

Update [Messaging](../design/messaging.md), [Hosting](../design/hosting.md), and
[Layout](../design/layout.md); generate the changelog through the existing workflow.

## Acceptance

- One Hub/backend supports two roots with same-name agents and equal or different
  human names: registration, `talk all` replies, renewals, and shutdown remain
  independent. Brice's root-code keys appear; Bryan's dataset stays unchanged.
- Identical conversation IDs retain independent rules, messages, statistics,
  cursors, presence, and event/SSE streams.
- Real CLI/setup tests cover root precedence, canonical aliases, and host/guest
  propagation without mocking `settings`.
- Invalid/missing roots and incompatible peers fail before dataset access;
  existing identity/ownership guards still apply. Metadata creates no keys.
  Concurrent requests keep namespace selection independent. Background expiry
  covers both roots after startup/restart; shutdown closes shared resources.
  Reuse existing storage-integrity tests.
- Run default offline checks and isolated Valkey wire tests for the shared-Hub,
  same-name exchange and independent leases/cursors/events.

## Risks and open questions

Root selection provides isolation, not authentication. Existing data remains in
its original namespace. Background expiry work scales with stored namespaces.
The separate inherited-login identity bug is addressed in
[#983](https://github.com/openhat-ai/toolang/pull/983). No open questions remain.
