# Define Compact Current-Agent Authoring Tools

## Status

Approved on 2026-09-02.

## Goal

Expose one compact `_me` authoring surface for the current agent's tasks,
chores, psyches, skills, services, prompts, and home flow modules. Models choose
an operation and a `kind`; Toolang validates the supplied content with the
kind's owning domain before changing authored files.

This feature changes only current-agent authored-data tools. It does not add a
new runtime toolset, state-application mechanism, or dynamic runnable call.

## Success Criteria

- `_me` exposes exactly five model-facing leaves: `list`, `get`, `create`,
  `update`, and `delete`.
- The tools accept the closed kinds `task`, `chore`, `psyche`, `skill`,
  `service`, `prompt`, and `flow`.
- Each operation-kind pair has an explicit input contract; unsupported
  operations, fields, and field combinations fail before filesystem mutation.
- Existing ready task/chore and psyche, skill, service, and prompt behavior is
  preserved behind the compact dispatch surface.
- Flow actions manage only direct
  `agents/<current-agent>/flows/<name>.too` files and accept only valid flow
  modules that compose with the current agent module and other home flows.
- Writes remain confined to the current agent's home layer, use existing locks
  and atomic publication, and never select an arbitrary path or agent.
- Source mutation does not itself publish or reload `AgentState`; existing
  watcher and `_too__reload` behavior remains authoritative.
- The default verification suite passes.

## Model-Facing Surface

The current kind-specific leaves are replaced atomically by:

```text
_me__list(kind)
_me__get(kind, key)
_me__create(kind, key?, content)
_me__update(kind, key, content, if_digest?)
_me__delete(kind, key, if_digest?)
```

`kind` is a JSON Schema enum, not free text. `key` is the stable task/chore id
for job kinds and the authored name for psyche, skill, service, prompt, and
flow. Create allocates job ids, so task/chore create rejects `key` while
named-resource create requires it. Get and update always require `key`.
Delete's `kind` enum contains only the five named resource kinds because
task/chore deletion is unsupported. Task/chore list and get address only ready
documents.

Create and update remain separate. A combined write/upsert operation would
save one leaf but make accidental replacement harder to prevent and would
require another mode discriminator. Five narrow actions are the compact
surface; action inference from missing fields is not part of the protocol.

Validation is not another model-facing leaf and create/update have no dry-run
mode. They validate the exact candidate as part of the same mutation boundary
and leave authored files unchanged on failure. A separate validate-then-write
sequence would enlarge the tool surface and could become stale between calls.
Editor linting or a future human-facing validation API may reuse the pure
validators without adding `_me__validate`.

The operation matrix is:

| Kind | `key` meaning | List | Get | Create | Update | Delete |
| --- | --- | --- | --- | --- | --- | --- |
| `task` | stable id | yes | yes | yes | yes | no |
| `chore` | stable id | yes | yes | yes | yes | no |
| `psyche` | authored name | yes | yes | yes | yes | yes |
| `skill` | authored name | yes | yes | yes | yes | yes |
| `service` | authored name | yes | yes | yes | yes | yes |
| `prompt` | authored name | yes | yes | yes | yes | yes |
| `flow` | public/authored name | yes | yes | yes | yes | yes |

Task and chore deletion is not added implicitly. Their lifecycle remains owned
by the job system.

`delete` never aliases archive. For psyche, skill, service, prompt, and flow it
means physical authored-file removal. The existing job vocabulary treats
archive as a reversible stage move and permits destructive job deletion only
from archived storage; mapping ready task/chore delete to archive would give
one leaf two incompatible meanings. A later lifecycle feature may add explicit
`archive` and `restore` leaves together with their archived read/delete rules.

## Content Contracts

`content` is a closed JSON object. The model-facing schema lists the union of
supported property names and sets `additionalProperties = false`; the runtime
then selects the exact content schema from `(operation, kind)`. Provider-side
JSON Schema validation is an early aid, not the authority.

| Kind | Create content | Update content |
| --- | --- | --- |
| `task` | required `body`; optional `title` | one or more of `body`, `title` |
| `chore` | required `body`; optional `title`, `schedule` | one or more of `body`, `title`, `schedule` |
| `psyche` | required `body` | required `body` |
| `skill` | required `description`, `body` | one or more of `description`, `body` |
| `service` | required `description`, `transport`, `target`; optional `body`, `headers`, `env` | one or more service fields |
| `prompt` | required `body` | required `body` |
| `flow` | required `source` | required `source` |

Omitted update fields retain their current values. Empty `body` is a real
replacement. Existing field semantics remain unchanged: a blank task/chore
title clears it, chore schedules are RRULE strings, service transport is
`http` or `stdio`, headers are a string map, and env is a non-empty list of
valid environment-variable names. A content field belonging to another kind
is rejected instead of ignored.

`if_digest` is an optional SHA-256 optimistic-concurrency precondition shared
by update and delete. When supplied, it must match the current authored file
exactly; mismatch leaves the resource unchanged. List and get return the digest
so a caller may opt into read-modify-write safety without making an extra read
mandatory for direct updates.

## Validation And Dispatch

Validation has four ordered boundaries:

1. **Envelope:** reject unknown arguments, unknown kinds, missing or forbidden
   `key`, malformed keys, invalid digest syntax, and unsupported operation-kind
   pairs.
2. **Typed content:** decode the content through a closed schema selected from
   the `(operation, kind)` registry. Required, unsupported, and empty update
   fields fail here. Runtime decoding is mandatory because the current
   function-tool helper does not enforce its declared JSON Schema.
3. **Domain:** delegate to the existing owner instead of duplicating rules:
   `JobFile`/`AuthoredJobs` for tasks and chores, and
   `CapFile`/`AuthoredCaps` for psyche, skill, service, and prompt.
4. **Storage:** resolve the current agent home from `ToolContext`, validate the
   canonical owned path, check the optional digest under the mutation lock,
   and publish atomically.

Validation errors identify the operation, kind, and failing field and produce
no partial mutation. Dispatch uses an exact registry; it never constructs a
function name from model-provided text and never falls through to another
toolset.

Every mutation has one explicit commit point. The handler validates and
serializes the complete candidate first, then acquires the owning mutation
lock, rechecks existence and `if_digest`, and performs the atomic replacement
or deletion. An error reported before that commit leaves create targets absent
and update/delete targets byte-for-byte unchanged. No validation, projection,
or other fallible domain work runs after the commit; the success result is
prepared from already validated data. A process loss after the operating
system commits but before the caller receives the response is an uncertain
delivery, not a reported validation failure, and the caller can resolve it by
get plus digest.

## Flow Authoring

For `kind = flow`, `key` is the public flow name and the only possible target
is `flows/<key>.too` below the current agent home. Nested paths, separators,
non-canonical runnable names, symlinks, and non-regular targets are rejected.

Flow create and update validate an in-memory candidate before publication:

1. parse and semantically validate `source` as a complete Toolang `Program`;
2. apply the existing flow filename/export contract for `flows/<key>.too`;
3. substitute the candidate into the current home program sources; and
4. run the existing public-runnable composition check, including conflicts
   with agent-module agics/flows and other flow modules.

The authoring path must reuse the language and State preparation functions that
own those checks. `_me` does not maintain a second flow parser or a weaker list
of rules. An invalid candidate returns the same ordered diagnostic vocabulary
as State preparation and leaves the prior file unchanged.

Create fails when the target exists. Update fails when it does not exist.
Identical update returns `changed = false`. Delete validates only identity,
path safety, existence, and the optional digest; it may remove an invalid flow
so the agent can recover from a manually authored bad file.

Manual filesystem edits remain allowed and may temporarily be invalid. This
feature's stronger preflight applies only to `_me` mutations.

## Results

All operations use one result envelope:

```text
list    -> {kind, items}
get     -> {kind, item}
create  -> {kind, item, created: true}
update  -> {kind, item, changed}
delete  -> {kind, key, deleted: true}
```

Every item has `key`, home-relative `path`, and `digest`. Get/create/update add
the normalized kind-specific `content`; list omits large bodies/source and
retains the existing compact metadata such as task title/thread id, chore
schedule, cap metadata, or flow byte count. Results never expose an arbitrary
target selector.

## Errors

Expected `_me` failures remain failed tool calls and also return a structured
error in the tool output:

```json
{
  "error": {
    "code": "invalid_flow",
    "message": "flow source is invalid",
    "operation": "create",
    "kind": "flow",
    "key": "research",
    "issues": [
      {
        "code": "invalid-program",
        "path": "content.source",
        "message": "unknown runnable: collect",
        "line": 4,
        "column": 7
      }
    ],
    "truncated": false
  }
}
```

Stable top-level codes are `invalid_request`, `unsupported_operation`,
`not_found`, `conflict`, `digest_mismatch`, `invalid_content`, `invalid_flow`,
and `storage_error`. `operation` and `kind` are always present after envelope
decoding; supplied `kind` and `key` values are retained when safe to echo.
Issues use model-facing field paths.
Flow issues retain the ordered State diagnostic code and source line/column
when available. At most 32 issues are returned; `truncated` reports omitted
issues. Errors never include stack traces, secrets, temporary paths, or source
content.

The canonical `ToolResultPart` keeps its non-null error summary so execution,
progress, and inspection classify the Step as failed. Its existing `output`
object carries the structured error so every model adapter receives actionable
details, including adapters that expose only output plus an error flag. A
narrow structured tool-failure exception transports these two values through
generic dispatch; unexpected exceptions retain the existing bounded fallback
and internal logging.

## State Visibility

Authored mutations and executable state remain separate:

- `_me` validates and atomically changes home source files;
- `StateWatcher` remains the only producer and publisher of prepared
  `AgentState` revisions;
- a later root run observes the watcher-produced current revision through the
  existing acceptance path; and
- an active model may use the existing `_too__reload` runtime action when it
  explicitly needs the latest valid revision.

No `_me` result claims that a saved flow is active. It reports authored file
identity and digest only.

## Compatibility

This is an intentional breaking tool-identity change. The 28 current
kind-specific `_me` leaves and hypothetical kind-specific flow/lifecycle
leaves are not coexposed with the compact five because aliases would preserve
the model-context cost this feature removes. Selectors, examples, docs, and
tests move to the five compact `_me` leaves.

Historical execution records are not rewritten. New runs resolve only compact
identities; a retry whose captured resource snapshot requires a removed leaf
fails as unavailable under the existing resource-snapshot rule.

## Scope

Included:

- the five compact `_me` definitions, schemas, dispatch, and uniform results;
- migration of all existing `_me` kinds to the compact protocol;
- home flow list/get/create/update/delete;
- flow candidate validation and safe atomic authoring;
- digest projection and optional optimistic concurrency for mutations; and
- selector, documentation, and offline test migration.

Excluded:

- root or shared flows, flow installation, rename, or imports;
- job archive, restore, reopen, run, cancel, delete, or other lifecycle and
  execution controls;
- automatic watcher publication or reload after mutation;
- changes to `_too__run`, `_too__execute`, `_too__reload`, runtime controls,
  or Flow syntax;
- public API or CLI CRUD redesign outside the `_me` toolset;
- a model-facing validate action or create/update dry-run mode; and
- compatibility aliases or historical-record migration.

## Implementation Touchpoints

- `src/toolang/execution/tools/agent_state`: split the current module into a
  package with the stable `create_toolset` entry point, protocol schemas,
  operation-kind registry, handlers, and result projection.
- `src/toolang/base` tool errors and
  `src/toolang/execution/executor/steps/tool.py`: carry structured expected
  failure output while preserving failed Tool Step semantics and the existing
  string summary.
- `src/toolang/catalog/job.py` and `cap.py`: reuse existing parsing and CRUD;
  add only narrow digest/projection helpers needed by every caller.
- `src/toolang/state/source.py`, `prepare.py`, and `state.py`: expose a pure
  in-memory home-program candidate validator that reuses current flow parsing,
  export, and composition behavior without publishing State.
- `src/toolang/common/files.py`: reuse atomic writes and file locking; add a
  shared compare-before-mutate helper only if existing catalog locks cannot
  perform the digest check atomically.
- `pyproject.toml`: keep the `_me` entry point stable while its leaf set changes.
- `docs/tools.md`, selectors/examples, and tool/resource tests: document and
  verify the compact identities and flow behavior.

## Acceptance Tests

1. `_me` publishes exactly `list`, `get`, `create`, `update`, and `delete`; no
   kind-specific legacy or flow-specific leaf is exposed.
2. List/get/create/update expose the seven applicable kinds, delete exposes
   only named resource kinds, every schema rejects additional properties, and
   none exposes agent, root, home, scope, or arbitrary path inputs.
3. Runtime validation rejects unknown kinds, unsupported operation-kind pairs,
   missing or forbidden `key`, malformed keys/digests, missing required
   content, foreign-kind fields, and empty updates without changing files.
4. Task and chore dispatch preserves ready-document id allocation, title and
   body updates, RRULE validation, and thread-id projection.
5. Psyche, skill, service, and prompt dispatch preserves authored home-only
   lookup, required metadata, allowed-field, transport, headers, env, and body
   validation.
6. List/get/create/update/delete flow operations are confined to direct home
   flow files and reject traversal, nested paths, symlinks, and non-regular
   targets.
7. Flow create/update rejects invalid syntax, semantic errors, filename/export
   mismatch, case-fold collisions, and public runnable conflicts before
   publication; valid named and unnamed exports succeed.
8. Flow delete can remove a manually authored invalid file without parsing it.
9. Update/delete digest mismatch is atomic and leaves every supported kind
   unchanged; identical update reports `changed = false`.
10. Every expected failure produces a failed Tool Step, a concise string
    summary, and bounded structured output with stable code, operation, kind,
    key when known, and ordered field or flow diagnostics.
11. `_me` mutation does not publish or reload State; the next root acceptance
    and explicit `_too__reload` retain their current behavior.
12. Current selectors, setup snapshots, model definitions, docs, and examples
    use only the compact identities; historical records remain decodable.
13. The default offline verification suite passes.

## Risks

- Generic tools trade a smaller model tool list for more runtime dispatch. The
  closed registry and explicit errors must remain the authority even when a
  provider accepts a conditionally invalid payload.
- The shared `content` schema still describes fields for all kinds. It reduces
  repeated tool metadata substantially, but model accuracy must be covered
  with representative provider-independent tool-call fixtures.
- The leaf migration intentionally breaks exact legacy selectors and captured
  resource identities.
- Full flow candidate validation may be more expensive than parsing one file;
  it must reuse bounded State preparation and perform no publication.
- Candidate validation and file replacement must share a mutation boundary or
  concurrent manual/tool writes could invalidate the preflight result.

## Open Questions

None.
