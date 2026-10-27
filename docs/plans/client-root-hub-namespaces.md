# Client-root isolation and Hub partitions

Status: proposed definition; no implementation in this PR.

One Hub must serve independent Toolang roots, including same-name agents under
one or multiple OS accounts. The partition key is the canonical root alone;
selecting the same root selects the same dataset, subject to existing permissions.

## Scope

- Script entry and `cd` use `TOOLANG_ROOT`, then `~/.toolang`. Reject explicit
  global `--root`/`-r` before agent preparation or shell launch; retain other
  commands' existing root options. `cd` only changes directory, without adding
  child-shell root inheritance.
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
| HTTP context | `teaming/{schemas,client,agent_client,api,agent_api}.py` | Carry/validate root and identity, then use one selected backend throughout each request or stream. Schemas remain protocol types. |

Integrate through `cli/toolang/commands/{script,cd,ps,hub}.py`,
`cli/common/messaging.py`, `setup/{teaming,watcher,tools}.py`,
`up/{server,sandbox,mounts}.py`, `plugin/toolsets/convo.py`, and
`execution/executor/conversations.py`. Source paths are relative to `src/toolang/`.

## Acceptance

- Scripts and both `cd` argument orders select environment/default roots;
  explicit root flags fail before preparation/launch. Other commands retain them.
- Stale `LOGNAME`/`USER` cannot change the UID-derived default human; configured
  names and captured host identity still work for guests.
- Same/different OS accounts use different roots with identical agent/conversation
  IDs: registration, Talk replies, tools, leases, and all API resources remain
  independent. Canonical aliases identify the same partition; no UID hash is added.
- Startup and probes create no business keys; first scoped access initializes only
  its root. Invalid selectors/identities and corrupt data never fall back elsewhere.
- Concurrent requests/SSE retain their root; expiry works across roots after
  restart, and shutdown closes shared resources. Reuse existing integrity tests.
- Add acceptance coverage in existing CLI/setup/messaging/tool/executor suites;
  pass default offline checks and isolated Valkey wire tests. Update
  [CLI](../design/cli.md), [Messaging](../design/messaging.md),
  [Hosting](../design/hosting.md), and [Layout](../design/layout.md), and generate
  the changelog through `too aid.too update_changelog` when implementing.

## Limits and risks

No authentication, cross-root messaging, data migration, namespace overrides,
new API paths, registry framework, per-root lifecycle, or unrelated CLI refactor.
Existing data stays in its original partition; root selection is not permission
control. Background expiry work scales with stored namespaces. No open questions.
