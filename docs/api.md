# Control Surfaces

This document defines the public CLI and local agent HTTP API.

Interactive CLI, TUI, and WebUI surfaces may parse the `ChatInput` forms
defined by [input-syntax.md](./input-syntax.md). Quick commands remain local to
the interaction surface. Execution surfaces resolve `RunOverride` and
`CallInput[str]` values into the structured run request described here.


## CLI

The CLI entry points are:

- `toolang`
- `too`
- `caps`

Top-level commands are:

- `new`
- `clone`
- `remove`
- `list`
- `hub`
- `text`
- `top`
- `info`
- `serve`
- `start`
- `stop`
- `chore`
- `task`
- `psyche`
- `skill`
- `service`
- `prompt`
- `chat`
- `steer`
- `cancel`
- `retry`
- `rerun`
- `rewind`
- `fork`
- `inspect`
- `caps`
- `models`
- `providers`
- `tools`
- `catalogs`
- `adapters`
- `toolsets`
- `sandboxes`
- `init`
- `run`

Global options:

- `--root`
- `--version`

Cap commands:

- `caps [AGENT] list [--query QUERY]`
- `caps [AGENT] <kind> list [--query QUERY]`
- `caps [AGENT] <kind> new <name>`
- `caps [AGENT] <kind> edit <name>`
- `caps [AGENT] <kind> delete <name>`
- `caps [AGENT] <kind> add <ref>`
- `caps [AGENT] <kind> remove <name>`
- `caps <kind> template [template-name]`

`<kind>` is one of `psyche`, `skill`, `service`, or `prompt`. Without `AGENT`,
cap mutations target root caps. With `AGENT`, they target the selected agent home's caps.

List output uses `REF`, `DESCRIPTION`, `LOCATION`, and `TAGS` for combined and
kind-specific lists. `location` addresses actual content, with `file:line` only
for inline caps. JSON and query keys are lowercase.

Resource query parameters use native TQ over public model/tool/cap records.
Cap identities have singular prefixes such as `skill/reviewer`, including
kind-specific lists. Use tags for provenance and scope, for example
`skill/*[tags has all (home,remote)]`. API response envelopes retain their
existing shapes; see [Resource Queries](queries.md) for the matching records.

Typical usage:

```bash
toolang new alice
toolang list
toolang init demo
toolang run demo/work.too
toolang run demo/work.too main --help
PY_LOG=toolang.execution=info too ./examples/flows/proposal_workshop.too -- "Propose a weekly release process"
too ./examples/flows/proposal_workshop.too --help
too ./examples/flows/proposal_workshop.too -- "Propose a weekly release process"
toolang serve alice
toolang serve alice --sandbox docker
toolang serve brice/alice
toolang serve https://toolang.ai/alice.too
toolang clone brice/alice
toolang start alice
toolang start alice --sandbox docker
toolang stop alice
toolang info alice
toolang alice info
toolang ./examples/flows/deep_search.too info
toolang alice chat
toolang alice chat --thread
toolang alice chat --thread term_3nprht9x
toolang alice chat --sandbox docker
toolang alice inspect controls
toolang alice inspect threads
toolang alice inspect runs
toolang alice inspect term_3nprht9x runs
toolang alice inspect run_ppkp9e94 steps
toolang alice inspect run_ppkp9e94.0/output/value
toolang alice inspect run_ppkp9e94.0 call
toolang alice retry run_ppkp9e94 --limit tokens=200000 --limit time=900
toolang alice rerun run_ppkp9e94 --model 'openai/gpt-5 effort=high'
toolang alice steer run_ppkp9e94 "Use the smaller patch"
toolang alice cancel term_3nprht9x
toolang alice rewind run_ppkp9e94
toolang alice fork run_ppkp9e94
toolang models
toolang catalogs
toolang toolsets
```

Top-level routing uses these command shapes:

- Script commands use `init DIR` and `run FILE [RUNNABLE]`. They appear
  in the final Script Commands help panel. `init` creates `aide.too` exclusively
  from the packaged template and never overwrites existing files or symlinks.
  The directory is required: `init` alone shows help; `init .` creates the file
  in the current directory. The generated file includes a shebang and executable
  permission, so it can also run directly as `./aide.too`.
  `run` accepts local `.too` files; foreground agents use `serve`.
- catalog commands are command-first only: `new`, `clone`, `list`, and
  `remove AGENT`
- agent-self commands accept either order: `info`, `serve`, `start`, and `stop`
- commands for an agent's execution history, caps, tasks, or chores require
  the target first, such as `toolang alice retry RUN` or
  `toolang alice skill list`

A command name wins whenever an unassigned token could be either a command or
a dynamic name. Use `agent:NAME` to force a colliding resident target. After a
local `.too` target, use `agic:NAME`, `flow:NAME`, or `runnable:NAME` to force a
colliding runnable name. A token ending in `.too` selects a local source path
even when that path does not exist; use `agent:NAME` for a resident name ending
in `.too`. Once a command is selected, its remaining operands are parsed by
that command and are not reclassified.

A target without a command shows the commands accepted by that placement.
Plain resident names are recognized from the selected root's agent catalog;
explicit resident selectors and remote selectors are unambiguous. Showing
remote target help does not resolve or fetch the agent. An incomplete selected
command shows its own help before target existence or other runtime validation.

Thread and run inspection, retry, rerun, steering, cancellation, rewind, and
fork open the selected agent's durable execution store directly. They do not
start or call the agent HTTP server. A run id selects its owning thread; a
thread id selects its active run for steering or cancellation and its latest
terminal run for retry, rerun, rewind, or fork. Retry reopens the same root run
and optionally starts at `--anchor`; rerun starts a new root run from the source
invocation. Fork retains the anchor run, while rewind removes it and the
following visible suffix.


### Automatic History Compaction

ModelCall preflight can initiate a runtime `_toolang.compact()` Step when the
assembled input exceeds its budget. The Step owns a same-thread child Run with
the reserved identity `_:compact`. It records Step-level read/model batches and
a cumulative text summary. A historical root can span batches and retain a suffix
starting at a Step. Only model preflight can initiate this internal operation.

After the child succeeds, the executor publishes its reference as the thread's
horizon and records the calling Run's compact control in one transaction. The
next Model Step adopts that summary plus retained history. Horizons also accept
the completed producer's final successful summary Model Step. Original records and
past ModelCalls remain inspectable. Failure stops the caller without dispatching
the oversized call. See [model configuration](models.md#automatic-compaction-configuration) for
`compact.model`, `summary`, `recent`, `trigger`, and runtime `--compact-model`
settings.


## Team activity

`too top` observes the running Hub; `too AGENT top` connects directly to an
existing agent. Neither starts execution. `--once` (also implied for redirected
output) prints a snapshot. Interactive updates are limited to twice per second.

```sh
too top --view agent
too alice top --view execution --tree
too top --since 1d --recent 1h --sort cost
too top --filter fs.read --active --once
```

Use `a/t/e` for Agent/Thread/Execution, F5 for List/Tree, F4 for filters, F6 for
sort, F7 for Recent, F8 for Stats, arrows to select/fold, Enter for details, and
`q` or Ctrl-C to exit. Editors accept Enter/Esc, Ctrl-U to clear, Tab for range
presets, and Ctrl-A to toggle Active in the filter editor. `<`/`>` scroll wide
rows. RUN always identifies the root; STEP holds the row's complete run or step
reference. Only running paths expand, including required completed ancestors.
Agent/Thread rows summarize active and failed root counts, without task titles.
Matched/eligible and loaded counts follow the view; Execution counts root runs,
including in Tree layout.

Stats defaults to the owning executor session; `--since` accepts `session`,
`all`, a duration resolved once, or a timestamp with timezone. Calls and cost
include descendants; TIME measures the row's own execution, or sums root durations
for Agent/Thread. Retries retain consumption. `TIME*` marks a custom Stats range.
Recent independently retains unfinished work and recently changed objects
(default `30m`). Unknown cost is `-`, estimates use `~`, partial cost uses `+`.
Legacy history and interrupted sessions carry explicit coverage information.

| Compact activity endpoint | Response |
| --- | --- |
| Agent `GET /api/v1/activity?offset=0` | One page of up to 200 root summaries and their running paths. |
| Agent `GET /api/v1/activity/batch` | Pages from one database snapshot. |
| Hub `GET /activity` | Agent pages, with presence and cached coverage. |
| Agent `GET /api/v1/activity/stream`, Hub `GET /activity/stream` | Absolute `activity_page` frames, committed together by `activity_checkpoint`. |

Queries accept `since`, `recent` (seconds), `all_recent=true`, `filter`, and
`active`. Pages include session/revision, observation time, Stats/Total,
coverage, root/thread counts and `next_offset`; only the first page carries the
thread rows. Individual page requests must agree on session/revision; otherwise
restart pagination. SSE connections always begin
with a fresh atomic snapshot. They do not replay token deltas or full outputs.
The agent reader shares committed aggregates and open duration anchors; Hub
queries agent HTTP and caches absolute values. Offline data freezes at its last
observation; unavailable ranges are labeled. Layout and sort changes stay local.

The existing canonical event subscription API remains separate:

An enabled agent exports its canonical stream independently of execution and
messaging. The authenticated Hub endpoint is `GET /events/stream`, with optional
`agent=agent:alice`, and either `thread=ID` or `run=ID` when an agent is selected.
No filter observes the team. `after` accepts only a Hub cursor
(`h1.<epoch>.<stream-id>`); local agent cursors are separate. Root subscriptions
close after the complete tree and queued retries; other scopes stay open.

Execution frames preserve their event name/data and add `agent`, `source_cursor`,
and Hub `cursor`. SSE IDs acknowledge Hub positions. Structural context has
`context: true` and no SSE ID. Ignore inherited SSE IDs on context,
`stream_prefill`, and `stream_status` frames. `stream_prefill` carries `{cursor, scope, replace}`:
`replace: null` resets the selected view; otherwise each `{agent, roots}` replaces
that agent's selected view (`roots: null`) or named trees. Stage the entire prefix
and atomically commit it at `stream_checkpoint`. `stream_status` supplies
`{agent, online, complete, reason}`; incomplete origins must not be treated as
completed executions. A failed or interrupted prefix advances no checkpoint.

Invalid filters/cursors return `400`, unknown or unretained scopes `404`, and
backend/schema failures `503`, with `code`/`detail` JSON. Once streaming starts,
`stream_error` reports `overflow`, `snapshot_limit`, `backend_unavailable`,
`scope_unavailable`, or `protocol_error`, then closes. Backend outages never
cancel local runs. Event retention and repair boundaries are specified in the
[team observation contract](plans/team-observation.md).

## Agent Selectors

Runtime commands accept these selector forms:

| Form | Meaning |
| --- | --- |
| `name` | A local managed agent such as `alice` |
| `agent:name` | An explicit local managed agent, including a name that collides with a command |
| `shorthand` | A convention-based remote selector such as `brice/alice` or `toolang.ai/alice` |
| `ref` | A canonical remote ref such as `github://brice/agents/alice.too@main` or `https://toolang.ai/alice.too` |

Current shorthand expansion rules are:

| Shorthand | Expanded refs |
| --- | --- |
| `owner/name` | probes `github://owner/agents/agents/name.too@<default-branch>`, then `github://owner/agents/name.too@<default-branch>` |
| `owner/repo/name` | probes `github://owner/repo/agents/name.too@<default-branch>`, then `github://owner/repo/name.too@<default-branch>` |
| `host/name` | `https://host/name.too` |

Three-part shorthand specifies the repository exactly. It does not probe other
repository names.

GitHub refs must include one revision suffix:

- `github://owner/repo/path/to/agent.too@rev`

`rev` is one git revision token. Toolang does not distinguish branch, tag, and
commit in selector syntax.

Foreground runtime port selection depends on the agent mode:

| Mode | Selector | Default port behavior |
| --- | --- | --- |
| Resident | Local managed name such as `alice` | Reuse the agent's last port when available, otherwise choose from `7001-7999` |
| Visiting | Remote selector such as `brice/alice` or `https://toolang.ai/alice.too` | Reuse the visiting root's last port when available, otherwise choose an OS temporary port |
| Script run | Local `.too` path with an agic or flow name | No port for embedded host execution; attached and temporary guest execution use the selected AgentServer endpoint |


## Script Run Surface

A script run uses one local `.too` source path:

```text
toolang run <SCRIPT> [RUNNABLE] [OPTIONS] [NAME=VALUE...] [-- <INPUT> | -]
toolang <SCRIPT> [RUNNABLE] [OPTIONS] [NAME=VALUE...] [-- <INPUT> | -]
```

Script progress, inspection output, and chat TUI activity use the shared
execution presentation language defined in
[execution-presentation.md](./execution-presentation.md). Script mode retains
its stdout/stderr contract; it does not use the TUI renderer.

Arguments:

- `SCRIPT` is the local Toolang script or agent file
- `RUNNABLE` is the uniquely named authored agic or flow to run; omitting it
  selects the module's unnamed entry, or shows file help when none exists
- `NAME=VALUE` supplies a named runnable parameter; repeat for other parameters
- `INPUT` is one logical input. With an explicit selector, ordinary trailing
  words start input. Without a selector, use `--` to start primary text;
  `-` reads stdin through EOF, and omitted command-line text reads piped or
  redirected stdin

The synopsis omits `[NAME=VALUE...]` when there are no named parameters and
`[-- <INPUT> | -]` when the signature forbids primary input. The input group is
optional on the command line because piped or redirected stdin can supply it.
The **Arguments** panel shows per-name metavars such as `begin=<BEGIN>`, without a
separate type label, with parameter doc comments or `Named input (Text)` using
the authored type. INPUT is last, with an authored description or a typed fallback
such as `Primary input (Part[])`, followed by
`reads stdin with - or when input is omitted`. A `*` marks required
parameters; brackets in Usage do not make required signature inputs optional.

Runnable descriptions use `Run KIND NAME.` or `Run KIND NAME - DESCRIPTION`
when a doc comment exists, followed by Usage, **Arguments**, and **Options**.
Flows end with an epilog: `The flow proceeds as follows:`, a blank line, and an
outline in normal style with blank lines between sibling steps.
Top-level Script help identifies `_` as the default when an unnamed
entry exists and marks `[RUNNABLE]` optional; otherwise it shows `<RUNNABLE>`. It
lists **Runnables** before Options in name, kind, and description columns. An
unnamed entry shows `_` with its lined identity and any authored comment. Both
qualified selectors (`agic:_`, `flow:_`) and `_` select that entry; `<entry>` is
no longer a selector. Named runnables retain their names and descriptions.

Both levels show the same common options, ordered as `-q` / `--quiet`,
`-o` / `--out`, `--model`, `-w` / `--workspace`, `-d` / `--workdir`,
`--sandbox`, `--allow`, `--limit`, `--no-auto-workspace`, `--dev`, then
`-h` / `--help`.
Common options may appear before or after RUNNABLE, before input. Explicit
runnable-level scalar values override root values; repeated `--workspace`,
`--allow`, and `--limit` values accumulate in command-line order. `--workdir`
may appear only once across both levels. Quiet mode is enabled at
either level. `--help` describes the level where it appears.

Script mode parses policy prefixes but does not accept chat quick commands.

Behavior:

- a local `.too` path enters script-run mode
- `agic:NAME`, `flow:NAME`, and `runnable:NAME` explicitly select a runnable
  when its name collides with a top-level command
- default agics and generated internal agics are not exposed as script commands
- runnable command descriptions use their authored `doc` or an agic/flow fallback
- stdout is reserved for explicitly requested runnable output
- progress messages are written to stderr by default
- TTY progress uses color and live replacement; non-TTY progress is stable,
  append-only, and contains no ANSI control sequences
- `-q` or `--quiet` suppresses prepare and execution progress
- `--out FILE` or `-o FILE` writes the Run result to a file; `--out -` or `-o -`
  writes it to stdout. Without this option, the result remains stored without
  being copied to stdout. This replaces the removed `--save` option.
- `--sandbox SANDBOX` selects the execution sandbox for this invocation; an
  already-running compatible AgentServer is attached instead
- `--dev [PATH]` installs Toolang in a newly started guest from one wheel; a
  directory selects its newest Toolang wheel recursively. Bare `--dev` uses `.`
- `--model MODEL` supplies an invocation model identity and typed
  parameters, for example `--model 'openai/gpt-5 effort=high'`
- `--limit LIMIT=VALUE` overrides one run limit; it may be repeated
- `--allow RESOURCE=QUERY` sets one of `models`, `tools`, `psyches`, `skills`,
  `services`, or `prompts` and may be repeated
- host execution remains embedded when no AgentServer is active; a selected
  non-host sandbox starts a temporary AgentServer and cleans it up after the run
- an explicit sandbox that does not match an active AgentServer is rejected
  before creating a thread or run; `--dev` cannot modify an active AgentServer
- `PY_LOG=toolang.execution=info toolang a.too summarize ...` writes runtime logs
  under `.toolang/agents/<agent>/.runtime/logs/<runnable>/<run_id>.log`
- `PY_LOG=debug toolang a.too summarize ...` also writes lower-level provider
  and HTTP logs to that run log file
- `toolang a.too --help` lists public runnables
- `toolang a.too summarize --help` prints runnable-specific dynamic usage
- `toolang a.too` shows usage instead of running a default agic
- a runnable missing a required named argument or required primary input shows
  its dynamic help and does not create a run; omitted input is first read from
  stdin when available
- script run reads one effective Setup and State publication; the executor
  narrows their model, tool, and cap collections with request and runnable
  directives
- `name=value` supplies one named argument and is coerced using its declared
  parameter type; arguments and command options may be interspersed before input
- the first ordinary operand starts line input; `--` explicitly starts it when
  the text begins with an option or assignment
- after line input starts, all remaining words are content, including
  `name=value`, `--help`, `-`, and `---`; an unknown assignment in the command
  header is an error, even when shell-quoted
- line-input rules, with or without `--`:
  - adjacent ordinary shell words are joined with spaces into one text item
  - `TEXT` adds one text part; use `@@TEXT` for literal text beginning with `@`
  - `@PATH` adds one path-based percept part; text-like paths become text parts
  - image extensions such as `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, and `.svg` infer image parts
  - `.mp3` and `.wav` infer audio parts
  - supported document extensions infer document parts
  - unsupported video, archive, executable, and binary formats are rejected
  - explicit `--` requires nonempty input
- a standalone `-` must be the final command-line token; it reads stream input
  through stdin EOF and may be empty
- a standalone `---` in the command header is rejected with guidance to use
  `-`; remove the closing fence when migrating, since stdin is read through EOF
- `---` remains literal in line input, stdin, and argument or option values;
  prompt calls inside input retain fenced syntax
- omitting input reads non-interactive stdin as an implicit stream when
  available; an empty stream means input is absent, and TTY stdin is not read
- complete Call Input syntax is defined in [call-input.md](./call-input.md)
- `--option` selects Toolang runtime options before input begins
- `PY_LOG` uses env_logger-style directive formatting and does not affect stdout
- key execution events are recorded in `runs.db` for script runs just like chat,
  task, and chore runs

The same roaming source path can select agent commands:

```bash
toolang SCRIPT info
toolang SCRIPT run
toolang SCRIPT chat [--thread [THREAD]]
toolang SCRIPT inspect SUBJECT... [PROJECTOR] [--human | --json]
toolang SCRIPT retry RUN [--anchor STEP]
toolang SCRIPT rerun RUN
```

It also supports `steer`, `cancel`, `rewind`, and `fork`. These command names
immediately following the source are interpreted as agent commands. Prefix a
same-named runnable with `agic:`, `flow:`, or `runnable:` to invoke it. The
removed `threads` and `runs` command names are available to authored runnables
without a typed prefix.

Visiting selectors support the same agent-self and execution-history commands:

```bash
toolang brice/alice info
toolang brice/alice run
toolang brice/alice chat [--thread [THREAD]]
toolang brice/alice inspect SUBJECT... [PROJECTOR] [--human | --json]
toolang brice/alice retry RUN
```

Commands that execute or inspect current program state (`info`, `serve`, `chat`,
`retry`, and `rerun`) resolve and materialize the remote program. History-only
commands, including `inspect`, derive the stable visiting layout and read its
existing `runs.db` without fetching the source.

### Historical record inspection

`inspect` evaluates a subject chain and an optional terminal projector:

```text
toolang AGENT inspect SUBJECT... [PROJECTOR] [--human | --json]
```

The former top-level `threads` and `runs` commands have been removed. Replace
them with `inspect threads`, `inspect runs`, or `inspect THREAD runs`. The old
filters have no inspect equivalent; use `--json` and filter externally when
needed. Inspect collections are unbounded rather than limited to 50 rows, and a
missing `runs.db` is an error rather than an empty successful result.

The initial collection and relation subjects are:

```text
threads                 every Thread record
runs                    every physical Run record, including rewound Runs
controls                Thread controls and controls of existing Runs
THREAD runs             Runs in THREAD's logical history, including child Runs
RUN steps               every Step physically owned by RUN
STEP runs               every Run directly accepted by STEP
LOOP_STEP steps         every direct same-Run Step owned by LOOP_STEP
```

Collections are unbounded and preserve durable ordering. Their JSON form is a
bare array of canonical record objects; every object is identical to inspecting
its printed Pointer individually. A missing `runs.db` is an error, while an
existing store with no matching records returns an empty collection.
`controls` combines all Thread Controls with Controls belonging to existing Runs,
including rewound Runs. It excludes a Run Control when retry deleted its owning
Run and orders records by creation time descending, target ascending, then
Control index descending.

A Pointer still selects one durable Thread, Control, Run, or Step record, or a
field inside that record:

```text
term_ab12                         Thread record
run_ab12                          Run record
run_ab12.0                        Step record
term_ab12@0                       Thread Control record
run_ab12@1                        Run Control record
run_ab12.0/output/value/0   nested field
run_ab12@1/payload/input/_        nested Control field
```

`.` enters the Step hierarchy, `@` selects a Control index, and `/` enters a
field using RFC 6901 escaping (`~0` for `~` and `~1` for `/`). Run ids occupy
the `run_` namespace; thread ids cannot begin with `run_`.

Exact `threads`, `runs`, `controls`, and `steps` tokens are reserved by this
grammar and take precedence over Thread Pointer parsing. The accepted
transitions are Agent to `threads`, `runs`, or `controls`, Thread to `runs`, Run
to `steps`, container Step to `runs`, and loop Step to `steps`. The Step
relations are direct and return an empty canonical array when no child was
created. Collections, Controls, non-container Steps, and fields do not accept
relation subjects.

Every successful query selects one projector after its subject resolves:

| Projector | Selection | Human result | JSON result |
| --- | --- | --- | --- |
| `records` | collection subject | summarized record rows | canonical record array |
| `fields` | browsable Pointer value | direct child fields | exact selected value |
| `value` | scalar, empty, resolved, or specialized value | rendered value | exact selected value |
| `output` | Run with the explicit terminal name | complete result body | resolved result value |
| `tree` | Run with the explicit terminal name | hierarchical durable execution | flat depth-first node array |
| `call` | supported Step with the explicit terminal name | Step-owned historical call | normalized call or flat node array |

`records`, `fields`, and `value` are implicit view kinds, not accepted command
tokens. `tree` and `output` are explicit only on a whole Run. `call` is explicit
only on a whole model, tool, run, par, or loop Step. Run and Step subjects never expose
equivalent projector names, and the removed `model-call` spelling is not an
alias.

Read a Run's result with `output` (`too` is an alias for `toolang`):

```sh
too SCRIPT inspect run_ab12 output
too SCRIPT inspect run_ab12 output | rich -m
too SCRIPT inspect run_ab12 output | jq '.'
too SCRIPT inspect run_ab12 output --json
```

`output` resolves stored references and extracts the value from `Output`.
The default view writes Text unchanged and concatenates TextPart bodies in
order for textual Parts. It preserves whitespace and Markdown source, adding
only a final newline when nonempty text lacks one. Empty text and empty Parts
emit no body. Structured values and nontext Parts emit complete indented JSON.
The view does not render Markdown, add colors, wrap, or truncate content; use
external tools such as `rich` or `jq` to present it. The `jq` example requires
structured output or text that itself contains valid JSON.

Textual Parts omit reasoning and nontext content from the default text view.
The existing `--json` flag preserves every resolved Part and nested value;
Text becomes a JSON string with its original whitespace. A JSON-looking Text
value remains text and is never parsed to infer a type. `--human` and `--json`
remain mutually exclusive; no new formatting flags are added. Output behavior
is the same in a terminal and a pipe, including when `FORCE_COLOR` is set.

A Run without an output fails with its ID and status instead of returning a
blank success or waiting for completion. Present null and empty outputs succeed.
Use `inspect RUN/output` to inspect the output wrapper, or
`inspect RUN/output/value --json` for the raw stored value, including
unresolved references. Existing Pointer queries retain their original behavior.
The `output` projector is available only for whole Runs, not Steps or collections.

`call` on a model Step reconstructs the complete normalized call persisted for
that Step, including instructions, messages, tool definitions, the
structured-output schema, and continuation data:

```bash
toolang alice inspect run_ab12.0 call
toolang alice inspect run_ab12.0 call --json
```

This is distinct from `run_ab12.0/given/call`, which exposes compact persisted
references. Projection is local and read-only: it does not prepare a call,
select a model, construct a provider-native request, or send provider traffic.

The Human view follows call lifecycle order: summary, non-empty instructions,
messages, tools, output contract, continuation, and result. Empty sections are
omitted. Request text and result payloads are never truncated. All tool
signatures and descriptions are shown, while parameter schemas remain
summarized by the signatures. Text preserves authored line breaks. Messages use
descending review numbers, tool-call and tool-result parts retain their fenced
Human layout, structured values use indented key and index lines, and the
output contract uses formatted JSON. Section headers contain only their title:
message and tool counts and result Pointers are not appended. Result is last.
The JSON view keeps the complete normalized call and exact `output_schema`
value, including `null` for unstructured and historical calls.

The model-call summary usage line uses
`↑INPUT(CACHE%) ↓OUTPUT(REASONING)`. Output is inclusive and already contains
reasoning. An explicit zero reasoning meter renders `(0)`; a missing meter
omits the output parenthetical.

`call` on a tool Step shows a summary, its persisted plugin, normalized
invocation, and the stored result payload without truncation when present. Tool
results retain the same fenced, structured Human layout used inside model
messages. Both identifiers are shown only when they differ. Its JSON is the bare
canonical `ToolCall` with exactly `tool_call_id`, `call_id`, `name`, and `input`;
it does not add the result or an inspection envelope.

`tree` on a Run and `call` on a run, par, or loop Step render the same durable
structural model. A Run tree starts at that Run. A container-Step call starts at
the selected Step, omits its owning Run and siblings, and retains each accepted
child Run as a separate node:

```text
NODE                  ACTIVITY                       OCCUR
run_parent            ✔ <flow>  parent
└─ run_parent.0       ✔ [run]   <agic>  child        3 items · 2 lanes
   └─ run_child       ✔ <agic>  child                item 2 · lane 1
      └─ run_child.0  • [model] openai/gpt-5
```

Human trees contain exactly `NODE`, `ACTIVITY`, and `OCCUR`. Activity starts
with `•` for pending or running, `✔` for succeeded, and `✖` for failed or
canceled; color distinguishes the statuses that share a marker. A child shows
only one-based item and lane indexes, while the owning Step shows known totals.
JSON is a flat depth-first array with `pointer`, `record_kind`, `step_kind`,
`parent`, `depth`, operation, status, occurrence, timestamps, canonical error,
and metrics. This is a transactionally consistent structural snapshot, not
event replay or a live trace; exact interleaving is unavailable because
execution events are not persisted as a journal.

Tree metrics include `reasoning_tokens` and `reasoning_complete` alongside the
inclusive input/output totals. Every counted model call must have an integral
`output.reasoning` token meter for the reasoning aggregate to be complete. A
partial numeric value is a known lower bound, all-unknown reasoning is `null`,
and zero model calls produce `reasoning_tokens: 0` with
`reasoning_complete: true`. Consumers must not add reasoning tokens to output.

Human output is the default. Record and container tables use the CLI's
horizontal-rule Rich style. Run collections order identity, runnable, status,
Step count, ownership, and creation time; they never show occurrence. Step
collections use `STEP`, `ACTIVITY`, `CHILD RUNS`, `CHILD STEPS`, optional
`PARENT STEP`, `CREATED`, and `OCCUR`. Their activity uses the same marker
vocabulary as trees, and both child counts are direct visible relations.

Field tables always use `FIELD`, `TYPE`, and `VALUE`. They list direct children
as relative field suffixes and show a bounded preview of the raw canonical value
in the third column. Long or multiline strings include size facts. Field tables
do not follow a Pointer, mark a resolved type, or fail because a
stored Pointer is missing, cyclic, or has a mismatched target. A directly
selected value retains normal Pointer resolution and validation.

Human projections have no trailing context footer. Direct scalar and
specialized values print only their value. Strings have no JSON quotes, and
nullable Human type labels use `T?`. Multiline Part content stays aligned
inside the VALUE cell without a leading bullet. Pointer queries with `--json`
do not resolve Pointers and print only the selected canonical JSON value. The
explicit Run `output` view instead returns the resolved result as described
above. Display modes are mutually exclusive, and `--type` is not an option.
Inspection is read-only and historical and does not load a runnable.

## Runtime Commands

| Command | `name` | `shorthand` | `ref` |
| --- | --- | --- | --- |
| `toolang serve` | yes | yes | yes |
| `toolang clone` | yes | yes | yes |
| `toolang start` | yes | no | no |

Behavior:

| Command | Behavior |
| --- | --- |
| `toolang serve` | Runs a local agent, or fetches one remote agent program into a stable visiting root and runs it in the foreground |
| `toolang clone` | Clones one local agent, or fetches one remote agent program into a new local managed agent |
| `toolang start` | Starts one local managed agent only. Remote selectors must be cloned first |

`toolang serve` and `toolang start` resolve the same `LaunchSpec` and call the
same sandbox lifecycle. A hidden `toolang _serve` command is the only
AgentServer process entrypoint. The sandbox implementation launches that
entrypoint locally, in Docker, or in another environment; the execution core
is shared across sandboxes.

The CLI preserves the interpreter's process name and command-line arguments.
Terminal conversation titles continue to use OSC independently of process names.

Server discovery uses the sandbox reference under the Toolang root, independently
of process titles. Host references validate PID and process creation time;
container references use adapter-owned instance IDs. `info` shows the referenced
PID or instance even during startup and ignores status reports from another
workload. Missing or unverifiable control state produces a diagnostic instead of
adopting a process by name. Stop unregistered legacy servers before upgrading.

Both commands report the same ordered operational work on stderr: preparing the
sandbox, creating the runtime, and connecting to the Agent API. Docker adds
Toolang installation and compatibility checks. A TTY uses one transient line
without a spinner and shows elapsed time after one second. A non-TTY writes
each action and outcome as an append-only plain-text line. The stable
`Agent NAME running: ...` and `Agent NAME started: ...` result lines are written
only after readiness succeeds.

Both commands accept repeatable `--allow RESOURCE=QUERY`,
`--limit LIMIT=VALUE`, and `--default SETTING=VALUE` options. The CLI parses these
with `TOOLANG_ALLOW_*`, `TOOLANG_DEFAULT_*`, and `TOOLANG_LIMIT_*` into frozen
field overrides passed to `SetupWatcher`.

`--compact-model MODEL` selects the new runtime's compaction model,
using the same model expression as `--model`, without a `model=` prefix.

Setup policy uses the following TOML shape in root and agent-home `config.toml`
files:

```toml
[allow]
models = ["gateway/*"]
tools = ["shell/*"]
skills = ["skill/reviewer"]

[default]
model = "gateway/chat effort=high"
runnable = "agic:chat"

[limit]
agic_model_calls = 200
agic_tool_calls = "none"
tokens = 200000
cost = "2.50"
time = 900
```

Limit fields are non-negative. Quoted `"none"` disables a limit. Empty allow
arrays deny all resources in that field. Text CLI/environment values use
`none` for an empty allow set, a cleared runnable, or an unlimited limit,
according to the target field. Models use canonical `unset`; legacy `none` in
Setup default sources is normalized to it. `all` removes an allow restriction.
Empty text is always invalid.

Default model values use one shared model body across TOML,
`TOOLANG_DEFAULT_MODEL`, and startup `--default model=BODY`. A body contains an
optional exact identity followed by typed assignments such as `effort=high`,
`effort=4096`, `effort=auto`, `max_output=8192`, or `max_output=auto`. Setup publishes the resulting complete model
request only after validating it against the effective model. Chat startup uses
`--default model=BODY` for its multi-run session, while Script and rerun use
`--model BODY` for one-run selection above the Setup default.

The precedence order is built-in values, root config, agent-home config,
runtime environment, CLI, then any request-level binding or limit fields.
Config is re-read dynamically; environment and CLI mappings remain fixed for
the process lifetime. Each `--default` or `--limit` field may occur once;
repeated `--allow` values for the same domain accumulate within the CLI layer.

When `--sandbox` is omitted, resident run/start commands use the effective
root/home `[sandbox]` binding, falling back to `host` when no binding exists.
An explicit selector, including `--sandbox host`, overrides that binding.
Docker sandbox control is supported from Linux and macOS hosts. Windows users
must run Toolang through WSL2; native Windows host control is not supported.

Chat uses the same explicit/configured/host selection when no AgentServer is
active and starts a persistent server for either host or guest execution.
If an AgentServer is already running, Chat attaches to it and rejects an explicit
incompatible selector without executing or restarting the server.
`chat --dev [PATH]` installs a local Toolang wheel in a new non-host runtime;
bare `--dev` searches the working directory. It is rejected for host execution
or when Chat attaches to an existing
AgentServer. Chat leaves the server running on exit; use `too stop <agent>` to
stop it.

For execution commands, `--catalog` and `--compact-model` apply only when
starting a runtime. Stop the existing agent before changing those startup
settings; an attached command rejects them rather than ignoring them.

Commands that start a new guest accept `--dev [PATH]`. This includes `serve`,
`start`, `chat`, Script `run`, `retry`, and `rerun`. Omitting `--dev` keeps the
existing package selection. Bare `--dev` uses `.` (the process working directory,
not the script directory or agent home). An explicit `PATH` selects that path.
`PATH` is either one Toolang `.whl` file or a directory to search recursively
for Toolang wheels. Directory selection uses the most recent file modification
time and breaks equal-time ties by absolute path. The selected concrete wheel
is staged into Docker and supplies its `too _serve` command. Build a current
wheel and select it with:

```sh
uv build --wheel
too alice run --sandbox docker --dev
```

A following non-option token is consumed as PATH. Before a runnable selector or
input, use `--dev=.` or terminate options with `--dev --`. For example,
`too demo.too --dev=. summarize hello` and
`too demo.too summarize --dev -- hello` both select from the working directory.
Use `--dev=PATH` or a `./` prefix for paths beginning with `-`. Repeated `--dev`
options use the last value; a runnable-level occurrence overrides the script
root value. Explicit empty paths (`--dev=` or `--dev ""`) also resolve to `.`.

Help displays environment, default, and bare-option metadata as separate tags:
`[env: NAME=] [default: VALUE] [bare: VALUE]`. Only applicable tags are shown.
For `--dev`, `[bare: .]` describes the value used when PATH is omitted; it does
not change the default when the entire option is absent.

`--dev` does not treat a directory as a source project and does not rebuild
after launch. It applies only while starting a new guest: host execution uses
the current Toolang installation, and an attached AgentServer has already
selected its package. When the controlling CLI runs from development source, a
new guest without `--dev` warns that it will install Toolang from the package
index instead of the local source. The warning includes the wheel build command
and does not block launch or query the package index.

Sandbox selection and implementation configuration are separate:

```toml
[sandbox]
driver = "docker"
target = "python:3.13-slim"

[plugin.sandbox.docker]
root = "/root/.toolang"
```

Root and agent plugin tables are merged before the selected sandbox factory is
created. Status, stop, and interrupted-launch recovery re-read this current
configuration; `SandboxState` stores only the sandbox selector and runtime
reference. Each plugin owns its runtime-root configuration and reports whether
the workload runs on the host or in a guest environment; orchestration does not
interpret plugin-specific path settings.

The host fixes the workload's runtime environment before sandbox preparation.
Docker includes every name authored in the root or agent `.env`, then includes
host-process names that match its default environment allow pattern.
That pattern covers Toolang controls, proxy and certificate settings, Python
bootstrap settings, and common model-provider variables. Override it with a
full-match regular expression when another process variable is required:

```toml
[plugin.sandbox.docker]
environment_allow_pattern = '^(?:COMPANY_CATALOG_TOKEN|HTTPS?_PROXY)$'
```

The configured pattern replaces the default and is compiled as written. Root
dotenv values are overlaid by agent dotenv values without applying the pattern.
A host-process value overrides those layers only when its name matches the
pattern.

Docker writes the selected mapping to one mode-`0600` staged dotenv file. Its
comments separate merged root/agent dotenv values from filtered host-process
values, with the process section last to preserve precedence. The file is
bind-mounted read-only over the guest agent's `.env`; the root `.env` is not
mounted, and the original agent `.env` remains hidden behind the nested file
mount. A small bootstrap reads this same generated dotenv into the guest process
before package installation or plugin loading. Dotenv values are literal on
both host and guest, so `${NAME}` is not expanded during either load.
Toolang passes only `TOOLANG_HOST_GATEWAY`, `TOOLANG_ROOT`, and
`TOOLANG_SANDBOX` through Docker's environment arguments; Docker also maps
`host.docker.internal` through the engine's `host-gateway`. The complete staging
mount is read-only in the guest. Staged files are removed on release and after a
failed Docker launch whose workload was removed successfully. If Docker cleanup
fails, the staged files remain with the persisted recovery reference.

For every sandbox implementation, AgentServer is the environment's primary
foreground workload. `serve` waits for that workload and releases it on exit,
while `start` returns after the health endpoint is ready. `stop` reloads the
persisted `SandboxState`, stops the primary workload, and releases its sandbox
resources. Agent removal also asks the sandbox lifecycle to release any stopped
workload before deleting the agent home; no caller deletes sandbox control state
as ordinary filesystem data.

Agent entrypoints also share one logging policy resolver:

| Entrypoint | Log destination |
| --- | --- |
| `toolang serve` | `stderr` |
| `toolang start` | `agent_log` under the agent `.runtime` directory |
| embedded host `.too` script run | `run_log` under the agent `.runtime` directory when `PY_LOG` is set, otherwise `none` |
| attached or temporary `.too` script run | AgentServer output remains in its `agent_log`; Script progress and result output retain their stderr/stdout split |

The lifecycle persists a versioned recovery reference immediately after the
workload is created, then attaches process-local output observers and performs
the readiness check. The Docker sandbox follows container output locally from
container creation for foreground `serve`. Background `start` instead creates the
host `agent_log` with mode `0600` and writes Docker launch diagnostics,
bootstrap errors, and AgentServer output there. Early container diagnostics are
copied to that log before a failed or stopped workload is released, bounded to
the final 2000 Docker log lines and streamed without buffering them in memory.
Diagnostic write failures do not prevent container cleanup. Foreground
interruption, ready-reporting errors, and wait failures stop and release the
workload; cleanup failures preserve `SandboxState` for a later forced stop.
`SandboxState` is host-control data under `.sandbox/<agent>/state.json`, outside
all guest mounts; only per-launch staging children are exposed to Docker.
An older guest-writable `agents/<agent>/.runtime/sandbox.json` is never trusted
or migrated automatically. Its presence blocks launch, stop, and agent removal
with instructions to stop the workload using the previous Toolang version or
clean it up manually.
Likewise, per-launch staging without a matching control reference is preserved
and blocks relaunch or removal until any associated workload is removed and the
staging directory is cleaned manually.

Docker uses the engine's default missing-image pull behavior. Its guest script
quietly bootstraps uv and installs the selected package with `uv tool install`.
For a source-local roaming agent, Docker overlays linked `agent.too` and
`config.toml` targets as explicit read-only guest files so host symlinks do not
become broken paths in the container.
Successful installation suppresses ensurepip chatter, pip's container root-user
warning, package lists, and uv progress bars; installer failure stderr remains
available. Before execution, the guest checks that the installed CLI can start
the required AgentServer. Structured startup errors identify whether package
index or wheel installation failed and recommend a source-appropriate fix. A
compatibility failure from a development CLI recommends `uv build --wheel` and
`--dev dist`; a selected wheel instead recommends rebuilding or selecting a
compatible wheel. Foreground output reports that diagnostic once; background
output also retains the guest diagnostic in `agent_log`.

Guest stage observation uses a unique mode-`0600`, append-only token file under
the agent runtime directory. Tokens are a closed vocabulary for install,
validation, and server-start transitions; they contain no commands, logs, or
environment values. Guest writes are best-effort, and the host reads only a
bounded, regular, non-symlink file. The file is presentation-only: unknown,
duplicate, out-of-order, or stale values cannot affect readiness, recovery, or
cleanup. The referenced file is removed with the sandbox resources.

An active `toolang info` uses the sandbox reference's structured workload
identity. Host workloads show `PID`; Docker workloads show `Container` with the
generated name and a 12-character hexadecimal ID, while recovery retains the
full immutable container ID. Other sandbox kinds show `Runtime KIND:ID` without
assuming their identifiers can be shortened. Older version-1 references without
identity fields remain readable as generic workloads.

When `toolang start` runs without `--port`, Toolang first tries the agent's last
runtime port. If that port is not reusable, Toolang scans its auto-assigned
local range `7001-7999`, starting at `7001` and counting upward, skipping ports
already recorded by other local agents, instead of asking the OS for a random
ephemeral port.


## Resource Inspection Commands

| Command | No agent | Selected resident agent |
| --- | --- | --- |
| `toolang [AGENT] caps [--all]` | Root-shared, allowed capability resources | Root plus agent-owned resources under effective allow and scope precedence |
| `toolang [AGENT] tools [--all] [--query QUERY]` | Root-configured effective tools | Tools under merged root/agent configuration and allow policy |
| `toolang [AGENT] models [--all] [--catalog PATH] [--query QUERY] [--json]` | Root-configured ready, allowed models | Models under the selected agent's configuration |
| `toolang [AGENT] providers [--all] [--catalog PATH] [--json]` | Providers with ready, allowed models under root configuration | Providers under the selected agent's configuration |

Omitting an agent never reads an implicit `agents/default`. Tools, models, and
providers read published setup views without starting or parsing the resident
agent's program. Model catalog selection is `--catalog`, effective
`TOOLANG_MODEL_CATALOG`, selected agent's `catalog.json`, root `catalog.json`,
then packaged data. These files must use the flat `{providers: [...], models: [...]}`
cata format; convert upstream models.dev data externally. Static catalog files
replace each other; root and agent files are not unioned. Additional catalog
plugins retain their existing behavior. Provider order follows the selected file;
model order follows its model array unless `allow.models` branch order ranks matches.

Resource `--all` shows the complete diagnostic view for that same scope:
allow-excluded caps, internal and allow-excluded tools, or unready and allow-excluded models/providers
(including empty providers). It does not grant runtime access or combine scopes.
Default tools hide internal `_toolang` leaves. Queries and counts describe the
selected view. `me` is not internally hidden and follows normal tool allow policy.

Resource lists support `--json` inspection arrays and default human tables
(`--human`). The two output flags cannot combine. Model/provider canonical records
retain the flat models-repository shape plus `_toolang`; model ownership is the
`provider` string and connection declarations use `override`. Runtime credentials
and connection payloads are excluded from public route metadata.
Providers never contain model records or ID lists. Their inspection `models`
field formats setup's stored ready/total counts, unchanged by `--all`.

Human headers uppercase record keys. Models use short inspection fields including
`CONTEXT`, `MAX_OUTPUT`, and `PRICE`; providers show `ID`, `MODELS`, `ADAPTER`,
`API`, `ENV`. Tools show `REF`, `DESCRIPTION`, `TAGS`; caps additionally show
`LOCATION` before `TAGS`. Models/tools/caps have availability tags; providers do
not. Caps also include form, scope, and origin; models include origin.
See [Resource Queries](queries.md) for the records, columns, and tag groups.
Providers have no built-in query option; their JSON supports external TQ.

Every resource `--all` accepts `-a`. Human lists show displayed-row summaries;
empty results show only `0 <items>`. Tools, aggregate caps, and models add the distinct
toolset, kind, or provider count when more than one row is displayed. JSON has
no summary and uses `[]` for an empty result. Prices are per million tokens.
API readiness does not prove remote reachability or entitlement.
See [models](models.md), [tools](tools.md), and [caps](caps.md) for exact semantics.


## Plugin Inventory Commands

- `toolang catalogs`
- `toolang adapters`
- `toolang toolsets [--all]`
- `toolang sandboxes`
- `toolang channel list`

These commands list installed entry-point `NAME` and distribution `PACKAGE`
(such as `toolang`). They have no query, `--json`, or `--human` options. They reject agent names and do not read setup,
configuration, or catalog files or invoke factories. An installed plugin remains
visible even when it cannot load. `toolsets` hides internal entries such as
`_toolang` unless `--all` is given. Plugin lists have no agent allow policy.
`tools` belongs to resource inspection, not plugin inventory.


## Agent HTTP API

Each running agent exposes one local FastAPI server.

The process keeps one `AgentCore`, `CapsManager`, and `JobsManager` for the
application lifetime. `AgentCore` owns the process-local executor, history,
thread manager, setup watcher, and state watcher. These owners are stored on
`app.state` and exposed through small typed request dependencies. The API also
uses the execution-owned `Subscriptions` service over the executor's canonical
stream and records.
Application-wide FastAPI dependencies are reserved for side-effect-only
concerns such as authentication or common validation. FastAPI lifespan owns
required startup and shutdown; module globals and `ContextVar` do not carry
application state.

`RunExecutor.run()` returns a `LocalRunHandle`; the application retains handles
only when its own protocol needs additional lifecycle bookkeeping.

Core endpoints are grouped as:

- `agent`
- `caps`
- `jobs`
- `runs`
- `threads`

Non-interactive execution uses `POST /api/v1/runs/stream`. It accepts an agic
or flow's unique `runnable` name, primary input, optional model, and optional
declared arguments, and returns the canonical trace event stream for HTTP
clients. An omitted model uses the current `AgentSetup.defaults.model`. CLI
script runs and TUI execution do not consume this endpoint.


## Agent Endpoints

- `GET /healthz`
- `GET /api/v1/stream`
- `GET /api/v1/profile`
- `GET /api/v1/models`
- `GET /api/v1/tools`
- `GET /api/v1/workspaces`
- `GET /api/v1/agics`
- `GET /api/v1/flows`

`GET /api/v1/workspaces` reads the server's current Setup and State. It returns
`revision`, ordered `items` (`name`, source `path`, and runtime `available`), and
`workdir`. A listing remains available when the runtime default cannot be resolved;
in that case `workdir` is `null`. Inspection does not create workspace directories.
Optional `workdir=NAME://path` validates that location independently of the runtime
default; invalid or unavailable requested locations return HTTP 400. In a guest sandbox,
availability requires a matching captured mount and an existing guest directory.

`too AGENT workspace list` uses this endpoint while the agent is running. Without
a server it prepares local State and inspects host directories. Inspection of a
running roaming agent keeps its runtime workspace defaults; it does not add an
automatic source workspace. Execution CLI commands parse local directory grants
but leave named workspace URI validation
to the embedded executor or remote server; client configuration and filesystem
paths do not determine remote availability.

`/api/v1/profile` returns:

- profile metadata
- runtime identity:
  - the server process's Toolang source `version`
  - `sandbox.driver`
  - the complete `sandbox.selector`
  - the complete, unprojected `sandbox.instance` for Docker, otherwise `null`
  - the host-plugin-supplied `sandbox.description` for non-Docker runtimes,
    otherwise `null`

- environment summary
- overview metrics:

| Metric Group | Contents |
| --- | --- |
| `threads` | Thread totals grouped by chat, chore, and task |
| `steps` | Step totals grouped by `model_call`, `tool_call`, and `runtime` |
| `tokens` | Aggregated input, output, and total token usage |

`sandbox.description` is optional presentation metadata. Its absence or a `null`
value does not block Chat: host execution falls back to the local host sandbox
plugin description, while Docker continues to use `sandbox.instance`. Runtime
profile readers ignore unknown additive fields, so TUI and executor releases can
be upgraded independently. Breaking protocol changes require a separately
versioned contract rather than making an additive display field mandatory.

`GET /api/v1/models` returns the server's current effective
`AgentSetup.models` collection. Runnable `models` directives are applied when a
run starts, not by this inspection endpoint. The response includes:

- `default`
- `items`
  - `ref`
  - `name`
  - `provider`
  - `parameters.reasoning.effort`
  - `parameters.reasoning.applicable`
  - `price.input`
  - `price.output`

`ref` is the exact catalog route identity, including nested model ids, and does
not include query predicates. Reasoning efforts are distinct recognized catalog
values in catalog order; an unsupported model returns an empty list.
`default` is always one of the returned item refs when `items` is non-empty: it
is the configured default when that ref is included, otherwise the first item.
It is `null` only for an empty result.
`parameters.reasoning.applicable` is true when the model advertises either
effort-level or token-budget control; a toggle by itself is not applicable.
`price.input` and `price.output` are base token prices converted to USD per one
million tokens and are `null` when the corresponding catalog price is absent.

`GET /api/v1/models` accepts repeatable `query` parameters evaluated by the
runtime model collection. `GET /api/v1/tools` uses the same convention and
returns effective tool `ref`, structured `toolset`, `plugin`, and `description`
fields. Omitting
`query` lists the complete effective collection. Repeated query values and
comma-separated top-level matches form a union. Models use first matching
branch order, then source order; tools retain source order. Overlaps are
deduplicated. Empty matches return an empty `items` list; the model response
also has a `null` default. Matching uses the public CLI JSON projection, while
these HTTP response fields remain unchanged.

`GET /api/v1/agics` and `GET /api/v1/flows` list the agent's runnable
definitions.


## Cap Endpoints

Summary:

- `GET /api/v1/caps`

Collections:

- `GET /api/v1/psyches`
- `GET /api/v1/skills`
- `GET /api/v1/services`
- `GET /api/v1/prompts`

The cap summary and collection endpoints accept repeatable `query` parameters.
They match the public cap records documented in [Resource Queries](queries.md),
using `kind/name` identities and `tags`, then retain source order. The summary
returns the existing grouped response and counts. Response `ref` values remain
source URIs; query records instead use `kind/name` as `ref` and a content address
as `location`. Source URIs are not a field in the query projection.

Cap list items include `form` and the additive `summary` display field.
`summary` is at most 256 Unicode code points and selects the first nonblank
title metadata, description metadata, or body paragraph. It does not replace
`description` for collection query matching. Clients can render cap discovery
without issuing one detail request per item.

Detail:

- `GET /api/v1/psyches/{name}`
- `GET /api/v1/skills/{name}`
- `GET /api/v1/services/{name}`
- `GET /api/v1/prompts/{name}`

Templates:

- `GET /api/v1/psyches/templates`
- `GET /api/v1/skills/templates`
- `GET /api/v1/services/templates`
- `GET /api/v1/prompts/templates`
- `GET /api/v1/psyches/templates/{template_name}`
- `GET /api/v1/skills/templates/{template_name}`
- `GET /api/v1/services/templates/{template_name}`
- `GET /api/v1/prompts/templates/{template_name}`

Write:

- `PUT /api/v1/psyches/{name}/authored`
- `PUT /api/v1/skills/{name}/authored`
- `PUT /api/v1/services/{name}/authored`
- `PUT /api/v1/prompts/{name}/authored`
- `DELETE /api/v1/psyches/{name}/authored`
- `DELETE /api/v1/skills/{name}/authored`
- `DELETE /api/v1/services/{name}/authored`
- `DELETE /api/v1/prompts/{name}/authored`
- `PUT /api/v1/psyches/{name}/configured`
- `PUT /api/v1/skills/{name}/configured`
- `PUT /api/v1/services/{name}/configured`
- `PUT /api/v1/prompts/{name}/configured`
- `DELETE /api/v1/psyches/{name}/configured`
- `DELETE /api/v1/skills/{name}/configured`
- `DELETE /api/v1/services/{name}/configured`
- `DELETE /api/v1/prompts/{name}/configured`

Authored write bodies use:

- `scope`: `home` or `root`; defaults to `home`
- `content`: raw cap content

Configured write bodies use:

- `scope`: `home` or `root`; defaults to `home`
- `ref`: external cap ref

Capability mutations write through the authored/configured catalogs, then await
State publication. PUT responses read the exact published root/home layer,
including a cap shadowed by a higher scope or excluded by runtime allow policy.
DELETE responses wait for publication too. If the source change is saved but
State rejects the candidate, HTTP 409 reports that distinction; the last valid
State remains active. Publication does not rebind an existing run.

Delete routes accept `scope=home|root` as a query parameter. Cap read
items include:

- `name`
- `description`
- `scope`
- `origin`
- `form`
- `ref`
- `definition_file`
- `line` when known
- `editable`

Read and write payloads use the same `root`, `home`, and `here` scope
vocabulary. Read payloads retain `form`, `scope`, and `origin`; CLI query
records expose all three through `tags`. CLI `ref` is `kind/name`, and
`location` addresses the actual content as an absolute path or GitHub HTTPS URL.
Only inline caps append a declaration line as `file:line`; CLI records have no
separate `source`, `definition`, or `line` field.


## Chat Client Orchestration

The HTTP API has no endpoint that accepts terminal `ChatInput` text. A chat
client creates a thread when needed, converts the interaction to the shared
`RunRequest` boundary, and starts each turn through the authored run stream:

1. `POST /api/v1/threads` with the client and optional peer descriptor.
2. `POST /api/v1/runs/authored/stream` with the returned thread id, request id,
   authored input, run/session overrides, and ordered runnable fallbacks.

An existing chat thread can be passed directly to the authored endpoint; the
client does not create another thread for every turn. The server resolves the
request against its current setup and Agent State, including fallback
selection, policy precedence, prompts, named input, and server-relative file
includes. This keeps the Chat TUI and a future WebUI on the same run protocol
without adding a chat-specific server vocabulary.

The separate non-interactive `POST /api/v1/runs/stream` endpoint continues to
accept a selected runnable and canonical percept parts such as:

- `text`
- `image`
- `audio`
- `document`

Actual part support still depends on the selected model route. The built-in
OpenAI Chat Completions and Responses adapters map text, image, audio, and
document inputs. Chat Completions rejects a `DocumentPart` that has only a
document URL; the caller must first provide document data or a provider file
id.

For multipart payload details:

- an image part's `image_url` may be a remote URL or a local `data:` URL
- an audio part's `data` should be base64 payload; `data_url` is also accepted
  as an alias and is normalized to base64
- a document part's `url` is for remote documents
- a document part's `data` carries inline provider-facing document data and may
  be a full `data:...;base64,...` URL
- `file_id` references a document already uploaded to the selected provider

Both run-streaming endpoints return the execution SSE protocol described below. A WebUI
that needs another presentation shape adapts these events client-side; the API
does not maintain a second chat event vocabulary.

The CLI command for interactive chat is `toolang AGENT chat [--thread [THREAD]]
[--sandbox SANDBOX] [--default SETTING=VALUE]
[--allow RESOURCE=QUERY] [--limit LIMIT=VALUE]
[--compact-model MODEL]`.
The `--thread` option has a short alias, `-t`, and accepts an optional value:

- Omit the option to start a new session; its terminal thread is created on
  first input. Help and exit without input do not create a thread.
- Pass bare `--thread` to resume the selected agent's most recently updated
  thread, including recorded run activity. All thread origins and statuses
  participate in selection. If no thread exists, the command fails with guidance
  to omit the option to start a new session.
- Pass `--thread THREAD` to continue that thread, or `--thread RUN` to continue
  the run's thread. `--thread=THREAD` and `-tTHREAD` also accept explicit values.

An explicitly empty thread value is invalid. Repeating the option uses the last
occurrence. A following option, as in `--thread --sandbox host`, leaves thread
selection bare. Positional thread IDs are no longer accepted.

A resident, roaming, or visiting agent uses its recorded endpoint through
`RemoteRunClient`. Chat starts the agent when needed; concurrent starters share
one runtime, and an existing startup must become ready before Chat connects.
Readiness failures are reported. Retry/rerun and steer/cancel/fork/rewind also
ensure the agent is ready and mutate run/thread state through its API.
Explicit Chat policy options become remotely validated session
commands, while the server keeps ownership of setup, environment, providers,
working directory, and sandbox.

The banner always shows the TUI process version, executor, sandbox, and
host-side agent home in that order. The executor links the normalized endpoint
and follows it with the server version when it is not a confirmed clean match, for example
`executor  http://localhost:7001 · v0.3.9`. Docker displays its complete selector
and conventional twelve-character container ID, for example
`sandbox  docker:python:3.13-slim · a1b2c3d4e5f6`. Host execution displays the
ready OS description produced by the host sandbox plugin, for example
`sandbox  host · macOS 27.0 arm64`. The middle-dot separators use the dim style,
and OS build identifiers are omitted. `/api/v1/profile` returns the same
twelve-character Docker hostname used to identify the guest; the host retains
the complete container ID for Docker lifecycle operations.
Job thread ids are inspectable and controllable through thread and run commands,
but `chat` does not implicitly reopen tasks or create manual chore runs.


## Job Endpoints

- `GET /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/archived`
- `GET /api/v1/jobs/archived/{job_id}`
- `GET /api/v1/tasks`
- `POST /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}`
- `PATCH /api/v1/tasks/{task_id}`
- `POST /api/v1/tasks/{task_id}/draft`
- `POST /api/v1/tasks/{task_id}/ready`
- `POST /api/v1/tasks/{task_id}/archive`
- `POST /api/v1/tasks/{task_id}/reopen`
- `POST /api/v1/tasks/{task_id}/cancel`
- `GET /api/v1/tasks/archived`
- `GET /api/v1/tasks/archived/{task_id}`
- `PATCH /api/v1/tasks/archived/{task_id}`
- `DELETE /api/v1/tasks/archived/{task_id}`
- `GET /api/v1/chores`
- `POST /api/v1/chores`
- `GET /api/v1/chores/{chore_id}`
- `PATCH /api/v1/chores/{chore_id}`
- `POST /api/v1/chores/{chore_id}/draft`
- `POST /api/v1/chores/{chore_id}/ready`
- `POST /api/v1/chores/{chore_id}/archive`
- `POST /api/v1/chores/{chore_id}/run`
- `POST /api/v1/chores/{chore_id}/cancel`
- `GET /api/v1/chores/archived`
- `GET /api/v1/chores/archived/{chore_id}`
- `PATCH /api/v1/chores/archived/{chore_id}`
- `DELETE /api/v1/chores/archived/{chore_id}`

`GET /api/v1/jobs` returns tasks and chores in one response. Use `kind=task` or
`kind=chore` to filter the unified list. `GET /api/v1/tasks` and
`GET /api/v1/chores` return the same projections split by kind.
The unified `/jobs` collection is read-only; mutations use the concrete
`/tasks` or `/chores` collection selected by the job kind.

List endpoints return authored job fields at the top level and runtime-derived
status under `runtime`.

Task items include:

- `id`
- `kind`
- `stage`
- `status`
- `title`
- `path`
- `updated_at`
- `runtime`

Chore items include:

- `id`
- `kind`
- `stage`
- `status`
- `schedule`
- `title`
- `path`
- `updated_at`
- `runtime`

`stage` values are:

- `ready`
- `draft`
- `archived`

Task status values are:

- `pending`
- `running`
- `done`
- `failed`
- `canceled`

Chore status values are:

- `pending`
- `running`
- `done`

`runtime` contains:

- `thread_id`
- `last_run`
- `next_run_at`
- `error`

`last_run` is the latest run object or `null`. If `last_run.status` is
`running`, that run is the active run. `next_run_at` is the next scheduled
chore timestamp or `null`. `runtime.error` is the current scheduler-side error;
`last_run.error` is the execution failure for that run.

Default job list endpoints return ready jobs. Draft and archived jobs are
available only through explicit `/archived` routes.

Detail endpoints return the same item shape plus `body`.

Collection endpoints return JSON arrays directly, and detail or mutation
endpoints return the projected resource directly. Destructive cap and archived
job deletion endpoints return `204 No Content`.

Task create requests accept:

```json
{
  "title": "Review API changes",
  "body": "Review the API changes and summarize risks."
}
```

Task patch requests accept any subset of `title` and `body`. Stage actions
use the task `draft`, `ready`, and `archive` endpoints. `task reopen` sets a
completed, failed, or canceled task back to scheduler status `pending`.
Delete is destructive and is available only through archived routes.

Chore create requests accept:

```json
{
  "title": "Check stale PRs",
  "body": "Check stale pull requests and summarize blockers.",
  "schedule": "FREQ=HOURLY;INTERVAL=6"
}
```

Chore patch requests accept any subset of `title`, `body`, and `schedule`.
Stage actions use the chore `draft`, `ready`, and `archive` endpoints.
`chore run` starts one manual occurrence without changing the schedule.
Delete is destructive and is available only through archived routes.


## Run And Thread Endpoints

- `POST /api/v1/runs/stream`
- `POST /api/v1/runs/authored/stream`
- `GET /api/v1/runs/defaults`
- `POST /api/v1/runs/workdir/resolve`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/stream`
- `POST /api/v1/runs/{run_id}/retry/stream`
- `POST /api/v1/runs/{run_id}/rerun/stream`
- `POST /api/v1/runs/{run_id}/retry`
- `POST /api/v1/runs/{run_id}/rerun`
- `POST /api/v1/runs/{run_id}/steer`
- `POST /api/v1/runs/{run_id}/cancel`
- `POST /api/v1/threads`
- `GET /api/v1/threads`
- `GET /api/v1/threads/{thread_id}`
- `GET /api/v1/threads/{thread_id}/result`
- `POST /api/v1/threads/{thread_id}/rewind`
- `POST /api/v1/threads/{thread_id}/fork`
- `GET /api/v1/threads/{thread_id}/stream`

`/api/v1/runs/{run_id}` is the main trace-detail endpoint.

Run collections return `RunInfo` arrays directly. `RunInfo` combines run
identity, status, input text, output summary, failure, and timestamps; there is
no separate `RunSummary` response type.

`RunDetail.output` contains the canonical message parts resolved from the
run's durable output edge. It is `null` until the run has an output edge and
may be an empty array when the resolved runnable result is empty.

`steer` and `cancel` operate on active (`pending` or `running`) runs. Steer
accepts a user message whose parts may be empty. `retry` and `rerun` accept a
terminal root run. Retry reopens that run from an optional canonical step-path
`anchor`; omitting it selects the latest retryable step. Rerun starts a new root
run from the source invocation and replaces the source in the visible thread
projection. Both accept optional `request_id` and partial `limits`; only rerun
accepts either an optional exact `model` replacement or a sparse
`model_override` with `identity` and `effort`, while retry preserves the
persisted model request. The two rerun fields are mutually exclusive, and a
sparse override is applied to the source run's complete persisted request.
Both return `202 Accepted` and execute on the server's owner event loop.

Thread `rewind` and `fork` request bodies take an optional `run_id` anchor and
`request_id`. An omitted run id selects the last visible run. Task and chore
threads cannot be rewound or forked because their thread ids are derived from
job ids.

`steer` and `cancel` return the accepted `RunControlInfo`. An accepted manual
chore start returns its `RunInfo`. Thread create and fork return the created
thread; rewind returns the updated existing thread representation. None of
these thread operations starts a follow-up run.

`POST /api/v1/runs/stream` accepts:

- `thread_id`: required existing thread id
- `request_id`: required globally unique caller-supplied control identifier
- `runnable.ref`: required concrete agic or flow ref
- `runnable.input`: flat input-value object; `_` holds primary input and other
  keys hold arguments, for example `{"_": "Summarize", "count": 2}`
- `model`: a concrete `ModelRequest`, or `null` for a model-free runnable
- `policy`: materialized `allow` ceilings and complete `limits`

HTTP limit fields are `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`,
and `time`. The caller materializes omitted session values before submission;
an explicit JSON `null` disables that field for the run.

`POST /api/v1/runs/authored/stream` accepts the authored `RunRequest` wire
shape:

```json
{
  "thread_id": "term_example",
  "request_id": "term_request",
  "runnable": {
    "ref": "agic:chat",
    "input": {
      "_": "Summarize\n@notes.md",
      "audience": "maintainers"
    }
  },
  "model": {
    "ref": "openai/gpt-5",
    "parameters": {"reasoning": {"effort": "high"}}
  },
  "policy": {
    "allow": [],
    "limits": {
      "agic_model_calls": 200,
      "agic_tool_calls": null,
      "tokens": 4000,
      "cost": null,
      "time": null
    }
  }
}
```

Both endpoints use the same object shape. Authored values must be strings
evaluated as Content; direct values are decoded against the runnable signature
without Content evaluation. A direct parts array belongs under `_`, for example
`{"_": [{"type": "text", "text": "Summarize"}]}`. Omitted `input` means `{}`;
`input: null` and primary null are invalid. Missing `_` and explicit empty input
remain distinct. Duplicate input keys are rejected before accepting a run.

This is a breaking format change: sibling `args`, source `named` lists, and
primary parts arrays as the complete container are no longer accepted. A valid
declared argument may still be named `primary`, `named`, or `args`. Input-bearing
control responses likewise use flat maps with self-describing value encodings.
Run/Step outputs use `{"type": "Text", "value": "result", "binding": "_"}`.
The binding is a name or null. The type describes the complete value; there is
no Local wrapper or dimension flag.
See [run-step-records.md](./run-step-records.md) for output reference paths.

The server reads setup and state once and validates the concrete runnable,
model parameters, policy, input, prompts, named sources, and file includes
before accepting the run. Reasoning effort is validated against the selected
catalog route and replaces that target's complete reasoning choice. Named input
names must be unique. Unknown fields and invalid combinations return `422`; a
missing thread returns `404`.

`GET /api/v1/runs/defaults` returns one concrete `model` request, including its
typed `parameters`, plus `runnable`, materialized `policy`, and the effective
canonical `workdir` for clients to adopt as session-owned defaults. The optional
`thread_id` selects the workdir inherited from that thread. The resolver endpoint
`POST /api/v1/runs/workdir/resolve` accepts optional `thread_id`, `workdir`, and
`workdir_base` fields and returns one canonical `workdir`. It applies the same
workspace authorization and path rules as run submission, but does not accept a
run or mutate client session settings. Run submissions still perform full
validation of the complete request.

`GET /api/v1/threads/{thread_id}/result` returns the newest succeeded root
`RunDetail` with a nonempty resolved output. An unknown thread and a known
thread without a result return distinct `404` details.

An accepted response exposes `X-Toolang-Run-ID` for compatibility; CORS exposes
it to allowed origins. New-run POST streams begin with root `run_begin`, which
also supplies the ID. Retry starts with `run_retried` and initializes its retained
prefix. Observation is installed before admission. Disconnecting never cancels
execution; reconnect with GET once the run ID is known, never repeat POST.
An older `after` cursor cannot prepend recovery to a newly admitted root: its
subscription starts at admission. Retry and GET recover the existing scope from `after`.

Clients create a thread explicitly with `POST /api/v1/threads` before the first
run. The thread request accepts `web`, `term`, `tui`, `chat`, or `script` as its
client placement; `script` creates a `script_*` thread.

All execution POST streams and these GET streams accept optional `?after=CURSOR`:

| Endpoint | Scope and lifetime |
| --- | --- |
| `/api/v1/stream` | This agent's events; stays open. |
| `/api/v1/threads/{thread_id}/stream` | Physical events of this thread, including forks from it; stays open. |
| `/api/v1/runs/{run_id}/stream` | Complete root tree, through its last active descendant. A child ID returns `409` with the root ID. |

Cursors are opaque agent-wide positions. Invalid or future cursors return `422`
before POST admission. Filtering leaves legitimate gaps. Use `after` explicitly;
`Last-Event-ID` alone does not select a cursor. The server replays retained events
or reconstructs structure from records at a fixed boundary, then sends its live
suffix. Finalized steps need no `part_*` replay. Joining an unfinished step supplies
its missing ancestors and suppresses partial progress through `step_end`.
Without `after`, a root initializes its recorded tree; thread/agent scopes initialize
active trees. Incompatible epochs replace the whole selected scope, including
retained terminal trees. Historical inspection remains separate; there is no
exact historical `/events` collection.

SSE `event` is the event name and `data` its payload. Source events carry `cursor`
and an SSE ID. Structural context carries `context: true`, an optional original
`cursor`, and **no new SSE ID**. Treat structure as upserts; an already applied End
must survive a repeated Begin of the same incarnation. SSE parsers may retain a
previous ID on context frames; do not acknowledge these as new source positions.

| Control | Payload and action |
| --- | --- |
| `stream_prefill` | `{"cursor": B, "scope": {"kind": "run", "id": ID}, "roots": [ROOT, ...]}` starts replacement of listed trees. `kind` is `run`, `thread`, or `agent`; agent scope omits `id`. `roots: null` replaces the entire scope. Buffer the following structural context. |
| `stream_checkpoint` | `{"cursor": B}`, with SSE ID B. Apply the complete replacement, then commit B; a disconnected prefix commits nothing. Also advances over filtered/suppressed events. |
| `stream_error` | `{"code": "overflow"}` or `{"code": "snapshot_limit"}` ends this subscription. A failed write may close without an error frame. |

Clear unfinished Part rendering on each attachment. `StreamClientState` handles
replacement and checkpoint commits before dispatch to presentation. Keep-alive
comments/checkpoints occur every 15 seconds; each ASGI write, including headers,
has a five-second deadline. Idle event waits do not consume that deadline.

Canonical run progress event names are:

- `run_retried` (retry invalidation)
- `run_begin`
- `step_begin`
- `part_begin`
- `part_delta`
- `part_end`
- `step_end`
- `run_end`

Every payload retains its canonical `type` discriminator. A `part_begin`
payload uses `part_type` for the message-part kind so it does not collide with
the event discriminator.

Reasoning uses these same events: `part_begin.part_type` is `"reasoning"`,
`part_delta.delta` is `{"kind":"reasoning","text":"..."}`, and `part_end.data`
is a canonical `ReasoningPart` with `type="reasoning"`. The call-local `part`
ordinal also identifies its position in the completed Model Step's output.
Optional `signature`, `provider`, and `provider_metadata` fields are retained on
the Part; signatures can also occur on normal text and tool-call Parts. Signature
fragments are not separate events. Deltas are live only; completed Parts are
durable and available through existing output/inspection endpoints.

Execution-store schema **53** persists structural cursors. Writable schema-52
stores upgrade in place; legacy records without cursors initialize from structure. See the
[model adapter contract](plugins.md#model-adapter) for the required indexed
stream interface. Human output continues to omit reasoning and native fields.

Run control acceptance and status are durable `ControlRecord` truth. Retry also
publishes `run_retried` with its control, invalidated steps, and removed runs. A thread stream may additionally carry
`thread_created`, `thread_forked`, and `thread_rewound`, and aggregates live run
events belonging to that thread.


## Hook Endpoints

- `POST /hook/runs`
- `GET|POST|PUT|PATCH|DELETE /hook/{binding_name}`

Hook endpoints queue runs or channel deliveries. They do not execute work
synchronously.
