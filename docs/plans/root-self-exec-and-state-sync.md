# Root self-exec and State synchronization

Status: proposed; the complete definition requires human approval before implementation.

## Goal and scope

Support one evolution sequence: edit authored files, wait for their publication,
return from any child work, and exec the root's own runnable from its entry.
Deliver root self-exec and the replacement for `me.loaded` together in one PR.
Success requires no caller-managed receipt comparison or extra handoff stage.

This definition supersedes the root-self rejection rule in
[live State resolution](runtime-live-resolution.md) and the `loaded` contract in
[me home files](me-home-files.md). Other invocation, publication, and
[exec lifecycle](flow-exec.md) rules remain applicable. No grammar changes,
ancestor replacement, child self-exec, Setup refresh, job synchronization,
revision pinning for exec, or multi-file write transactions are included.

## Root self-exec

- Permit the active-path exception only for native `exec` and `_toolang/exec`
  when the caller has no parent, its Run ID equals its root Run ID, and the
  resolved target's module/name equals its current runnable's identity.
  Compare identities, not bare names, original entry names, or revision hashes.
  Repeated execs replace one Run's binding; they never add Run ancestry.
- Keep `run` self-calls, child self-exec, and calls/execs to waiting ancestors
  rejected. A child may finish and let the root perform its own exec. Root
  self-exec requires no pending/running descendants; it does not terminate a
  child tree. Future asynchronous-child support must preserve this condition.
- Resolve the candidate from the latest published State once. For self-exec,
  require its normalized contract to match the outgoing root's pinned contract,
  including kind, parameter/output types, and referenced structs. A newer model
  catalog cannot bypass this comparison. Bind supplied input normally; missing,
  incompatible, removed, or renamed targets fail before committing.
- Apply ordinary visibility and handoff authorization. Advertise an authorized
  root-self route for `exec` only, never `run`; filter actions individually when
  a route supports both. Preserve unnamed-entry identity and generated-inline
  code ownership rules; do not rebind an inline declaration by source line.
- Reuse same-Run commit and transfer: preserve identity, thread, captured Setup,
  authority ceilings, root totals/time limit, and original entry output contract;
  close current repeat Steps atomically and skip the outgoing body. Agic-local
  counters reset as with existing handoffs. Events, physical Step IDs, replay,
  pre-commit rejection, and post-commit failure behavior follow existing exec.
- Allow an unchanged revision. Exec means restart the selected implementation,
  not prove progress; cancellation and root limits continue to apply. Source
  edits and effects of earlier Steps are not rolled back.

## Tool name and scope

Replace `me.loaded(receipts)` with **`me.sync()`**, exposed as `me__sync`.
Its closed input schema accepts only `{}`. The verb describes an operation that
waits for disk-to-State synchronization; `loaded` describes the old predicate,
and `reload` would suggest replacing active code.

Synchronize the current agent's tracked root and home source inputs using State's
existing discovery rules. Include raw-byte identities, shadowed inputs, skill
assets, additions, and deletions. Do not scan arbitrary home files or other agents.
Independent `tasks/` and `chores/` remain outside State. Configuration source
inclusion does not promise Setup adoption, runtime success, or effective selection
of every captured declaration.

The operation waits asynchronously and returns only after success or a concrete
failure. It uses the latest publication, not the immutable State captured for
the current model call. It never switches active runnable code, captured Setup,
the current model-call snapshot, or authority ceilings. Subsequent boundaries use
normal live resolution. Root self-exec remains the explicit code-replacement step.

## Return values

Success has exactly `revision` and `files`; no redundant `loaded`/`synced` flag:

```json
{
  "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "files": [
    {
      "scope": "home",
      "key": "agent.too",
      "digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  ]
}
```

- `revision` identifies the synchronized, immutable AgentState.
- `files` is that State's complete input manifest, sorted by `(scope, key)`.
  Each item is `{scope: "home" | "root", key, digest}`. Keys retain their existing
  scope-relative spelling; digests are lowercase raw-byte SHA-256. Scope avoids
  collisions between root and home paths. Deleted/untracked files are absent,
  not successful manifest entries with null digests.
- No changes is a success with the existing revision and manifest. A full
  manifest provides one verifiable receipt for the publication without requiring
  the caller to collect write receipts.

Operational failures use `ToolResult.error` and this flat output envelope:

```json
{
  "error": "state_rejected",
  "message": "Agent State could not be synchronized; the previous publication remains active.",
  "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "files": [
    {
      "scope": "home",
      "key": "agent.too",
      "digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  ],
  "differences": [
    {
      "scope": "home",
      "key": "agent.too",
      "disk_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "state_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  ],
  "diagnostics": [
    {
      "layer": "program",
      "module_kind": "agent",
      "authored_path": "agent.too",
      "line": 4,
      "code": "invalid-program",
      "message": "The candidate program contains a syntax error."
    }
  ]
}
```

The example diagnostics illustrate the shape; preserve actual loader codes and
messages. On failure, `revision` and `files` describe the last-valid publication
captured for that result, never the rejected candidate or a later unrelated check.
Use `revision: null, files: []` if no current valid publication can be obtained,
including an unavailable synchronization service.

`differences` compares a complete captured disk manifest with that publication,
sorted by `(scope, key)`. Every unequal entry has both digest fields. A null
`state_digest` means an addition; a null `disk_digest` means a deletion; two
different digests mean a modification. Equal entries are omitted. Use
`differences: null` when a complete disk manifest cannot be captured; never use
null digest to mean an unreadable file or claim completeness with a partial list.
An empty array means no raw-file difference, not absence of a preparation error.

`diagnostics` contains structured State preparation diagnostics for the candidate
that failed; it is empty when no such diagnostics exist. Differences identify
unpublished source differences, while diagnostics explain rejection. Do not
attribute a syntax failure to every changed file or reuse stale watcher errors.

| Error code | Meaning |
| --- | --- |
| `state_rejected` | The captured candidate failed parsing, validation, composition, or resource preparation. |
| `source_changed` | Three check-and-verify attempts could not establish a stable matching publication. |
| `io_error` | Source/manifest inspection failed; unavailable differences are null. |
| `sync_unavailable` | This execution host has no State synchronization service. |

Malformed tool arguments retain the existing `invalid_request` response without
performing synchronization. Cancellation uses normal canceled-Step handling,
not a fabricated synchronization success or a new error code.

## Completion, concurrency, and ownership

1. Queue synchronization through the owning StateWatcher's sole checking task.
   Capture a fresh raw source manifest before parsing, so even invalid TOML can
   produce file differences. Prepare/publish through the existing path,
   and retain candidate diagnostics and source identities together. A rejected
   candidate returns its diagnostics and differences immediately.
2. Before success, freshly verify that the publication's complete raw manifest
   equals a stable disk observation, including removed paths. Metadata alone
   cannot establish equality. A change during capture, preparation, or final
   verification retries the check; after three attempts return `source_changed`.
   Existing bounded capture retries stay internal to an attempt.
3. Success certifies the observed disk snapshot at that verification boundary.
   Later writes belong to a later sync. It does not guarantee that an earlier
   writer's bytes survived a concurrent overwrite, or reserve this revision for
   a later exec. Continue to enforce update/delete `if_digest` preconditions.
4. Await the existing publication service without blocking the event loop or
   holding home write locks. Concurrent sync callers receive their own serialized
   check results. Background checks cannot overwrite another call's diagnostics.
   No public timeout parameter is added; existing Run cancellation/time limits
   apply. Canceling a waiter need not cancel an already-owned preparation or
   undo a publication, matching current watcher cancellation semantics.

Inject an async synchronization capability through the execution host and
`MeToolContext`; do not construct a watcher or call the parser from me. Add an
owned State result type for publication, candidate manifest, differences, and
diagnostics as needed. Keep tool schemas dependency-free. All production local
Chat, Script, CLI client, and hosted agent executor constructors must supply the
same service used by their live State source. The remote client needs no new API:
the tool executes on its agent's host.

## Compatibility and implementation touchpoints

Remove the `loaded` callable without an alias. Migrate tool filters, authoring
guidance, examples, and tests to `sync()` and its result. Keep CRUD receipts and
optimistic write preconditions. Historical recorded `me__loaded` calls remain
readable; do not rewrite execution history or add a storage migration for the name.

| Concern | Likely owners |
| --- | --- |
| Root exception, pinned contract checks, and per-action advertisements | `execution/executor/{executor,frame,tool_runtime}.py`; existing runnable route types |
| Serialized sync, source comparison, exact diagnostics/results | `state/{watcher,source,types,errors}.py`; existing preparation path |
| Tool contract and async invocation | `execution/tools/me/{__init__,schemas,types,handlers,errors}.py`; `execution/executor/steps/tool.py` |
| Service injection | `up/core.py`; `cli/toolang/commands/{script,chat/local}.py`; `cli/common/run_client.py`; execution harness |
| Acceptance coverage | `tests/integration/execution/{test_flow_exec,test_runnable_reentry,test_agic_runtime_call_scenarios}.py`; `tests/integration/plugin/test_agent_state_tool.py`; `tests/unit/state/test_watcher.py` |
| Migration guidance | `docs/{tools,agent-state,executor}.md`; runtime authoring protocol; relevant bundled templates/examples and tests |

Implementation must generate the user-visible breaking-change entry through
`too aide.too update_changelog`; a maintainer verifies it before merge. This
definition-only change does not claim shipped behavior or add a changelog entry.

## Acceptance and risks

Use deterministic gates and fake providers to verify:

1. Native Flow and model-tool root self-exec adopt published code in the same
   Run; nested repeats close once, old statements are skipped, and replay agrees.
   Cover unnamed roots, unchanged revisions, cancellation, resource ceilings,
   cumulative limits, original output contracts, and failed candidate binding.
2. Root self appears only in authorized handoffs. `run self`, child self-exec,
   waiting-ancestor targets, and changed pinned contracts remain rejected. Module
   name collisions and successive replacements use current qualified identity.
3. Successful no-op/create/update/delete sync returns the exact full manifest;
   cover raw config, shadowed root/home inputs, binary assets, roaming sources,
   unchanged metadata, and excluded independent job files.
4. Invalid source retains the last valid publication and reports the matching
   diagnostics plus additions/modifications/deletions; repair succeeds. Cover
   failure without a valid State, unreadable manifests, and unavailable service.
5. Race a write during preparation and final verification: never return stale
   success. Cover bounded source churn, concurrent syncs, cancellation, and
   background failures that must not contaminate another result.
6. End-to-end: a child writes a compatible root update, awaits `me.sync`, returns,
   and the root self-execs into it. The model-call snapshot stays fixed through
   sync; the new root code is selected only at exec. Cover local and hosted wiring.
7. Validate schema/name migration and old record inspection. Before implementation
   commits, run Ruff lint/format, ty, and the default offline pytest suite.

Self-exec can repeat side effects or loop indefinitely; it does not establish
progress. Full-manifest receipts may be large. Concurrent writes remain separate
operations, and synchronization is not an atomic sync-plus-exec transaction.
All semantic choices above are proposed decisions; no alternatives are left to
the implementer. Human approval of the complete definition is outstanding.
