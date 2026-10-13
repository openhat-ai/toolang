# Agent source error isolation and chore ownership

Approved in chat on 2026-10-13, including the subsequent source-boundary and
flat sync-receipt decisions. This PR contains the definition only.

## Goal and scope

Publish valid source changes while excluding invalid independent resources.
State owns accepted definitions and source diagnostics; drivers own activation
and execution; Setup retains environments, models, and tools.

This supersedes the Markdown-chore exclusion and root-chore inheritance in
[execution drivers](serve-execution-drivers.md) and
[State revisions](agent-state-revisions.md), and whole-candidate resource error
handling in [runnable modules](flat-runnable-modules.md). Preserve their
identity, checkpoint, execution, driver selection, and source syntax contracts.

## Source boundaries

- Each `runnables/<name>.too` is a self-contained Program: direct symbol
  references resolve only its own definitions. Helpers, types, instructions,
  and caps remain private. Export exactly one agic or flow as `<name>`; retain
  the existing named or unnamed entry forms and private module identities.
- Each authored cap exposes its path-derived identity: the Markdown filename
  stem for psyches, services, and prompts; the directory name for
  `skills/<name>/SKILL.md`. A skill and its bundled assets are one unit.
  A home `chores/<name>.md` exposes only chore `<name>`. Metadata cannot create
  additional or differently named resources.
- Preserve current `with` behavior for external URL/GitHub shorthand resources.
  Capture them with their owning Program; resolution/materialization errors
  invalidate that unit. Do not introduce sibling source imports.
- Preserve directive selection, runtime visibility, model/tool use, and driver
  handler resolution. Missing requested targets fail through normal handling;
  runtime selection creates no preparation dependency graph.
- Preserve `agent.too` as the composition entry, including its current direct
  calls to public exports, external-declaration validation, public declarations,
  and default-entry rules. Validate it against accepted exports as today.
  A missing required export can invalidate this Program, while independent
  valid resource files still publish. Do not change standalone script behavior.

No new linking, dependency graph, or iterative fallback mechanism is introduced.

## Uniform resource errors

Apply the same policy to every authored cap kind, home Markdown chore, and
Program source. Root caps use it within their shared root layer.

| Source result | Contribution to the new State |
| --- | --- |
| Valid new or edited unit | Accept its current definition and bytes |
| Invalid new or edited unit | Exclude it and report a source diagnostic |
| Deleted unit | Remove it |
| Repaired unit | Accept it and clear its diagnostic |

There is no old-definition fallback, including after restart, explicit sync,
or forced refresh. Invalid inline caps/chores exclude their enclosing Program;
do not extract declarations from malformed source. If the agent Program is
excluded, retain only the structural empty `agent` module required by State,
with no accepted `agent.too` source or synthetic public entry. Valid independent
modules remain available; unavailable handlers fail through existing admission.

Use existing precedence among currently accepted units. Invalid home caps can
expose valid root caps; valid `agent.too` chores still override home files.
Same-scope identity or public export conflicts exclude all conflicting units
with diagnostics naming every source; never choose by scan order.

Parsing, validation, and resource-local read failures exclude only their unit.
An unreadable resource directory excludes its units and receives a directory
read diagnostic; do not report confirmed deletion. Invalid shared config,
unavailable root/home, failed persistence, and internal composition errors
still reject the whole candidate and retain the last published State.

Exclusion stops future invocations/dispatch of that definition. Existing runs
follow existing snapshot rules; an active chore is not canceled. Preserve
checkpoint/history records without accepting rejected definition hashes.
Repair resumes normal reconciliation, including existing one-off and schedule
rules; a repaired unchanged completed one-off must not rerun.

## Persistence and flat sync receipts

Include home chore files in State observation, capture, preparation, and loading.
Source parsing stays in `catalog`/`lang`; State must not depend on drivers,
stores, schedulers, or emitters. Persist accepted bytes and definition terms,
with source acceptance records identifying rejected observations separately.
State exposes an immutable chore index assembled from accepted home files and
accepted `agent.too`; chores in other runnable modules stay inactive as today.

Bump the layer schema and rebuild current layers from current sources. Existing
supported historical revisions remain loadable without disk/network access;
missing historical Markdown-chore data means an empty Markdown index. Never
invent old definitions from current files. Integrity checks cover accepted
bytes, terms, and source records. Stable diagnostics contain no timestamps or
absolute host paths in revision identity; identical checks reuse the revision.

A successful sync returns only `revision` and one `files` list of flat records:

```json
{
  "revision": "<published-state-revision>",
  "files": [
    {"scope": "home", "key": "psyches/review.md", "observed_digest": "<sha256>", "accepted_digest": "<sha256>"},
    {"scope": "home", "key": "chores/cleanup.md", "observed_digest": "<sha256>", "accepted_digest": null, "code": "invalid-source", "message": "Invalid chore schedule", "line": 2}
  ]
}
```

Both digests identify raw authored bytes. Accepted readable files have equal
digests, including valid shadowed inputs. Invalid readable files have an
observed digest and null accepted digest. Unreadable files/directories have
both null and a read-error code/message. Assets share their unit's acceptance.
Deleted files are absent. Sort records by scope/key. Optional `code`, `message`,
and `line` appear directly on the affected record, never in a nested object;
omit unknown lines. No separate sources/diagnostics/differences lists or
redundant status field. Publication does not imply every file was accepted.

A fatal rejected check returns top-level `error`, `message`, the prior published
`revision` (or null), and `files: []`; it issues no per-file acceptance receipt
for the rejected candidate. Existing sync errors keep their codes. Update
inspection and tool contracts together so callers distinguish this rejection
from successful publication containing invalid resources.

## Chore ownership, authoring, and directories

Remove root chore loading/watching/fallback. Leave existing root files untouched;
users relocate them into each intended agent home. Names, IDs, threads, and
`runs.db` checkpoints remain unchanged, preserving same-name progress.

Only the first explicit file write creates home `chores/`. Creation of an agent,
reads, State preparation, startup, and monitoring create neither root nor home
chores directories. Watch existing ancestors to detect creation, atomic edits,
deletion, and recreation. Missing home chores contributes no Markdown chores.

The chore driver consumes the accepted State index without a file watcher or
source scan. Reuse its State-revision polling. Capture one State, reconcile its
definitions, and resolve the handler from that same State before dispatch.
Runtime authoring/manual-run call sites refresh State on its owner loop before
driver reconciliation; avoid waiting on that loop from itself.

Offline inspection stays independent of handler preparation and reports invalid
files alongside valid siblings. Raw reads, complete replacement, and deletion
address source paths without parsing existing content; retain digest checks and
path safeguards. CLI editing opens raw source; invalid semantic patches direct
the caller to complete replacement. Excluded definitions cannot run manually.

Remove `toolang.work`: put chore runtime in `toolang.driver.chore`, pure
source definitions/authoring in `catalog`, incoming message handling in
`toolang.driver.talk`, and transport/presence orchestration in `teaming`.
Preserve public commands, flags, driver names, talk/transport behavior, and
startup/shutdown patterns. Do not add a generic driver framework or Work/task
entity.

## Touchpoints and acceptance

Likely files: `state/{source,prepare,cache,state,watcher,errors,types}.py`,
`catalog/{chore,cap,manager}.py`, existing parsing/validation callers,
`work/` and its new owners, `up/server.py`, CLI/API/internal state and authoring
tools, focused tests, and `docs/agent-state.md`. Generate implementation
changelog entries with the repository runnable.

1. Every cap kind, chore, and Program: valid sibling edits publish beside invalid
   new/edited units; startup, repair, deletion, rename, restart, sync, and forced
   refresh obey exclusion. Unreadable directories recover with clear diagnostics.
2. Private symbols and same-name named/unnamed exports retain current behavior;
   no sibling linking is added. Existing agent cross-file calls, URL/GitHub
   references, directives, and driver resolution work and fail as specified.
3. Inline-source errors, public collisions, and root/home or file/inline
   shadowing produce deterministic accepted indexes and per-source diagnostics.
4. Upgrade excludes invalid current sources; historical loading and integrity
   validation require no current source/network. Repeated checks reuse revisions.
5. Flat receipts cover valid, invalid, unreadable, shadowed, deleted, and repaired
   sources; fatal rejection carries no candidate acceptance records. Raw CLI/API
   and tool reads/replacement/deletion repair malformed files.
6. No read/startup/watch operation creates chores directories; first authoring
   creates only home chores. Root and sibling-agent chores have no effect.
7. Dispatch uses one State for definitions and handlers. Invalid definitions
   cannot dispatch or overwrite saved hashes; active runs/checkpoints survive.
   Repair, one-off completion, schedules, and recovery retain their contracts.
8. No production imports reference `toolang.work`; public commands, drivers,
   talk-disabled transport, startup, and shutdown remain functional.

Implementation requires the default offline lint, format, type, and pytest
checks. This definition requires source/link verification and `git diff --check`.

## Risks

Invalid edits remove future availability until repair, and valid lower-scope
resources may become effective. Root chores need manual relocation. Sync field
changes require aligned consumer/tool documentation updates and changelog
migration guidance. Driver moves must preserve lifecycle behavior. No open
design questions remain; this definition PR ships no implementation.
