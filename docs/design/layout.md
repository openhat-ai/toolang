# Layout and Storage

This document defines the current local filesystem layout.


## Root, place, and home

`TOOLANG_ROOT` selects the root for configuration, resident agents, hub files,
and runtime discovery. The internal path name PLACE denotes the agent root;
see [agent addresses](agent-addresses.md#vocabulary) for public wording.
A home is always `PLACE/agents/NAME`. Root, place, and home are paths. For resident agents,
`PLACE = ROOT`; visiting and roaming places are grouped by a short root code.

Authored scopes keep the labels `root` and `home`: root-scope inputs come from
PLACE, and home-scope inputs come from HOME.

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
    active/
      gus.json                      # Resident
      lex@<place_code>.json         # Visiting
      aid@<place_code>.json         # Roaming
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


## hub files

The root hub stores its process record, lock, and log at:

- `${TOOLANG_ROOT}/hub/hub.json`
- `${TOOLANG_ROOT}/hub/hub.lock`
- `${TOOLANG_ROOT}/hub/hub.log`

The record remains private.


## Agent Home

Each resident agent lives under:

- `${TOOLANG_ROOT}/agents/<agent>/`

Visiting agents fetched by Chat, start, or serve use a stable temporary place
derived from the selected root and Source Ref:

- `/tmp/toolang/<root_code>/<place_code>/`

Agent names are bounded to 64 ASCII characters. Visiting place codes use `t` plus a
10-character source hash; roaming place codes use `s` plus a 7-character path hash and
10-character timestamp. Each field uses canonical ordered Base32. The
7-character root code identifies the selected root, whose full path is retained
in `root.json`; it is excluded from place codes. See [agent addresses](agent-addresses.md)
for encoding and canonicalization.

Repeated visiting launches reuse their place. Each new source-only roaming launch
allocates a place; explicit instance and Thread/Run continuation reuse the selected
home. Different roots have separate root-code directories even for the same
source and place code.
Previous layouts and registry directories remain untouched and are not searched.
Temporary directory cleanup can remove its state and remembered settings. The cached
remote `agent.too` is refetched after one hour. Its source-owned
`agent.source.json` cache receipt records the acquired ref and byte digest;
State captures the same Program and provenance. Distinct roaming homes have
independent State and history. Host scripts serve the shared Agent API beside
their foreground workload on one event loop.
Visiting idle shutdown stops its server and automatic Docker container, preserving
this place and its history. Host control/stage files can be cleaned by the next
management call.

Visiting preparation and management claim full source ownership under the home
write guard before changing caches or history. An existing unbound home is an
error. `/tmp/toolang` is either current-user-owned `0700` or an administrator's
root-owned `01777` parent; new parents and places are private `0700`.
Instance status files are `0600`. Directory-relative, no-follow access rejects links,
unsafe owners/modes and path replacement without adopting or repairing them.
Predictable names can still be pre-created to deny a launch. IDs are public names,
not authorization secrets or content signatures.

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
agent. It records the current placement and derives every place, home, setup,
state, and runtime path from that identity.

| Placement | Place calculation |
| --- | --- |
| `resident` | The explicit Toolang root |
| `visiting` | `/tmp/toolang/<root_code>/<place_code>/` |
| `roaming` | `<source-directory>/.toolang/<root_code>/<place_code>/` |

All three placements use the same layout below their calculated place:
`agents/<agent>/` is the agent home, Agent State uses `.state/`, and durable
operational data uses `.runtime/`. Runtime setup revisions and derived model
views stay in memory; Toolang does not create or mount a model-cache `.setup/`
directory. Existing legacy `.setup/models` data is ignored.


## Setup Data

Setup inputs remain under the placement's configuration locations; resource
views and generations stay in memory. [Setup](setup.md) owns their capture,
materialization, and lifetime. No `.setup/` model-cache directory is created or
mounted; existing legacy `.setup/models` is ignored.


## Runtime state and discovery

Discovery references live under `${TOOLANG_ROOT}/.runtime/active/`:

| Placement | Reference name |
| --- | --- |
| Resident | `gus.json` |
| Visiting | `lex@<place_code>.json` |
| Roaming | `aid@<place_code>.json` |

- Short codes and root isolation follow [agent addresses](agent-addresses.md).
  Nonresident place basenames contain only the place code; registry filename stems
  add the agent name.
- Under the source cache's `.state/launch.lock`, allocate a timestamp after every
  retained launch of the same source within that Toolang root. Completion and
  restart times do not change creation order.
- Verify full ownership on retained reads and before registry publication. Hash
  collisions, corrupt ownership, and timestamp overflow cannot overwrite data.
- `launch_id` is a private startup-generation fence, independent of the place code.
- Registrations provide active discovery only. Removing one neither removes
  history nor proves process death. Known-path lookup works without registration.

Each reference contains only its version, absolute authoritative status path,
and launch ID. Read and write only format 5; rejected or unsupported documents
remain untouched. `RuntimeRegistration` publishes into one Toolang root and uses
one shared `active/register.lock` for publication and conditional removal.
It does not initialize or stop workloads; removing a reference changes no
authoritative lifecycle state. Removal is fenced by both status path and UUID;
failed publication cleans up only after attempting its own write.
The guard is released before starting or waiting for a workload. Pure reads
neither acquire nor create it. Cleanup of an existing reference creates a missing
guard, including when only an old guard remains after an upgrade; cleanup of an
absent reference creates no files. Old guards are retained. For example:

```json
{"version": 5, "status": "/project/.toolang/<root_code>/<place_code>/agents/aid/.runtime/status.json", "launch_id": "<fresh-launch-id>"}
```

`up.identity` owns immutable `InstanceIdentity` bindings and strict parsing,
validation, and serialization. Mutable control/workload fields remain outside
that value. `up.allocation` owns roaming allocation and retained selection;
`up.registry` owns `RuntimeReference`, publication, and conditional removal.
Allocation and observation consume those boundaries without depending on each
other. `RuntimeRegistration.publish()` supports explicit publication, while its
context manager removes the matching reference on exit.

Each agent runtime stores operational state under:

- `<place>/agents/<agent>/.runtime/`

Key paths:

| Path           | Purpose                                                      |
| -------------- | ------------------------------------------------------------ |
| `status.json` | Version 5 authoritative control/workload data in the `default` entry |
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
in the status document; no lock is held across startup, execution, or shutdown.
`up.instance` owns status-file initialization, reads, and atomic updates;
`up.workload` owns preparation, running/stopped/failed transitions, and
management-operation ownership. Initial roaming status is written only after
exclusive place creation under both the source and registry guards.

Stop, exit, and failure retain every published place, agent home, status, and
history. Only process/container/bootstrap resources and matching global references
are released. Runtime paths never add an execution-instance subdirectory. Earlier
places and histories remain untouched and outside the new runtime scope.
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
`layout.place_edit_lock` or `layout.home_edit_lock` according to source ownership;
for shared roaming `toolang.toml`, use `layout.place_edit_lock`. Parsing remains
independent of locks through `CapFile.parse`, `ChoreFile.parse`,
`ConfiguredCaps.parse`, and `ConfiguredWorkspaces.parse`.

Other locks remain beside their protected objects:

| Lock | Purpose |
| --- | --- |
| `<database-name>.init.lock` | SQLite initialization |
| `<thread-id>.compact.lock` | Thread compaction across model calls |
| `status.json.lock` | Transient shared host/guest writer claim for the authoritative status file |
| `.runtime/active/register.lock` | Discovery publication/removal within one Toolang root |
| `hub/hub.lock` | One hub process per root for its lifetime |

Visiting preparation briefly holds `/tmp/toolang/.<root_code>.lock` to publish
root ownership, then uses `/tmp/toolang/<root_code>/.<place_code>.lock` to claim its
home. Different homes prepare concurrently; repeated claims of one home serialize.

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
| Root layer | `<place>/.state/root/` |
| Home layer | `<place>/agents/<agent>/.state/home/` |
| Agent composition | `<place>/agents/<agent>/.state/agent/` |

Each directory holds its current pointer, immutable revisions, and appropriate
build/publication locks. [State](state.md#persistent-layout) owns revision
contents, formats, integrity checks, and publication. [Workspaces](workspaces.md)
owns path capture; [Hosting](hosting.md) owns guest mounts.


## Durable State

Durable authored state lives in:

- `PLACE/config.toml`
- place cap directories
- `HOME/config.toml`
- `HOME/agent.too`
- home cap directories
- `HOME/chores/`

Durable execution state does not live in authored files. It lives in
the `chores` table and execution tables in `runs.db`.
