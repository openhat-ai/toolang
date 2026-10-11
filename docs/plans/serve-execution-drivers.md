# Serve execution drivers

Status: discussion plan for review; resolve the open decisions and obtain human
approval before implementation. This PR changes no product behavior.

## Goal and scope

Make chat, talk, and chore independently selectable execution drivers with
consistent handlers, durable identities, and observable execution. A driver
accepts triggers, a thread retains context, and a run records one execution.
Scope includes hosting/API admission, unified chores, thread naming, and
observation; it excludes a generic driver plugin framework and live toggles.

Today, hosting starts the scheduler unconditionally, messaging creates a new
`script_` thread per batch, and jobs distinguish task/chore and reject duplicate
IDs. The following contracts replace those behaviors.

## Drivers and hosting

| Driver | Trigger | Context |
| --- | --- | --- |
| `chat` | Interactive requests over FastAPI | One `chat_` thread per conversation |
| `talk` | Incoming agent messages | One durable `talk_` thread per agent |
| `chore` | One-off, scheduled, or manual work | One `chore_` thread per chore |

- Defaults: resident enables all three; visitor enables chat/talk; roaming enables
  none. Visitor is currently called `visiting` internally. Standalone scripts
  start no drivers; attaching to a runtime does not change its selection.
- Every driver resolves a runnable handler from State and uses the common executor
  with a consistent State/Setup snapshot. Handler is a role, not a new runnable
  type. Drivers need not all be loops.
- TUI/WebUI use chat over HTTP. Disabling chat retains the management API, health,
  inspection, authoring, cancellation, script execution, and other drivers.
  Gate interactive creation, submission, retry/rerun, and steer before execution
  writes, including when the target thread originally came from another driver.
- Disabled chore/talk drivers do not dispatch or consume triggers. Separate talk
  consumption from transport, presence, outbound tools, and observation. Explicit
  startup/shutdown ownership must also clean up partial startup failures.
- Replace client type with `client_id` and `request_id`, recording the initiating
  client instance and request alongside execution. Neither owns the thread nor
  selects its driver. Preserve request identity across retries; this change alone
  does not define a new deduplication scope.

## Command-specific driver option

- Expose `--driver` only on `serve`, `start`, and `chat`. It is a command-specific
  option, not a root/global option or a general runtime-acquisition parameter
  exposed by every command.
- `chat` connects to a running agent or implicitly performs `start` when the agent
  is inactive. A new startup uses the same driver-selection rules as `start`.
- Accept a comma-separated driver set; `--driver none` explicitly selects none
  and cannot be combined with driver names. An explicit set replaces defaults;
  omission on startup uses the placement defaults above. Reject invalid values
  before starting a process, resolve once at the CLI boundary, and forward the
  concrete set through foreground/background, hidden `_serve`, and host/guest launches.
- When chat finds an existing agent, omitting `--driver` preserves its selection
  without reapplying defaults. An explicit set is a compatibility requirement:
  an equal set permits connection; a different set fails with restart guidance.
  Chat also fails if chat is disabled, even when the explicit set matches. It never
  enables drivers, restarts the agent, or launches another instance automatically.
  For a new agent, reject a chat invocation whose resolved set excludes chat before
  starting it. Report the effective set in startup/status output.
- `talk` is a Hub messaging client and does not start the receiving agent.
  Script execution owns a run's lifecycle, not persistent drivers. Neither command
  accepts `--driver`; use `serve` to activate drivers and inline chores in a `.too`
  app. A script attached to an existing agent leaves its driver selection intact.

Proposed examples (`too` is the `toolang` alias):

```sh
too serve alice --driver chat,talk,chore
too start alice --driver talk,chore
too serve alice --driver none
too alice chat --driver chat,talk,chore
```

## Chore definitions and persistence

Remove Task and Job as separate concepts. Chore unifies one-off and repeated work
and remains authorable in Markdown and inline `.too` for portable apps.

| Value | Rule |
| --- | --- |
| `name` | Inline declaration name or filename without `.md`, e.g. `cleanup` |
| `ref` | `chore/cleanup`, following the `skill/a-b-c` convention |
| `id` | Deterministic Base32 identity derived from name |
| `thread_id` | `chore_<id>` |
| `title` | Optional display text, excluded from identity |

Allow shadowing through established definition-resolution precedence and dispatch
only the winning definition. The same name keeps its ID, thread, and progress
when the winning source changes. Identity is scoped to the owning agent's durable
state; content, schedule, directory, and revision do not enter the ID. Changing a
declaration name or filename creates a new identity.

Definitions contain no execution state or separately maintained IDs/refs/thread
IDs. State may retain inline declarations and runnable handlers; source parsing
remains with language/catalog owners. ChoreDriver resolves definitions and owns
scheduling; progress does not belong in State or Setup.

A `chores` table keyed by derived ID persists definition/revision association,
status, active run, and schedule checkpoints. Runs retain each execution's history.
Reload/restart must not redispatch a completed one-off revision or reset schedule
progress. Definition edits and manual requests follow explicit activation rules;
the driver never writes progress back into Markdown or `.too`.

## Threads, instances, and observation

- Use `chat_`, `talk_`, and `chore_` for driver contexts; retain `script_` for
  scripts. Independent contexts created by `spawn` use `worker_`, regardless of
  parent prefix. Store ancestry in records; ordinary nested calls retain their
  thread. Each execution still has a `run_` ID.
- Reuse the talk thread across batches and restarts with normal history/compaction.
  Keep conversation/backend checkpoints separate and preserve the
  [transport contract](agent-hub-transport.md). Avoid accidentally inheriting a
  previous batch's workdir.
- The same `.too` source does not imply shared threads, drivers, or progress.
  Independent agent instances remain separate; restarting the same durable agent
  restores its state.
- Within its selected scope, `top` observes every running resident, visitor,
  roaming, and script instance as instance -> thread -> run. Runtime observation
  works with talk disabled; idle driver status is separate from active runs.

## Delivery and acceptance

Paths below are relative to `src/toolang/`. Deliver independently verifiable
changes after approval:

1. **Selection/admission:** serve/start/chat command signatures/factories,
   `cli/common/agent_server.py`, `up/`, and `api/`. Test help/parsing on the selected
   commands, rejection on talk/script, all eight combinations, placement defaults,
   driver-free standalone scripts, and launch forwarding. Cover chat's implicit
   start and existing-agent connection with omitted/equal/conflicting selections
   and chat disabled; failures must preserve the running configuration and create
   no execution records. Management remains healthy for every selection.
2. **Unified chores:** `lang/`, `catalog/`, `state/`, `work/`, `common/ids.py`,
   and chore CLI/API surfaces. Test inline/file equivalence, shadowing, rename,
   deterministic IDs, restart without redispatch, schedule/manual serialization,
   and unchanged authored definitions.
3. **Threads/handlers:** `work/{messaging,teaming}.py`,
   `execution/{types,threads,schemas}.py`, and `execution/executor/spawn.py`.
   Test durable talk/chore reuse, worker ancestry, agent isolation, history,
   reconnect/receipts, and cleanup after interrupted startup/shutdown.
4. **Observation/compatibility:** `execution/{activity,client}.py`, `teaming/`,
   `cli/toolang/commands/top.py`, client/API schemas, and existing integration
   tests. Verify client/request identity, all placements, concurrent instances,
   talk-disabled visibility, and preserved completed work/history on upgrade.
   Update CLI help, task/work/messaging docs, and the generated changelog with the
   implementation.

Code commits require the repository's Ruff, formatting, type, and full offline
pytest checks. This plan-only PR requires source/path/link review and
`git diff --check`; proposed CLI syntax is not yet available.

## Open decisions and migration risks

1. **API admission:** the concrete chat/script API boundary that enforces driver
   selection without relying on client identity or thread prefixes.
2. **Identity:** name normalization, Base32 derivation/length, collision handling,
   and same-scope shadow precedence; instance discovery/storage that distinguishes
   restart from another instance of the same source.
3. **Activation/migration:** confirm no schedule means one-off and fix edit/retry/
   manual activation rules. Migrate authored IDs, task/job records, and today's
   default recurring schedule without replaying completed work. Preserve historical
   thread/run IDs with an explicit association to new chore identities.
