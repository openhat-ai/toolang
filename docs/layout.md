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
  .prepared/
  .sandbox/
  agents/
    <agent>/
      <agent>.too
      config.toml
      psyches/
      skills/
      services/
      prompts/
      tasks/
      chores/
      archive/
      .prepared/
      .runtime/
```


## Agent Home

Each resident agent lives under:

- `${TOOLANG_ROOT}/agents/<agent>/`

Visiting agents fetched by `toolang run <remote>` are materialized under a
stable system temporary root derived from `TOOLANG_ROOT` and the canonical
remote ref:

- `/tmp/toolang-visiting/<agent>-<root-hash>-<ref-hash>/`

The stable visiting root lets prepared state and the last runtime port be
reused across repeated foreground runs of the same remote agent ref while
remaining disposable across machine restarts or normal temporary-directory
cleanup. Roaming `.too` file invocation uses the source file's sibling
`.toolang` directory and does not start a long-lived HTTP runtime.

Key paths:

| Path | Purpose |
| --- | --- |
| `<agent>.too` | Agent program |
| `config.toml` | Agent-local configuration |
| `psyches/`, `skills/`, `services/`, `prompts/` | Agent-local cap definitions |
| `tasks/` | Task documents |
| `chores/` | Chore documents |
| `archive/` | Retired task and chore documents |
| `.prepared/` | Prepared runtime artifacts |
| `.runtime/` | Live runtime state |


## Runtime Room

Each agent runtime stores operational state under:

- `${TOOLANG_ROOT}/agents/<agent>/.runtime/`

Key paths:

| Path           | Purpose                                                      |
| -------------- | ------------------------------------------------------------ |
| `runtime.json` | Runtime status, endpoint, sandbox summary, and enabled features |
| `agent.log`    | Runtime log                                                  |
| `execution.db` | Runs, steps, updates, and instruction blobs                  |
| `ids.json`     | Local id allocator state                                     |
| `pulse.json`   | Pulse loop state                                             |
| `tools/`       | Per-tool plugin working directories                          |
| `channels/`    | Per-channel plugin working directories                       |


## Prepared State

Prepared state is immutable runtime input derived from durable authored state.

Prepared directories:

| Scope | Directory |
| --- | --- |
| Global | `${TOOLANG_ROOT}/.prepared/` |
| Per-agent | `${TOOLANG_ROOT}/agents/<agent>/.prepared/` |

Each prepared directory stores a `lock.json` and materialized files.


## Durable State

Durable authored state lives in:

- root `config.toml`
- root cap directories
- agent `config.toml`
- agent program
- agent cap directories
- agent `tasks/`
- agent `chores/`
- agent `archive/`

Durable execution state does not live in authored files. It lives in
`execution.db`.
