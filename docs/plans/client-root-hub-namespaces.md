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

1. Resolve the root at CLI/setup boundaries using existing command contracts:
   `--root` where supported, then `TOOLANG_ROOT`, then `~/.toolang`.
   Canonicalize the host root there and carry it through visiting, roaming, and
   guest launches independently of HOME, PLACE, and mounted paths. Equal canonical
   roots identify the same dataset across OS accounts/machines, subject to existing
   permissions and local ownership checks. The OS account supplies defaults and
   permissions, not another namespace dimension. Script entry keeps its existing
   rejection of global `--root`; use `TOOLANG_ROOT` there.
2. Add the concrete root to `HubConnection` and carry it through HTTP/SSE using
   the [API contract and complete before/after inventory](client-root-hub-api.md).
   Validate paths lexically on the Hub, without resolving its local filesystem.
3. Reuse dataset identity, lease fencing, batch pinning, and uncertain-send
   handling with the selected root included in connection/cache identity.
4. Reuse `toolang:<root_code>:v0:*`, store schema 5, full-root ownership records,
   and collision checks. The selected backend owns all participant, conversation,
   lease, cursor, statistics, staging, and event operations.
5. Keep one Hub lifecycle, connection pool, and presence worker. Bind each request
   or subscription to its namespace without mutating a shared current-root field.
   Reuse atomic namespace initialization and preserve its integrity checks per
   namespace. For background expiry, traverse existing root ownership keys with
   bounded SCAN and apply the existing expiry logic within each namespace; isolate
   namespace errors and preserve cancellation. No per-root lifecycle map is needed.
6. The startup root owns configuration and local process records. Startup and
   service probes create no business namespace. `/healthz` checks global service
   readiness; `/info` identifies a data partition only when a root is supplied.
   API negotiation and peer compatibility follow the linked API contract.

## Affected paths

All source paths below are relative to `src/toolang/`.

| Path | Main touchpoints |
| --- | --- |
| Human `talk` / `convo` | `cli/common/messaging.py`, `teaming/{config,schemas,client}.py` |
| Registration, leases, incoming Talk | `up/server.py`, `teaming/{api,agent_api,agent_client,lifecycle}.py` |
| Conversation tools and rules/preflight | `plugin/toolsets/convo.py`, `execution/executor/conversations.py` |
| Event publication, SSE, conversation statistics | Selected backend dependencies in `teaming/` |
| Visiting, roaming, guest root propagation | `setup/{teaming,watcher,tools}.py`, `up/{server,sandbox,mounts}.py` |
| Hub availability and metadata | `cli/toolang/commands/{ps,hub}.py`, existing Hub metadata/health routes |

Update [Messaging](../design/messaging.md), [Hosting](../design/hosting.md), and
[Layout](../design/layout.md); generate the changelog through the existing workflow.

## Acceptance

- One Hub/backend supports two roots from the same or different OS accounts,
  with same-name agents and equal or different human names: registration,
  `talk all` replies, renewals, and shutdown remain independent. Brice's root-code
  keys appear; Bryan's dataset stays unchanged.
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
