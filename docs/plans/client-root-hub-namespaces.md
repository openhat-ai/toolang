# Hub namespaces selected by the client root

Status: proposed; implementation requires human confirmation.

## Goal and success criteria

One running Hub must serve `/Users/brice/.toolang` and
`/Users/bryan/.toolang` independently, including agents with identical names.
Every client selects its dataset using its canonical host Toolang root.
Connecting to the same Hub URL must not merge their participants or conversations.

The existing storage [root-code contract](../design/agent-addresses.md#short-codes)
already provides the required key format. The missing boundary is HTTP:
`HubConnection` currently carries only endpoint and dataset identity, while
`create_app` binds every request to the Hub startup root. A second owner's
same-name agent consequently fails registration with `Agent owner mismatch`.

## Scope

Cover human Talk/conversation commands, agent registration and Talk consumption,
conversation tools, team inspection, execution-event publication/subscriptions,
and root-scoped Hub availability checks. Preserve public commands, flags,
configuration defaults, and local process ownership.

Authentication, cross-root messaging, custom namespace overrides, data migration,
automatic Hub startup, and `cd` shell root inheritance are outside scope. `cd`
only changes directory; it need not preserve root selection for child commands.
`talk enabled` does not start an agent; a stopped agent cannot respond.

## Decisions

1. **Root identity.** Resolve `--root` > `TOOLANG_ROOT` > `~/.toolang` at the
   existing CLI/setup boundary and canonicalize there. Use the selected host root
   for resident, visiting, and roaming agents; never hash HOME, PLACE, the source
   path, the human name, or the Hub startup root. Guest launches carry the original
   host identity separately from mounted guest paths. Core transport/storage
   receives the concrete root string without reading environment or files.
   Canonical aliases share a dataset; different canonical roots do not. Identical
   canonical paths on different machines still share a dataset, as in the existing
   path-based contract.
2. **Wire selection.** Add the concrete namespace root to `HubConnection` and send
   it as percent-encoded UTF-8 in `X-Toolang-Root` on metadata, application, and SSE
   requests. Validate absolute, lexically normalized paths without resolving them
   against the Hub machine's filesystem. Reject malformed or missing selectors
   before dataset initialization or application access; never fall back to the
   Hub startup root. Retain full-path ownership checks behind the R8 hash.
3. **Metadata and fencing.** `/info` with a root returns version, API version,
   root, and that root's opaque dataset identity without initializing storage or
   registering a participant. Clients require the echoed root to match before
   application requests. Keep the existing backend identity formula over backend
   URL and canonical root, and require `X-Toolang-Backend` on application requests
   and SSE. Bind connection caches, admitted Talk batches, tools, and subscriptions
   to endpoint/root/identity together. Refreshing configuration cannot retarget an
   admitted operation or replay an uncertain send.
4. **Hub ownership.** The startup root owns listener/backend configuration, PID,
   log, and lock only. A Hub-owned registry lazily admits each selected root with
   its own `BackendConfig`, backend, and existing `HubLifecycle`. Serialize first
   admission per root; initialize and reconcile expired presence before exposing
   that dataset to requests. Retain admitted lifecycles until Hub shutdown; close
   every backend and worker, including partial admissions. Requests and streams
   retain their selected backend directly rather than reading mutable global
   selection. A corrupt dataset or failed namespace worker rejects that root's
   requests without disabling healthy roots. After restart, the first request for
   a root readmits it and reconciles its persisted leases; no global key discovery
   or filesystem scanning is needed.
5. **Storage.** Reuse `toolang:<root_code>:v0:<suffix>`, store schema 5, atomic
   namespace initialization, full-root ownership records, and collision checks.
   Apply the selected backend consistently to members, owner/lease fencing,
   conversations, messages, rules, cursors, statistics, staging, and events.
   Same-name agents may coexist across roots; existing conflicts within one root
   remain errors. Preserve existing datasets without copying or relabeling data.
6. **Inspection and compatibility.** Root-scoped `ps` probes metadata and health
   with its selected root. Rootless `/info` exposes process version/API metadata
   only; rootless `/healthz` checks process/backend connectivity without selecting
   a dataset, supporting existing Hub lifecycle probes. `hub status/start/stop`
   retain local process-record ownership. New clients reject old servers missing
   the root echo; new servers reject old unscoped application requests. Keep API
   and event format version 0 because payload formats remain unchanged; deploy Hub
   and clients together. Previously mixed data stays under its original root.

## Implementation touchpoints

- `setup/teaming.py`, `setup/watcher.py`, `setup/tools.py`, and `cli/common/messaging.py`:
  carry the selected host root into human, agent, and tool connections.
- `teaming/{schemas,config,client,agent_client}.py`: concrete root selection,
  metadata validation, request headers, and connection/batch fencing.
- `teaming/{api,agent_api,lifecycle}.py`: namespace registry, request-scoped
  backend dependencies, maintenance, events, and shutdown ownership.
- `execution/executor/conversations.py`: conversation resolution, rules loading,
  recall, and tool preflight must use the same scoped connection as delivery.
- `up/{server,sandbox,mounts,hub}.py`, `plugin/toolsets/convo.py`, and
  `cli/toolang/commands/{ps,hub}.py`: host/guest propagation and process probes.
- Existing messaging/setup/CLI tests and
  `tests/integration/messaging/test_valkey.py`: regressions and isolated wire tests.
  Update [Messaging](../design/messaging.md), [Hosting](../design/hosting.md), and
  [Layout](../design/layout.md) to describe the implemented contract; generate the
  changelog through `too aid.too update_changelog`.

## Related-path audit

Six affected paths share the missing client-root selection boundary:

| Path | Required correction |
| --- | --- |
| Human `talk` and every `convo` operation | Send the selected root on resolution, reads, writes, rules, and membership changes. |
| Agent lifecycle and incoming Talk | Scope registration, owner checks, leases, delivery, and cursor advancement together. |
| Conversation tools and executor guidance | Scope tool access, target preflight, rules recall, and backend fencing together. |
| Hub event publication and inspection | Scope staging, exports, SSE subscriptions, presence, and conversation statistics together. |
| Visiting, roaming, and guest setup | Retain the selected host root independently of PLACE and guest filesystem paths. |
| `ps` Hub availability | Probe the selected namespace instead of accepting another root's healthy dataset. |

Local `top`, runtime registration/address discovery, execution/usage stores, and
Talk history already use root/instance-specific paths; this audit found no shared
Hub namespace dependency in those paths. Preserve their existing isolation.

## Acceptance tests

- One Hub URL and backend serve two roots with identical human and agent names:
  both register, renew, receive/reply to `talk all`, and unregister independently;
  stopping either agent leaves the other lease and delivery intact.
- Repeat with different owners `human:brice` and `human:bryan`: no owner conflict
  across roots. Conversations, rules, messages, statistics, cursors, and event/SSE
  streams expose only their selected root, including identical conversation IDs.
- Exercise real CLI/setup selection without mocking `settings`: explicit root
  beats environment, environment beats default, aliases agree, and visiting/
  roaming/guest agents share the invoking host root's human Talk dataset.
- Brice's first successful registration creates `toolang:<brice_root_code>:v0:*`
  with the matching full root ownership value; Bryan's existing namespace remains
  byte-for-byte unchanged by Brice's operations.
- Missing/malformed selectors, mismatched metadata/identity, short-code ownership
  collisions, corrupt stores, and incompatible peers reject without cross-root
  reads/writes or fallback. Metadata probes alone create no namespace keys.
- Concurrent admission creates one lifecycle per root; failed admission leaks no
  resources. Restart readmission reconciles expired leases. Namespace failure and
  shutdown preserve other roots and cancel/close all owned tasks/connections.
- Default offline checks pass; isolated Valkey wire tests verify the shared-Hub,
  same-name exchange and independent presence/cursors/events end to end.

## Risks and open questions

Path-based namespacing isolates normal operations, not hostile clients; callers
can deliberately select another root without authentication. Earlier mixed data
cannot be automatically attributed to owners. Registry resources grow with roots
admitted during a process lifetime; eviction and deployment quotas are deferred.

No implementation questions remain. Human confirmation of this definition is
required before implementation.
