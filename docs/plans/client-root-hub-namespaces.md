# Hub namespaces selected by the client root

Status: proposed; implementation requires human confirmation.

## Goal and scope

One Hub must serve Brice's and Bryan's Toolang roots independently, even with
same-name agents. Currently HTTP requests use the Hub startup root, causing
shared conversations and `Agent owner mismatch` during registration.

Cover the six paths below. Preserve commands, flags, configuration defaults, and
local process ownership. Exclude authentication, cross-root messaging, migration,
custom namespace overrides, automatic startup, and changes to `cd`.

## Contract

1. Resolve `--root` > `TOOLANG_ROOT` > `~/.toolang` at CLI/setup boundaries.
   Canonicalize the host root there and carry it through visiting, roaming, and
   guest launches independently of HOME, PLACE, and mounted paths. Equal canonical
   paths share a dataset, including across machines; different roots do not.
2. Add the concrete root to `HubConnection`; send percent-encoded UTF-8 in
   `X-Toolang-Root` on metadata, application, and SSE requests. Validate absolute,
   normalized paths without resolving them on the Hub filesystem. Reject missing
   or invalid application selectors before storage access; never use a default.
3. Scoped `/info` echoes root and dataset identity without creating keys or
   participants. Clients verify the root and pin endpoint/root/identity together;
   application/SSE requests require `X-Toolang-Backend`. Keep existing identity,
   lease fencing, batch isolation, and uncertain-send behavior.
4. Reuse `toolang:<root_code>:v0:*`, store schema 5, full-root ownership records,
   and collision checks. The selected backend owns all participant, conversation,
   lease, cursor, statistics, staging, and event operations.
5. A Hub registry admits one backend/`HubLifecycle` per requested root, serializing
   initialization and reconciling expired leases before use. Retain lifecycles
   until shutdown and close partial admissions, workers, and connections. Requests
   retain their backend directly; namespace failures cannot disable healthy roots.
   After restart, readmit/reconcile roots on demand without global discovery.
6. The startup root owns listener/backend settings, PID, log, and lock only.
   Rootless `/info` returns process version/API metadata; rootless `/healthz`
   checks process/backend connectivity. Scoped health checks validate their root.
   Keep API/event version 0, but require root-aware peers: reject missing root
   echoes and unscoped application requests. Upgrade clients and Hub together;
   leave existing data under its original root.

## Affected paths

All source paths below are relative to `src/toolang/`.

| Path | Main touchpoints |
| --- | --- |
| Human `talk` / `convo` | `cli/common/messaging.py`, `teaming/{config,schemas,client}.py` |
| Registration, leases, incoming Talk | `up/server.py`, `teaming/{api,agent_api,agent_client,lifecycle}.py` |
| Conversation tools and rules/preflight | `plugin/toolsets/convo.py`, `execution/executor/conversations.py` |
| Event publication, SSE, conversation statistics | Selected backend dependencies in `teaming/` |
| Visiting, roaming, guest root propagation | `setup/{teaming,watcher,tools}.py`, `up/{server,sandbox,mounts}.py` |
| Scoped Hub availability | `cli/toolang/commands/{ps,hub}.py`, `up/hub.py` |

Local `top`, runtime discovery, execution/usage stores, and Talk history already
use root/instance-specific paths. No changes are planned there or in `cd`.
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
- Invalid selectors, incompatible peers, identity/ownership mismatches, collisions,
  and corrupt stores fail without fallback or cross-root access. Metadata creates
  no keys; concurrent/failed admission and restart/shutdown leak no resources.
- Run default offline checks and isolated Valkey wire tests for the shared-Hub,
  same-name exchange and independent leases/cursors/events.

## Risks and open questions

Root selection provides isolation, not authentication. Previously mixed data
cannot be attributed automatically. Registry resources grow with admitted roots;
quotas/eviction are deferred. No open design questions remain.
