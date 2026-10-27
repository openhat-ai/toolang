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

This precedence also applies to scripts and `cd`. The canonical host root supplies
the R8 code for Hub keys; OS usernames and guest mount paths do not enter that code.

Current root layout:

```text
${TOOLANG_ROOT}/
  config.toml
  catalog.json                      # Optional root catalog
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
after stop; agent clients use `[teaming]` in their PLACE root layer. Human and backend
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

`AgentLayout` derives placement paths. In the table, `R` is the selected host ROOT,
`G` is the configured Docker root (default `/root/.toolang`), and `DIR` is the
canonical roaming source directory. `R8`, `P10`, and `P19` denote complete codes.

| Placement | Host ROOT | Host PLACE | Guest ROOT = PLACE |
| --- | --- | --- | --- |
| Resident | `R` | `R` | `G` |
| Visiting | `R` | `/tmp/toolang/<R8>/<P10>` | `G` |
| Roaming | `R` | `DIR/.toolang/<R8>/<P19>` | `G` |

Each Docker guest hosts one agent. In both environments, HOME is
`PLACE/agents/NAME` and lab is `PLACE/workspaces/NAME/lab`. Other host workspaces
may live anywhere; their guest targets are `G/workspaces/NAME/WORKSPACE`.
Roaming HOME does not depend on exposing its source directory. The guest retains
the original placement and full address even though its physical ROOT and PLACE
coincide. [Hosting](hosting.md#docker-filesystem) defines the mounts.


## Setup Data

Setup inputs remain under the placement's configuration locations; resource
views and generations stay in memory. [Setup](setup.md) owns their capture,
materialization, and lifetime. No `.setup/` model-cache directory is created or
mounted; existing legacy `.setup/models` is ignored.


## Runtime state and discovery

The host owns one directory per complete address under ROOT, regardless of PLACE:

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
no status filename: status is always `HOME/.runtime/status.json`.

For example, the guest view for a roaming Docker launch is:

```json
{
  "version": 6,
  "address": "aid@<P19>",
  "home": "/root/.toolang/agents/aid",
  "launch": "<launch-id>",
  "location": "guest",
  "sandbox": "docker:python:3.13-slim"
}
```

The guest receives its read-only view at `ROOT/.runtime/active/NAME/`; it writes
status through the separately mounted HOME. The host removes the address directory
only after resource release and generation checks. Registration enables active
discovery; removing it does not delete history or prove process death.

`up.records` owns reference parsing; `up.registry` owns guarded publication and
conditional removal; `up.identity` validates full source/root/home ownership.
[Hosting](hosting.md#runtime-observation-and-retirement) owns recovery and
retirement. Legacy format-5 references remain readable for resource recovery.

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
| `.state/root/v<format>/build.lock` | Root layer construction and publication |
| `agents/<agent>/.state/home/v<format>/build.lock` | Home layer construction and publication |
| `agents/<agent>/.state/agent/v<format>/publish.lock` | Complete agent preparation, composition, and publication |

Edit locks first appear on a mutation attempt, including one that fails or makes
no content change. Read-only operations never create edit locks or their parent
directories: they acquire an existing guard, or read optimistically and retry
under the guard if the first writer creates it during the read. An unchanged
roaming configuration projection also leaves an absent edit lock absent.
Shared roaming `toolang.toml` edits use `<srcdir>/.toolang/.state/root/edit.lock`; projected
PLACE configuration uses `PLACE/.state/root/edit.lock`. Release edit locks before refreshing
State. The watcher uses source-change detection independently of edit locks.
Roaming projection verifies its root inputs again after releasing the projection guard
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
| Root layer | `<place>/.state/root/v<format>/` |
| Home layer | `<place>/agents/<agent>/.state/home/v<format>/` |
| Agent composition | `<place>/agents/<agent>/.state/agent/v<format>/` |

`<format>` is the running `AGENT_STATE_FORMAT`, independent of the software
release version. Each directory holds its current pointer, immutable revisions,
and build/publication locks. Source edit locks remain in the unversioned scope
directories. Other formats and old unversioned caches remain untouched and are
not searched. [State](state.md#persistent-layout) owns revision
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
