# Define agent command argument order

Status: approved by the human in conversation on 2026-10-22, including
implementation. This records the final scope after review.

## Goal and decisions

Express the command's viewpoint through agent placement. The `too` and
`toolang` entry points share these explicit leaf-owned policies:

| Commands | Accepted order | Examples |
| --- | --- | --- |
| `start`, `stop`, `serve`, `cd`, `info`, `chat` | Both | `too start gus` and `too gus start` |
| `new`, `remove`, `clone` | Command then agent/source/name only | `too new gus`; `too remove gus`; `too clone gus bob` |
| `inspect`, `top`, `caps`, `tools`, `models`, `providers` | Agent then command | `too gus inspect runs`; `too gus models` |
| `steer`, `cancel`, `rerun`, `fork`, `rewind` | Agent then command and existing record/message operands | `too gus rerun RUN` |
| `workspace`, `chore`, `psyche`, `skill`, `service`, `prompt` and all their children | Optional/existing agent scope before the group | `too gus skill new review`; `too gus chore run daily` |

Creation, removal, and cloning use an external management viewpoint.
Running an existing agent and entering its home, viewing
its info, or chatting with it read naturally in either order. Inspection and
control operate within an agent's scope. Resource groups require their owner
before the resource/action; their local `new/remove` operands name resources.

This selectively supersedes the prefix-only restriction in
[CLI command recognition](../design/cli-command-recognition.md).
Catalog commands retain their existing native name/source operands.

Omitted-agent behavior is outside this change and remains as implemented.
Commands without an agent parameter also remain unchanged: `talk` (CONVO),
`convo`, `hub`, `list`, plugin inventories, language commands, `init`, `run`,
help topics, and private `_serve`. Implicit Script entry is unchanged.
The audit covered all 42 top-level registrations and 78 executable leaves,
including hidden commands. No command-order questions remain open.

## Design and implementation touchpoints

- Add explicit dual-order support to the existing immutable `AgentArgument`
  contract; default to agent-first. Native parameter registration consumes an
  agent operand only when its leaf permits command-first input and no prefix
  is inherited. Preserve one conversion, callback, and command-line parameter
  source. Never mutate cached command trees per invocation.
- `src/toolang/cli/common/{agent_argument,help}.py` own native registration,
  help, and completion. Reuse the existing lazy factories and entry points.
- Declare the six changed leaf policies in
  `src/toolang/cli/toolang/commands/{agent,chat/__init__,cd,runtime}.py`.
  Preserve target forms, placement/driver limits, preparation, and execution.
- Help shows both usage forms for the six dual-order commands and one AGENT
  argument row. Completion offers agents in accepted positions.
- Preserve command precedence, `agent:NAME` escaping, Script routing, native
  options and optional values, literal `--`, eager help, and free-text tails.
  Recognition/help perform no acquisition or command effects.
- Extend CLI argument/routing/help and representative offline integration
  tests. Update current CLI documentation; retain old plans.
- Generate the changelog with `too aid.too update_changelog` and have a
  maintainer verify the entry before merge. No version changes are required.

## Acceptance tests

1. Both forms of `start/stop/serve/cd/info/chat` produce identical target bindings,
   options, parameter sources, output, and effects; conversion runs once.
2. Catalog commands remain command-first; names and clone source/target roles
   remain intact.
3. All inspection/control/group commands retain prefix-only selection. Their
   business operands and existing omitted-agent behavior remain unchanged.
4. Accepted selectors, command-name collisions, required arguments, option
   values, optional-value flags, and `--` retain native validation. Help and
   completion advertise only accepted forms and cause no acquisition/effects.
5. Repeated/concurrent mixed-order invocations do not leak target state or
   mutate caches. Script and non-agent commands retain their behavior.
6. Run `git diff --check`, `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run ty check`, and `uv run pytest -n auto` before code/test commits.

## Risks

Native parameter and completion adapters require coverage against the pinned
Typer implementation. No unresolved design questions remain.
