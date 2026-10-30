# Layout and Storage

This document defines the current local filesystem layout.


## Roots and agent home

`TOOLANG_ROOT` selects the Toolang root for policy, resident agents, Hub, and
runtime discovery. Placement resolves an agent root for shared inputs/State and
a runtime root for instance data. Agent home is `AGENT_ROOT/agents/NAME`;
runtime data is `RUNTIME_ROOT/agents/NAME/.runtime`. `AgentLayout.place` and
`AgentLayout.home` identify the runtime root and its agent directory. Authored
root/home scopes use agent root and agent home. See
[agent addresses](agent-addresses.md#vocabulary).

Default root:

- `~/.toolang`

Overrides:

- `too --root PATH`
- `TOOLANG_ROOT`

This precedence also applies to scripts and `cd`. The canonical host root supplies
the R8 code for Hub keys; OS usernames and guest mount paths do not enter that code.

Current root layout:

```text
${TOOLANG_ROOT}/
  config.toml
  catalog.json                    # Optional root catalog
  psyches/
  skills/
  services/
  prompts/
  .state/
  .runtime/
    active/
      gus/info.json                 # Resident, host path view
      lex@<P10>/info.json            # Visiting
      aid@<P19>/info.json            # Roaming
      <address>/docker/             # Guest inputs; see runtime layout below
      <address>/register.lock       # Present while owned
  hub/
  human/
    talk-input/<conversation-id>.jsonl
  workspaces/
    <agent>/
      lab/                           # Retained independently of agent home
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

- `${TOOLANG_ROOT}/hub/pid.json`
- `${TOOLANG_ROOT}/hub/hub.lock`
- `${TOOLANG_ROOT}/hub/hub.log`

The private `pid.json` contains only `pid`, `created`, `host`, `port`, and
`status` (`starting` or `running`). It describes the local process and is removed
after stop; agent clients use `[teaming]` in their agent-root layer. Human and backend
identity are absent from this record.
The process may serve many client roots; its launch root only locates these
management files and server configuration.


## Agent Home

Each resident agent lives under:

- `${TOOLANG_ROOT}/agents/<agent>/`

A resident agent exists when this home directory exists. Lookup and enumeration
do not require `agent.too`; authoring and inspection can use an incomplete home.
Creation requires an unused destination. Each operation validates its required
files, authored content, records, or running instance separately.

Visiting agents fetched by Chat, start, or serve use a stable temporary place
derived from the selected root and Source Ref:

- `/tmp/toolang/<root_code>/<place_code>/`

Agent names are bounded to 64 ASCII characters. Code notation, encoding, and
canonicalization follow [agent addresses](agent-addresses.md#short-codes).

Repeated visiting launches reuse their place. Each new source-only roaming launch
allocates a runtime root; explicit instance and Thread/Run continuation reuse it.
Different roots have separate root-code directories even for the same source and place code.
Previous layouts and registry directories remain untouched and are not searched.
Temporary directory cleanup can remove its state and remembered settings. The cached
remote `agent.too` is refetched after one hour. Its source-owned
`source.json` cache receipt records the acquired ref and byte digest;
State captures the same Program and provenance. Roaming instances share State, Setup inputs, and lab under `DIR/.toolang`,
including across selected Toolang roots; runtime records/history remain independent. Host scripts serve the shared Agent API beside
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
| `RUNTIME_ROOT/agents/NAME/.runtime/` | Instance runtime state |


## Agent Placement

`AgentLayout` derives placement paths. In the table, `ROOT` is the selected host Toolang root,
`GR` is the configured guest root (default `/root/.toolang`), and `DIR` is the
canonical roaming source directory. `R8`, `P10`, and `P19` denote complete codes.

| Placement | Host agent root | Host runtime root | Guest root |
| --- | --- | --- | --- |
| Resident | `ROOT` | `ROOT` | `GR` |
| Visiting | `/tmp/toolang/R8/P10` | `/tmp/toolang/R8/P10` | `GR` |
| Roaming | `DIR/.toolang` | `DIR/.toolang/R8/P19` | `GR` |

Each Docker guest hosts one agent. Agent home is `AGENT_ROOT/agents/NAME` and
lab is `AGENT_ROOT/workspaces/NAME/lab`; guest shared and runtime roots both map
to `GR`, with the selected instance `.runtime` mounted separately in every placement.
Guest agent home (`GH`) is `GR/agents/NAME`. Other host workspaces
may live anywhere; their guest targets are `GR/workspaces/NAME/WORKSPACE`.
Roaming agent home does not depend on exposing its source directory. The guest retains
the original placement and full address even though its physical agent and runtime roots
coincide. [Hosting](hosting.md#docker-filesystem) defines the mounts.


Roaming shared paths retain the ordinary State layout:

```text
DIR/.toolang/
  config.toml                        # Hardlink to the nearest toolang.toml
  catalog.json                       # Hardlink to the selected catalog, if any
  source.json                        # Shared configuration/catalog origins
  .state/root/v24/
  agents/NAME/agent.too               # Hardlink to FILE.too
  agents/NAME/source.json             # Program ownership, origin, and digest
  agents/NAME/.state/home/v24/
  agents/NAME/.state/agent/v24/
  workspaces/NAME/lab/
  R8/P19/agents/NAME/.runtime/        # Independent instance records/control
```

Preparation and exact history loads use shared State; new runs use the shared lab.
Startup fetch refreshes hardlinks; watchers read only this resolved layout.
An upstream atomic replacement becomes visible on the next fetch. In-place writes
share the linked inode. No previous layout is searched or migrated.

## Setup Data

Setup inputs remain under the placement's configuration locations; resource
views and generations stay in memory. [Setup](setup.md) owns their capture,
materialization, and lifetime. No `.setup/` model-cache directory is created or
mounted; existing legacy `.setup/models` is ignored.


## Runtime state and discovery

The host owns one directory per complete address under ROOT, regardless of runtime root:

```text
ROOT/.runtime/active/<address>/
  info.json                         # Host path view
  register.lock                     # Present while owned
  docker/<launch-hash>/             # Docker launches only
    active/
      info.json                     # Guest path view
      guest.env
      guest.sh
      <development-wheel>.whl        # Optional
```

Both format-6 info files contain the full address, absolute `home`, `launch`,
execution `location`, and `sandbox`. Optional `source` and `source_ref` describe
the source available in that reader's filesystem. The copies describe the same
launch and location; a Docker launch has `location = "guest"` in both. They store
no status filename: status is always `RUNTIME_ROOT/agents/NAME/.runtime/status.json`.

For example, the guest view for a roaming Docker launch is:

```json
{
  "version": 6,
  "address": "aid@<P19>",
  "home": "/root/.toolang/agents/aid",
  "launch": "<launch-id>",
  "location": "guest",
  "sandbox": "docker:python:3.13-slim",
  "source": "/root/.toolang/agents/aid/agent.too"
}
```

The guest receives its read-only view at `GR/.runtime/active/NAME/`; it writes
status through the separately mounted runtime directory. The host removes the address directory
only after resource release and generation checks. Registration enables active
discovery; removing it does not delete history or prove process death.

`up.records` owns reference parsing; `up.registry` owns guarded publication and
conditional removal; `up.identity` validates full source/root/home ownership.
[Hosting](hosting.md#runtime-observation-and-retirement) owns recovery and
retirement. Legacy format-5 references remain readable for resource recovery.

Each agent runtime stores operational state under:

- `RUNTIME_ROOT/agents/NAME/.runtime/`

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

Human Talk stores accepted input history at
`<root>/human/talk-input/<conversation-id>.jsonl`, shared by human viewers of the
same conversation in one root. Chat uses the selected agent runtime's
`chat-input.jsonl` in the selected instance's runtime directory. These are input recall,
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
| `.state/root/v<format>/build.lock` | Root layer construction and publication |
| `agents/<agent>/.state/home/v<format>/build.lock` | Home layer construction and publication |
| `agents/<agent>/.state/agent/v<format>/publish.lock` | Complete agent preparation, composition, and publication |

Edit locks first appear on a mutation attempt, including one that fails or makes
no content change. Read-only operations never create edit locks or their parent
directories: they acquire an existing guard, or read optimistically and retry
under the guard if the first writer creates it during the read.
Roaming startup fetch holds the shared root edit lock, then the home edit lock,
while updating hardlinks and receipts. Program writes share the home guard with
State authoring. Release edit locks before refreshing State. Watchers observe
placement-local sources independently of edit locks and never fetch upstream inputs.
Chore inspection reads inline
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
| `.runtime/active/<address>/register.lock` | Discovery publication/removal for one address |
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

State formats have independent build/publication locks and share source edit
locks. Runtime status and databases retain their own compatibility rules; stop
host/guest writers together when upgrading those incompatible contracts.
Native lock guards remain stable after release; existence does not mean a lock
is held. The shared `status.json.lock` claim instead contains writer identity
and is removed only by its owner or confirmed-dead recovery. Do not delete live
guards. Legacy lock files may be removed only while the affected scope is offline.


## Agent State

Agent State is immutable runtime input derived from durable authored source.

State directories:

| Value | Directory |
| --- | --- |
| Root layer | `AGENT_ROOT/.state/root/v<format>/` |
| Home layer | `AGENT_HOME/.state/home/v<format>/` |
| Agent composition | `AGENT_HOME/.state/agent/v<format>/` |

`<format>` is the running `AGENT_STATE_FORMAT`, independent of the software
release version. Each directory holds its current pointer, immutable revisions,
and build/publication locks. Source edit locks remain in the unversioned scope
directories. Other formats and old unversioned caches remain untouched and are
not searched. [State](state.md#persistent-layout) owns revision
contents, formats, integrity checks, and publication. [Workspaces](workspaces.md)
owns path capture; [Hosting](hosting.md) owns guest mounts.


## Durable State

Durable authored state lives in:

- `AGENT_ROOT/config.toml`
- agent-root cap directories
- `AGENT_HOME/config.toml`
- `AGENT_HOME/agent.too`
- home cap directories
- `AGENT_HOME/chores/`

Durable execution state does not live in authored files. It lives in
the `chores` table and execution tables in `runs.db`.
