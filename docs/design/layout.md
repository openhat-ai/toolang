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
      aid@<instance_id>.json         # Roaming
      register.lock
  hub/
  human/
    talk-input/<conversation-id>.jsonl
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

- `/tmp/<agent>@v<hash:16>/`

Agent Names are bounded to 64 ASCII characters; the complete 16-character
source hash is retained without shortening either component. See
[Agent Sources](architecture.md#agent-sources) for derivation and migration rules.

Repeated launches of the same source under the same Toolang root reuse this
agent root; a different Toolang root gets a different visiting root. Earlier
eight-character roots are left untouched and are no longer selected. Stop old
visiting workloads before upgrading; retain old roots for manual history
migration. Temporary directory cleanup can remove its state and remembered
settings. The cached
remote `agent.too` is refetched after one hour. Its source-owned
`agent.source.json` cache receipt records the acquired ref and byte digest;
State captures the same Program and provenance. Each roaming execution gets a
fresh root below the source file's sibling `.toolang`, with independent State and
history. Host scripts serve the shared Agent API beside their foreground workload
on one event loop; neither executor nor history is shared with another launch.
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
| `visiting` | `/tmp/<agent>@v<toolang-root-and-source-hash:16>/` |
| `roaming` | `<source-directory>/.toolang/<agent>@<instance_id>/` |

All three placements use the same layout below their calculated root:
`agents/<agent>/` is the agent home, Agent State uses `.state/`, and durable
operational data uses `.runtime/`. Runtime setup revisions and derived model
views stay in memory; Toolang does not create or mount a model-cache `.setup/`
directory. Existing legacy `.setup/models` data is ignored.


## Setup Data

Setup inputs remain under the placement's configuration locations; resource
views and generations stay in memory. [Setup](setup.md) owns their capture,
materialization, and lifetime. No `.setup/` model-cache directory is created or
mounted; existing legacy `.setup/models` is ignored.


## Runtime Room

Discovery references live under `${TOOLANG_ROOT}/.runtime/instances/`:

| Placement | Reference name |
| --- | --- |
| Resident | `gus.json` |
| Visiting | `lex@v<hash>.json` |
| Roaming | `aid@<instance_id>.json` |

- Visiting IDs are `v` plus the existing 16-hex source hash: canonical Toolang root,
  NUL, canonical Source Ref; alias normalization is unchanged.
- Roaming IDs are `r` plus 16 hex digits of startup order and 16 hex digits of a
  SHA-256 prefix over canonical source path, NUL, and a fresh UUID nonce.
- Startup order is `max(time_ns(), highest_retained_order_ns + 1)` under the source
  cache's `.state/launch.lock`. Publish identity/status before preparation;
  hidden staging is unselectable. Compare complete IDs lexically.
- `instance_name` is agent name plus `@<instance_id>` for nonresidents; resident
  names stay unchanged. Top ownership uses the complete instance name.
- `launch_id` remains a private UUID4 workload-generation fence for every placement.
  It selects neither agent roots nor CLI instances; replacement workloads renew it.
- Only global active registrations supply discovery. No resident or project scans
  supplement them; deleting references loses no history and proves no process death.

Each reference contains only its version, absolute authoritative status path,
and launch ID. `RuntimeRegistration` publishes into one Toolang root and uses
one shared `instances/register.lock` for publication and conditional removal.
The guard is released before starting or waiting for a workload. Pure reads
neither acquire nor create it. Cleanup of an existing reference creates a missing
guard, including when only an old guard remains after an upgrade; cleanup of an
absent reference creates no files. Old guards are retained. For example:

```json
{"version": 3, "status": "/project/.toolang/aid@<instance_id>/agents/aid/.runtime/status.json", "launch_id": "<fresh-launch-id>"}
```

Each agent runtime stores operational state under:

- `<agent-root>/agents/<agent>/.runtime/`

Key paths:

| Path           | Purpose                                                      |
| -------------- | ------------------------------------------------------------ |
| `status.json` | Version 3 authoritative control/workload data in the `default` entry |
| `agent.log`    | Runtime log                                                  |
| `logs/<agic>/<run_id>.log` | Per-run script logs when `PY_LOG` is set |
| `runs.db` | Threads, controls, runs, steps, model inputs, chore checkpoints, and ID sequences |
| `chat-input.jsonl` | Chat input recall history |
| `stats.db` | Derived usage statistics; see [Execution statistics](execution-statistics.md) |
| `tools/`       | Per-toolset plugin working directories                       |

`status.json` owns both lifecycle sections. `control` contains intent, recovery
references, resolved startup bindings, and last successful settings; `workload`
contains namespace-local PID/creation time, readiness, timestamps, and errors.
Host and guest merge their owned fields through one short shared-file claim,
validating the launch ID before atomic replacement. Management ownership lives
in the record; no lock is held across startup, execution, or shutdown.

Stop, exit, and failure retain every published root, agent home, status, and
history. Only process/container/bootstrap resources and matching global references
are released. Runtime paths never add an execution-instance subdirectory. Old
shared roots and nested histories remain untouched for explicit migration.
[Hosting](hosting.md#runtime-recovery-and-logs) owns resource recovery.

Docker mounts the target agent home read-write, necessary inputs/workspaces,
and bootstrap materials read-only. Discovery references and entire roots are
not mounted. Shared control/workload field ownership is a cooperative contract;
it does not isolate control fields from arbitrary guest file writes.

Human Talk stores accepted input history at
`<root>/human/talk-input/<conversation-id>.jsonl`, shared by human viewers of the
same conversation in one root. Chat uses the selected agent runtime's
`chat-input.jsonl` in the selected instance's agent home. These are input recall,
separate from the conversation message store and execution history. Unsent
drafts remain in memory and are not saved to draft files.

Old `chat-input-history.jsonl`, `chat-history.jsonl`, `.runtime/talk-history/`,
and `talk-v2/` data are neither read nor imported. Existing files remain
untouched.

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
Shared roaming `toolang.toml` edits use `<srcdir>/.toolang/.state/root/edit.lock`; projected
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

Each directory holds its current pointer, immutable revisions, and appropriate
build/publication locks. [State](state.md#persistent-layout) owns revision
contents, formats, integrity checks, and publication. [Workspaces](workspaces.md)
owns path capture; [Hosting](hosting.md) owns guest mounts.


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
