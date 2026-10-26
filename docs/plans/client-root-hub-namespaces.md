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
5. Use an app-local dictionary keyed by full root and one initialization lock.
   Retain the startup backend/lifecycle; lazily create a backend/`HubLifecycle`
   pair per additional root using existing implementations. Publish only after
   initialization/reconciliation; clean up on failure and shutdown. Pass one
   selected backend through each request/subscription. After restart, initialize
   additional roots on demand.
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
  Concurrent initialization starts one lifecycle per root; failure and shutdown
  close owned resources. Reuse existing storage-integrity tests.
- Run default offline checks and isolated Valkey wire tests for the shared-Hub,
  same-name exchange and independent leases/cursors/events.

## Risks and open questions

Root selection provides isolation, not authentication. Existing data remains in
its original namespace. Backends/workers grow with roots used during a Hub
lifetime. No open design questions remain.
