# Client-root isolation and Hub partitions

Status: proposed definition; no implementation in this PR.

One Hub must serve independent Toolang roots, including same-name agents under
one or multiple OS accounts. The partition key is the canonical root alone;
selecting the same root selects the same dataset, subject to existing permissions.

## Scope

- Script entry and `cd` reuse `--root`/`-r`, then `TOOLANG_ROOT`, then
  `~/.toolang`. Remove the script-only rejection; keep global options before the
  command or agent. `cd` only changes directory, without child-shell root binding.
- Complete root propagation for `talk`, every `convo` command, conversation tools
  and executor guidance, agent registration/consumption, events, and guest launch.
  Carry the original host root independently of HOME, PLACE, and guest paths.
- Apply the [Hub API contract](client-root-hub-api.md) to all 48 existing operations.
  Keep one Hub lifecycle, connection pool, and presence worker. Requests bind a
  namespace without changing shared current-root state; background expiry uses
  bounded traversal of existing ownership records and isolates partition errors.

## Module boundaries

Use small shared functions and existing factories/entry points.

| Concern | Owner | Responsibility |
| --- | --- | --- |
| OS username | New `common/accounts.py` | One `current_username()` UID lookup reused by setup and guest launch; preserve #983 behavior and explicit `teaming.human` overrides. |
| Root selection | `cli/common/context.py`, setup/launch boundaries | Resolve and canonicalize once from supported inputs; pass concrete values into core modules. |
| Partition calculation | `common/place_codes.py`, `teaming/config.py` | Reuse existing R8 calculation; derive dataset identity from backend URL and selected root. |
| Storage keys | `teaming/backend/valkey/keys.py` and backend Lua | Own key names; Lua receives the same computed prefix. Preserve `toolang:<root_code>:v0:*`, schema 5, and ownership guards. |
| Backend lifetime | `teaming/backend/`, `teaming/{messaging,lifecycle,presence}.py` | Own shared connections and root-bound backend/event views; retain initialization state per root. Request/stream cleanup never closes the shared connection. |
| HTTP context | `teaming/{schemas,client,agent_client,api,agent_api}.py` | Carry/validate root and identity, then use one selected backend throughout each request or stream. Schemas remain protocol types. |

Integrate through `cli/toolang/commands/{script,cd,ps,hub}.py`,
`cli/common/messaging.py`, `setup/{teaming,watcher,tools}.py`,
`up/{server,sandbox,mounts}.py`, `plugin/toolsets/convo.py`, and
`execution/executor/conversations.py`. Source paths are relative to `src/toolang/`.

## Acceptance

- Implicit/explicit script entry and both `cd` argument orders honor
  flag/environment/default precedence; existing script argument parsing and
  other commands remain unchanged.
- Stale `LOGNAME`/`USER` cannot change the UID-derived default human; configured
  names and captured host identity still work for guests.
- Same/different OS accounts use different roots with identical agent/conversation
  IDs: registration, Talk replies, tools, leases, and all API resources remain
  independent. Canonical aliases identify the same partition; no UID hash is added.
- Global/scoped `/info` return the defined shapes; startup and probes create no
  business keys. Invalid selectors/identities cause no writes or fallback.
- First data access initializes only its root. Deleting/corrupting an initialized
  partition causes failure without recreation and leaves healthy roots usable.
- Concurrent requests/SSE retain their root; expiry works across roots after
  restart, and shutdown closes shared connections once. Reuse existing integrity
  tests.
- Add acceptance coverage in existing CLI/setup/messaging/tool/executor suites;
  pass default offline checks and isolated Valkey wire tests. Update
  [CLI](../design/cli.md), [Messaging](../design/messaging.md),
  [Hosting](../design/hosting.md), [Events](../design/events.md), and
  [Layout](../design/layout.md), and generate the changelog through
  `too aid.too update_changelog` when implementing.

## Limits and risks

No authentication, cross-root messaging, data migration, manual namespace IDs,
new API paths, registry framework, per-root lifecycle, or unrelated CLI refactor.
Existing data stays in its original partition; root selection is not permission
control. Expiry discovery time grows with the shared Valkey keyspace. No open
questions.
