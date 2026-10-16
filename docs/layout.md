# Layout and Storage

This document defines the current local filesystem layout.


## Toolang Root

`TOOLANG_ROOT` selects the Toolang root for resident agents, Hub data, and
runtime discovery. Each agent also has an internally resolved agent root for
its own authored, prepared, and execution data. The two roots coincide for a
resident agent.

Default root:

- `~/.toolang`

Overrides:

- `too --root PATH`
- `TOOLANG_ROOT`

Current root layout:

```text
${TOOLANG_ROOT}/
  config.toml
  psyches/
  skills/
  services/
  prompts/
  .state/
  .runtime/
    instances/
      gus.json                       # Resident
      lex@v<source-hash>.json         # Visiting
      aid@r<source-hash>.json         # Roaming server
      register.lock
  hub/
  .sandbox/
    <agent>/launches/<launch-id>/  # Docker bootstrap materials only
  agents/
    <agent>/
      agent.too
      runnables/
        <name>.too
      config.toml
      psyches/
      skills/
      services/
      prompts/
      chores/
      .state/
      .runtime/
```


## Hub Files

The root Hub stores its process record, lock, and log at:

- `${TOOLANG_ROOT}/hub/hub.json`
- `${TOOLANG_ROOT}/hub/hub.lock`
- `${TOOLANG_ROOT}/hub/hub.log`

The record remains private.


## Agent Home

Each resident agent lives under:

- `${TOOLANG_ROOT}/agents/<agent>/`

Visiting agents fetched by Chat, start, or serve use a stable temporary agent
root derived from the resolved Toolang root and Source Ref:

- `/tmp/toolang-<agent>-<hash:16>/`

Agent Names are bounded to 64 ASCII characters; the complete 16-character
source hash is retained without shortening either component. See
[Agent Sources](concepts.md#agent-sources) for derivation and migration rules.

Repeated launches of the same source under the same Toolang root reuse this
agent root; a different Toolang root gets a different visiting root. Earlier
eight-character roots are left untouched and are no longer selected. Stop old
visiting workloads before upgrading; retain old roots for manual history
migration. Temporary directory cleanup can remove its state and remembered
settings. The cached
remote `agent.too` is refetched after one hour. Its source-owned
`agent.source.json` cache receipt records the acquired ref and byte digest;
State captures the same Program and provenance. Roaming agents use the source
file's sibling `.toolang` directory. `too file.too serve` hosts the workload;
ordinary host script invocation executes directly without a persistent server.
Visiting idle shutdown stops its server and automatic Docker container, preserving
this root and its history. Host control/stage files can be cleaned by the next
management call.

Key paths:

| Path | Purpose |
| --- | --- |
| `agent.too` | Agent program |
| `runnables/<name>.too` | Independent module exporting one agic or flow |
| `config.toml` | Agent-local configuration |
| `psyches/`, `skills/`, `services/`, `prompts/` | Agent-local cap definitions |
| `chores/` | Current chore definitions |
| `.state/` | Immutable Agent State revisions |
| `.runtime/` | Live runtime state |


## Agent Placement

`AgentLayout` is the immutable process-owned description of one materialized
agent. It records the current placement and derives every root, home, setup,
state, and runtime path from that identity.

| Placement | Root calculation |
| --- | --- |
| `resident` | The explicit Toolang root |
| `visiting` | `/tmp/toolang-<agent>-<toolang-root-and-source-hash:16>/` |
| `roaming` | `<source-directory>/.toolang/` |

All three placements use the same layout below their calculated root:
`agents/<agent>/` is the agent home, Agent State uses `.state/`, and durable
operational data uses `.runtime/`. Runtime setup revisions and derived model
views stay in memory; Toolang does not create or mount a model-cache `.setup/`
directory. Existing legacy `.setup/models` data is ignored.


## Setup Data

Setup configuration and catalog sources remain under the agent root and home,
with placement deciding which sources participate. `SetupWatcher` captures
source revisions and builds `AgentSetup` views in
memory when their lazy accessors are first used. Model/catalog data is not stored
or shared through a `.setup/` cache directory.


## Runtime Room

Discovery references live under `${TOOLANG_ROOT}/.runtime/instances/`:

| Placement | Reference name |
| --- | --- |
| Resident | `gus.json` |
| Visiting | `lex@v<hash>.json` |
| Roaming server | `aid@r<hash>.json` |

The hash is the first 16 hex characters of SHA-256. Visiting uses the same key
as its agent root: the canonical Toolang root, a NUL separator, and the Source
Ref target. GitHub prefix aliases normalize before hashing; shorthand targets
do not require resolving a branch. Complete URL targets retain their spelling.
The temporary root and discovery reference use that same 16-character hash.
Roaming uses the canonical host absolute `.too` source path. The host saves
`source_id` with its control bindings so guest mounts cannot change identity.
Changing ports, sandbox, launch IDs, or source contents does not change this hash.
Agent Names contain neither `@` nor `-`; placement markers remain unambiguous.

An isolated temporary execution appends `@<execution-id>` before `.json`;
its source hash remains stable and parallel
executions remain independently visible. Repeated invocations of the same source
and config reuse prepared Agent State; execution histories remain separate.

Each reference contains only its version, absolute authoritative status path,
and launch ID. `RuntimeRegistration` publishes into one Toolang root and uses
one shared `instances/register.lock` for publication and conditional removal.
The guard is released before starting or waiting for a workload. Pure reads
neither acquire nor create it. Cleanup of an existing reference creates a missing
guard, including when only an old guard remains after an upgrade; cleanup of an
absent reference creates no files. Old guards are retained. For example:

```json
{"version": 1, "status": "/project/.toolang/agents/aid/.runtime/status.json", "launch_id": "<fresh-launch-id>"}
```

Each agent runtime stores operational state under:

- `<agent-root>/agents/<agent>/.runtime/`

Key paths:

| Path           | Purpose                                                      |
| -------------- | ------------------------------------------------------------ |
| `status.json` | Version 2 authoritative control and workload entries, keyed by execution instance |
| `agent.log`    | Runtime log                                                  |
| `logs/<agic>/<run_id>.log` | Per-run script logs when `PY_LOG` is set |
| `runs.db` | Threads, controls, runs, steps, model inputs, chore checkpoints, and ID sequences |
| `chat-history.jsonl` | Chat input recall history                               |
| `tools/`       | Per-toolset plugin working directories                       |

`status.json` owns both lifecycle sections. `control` contains intent, recovery
references, resolved startup bindings, and last successful settings; `workload`
contains namespace-local PID/creation time, readiness, timestamps, and errors.
Host and guest merge their owned fields through one short shared-file claim,
validating the launch ID before atomic replacement. Management ownership lives
in the record; no lock is held across startup, execution, or shutdown.

Successful stop retains the persistent record, remembered endpoint/sandbox,
and database/log history. Successfully finalized temporary entries are removed.
Existing isolated script databases/logs remain under `.runtime/instances/<id>/`;
all their lifecycle entries share the agent's `status.json`. Interrupted entries
and unreleased resource references remain recoverable. See the
[runtime plan](plans/unified-runtime-status.md) for restart and recovery rules.

Docker mounts the target agent home read-write, necessary inputs/workspaces,
and bootstrap materials read-only. Discovery references and entire roots are
not mounted. Shared control/workload field ownership is a cooperative contract;
it does not isolate control fields from arbitrary guest file writes.

Human Talk stores input history and drafts under the root's
`.runtime/talk-history/<conversation-id>.jsonl` and
`.runtime/talk-history/<conversation-id>.draft.txt`. These files are shared by
human viewers of the same conversation ID in one root. Chat history follows the
selected agent runtime, including its instance directory. Neither file is the
conversation message store or execution history.

On first use, absent destinations import the old `chat-input-history.jsonl` or
the current viewer/dataset's `talk-v2/<identity>/<conversation-hash>/` files.
Existing destinations win and legacy files remain untouched.

## File Locks

Authored scopes and derived State use locks in their existing `.state` directories:

| Lock path | Purpose |
| --- | --- |
| `.state/root/edit.lock` | All edits to shared root sources |
| `agents/<agent>/.state/home/edit.lock` | All edits to one agent's sources |
| `.state/root/build.lock` | Root layer construction and publication |
| `agents/<agent>/.state/home/build.lock` | Home layer construction and publication |
| `agents/<agent>/.state/agent/publish.lock` | Complete agent preparation, composition, and publication |

Edit locks first appear on a mutation attempt, including one that fails or makes
no content change. Read-only operations never create edit locks or their parent
directories: they acquire an existing guard, or read optimistically and retry
under the guard if the first writer creates it during the read. An unchanged
roaming configuration projection also leaves an absent edit lock absent.
Shared roaming `toolang.toml` edits use the roaming root edit lock; projected
home configuration uses its home edit lock. Release edit locks before refreshing
State. The watcher uses source-change detection independently of edit locks.
Roaming projection verifies its root inputs again after releasing the home guard
and retries changed inputs, up to three attempts. Chore inspection reads inline
and file definitions together. API patches hold edit across winner checks and
read/modify/write; CLI saves/deletes recheck the winner under edit through commit.
The interactive editor runs outside the guard.

Python callers of `AuthoredCaps`, `AuthoredChores`, `ConfiguredCaps`, and
`ConfiguredWorkspaces` must now pass `lock_path=` explicitly. Use
`layout.root_edit_lock` or `layout.home_edit_lock` according to source ownership;
for shared roaming `toolang.toml`, use the root path. Parsing remains independent
of locks through `CapFile.parse`, `ChoreFile.parse`, `ConfiguredCaps.parse`, and
`ConfiguredWorkspaces.parse`.

Other locks remain beside their protected objects:

| Lock | Purpose |
| --- | --- |
| `<database-name>.init.lock` | SQLite initialization |
| `<thread-id>.compact.lock` | Thread compaction across model calls |
| `status.json.lock` | Transient shared host/guest writer claim for the authoritative record |
| `.runtime/instances/register.lock` | Discovery publication/removal within one Toolang root |
| `hub/hub.lock` | One Hub process per root for its lifetime |

Thread fork/rewind use SQLite transactions, without `runs.db.threads.lock`.
Each agent reuses one `talk_` thread per backend/conversation. Its `runs.db` stores
`talk_bindings` and admitted `talk_batches`, including exact input, cursor range,
and Run receipt. The thread retains execution history; the per-conversation
cursor lives in the teaming backend. Agents generate no `.runtime/channels/` files.
ID allocation updates the `seqs` table in `runs.db` under a short SQLite write
transaction; there is no separate allocator file or lock.

Stop all host/guest processes and writers sharing the affected root, upgrade
together, then restart. Mixed versions cannot coordinate across the new paths.
Native lock guards remain stable after release; existence does not mean a lock
is held. The shared `status.json.lock` claim instead contains writer identity
and is removed only by its owner or confirmed-dead recovery. Do not delete live
guards. Legacy lock files may be removed only while the affected scope is offline.


## Agent State

Agent State is immutable runtime input derived from durable authored source.

State directories:

| Value | Directory |
| --- | --- |
| Root layer | `<agent-root>/.state/root/` |
| Home layer | `<agent-root>/agents/<agent>/.state/home/` |
| Agent composition | `<agent-root>/agents/<agent>/.state/agent/` |

Each directory stores a current-revision pointer, a build or publish lock, and
immutable revision directories. Each layer contains `layer.json`, original
accepted bytes under `source/`, and fetched bytes under
`materialized/<acquisition-key>/`, keyed by SHA-256 of the canonical resolved
Source Ref. JSON stores parsed values, file sizes and
digests, resolutions, and a creation time included in the revision hash. The same
revisions can be loaded when root and home are mounted at different absolute
paths. Stored workspace paths remain frozen; preparing relative workspace
configuration at a new location produces a new revision. Sandbox preparation
mounts symbolic-linked State source files read-only at their logical guest
paths. See [agent-state.md](./agent-state.md) for the canonical documents,
revision calculation, validation, and publication rules.


## Durable State

Durable authored state lives in:

- root `config.toml`
- root cap directories
- agent `config.toml`
- agent program
- agent cap directories
- agent `chores/`

Durable execution state does not live in authored files. It lives in
the `chores` table and execution tables in `runs.db`.
