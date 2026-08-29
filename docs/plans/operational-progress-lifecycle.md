# Unify Operational Progress

## Status

Feature definition, approved for implementation on 2026-08-28. This plan
supersedes the progress vocabulary, presenter split, and foreground output ownership in
[Sandbox Startup UX](sandbox-startup-ux.md). It preserves that plan's Docker
installation, compatibility, identity, recovery, and diagnostic requirements.
[Execution Progress Projection](execution-progress-state-machine.md) remains
authoritative for Run presentation.
[Operational Progress Presentation](operational-progress-presentation.md)
proposes the exact operational row grammar and terminal handoffs for this
contract.

## Current Problem

- `ProgressEvent` uses untyped dotted phases. Agent, cap, state, startup, and
  shutdown producers use inconsistent prefixes and item IDs.
- `CliProgress` handles source and state preparation, while runtime startup and
  shutdown have separate public presenters. Some clone, cap, embedded,
  inspection, pre-launch, stop, and cleanup paths emit no progress.
- `SetupWatcher.refresh()` loads plugins and catalogs and discovers Ollama and
  llama.cpp without progress.
- Script, Chat, retry, and rerun already use durable `RunEvent` values through
  `ProgressProjector`; those events describe execution, not environment work.
- Foreground host and Docker startup can write child logs while Rich owns a live
  line, splitting or duplicating the progress display.

## Goal

Use one small contract for blocking operational work, cover every command that
performs that work, and give one component terminal ownership at a time. Keep
execution on the existing `RunEvent` contract.

## Contract

Operational progress uses:

```text
ProgressEvent(id, kind, stage, label, status, detail)
```

`kind` and `stage` replace `phase`. Define closed `ProgressKind` and
`ProgressStage` literals and validate their pairing. Remove the dotted-phase
contract without a compatibility alias; migrate current producers and consumers
together.

| Kind | Stages | Meaning |
| --- | --- | --- |
| `prepare` | `resolve`, `fetch`, `materialize` | turn authored agent and cap sources into local prepared state |
| `setup` | `load`, `discover` | construct the installed plugin, tool, and model view |
| `runtime` | `create`, `start`, `stop`, `destroy` | own an AgentServer runtime resource |

The qualified pair, such as `prepare.resolve` or `runtime.create`, is the
semantic name. `label` describes the current user-visible activity; `detail` is
optional bounded context. Neither may expose commands, environment values,
installer output, or secrets.

An `id` remains stable while one item advances:

- `agent:SOURCE` for an agent source;
- `cap:KIND:REF` for a cap;
- `setup:TARGET` for a setup target;
- `runtime:LAUNCH_ID` for a runtime resource.

For one `(kind, id)`, a producer closes the running stage with `ok`, `failed`,
or `skipped` before advancing. Separate items may run concurrently, such as cap
fetches or catalog discovery, and the CLI aggregates them. Missing or failing
progress delivery cannot alter preparation, setup, runtime, recovery, or
execution decisions.

There is no `run` kind or `ProgressEvent` adapter. `RunBegin`, Step and Part
events, and `RunEnd` continue directly through `ProgressProjector` to the Script
and Chat presenters.

## Stage Boundaries

### Prepare

- `resolve`: select and canonicalize an authored source;
- `fetch`: obtain remote content when it is not current;
- `materialize`: create or update the local prepared representation.

Skipped or cached work must not leave pending stages.

### Setup

- `load`: read root and agent configuration and environment inputs, then load
  toolsets, model adapters, static catalogs, and configured plugins;
- `discover`: query dynamic catalogs, including Ollama and llama.cpp, and merge
  their current snapshots.

Only an initial blocking refresh or explicit user-requested refresh owns CLI
progress. Periodic refresh after AgentServer readiness uses logging instead.

### Runtime

- `create`: resolve the sandbox plan and establish its runtime environment;
- `start`: start AgentServer and wait for a valid API and runtime identity;
- `stop`: stop the AgentServer workload;
- `destroy`: remove Toolang-owned runtime resources and records.

Implementation actions update the stage label or detail; they do not add
stages. Pulling an image, installing or validating Toolang, and preparing mounts
belong to `runtime.create`. Starting the server and waiting for readiness belong
to `runtime.start`. Resource cleanup belongs to `runtime.destroy`.

`destroy` never removes mounted agent homes, workspaces, inboxes, or other user
data. A host driver may omit it when no separate resource exists.

## Production and Guest Transport

State preparation and `SetupWatcher` accept an optional `ProgressSink` at their
blocking entry points. Runtime orchestration owns the four runtime stages;
sandbox plugins may emit bounded activity updates within them.

The launch-scoped Docker progress file remains an advisory channel, separate
from recovery state. Replace startup-specific tokens with a closed vocabulary
for runtime activity and initial `setup.load` and `setup.discover` transitions.
Tokens carry no arbitrary guest data. Ignore malformed, duplicate,
out-of-order, oversized, or stale input, and never use it for readiness or
cleanup decisions.

The initial guest setup receives a file sink before the API exists. Its observer
ends after initial setup is terminal or the workload exits; later refreshes do
not append. Host and embedded execution pass the sink directly.

## CLI and Output Ownership

`CliProgress` and `make_cli_progress()` become the only public operational CLI
presenter and factory. Remove the runtime startup and shutdown presenter types
and factories; private kind-specific state is allowed. Rename the CLI's
resolved `RuntimeStartup` data value to `RuntimeLaunch`.

TTY output:

- uses one Rich live region on stderr for the current operational scope;
- shows activity, bounded detail, and elapsed time;
- aggregates concurrent prepare or setup items;
- closes before a Run presenter or foreground workload logs take ownership;
- opens a new operational scope for later stop and destroy work.

Non-TTY output writes plain, append-only, ANSI-free stderr when an activity
starts or materially changes and on terminal failure. It preserves stdout
contracts and existing `--quiet` behavior.

Before runtime readiness, host and Docker startup output is drained to a
diagnostic log or bounded buffer rather than inherited or followed. After the
operational presenter closes, foreground logs may be attached or relayed.
Background logs remain in `agent.log`. Ctrl+C releases the log follower before
stop and destroy presentation begins.

Failures report the qualified stage, current activity, original or curated
cause, applicable remedy, and diagnostic log. An interrupt closes the active
display and uses the existing recovery-safe stop and destroy path. Failed stop
or destroy work retains the full runtime identity needed for recovery and is
never reported as successful.

## Command Coverage

Progress follows work performed, not command spelling:

| Command path | Operational progress | Execution progress |
| --- | --- | --- |
| `too run`, `too start` | prepare, initial setup, runtime create/start | none |
| `too stop` | runtime stop/destroy | none |
| `too chat`, `too script`, `too retry`, `too rerun` with an owned guest | prepare as needed, setup, runtime create/start and stop/destroy | original `RunEvent` stream |
| the same commands attached to a running AgentServer | none | original `RunEvent` stream |
| embedded host execution | prepare and setup as needed | original `RunEvent` stream |
| `too clone` and state-refreshing cap commands | prepare | none |
| `too info`, `too models`, `too providers`, `too tools` when setup blocks | setup | none |

Agent-first and command-first routed forms share orchestration. Cap `new`,
`edit`, `remove`, and `delete` show prepare progress when they refresh state.
Pure inspection and control commands remain silent. Inbox processing and
asynchronous chores are excluded.

Retry keeps the source Run's sandbox and does not gain `--sandbox`; rerun keeps
its existing selector. Attaching requires the requested or inherited sandbox to
match before progress or execution starts.

## Scope and Touchpoints

In scope:

- migrate `ProgressEvent` and its prepare, setup, and runtime producers;
- unify operational CLI presentation and command coverage;
- forward initial guest setup progress;
- serialize operational, Run, and foreground-log terminal ownership;
- preserve failure, interrupt, and recovery guarantees;
- update focused unit, integration, PTY, live Docker, API, and plugin docs.

Likely code areas are `base/types/progress.py`, `common/progress.py`, state
preparation, `setup/watcher.py`, `up/{core,server,sandbox}.py`, the sandbox
protocol and host/Docker plugins, `cli/common/{progress,execution_runtime}.py`,
and the affected command call sites.

Out of scope:

- changing `RunEvent`, `ProgressProjector`, or Script and Chat Run grammar;
- changing commands, flags, sandbox selection, mounts, environment exposure,
  recovery keys, or readiness authority;
- presenting periodic setup refreshes, inboxes, chores, passive history, or
  control-only work;
- native Windows Docker control beyond the existing WSL2 boundary;
- compatibility for dotted progress phases.

## Acceptance Tests

1. `ProgressEvent` accepts only documented kind-stage pairs and rejects dotted
   phases; IDs remain stable across an item's transitions.
2. Agent and cap preparation consistently emits resolve, fetch, and materialize
   without pending cache-hit paths.
3. Initial setup emits load and discover for plugins and dynamic catalogs,
   covering Ollama and llama.cpp success, absence, timeout, and partial failure.
4. Host and Docker emit create, start, stop, and applicable destroy stages;
   image, installation, compatibility, readiness, and cleanup are activities.
5. Guest setup tokens reach the host without arbitrary data and cannot affect
   readiness or recovery.
6. TTY foreground startup has one live owner through readiness; logs appear only
   after it closes. Background startup remains visible while diagnostics stay in
   `agent.log`.
7. Non-TTY operational output is ordered, append-only, ANSI-free stderr and
   preserves stdout.
8. Stop, temporary cleanup, Ctrl+C, early exit, and recovery report stages
   accurately and retain recoverable state after failure.
9. Chat, script, retry, and rerun show only work they own before handing the
   terminal to the unchanged Run presenters; routed forms behave identically.
10. Clone, cap mutation, embedded execution, and blocking setup-inspection paths
    have no silent gaps.
11. Linux, macOS, Windows host, and Docker controlled from Linux, macOS, or WSL2
    share the semantic contract; opt-in live Docker tests cover wheel success,
    package failure, foreground Ctrl+C, background startup, stop, and cleanup.
12. The default verification suite passes.

## Risks

- Host startup needs a drained pipe or followable file so redirected child
  output cannot deadlock or reorder logs.
- Initial setup runs while runtime start is pending. It may temporarily own the
  single live region, but must not open a second one.
- Third-party sandbox plugins using dotted startup phases must migrate in the
  same release.

There are no open product questions.
