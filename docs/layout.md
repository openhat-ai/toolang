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
      config.toml
      psyches/
      skills/
      services/
      prompts/
      collab/
        MEMO.md
      lab/
        MEMO.md
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

The hash is derived from `<agent-source>`. The stable visiting root lets
prepared state and the last runtime port be reused across repeated foreground
runs of the same remote agent ref, independent of the local `TOOLANG_ROOT`,
while remaining disposable across machine restarts or normal
temporary-directory cleanup. The cached remote `agent.too` is refetched after
one hour. Roaming `.too` file invocation uses the source file's sibling
`.toolang` directory and does not start a long-lived HTTP runtime.

Key paths:

| Path | Purpose |
| --- | --- |
| `agent.too` | Agent program |
| `config.toml` | Agent-local configuration |
| `collab/MEMO.md` | Agent-maintained collaboration notes |
| `lab/MEMO.md` | Agent-maintained exploration notes |
| `psyches/`, `skills/`, `services/`, `prompts/` | Agent-local cap definitions |
| `tasks/` | Ready task documents |
| `chores/` | Ready chore documents |
| `drafts/` | Draft task and chore documents |
| `archive/` | Retired task and chore documents |
| `.setup/` | Rebuildable installed-environment caches |
| `.state/` | Immutable prepared state generations |
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
`agents/<agent>/` is the agent home, rebuildable setup and prepared state use
`.setup/` and `.state/`, and durable operational data uses `.runtime/`.


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
| `tools/`       | Per-tool plugin working directories                          |
| `channels/`    | Per-channel plugin working directories                       |


## Prepared State

Prepared state is immutable runtime input derived from durable authored state.

Prepared directories:

| Scope | Directory |
| --- | --- |
| Global | `${TOOLANG_ROOT}/.state/` |
| Per-agent | `${TOOLANG_ROOT}/agents/<agent>/.state/` |

Each prepared directory stores a current-version pointer, a per-scope writer
lock, and immutable generation directories. See
[prepared-state.md](./prepared-state.md) for the generation format and
publication rules.


## Durable State

Durable authored state lives in:

- root `config.toml`
- root cap directories
- agent `config.toml`
- agent program
- agent cap directories
- agent `collab/` and `lab/` run spaces
- agent `tasks/`
- agent `chores/`
- agent `drafts/`
- agent `archive/`

Durable execution state does not live in authored files. It lives in
`jobs.db`, `runs.db`, and `files.db`.

External workspace grants live only in the agent `config.toml` `[workspaces]`
table. They are not copied into either memo file.

Inbox directories passed with `--inbox` are external user directories. Toolang
does not write marker files into them; file request progress is recorded under
the agent runtime room in `files.db`.
