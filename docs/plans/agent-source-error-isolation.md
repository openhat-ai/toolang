# Agent source error isolation and chore ownership

Status: proposed. State ownership, home-only chores, lazy directories, and
nonblocking chore errors were confirmed in chat on 2026-10-13. The complete
file-isolation policy below awaits human approval before implementation.

## Goal and scope

Publish valid agent source changes even when an independent chore, psyche, or
runnable source is invalid. State owns accepted definitions and source
diagnostics; drivers own activation and execution. Setup retains installed
environment, model, and tool configuration.

This plan replaces the independent-chore exclusion and root-chore inheritance
in [execution drivers](serve-execution-drivers.md) and the corresponding
exclusion in [State revisions](agent-state-revisions.md). Keep their identity,
checkpoint, driver selection, and execution contracts.

## Source acceptance

Use the same policy for each immediate home `chores/<name>.md`, each authored
psyche definition, and each Program source (`agent.too` or an immediate
`runnables/<name>.too`). A directory-form psyche and its materialized files are
one unit. Root psyches use the same policy within the shared root layer.

| Source result | Accepted definition | Publication |
| --- | --- | --- |
| Valid new or edited unit | Current validated unit | Publish with other accepted changes |
| Invalid edited unit | Its last accepted unit at the same logical source path | Publish other valid changes and report the error |
| Invalid new unit | Omit the unit | Publish other valid changes and report the error |
| Deleted unit | Remove it; do not restore an older version | Publish the removal |
| Repaired unit | Accept its current definition and clear its diagnostic | Publish the repair |

Fallback is per source path and scope, never to a similarly named resource or
another agent. An invalid replacement after a published deletion is new and has
no fallback. A rename is deletion plus addition. Retain complete accepted
units, including module-local caps and materialized psyche files.

An invalid inline chore belongs to its enclosing Program unit: retain that
Program's accepted version while unrelated files advance. General `.too`
syntax remains strict; do not guess declarations out of a broken file. On
first preparation, an invalid `agent.too` contributes an empty `agent` Program
and a diagnostic, following the existing empty-module shape. Other valid
modules remain available; missing handlers fail through existing admission.

Validate references against the selected accepted units. Reject an update with
an unresolved dependency, falling back only for that dependent unit. Identity
or export collisions reject all conflicting candidate updates; preserve their
compatible accepted units and omit new conflicting units. Iterate selection
until references and identities are consistent; unaffected units still publish.
Never publish dangling references or choose a winner by scan order.

Resource-local parsing, validation, and read failures are diagnostics for their
unit. An unreadable resource directory retains its accepted contents until a
complete listing succeeds; it must not be interpreted as deletion. Invalid
shared config, an unavailable root/home, failed persistence, or an internally
inconsistent final composition still rejects the entire candidate. Existing
behavior for other capability kinds is outside this change.

## State persistence and diagnostics

Include chore sources in home observation, capture, preparation, and revision
loading. Parsing remains in `catalog` and `lang`; State must not import a
driver, scheduler, store, or event emitter. Place pure chore vocabulary and
source conversion with the authored chore owner.

Keep observed source identity separate from accepted content identity. Persist
accepted units and their exact bytes in each layer, copying retained units into
the new revision. Record the observed digest or read error, the accepted digest,
and a stable per-unit diagnostic. A rejected file must never be represented as
accepted merely because a fallback exists. Diagnostics identify kind, scope,
authored path, optional line, code, message, and whether a fallback is retained.
Do not include timestamps in revision identity.

State exposes an immutable effective chore index: home Markdown definitions
followed by `agent.too` declarations, with later inline declarations winning.
Keep other runnable-module chores inactive as today. Persist everything needed
to reconstruct accepted definitions and diagnostics without authored files or
network access. Bump the layer schema; rebuild current layers from source.
Existing supported historical revisions remain loadable with an empty Markdown
chore index when absent, without inventing historical definitions from disk.

Distinguish successful publication with source diagnostics from fatal State
rejection in refresh and sync results. Per-unit fallback must also work on
explicit sync and forced refresh. Rechecking identical sources, accepted units,
and diagnostics retains the same revision; repair clears only repaired errors.
Expose this distinction through existing State inspection/sync surfaces, using
additive fields where necessary.

## Chore driver and filesystem

Remove root chore loading, watching, authoring, and fallback. Existing root
files remain untouched and ignored; users move or copy them into each intended
agent home. Keep chore names, derived IDs, threads, and `runs.db` checkpoints,
so a same-name move within an agent preserves its progress.

Agent creation, reads, State preparation, driver startup, and monitoring must
not create a chores directory. Missing home chores means no Markdown chores.
Create it only when an explicit authoring operation writes its first file.
StateWatcher monitors existing ancestors and detects creation, atomic edits,
deletion, and recreation without directory creation side effects.

The chore driver consumes the accepted State chore index, with no separate
filesystem watcher or definition scan. Reuse the existing State-revision poll
for reconciliation. At dispatch, take one State and reconcile its definitions
before resolving handlers from that same State. Keep deadlines, checkpoints,
recovery, cancellation, and progress driver-owned.

Runtime authoring and manual-run call sites request State refresh on its owner
loop before reconciling the driver; never wait for State from its own loop or
move parsing into the scheduler thread. A resource diagnostic permits accepted
fallback execution; a newly invalid definition cannot be manually dispatched.
Offline authoring/listing remains independent of handler preparation. A bad
file is reported without hiding valid siblings; runtime inspection distinguishes
authored errors from the accepted definition actually used by the driver.

Remove the `toolang.work` ownership layer: move chore runtime modules into
`toolang.driver.chore`, pure definitions and authoring into `catalog`, incoming
message handling into `toolang.driver.talk`, and transport/presence orchestration
into `teaming`. Keep public commands, driver names, flags, and execution APIs.
Do not introduce a generic driver framework or a new Work/task entity.

## Implementation touchpoints

- `state/{source,prepare,cache,state,watcher,errors,types}.py`: source selection,
  acceptance, persistence, diagnostics, and publication results.
- `catalog/{chore,cap,manager}.py`, `lang` parsing callers: pure resource units
  and consistent diagnostics without weakening source syntax.
- `work/` and the new `driver/` owners, `up/server.py`, `teaming/`: relocate
  responsibilities and consume accepted State through existing startup patterns.
- CLI/API and internal authoring/state tools: imports, refresh ordering,
  isolated listings, and accepted-versus-authored inspection.
- Focused State, catalog, driver, CLI/API tests and `docs/agent-state.md`.
  Generate implementation changelog entries through the repository runnable.

## Acceptance

1. For each source kind, invalid new/edited files beside valid edits yield a
   new State containing the valid edits and the specified fallback/omission.
2. Startup with invalid independent units succeeds with diagnostics; repair,
   deletion, rename, and restart preserve the acceptance table.
3. Invalid inline chore metadata retains only its enclosing Program unit;
   valid sibling Programs, psyches, and Markdown chores still publish.
4. Dependency and collision failures isolate affected units and cannot produce
   dangling references. Unreadable listings never silently delete definitions.
5. Retained units survive deletion of authored files and loading exact revisions;
   integrity validation covers retained bytes and accepted definition identities.
6. Resource errors appear in sync/inspection as successful publication with
   diagnostics; fatal config/persistence errors retain the entire prior State.
7. Startup, listing, sync, and monitoring create neither root nor home chores.
   First authoring lazily creates only home chores; subsequent directory
   recreation is detected. Root chores and sibling-agent chores have no effect.
8. Chore dispatch uses one accepted State for both definitions and handlers;
   invalid updates never alter checkpoints, trigger execution, or block valid
   sibling chores. Existing scheduling/recovery scenarios remain green.
9. No production imports reference `toolang.work`; public driver selection,
   talk/transport behavior, command names, and checkpoint layout are preserved.

Run default offline lint, format, type, and pytest verification for implementation.
For this definition, validate source references and run `git diff --check`.

## Risks and approval

Fallback deliberately allows older accepted behavior to continue; diagnostics
must make that visible. The first upgrade cannot recover a prior valid unit that
was never persisted. Partial acceptance must preserve cross-file invariants,
and accepted-versus-observed identities must remain explicit in storage and UI.
Root chores require manual relocation. Driver moves must preserve startup and
shutdown behavior, including transport with talk disabled.

Open decision: approve the complete file-level fallback, dependency, and fatal
error boundaries above. No implementation is authorized by this definition PR.
