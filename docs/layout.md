# Layout and Storage

This document defines the current local filesystem layout.


## Toolang Root

Toolang stores all local state under one root directory.

Default root:

- `~/.toolang`

CLI override:

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
  hub/
  .sandbox/
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

Visiting agents fetched by `toolang serve <remote>` are materialized under a
stable system temporary root derived from the canonical remote ref:

- `/tmp/toolang-<agent>-<hash:8>/`

The hash is derived from `<agent-source>`. The stable visiting root lets Agent
State and the last runtime port be reused across repeated foreground
runs of the same remote agent ref, independent of the local `TOOLANG_ROOT`,
while remaining disposable across machine restarts or normal
temporary-directory cleanup. The cached remote `agent.too` is refetched after
one hour. Roaming `.too` file invocation uses the source file's sibling
`.toolang` directory and does not start a long-lived HTTP runtime.

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
| `visiting` | `/tmp/toolang-<agent>-<source-hash:8>/` |
| `roaming` | `<source-directory>/.toolang/` |

All three placements use the same layout below their calculated root:
`agents/<agent>/` is the agent home, Agent State uses `.state/`, and durable
operational data uses `.runtime/`. Runtime setup revisions and derived model
views stay in memory; Toolang does not create or mount a model-cache `.setup/`
directory. Existing legacy `.setup/models` data is ignored.


## Setup Data

Setup configuration and catalog sources remain under the Toolang root and agent
home. `SetupWatcher` captures source revisions and builds `AgentSetup` views in
memory when their lazy accessors are first used. Model/catalog data is not stored
or shared through a `.setup/` cache directory.


## Runtime Room

Runtime data shared by every local agent process lives under:

- `${TOOLANG_ROOT}/.runtime/`

Each agent runtime stores operational state under:

- `${TOOLANG_ROOT}/agents/<agent>/.runtime/`

Key paths:

| Path           | Purpose                                                      |
| -------------- | ------------------------------------------------------------ |
| `status.json` | Runtime status, endpoint, sandbox summary, and selected models |
| `agent.log`    | Runtime log                                                  |
| `logs/<agic>/<run_id>.log` | Per-run script logs when `PY_LOG` is set |
| `runs.db` | Threads, controls, runs, steps, model inputs, chore checkpoints, and ID sequences |
| `chat-history.jsonl` | Chat input recall history                               |
| `tools/`       | Per-toolset plugin working directories                       |

Server ownership lives separately in
`${TOOLANG_ROOT}/.sandbox/<agent>/state.json`, the authoritative sandbox
reference. Host references contain PID, process creation time, and signal scope;
guest references retain the sandbox adapter's instance identity. `status.json`
is a descriptive report and does not establish ownership. The adjacent
`state.json.lock` serializes management operations and remains outside the agent home
so removal cannot replace a lock held by another operation.

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
| `status.json.lock` | Runtime status updates |
| `state.json.lock` | Sandbox lifecycle |
| `hub/hub.lock` | One Hub process per root for its lifetime |

Thread fork/rewind use SQLite transactions, without `runs.db.threads.lock`.
Incoming message batches create separate `talk` threads. Each agent's per-conversation
talk cursor lives in the teaming backend; agents generate no `.runtime/channels/`
files and retain no previous-message batch context.
ID allocation updates the `seqs` table in `runs.db` under a short SQLite write
transaction; there is no separate allocator file or lock.

Stop all host/guest processes and writers sharing the affected root, upgrade
together, then restart. Mixed versions cannot coordinate across the new paths.
Released sidecars remain empty files; existence does not mean a lock is held.
Do not delete them while processes may use them. Legacy lock files remain in
place and may be removed only while the entire affected scope is offline.


## Agent State

Agent State is immutable runtime input derived from durable authored source.

State directories:

| Value | Directory |
| --- | --- |
| Root layer | `${TOOLANG_ROOT}/.state/root/` |
| Home layer | `${TOOLANG_ROOT}/agents/<agent>/.state/home/` |
| Agent composition | `${TOOLANG_ROOT}/agents/<agent>/.state/agent/` |

Each directory stores a current-revision pointer, a build or publish lock, and
immutable revision directories. Layer source identities contain only logical paths,
canonical byte sizes, and content digests, so the same revisions remain usable
when root and home are mounted at different absolute paths. Sandbox preparation
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
