# Unreleased Grammar Integration

The current runtime requires tree-sitter-toolang 0.3.1. This grammar upgrade
does not change the Toolang package version.

Use explicit `using`, `if`, and `by` clauses, `in N lanes` (`in 1 lane`), and
`repeat N times` (`repeat 1 time`). Replace an unbound `rank score top 3` with:

```too
sort descending by score
keep first 3
```

Sort and keep commit separately. Sort scores every item once; keep performs
local selection without a model call. Retry reuses committed sorting work.
For bottom-N, use descending sort followed by `keep last N` to preserve order.
Named or discarded rank-with-selection needs explicit re-authoring because
the new statements have different binding boundaries. See
[Flow syntax](./docs/flow-syntax.md) for the full migration rules.

Block ownership follows indentation. Required bodies must contain substantive
content; comments cannot make an empty loop valid or pull an outer statement
into it. A final `until` belongs to the repeat at its sibling indentation.
Every implicit prose line checks its first token for lowercase keywords.
Capitalize keyword-led prose or put it inside an explicit `run:` text block,
where keywords and Markdown remain literal content.

State layer schema advances from 4 to 5 and rebuilds from migrated source.
Execution store schema advances from 38 to 39. The runtime rejects old stores
without modifying or deleting them, including stores without rank history.
Preserve the previous runtime and source to inspect or retry old executions;
use a separate compatible store for new executions. No history migration or
downgrade conversion is included.

The historical release notes below describe the previously released syntax.


# Toolang 0.3.0 Release Notes

Release date: August 3, 2026.

Toolang 0.3.0 is the first release built on the static agic/flow language and
the rebuilt durable execution runtime. It is an alpha release and contains
intentional breaking changes from 0.2.7.


## Highlights

- Define model/tool runnables as `agic` declarations and deterministic
  orchestration as `flow` declarations.
- Run a public runnable directly with `toolang SCRIPT RUNNABLE`.
- Use one durable execution model across scripts, chats, tasks, and chores.
- Inspect threads, recursive run trees, steps, controls, outputs, and failures
  without requiring a running HTTP server.
- Steer or cancel active runs, and rewind or fork terminal thread history.
- Serve the same runtime through the local Agent API and native run-event SSE.
- Extend Toolang through explicit tool, channel, sandbox, model-provider, and
  model-adapter entry points.


## Breaking Language Changes

The old `use` and `thunk` spellings are no longer accepted. A typical program
moves to `with` and `agic` and uses `_` for primary input:

```too
with skill briceyan/codebase-navigation

agic review(_: Part[], focus?: Text):
  skills += codebase-navigation

  Review {{_}} with emphasis on {{focus}}.
```

Runnable parameters are named `params` in declarations and supplied as
`args` at runtime. `Part` and `Part[]` are the language-level percept types;
`Message` is reserved for model-call and chat protocol values.

Flows share the runnable namespace with agics:

```too
flow review_twice(_: Part[]):
  repeat 2:
    run review
```

See `docs/program.md`, `docs/flow-syntax.md`, and `docs/input-syntax.md` for the
complete current language.


## CLI And Plugin Migration

- Replace legacy invocation commands with `toolang SCRIPT RUNNABLE`.
- Use `toolang AGENT chat`, `threads`, `runs`, and `inspect` for direct local
  execution and durable history.
- Update external plugin imports to use the contracts and values in
  `toolang.base`.
- Remove loop-plugin integrations. Agic model/tool sequencing is now fixed
  executor behavior.
- Update synchronous runtime integrations to the current async tool, channel,
  sandbox, adapter, and tracer contracts.


## Runtime Data Upgrade

The 0.2.7 `runs.db` schema is not migrated by this release candidate. Before
upgrading:

1. Stop all Toolang agent processes.
2. Back up the complete Toolang root.
3. Preserve or move each agent's `.runtime/runs.db` if the old history matters.
4. Migrate authored `.too` programs to the new language.
5. Start the new runtime and let it create current runtime stores.

Authored agent, cap, task, and chore files remain the durable source material;
generated setup, state, and runtime projections can be rebuilt.


## Installation

Upgrade an existing installation with:

```bash
uv tool upgrade toolang
toolang --version
```

New installations can use:

```bash
uv tool install toolang
```

Review [Known Limitations](./KNOWN_LIMITATIONS.md) before running remote agents
or exposing an Agent API endpoint.
