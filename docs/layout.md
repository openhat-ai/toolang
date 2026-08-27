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
  .setup/
    models/
  .state/
  .runtime/
  .sandbox/
  agents/
    <agent>/
      agent.too
      flows/
        <name>.too
      config.toml
      psyches/
      skills/
      services/
      prompts/
      tasks/
      chores/
      archive/
      .setup/
      .state/
      .runtime/
```


## Agent Home

Each resident agent lives under:

- `${TOOLANG_ROOT}/agents/<agent>/`

Visiting agents fetched by `toolang run <remote>` are materialized under a
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
| `flows/<name>.too` | Independently validated home flow module |
| `config.toml` | Agent-local configuration |
| `psyches/`, `skills/`, `services/`, `prompts/` | Agent-local cap definitions |
| `tasks/` | Ready task documents |
| `chores/` | Ready chore documents |
| `drafts/` | Draft task and chore documents |
| `archive/` | Retired task and chore documents |
| `.setup/` | Rebuildable installed-environment caches |
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
`agents/<agent>/` is the agent home, rebuildable setup uses `.setup/`, Agent
State uses `.state/`, and durable operational data uses `.runtime/`.


## Setup Cache

Rebuildable environment discovery data lives under:

- `${TOOLANG_ROOT}/.setup/`

| Path | Purpose |
| --- | --- |
| `models/` | Last-good provider model lists shared across agents and processes |


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
| `jobs.db` | Ready-job checkpoints, RRULE cursors, and active claims       |
| `runs.db` | Threads, controls, runs, steps, and replayable model inputs   |
| `files.db` | File request claims, fingerprints, and completion state        |
| `ids.json`     | Local id allocator state                                     |
| `tools/`       | Per-toolset plugin working directories                       |
| `channels/`    | Per-channel plugin working directories                       |


## Agent State

Agent State is immutable runtime input derived from durable authored source.

State directories:

| Value | Directory |
| --- | --- |
| Root layer | `${TOOLANG_ROOT}/.state/root/` |
| Home layer | `${TOOLANG_ROOT}/agents/<agent>/.state/home/` |
| Agent composition | `${TOOLANG_ROOT}/agents/<agent>/.state/agent/` |

Each directory stores a current-revision pointer, a writer lock, and immutable
revision directories. See [agent-state.md](./agent-state.md) for the canonical
documents, revision calculation, validation, and publication rules.


## Durable State

Durable authored state lives in:

- root `config.toml`
- root cap directories
- agent `config.toml`
- agent program
- agent cap directories
- agent `tasks/`
- agent `chores/`
- agent `drafts/`
- agent `archive/`

Durable execution state does not live in authored files. It lives in
`jobs.db`, `runs.db`, and `files.db`.

Inbox directories passed with `--inbox` are external user directories. Toolang
does not write marker files into them; file request progress is recorded under
the agent runtime room in `files.db`.
