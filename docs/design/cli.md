# CLI

The `too` and `toolang` entry points use the same command router. This guide
explains routing, inspection, and client orchestration for maintainers.
`too --help` shows common commands; `too more` shows the additional groups.
Script inputs/configuration belong to [Scripts](scripts.md), and HTTP to [API](api.md).

`parse`, `fmt`, and `highlight` run offline without agent or runtime acquisition.
Parsing constructs a validated [Program](program.md#parsing-and-validation) or
exposes raw CST; formatting requires valid syntax; highlighting tolerates
incomplete source. [Authoring conventions](../authoring.md#automatic-formatting) define
formatting conventions. Command help describes their options and output modes.

## Routing

| Input | Interpretation |
| --- | --- |
| `too new NAME`, `too clone SOURCE [NAME]`, `too remove NAME`, `too list` | Agent catalog commands; command first |
| `too serve TARGET`, `too TARGET serve` | Foreground server; target can be resident, visiting, or local `.too` |
| `too start TARGET`, `too stop TARGET` | Background lifecycle for resident/visiting targets; either target order |
| `too TARGET info` | Agent information; command-first form is also accepted |
| `too cd`, `too TARGET cd` | Interactive shell in the Toolang root or selected agent home; `exit` returns to the calling shell |
| `too TARGET chat`, `too TARGET chore list`, `too TARGET inspect runs` | Targeted work, authoring, and history |
| `too run FILE [RUNNABLE]`, `too FILE [RUNNABLE]` | Local script execution |
| `too init DIR` | Create executable `aid.too` and comment-only `toolang.toml` without overwriting |

A command name wins when an unassigned token could name either a command or a
dynamic target. `agent:NAME` forces a resident target. After a `.too` target,
`agic:NAME`, `flow:NAME`, and `runnable:NAME` force a colliding runnable name.
A `.too` suffix selects a local source path even if absent; an explicit resident
prefix can select a resident name ending in `.too`. Once a command is selected,
its operands are not rerouted.

A target alone shows its supported commands. An incomplete selected command
shows command help before target/runtime validation. Remote target help does
not fetch source. Agent names, explicit remote refs, and placement are defined
in [Architecture](architecture.md#agent-sources).

Top-level `--root`/`-r` and `TOOLANG_ROOT` select the resident root. Source-local
scripts use their own layout and do not accept the global root override.

## Script selection and output

Omitting RUNNABLE selects the unnamed entry; without one, it shows file help.
`_`, `agic:_`, and `flow:_` explicitly select an unnamed entry. Help and progress
label it `agic:` or `flow:`. File help can also show `<entry:LINE>` as the entry's
source identity; `_` remains the invocation selector.
Script help does not read stdin, prepare State, or create execution records.
Required input failures show the runnable's dynamic help before Run admission.

```sh
too aid.too --help
too aid.too update_changelog --help
```

These help examples use the repository's `aid.too`, which defines
`update_changelog`. A file created by `too init` uses the bundled script template
and provides a different runnable set. [Call Input](scripts.md#runnable-input-and-help)
owns assignments, line input, stdin, escaping, and input boundaries.

Progress goes to stderr. Results remain in durable history unless `-o`/`--out`
requests a file or `-` for stdout. Quiet mode suppresses preparation/execution
progress. Non-TTY progress is plain append-only text. [Execution presentation](execution-presentation.md)
owns rendering and [Scripts](scripts.md) owns temporary grants and path origins.

Each roaming script or Chat launch creates a fresh root/executor. Roaming does
not support `serve`, `start`, or `stop`. Host scripts expose the shared Agent API
on the workload's event loop; guest workloads
use existing sandbox serving. Resources close when the owning foreground caller
exits. [Hosting](hosting.md) owns startup compatibility and recovery.

### Instance selection

- Existing targets accept `--instance` / `-i` on history/control commands.
- `-i I` / `--instance I` takes the complete 17-character ID, including `v`/`r`.
  Top displays `A@I`; selectors reject `A@I`, markerless bodies and noncanonical
  spellings. Residents need no extra ID.
- Default roaming selection is the latest startup request, including failure/exit,
  derived from retained local metadata. Completion time and mtime never reorder it.
- `info`/`inspect` calculate agent roots without global references or preparation;
  `info` uses saved resource summaries/State. Source contents may be unavailable.
- A command pins its root/identity/generation once; controls attach only to that
  live owner. Offline fork/rewind edit history under verified stopped ownership.
- Roaming rerun and explicit Chat continuation snapshot into fresh instances;
  `-i` cannot revive a retained root. See [Execution](execution.md#isolated-replay).


## Resource and plugin inspection

`too TARGET info` (also `too info TARGET`) shows enabled `Drivers` immediately
after `Status` while the agent is running. It reads existing runtime metadata
and formats selections in `chat,talk,chore` order, with `none` for an empty set
and `-` for unavailable metadata. Nonrunning agents omit the row.

`too [TARGET] caps`, kind-specific cap lists, `tools`, `models`, and `providers`
inspect the selected scope. No target means root configuration, not an implicit
`default` agent. `--all`/`-a` changes visibility for that same scope and grants no
runtime authority. `--json` emits inspection arrays; human output is the default.
See [Queries](queries.md) for records and query semantics. The standalone `caps`
executable is removed; `too caps` remains supported.

`catalogs`, `adapters`, `toolsets`, and `sandboxes` list installed entry-point names
and distribution packages. They do not invoke factories or load agent settings.
`toolsets --all` reveals internal entries. These are plugin inventory, distinct
from the configured resources exposed by `tools` and `models`.

Shared list and inspection tables start at terminal column zero, with one header
separator and no outer border rules. Responsive human tables use at least 80
display cells and otherwise use the full terminal width; terminals narrower than
80 retain the 80-cell layout. Agent, workspace, chore, and plugin inventory
lists label the first column with the singular object kind, such as `AGENT`,
`WORKSPACE`, `CHORE`, or `ADAPTER`. Nonempty lists end with one blank line
followed by an unindented count of the displayed records; resource lists retain
their group counts. Empty lists print only `0 <items>`, such as `0 agents` or
`0 workspaces`.
Inspection record, value, and tree tables use the same border style; collection
counts apply only to the thread, run, step, and control lists. Conversation
directory tables use the same border and plain count style, label their identifier
column `CONVO`, and right-align `JOINED`.
`too list` displays the `stopped` process state as `not running`.
Agent ports and inspection run, step, and child counts are right-aligned; text
fields retain their existing alignment.

`too models` displays `MODEL`, `CONTEXT`, `MAX_OUTPUT`, `PRICE ($/1M)`, `INPUT`,
`OUTPUT`, `FEATURES`, and `STATUS`, in that order. `MODEL`, `CONTEXT`, `PRICE`,
and `STATUS` are mandatory. When the selected rows do not fit, whole optional
columns are omitted in order: `OUTPUT`, `MAX_OUTPUT`, `FEATURES`, `INPUT`,
preserving the display order of the remaining columns. Every displayed value
stays complete on one logical row; if mandatory columns exceed the width budget,
the table may exceed the terminal width. `STATUS` contains availability
tags with spaces instead of underscores, omitting provenance tags. Context,
maximum output, and prices are right-aligned; input/output price components align
across rows. JSON and queries retain their existing fields and values.

`too providers` uses single rows with `PROVIDER`, right-aligned `MODELS`, `API`,
and `ENV`. API addresses shorten first; the API column is omitted when fewer
than 24 cells remain for an address, unless the complete address is shorter.
ENV requirements shorten only after API is omitted. Provider identities and
model counts remain complete. ENV displays required variable names even when
unset, joining required names with `+` and alternatives with `OR`; no values
are displayed. JSON retains its complete records, including adapter metadata.

Cap and tool lists keep each resource on one line and follow this width policy,
with a 120-cell fallback when geometry is unavailable. Only descriptions
and locations shrink; identities and statuses remain complete. Long cells use `…`; source
locations preserve their beginning and end. Local cap locations are relative to
the selected agent root; URLs and canonical refs keep their existing form.
`STATUS` shows only `ready` or `not_allowed`; all tags remain in JSON and queries. The tool identity heading is `TOOL`,
while JSON and query records retain `ref`. These lists have no `--long`; complete
field values remain available through `--json`.

## Historical inspection

`too TARGET inspect SUBJECT... [PROJECTOR] [--human | --json]` reads durable
history without starting a server or fetching a visiting source. Collections
are unbounded; absent storage is an error. Empty existing collections print
`0 threads`, `0 runs`, `0 steps`, or `0 controls` in human output, and `[]` with
`--json`.

| Subject | Selects |
| --- | --- |
| `threads`, `runs`, `controls` | Physical records, including rewound Runs |
| `THREAD runs` | Logical history membership |
| `RUN steps` | Direct physical Steps |
| `STEP runs` | Direct accepted child Runs |
| `LOOP_STEP steps` | Direct same-Run body Steps |
| `POINTER` | One record or nested canonical field |

[Records](records.md) owns IDs, references, canonical JSON, and resolution.
`--json` on a raw Pointer does not follow stored references. Field tables show
one level and bounded previews. Run `tree` and supported Step `call` projections
read a consistent structural snapshot rather than replaying an event timeline.
Model `call` reconstructs its persisted normalized request, including messages,
tools, schema, and continuation, without calling a provider.

`inspect RUN output` resolves the Run's result. Text is emitted unchanged with
only a missing final newline added; textual Parts concatenate text while
omitting reasoning/media. Structured or nontext output emits complete JSON.
`--json` preserves the full resolved value. Absent output fails with Run ID and
status; present null and empty values succeed. Raw `RUN/output/value` instead
inspects the stored edge.

## Runtime acquisition and controls

Chat, rerun, and fork/rewind acquire a compatible shared server and use its API.
Inspection stays offline. Resident/visiting steer and cancel attach to a healthy
live owner without starting a replacement; invocation guards prevent delayed
requests from reaching a different Run incarnation.

A Run ID selects its Thread; a Thread ID selects its active Run for steering/
cancellation or its latest terminal root for rerun/fork/rewind. Rerun accepts a
new root from the source invocation. Fork includes its anchor; rewind removes
its anchor and the visible suffix. All Thread kinds use the same branch state checks.
[Execution](execution.md) owns admission and durable effects;
[Hosting](hosting.md#server-lifetime-and-client-leases) owns server retention,
and [Chat](chat.md) owns session snapshots and queue behavior.

The Hub runs independently of an agent; bare `too hub` shows its instance status.
[Messaging](messaging.md) owns Talk/Convo and shared rules. [Top](top.md) owns
current and recent observation.
## Terminal titles and pane marks

Foreground interactive commands publish OSC 0 titles, which terminals and tmux
`pane_title` can display according to user settings. Session/window names are
untouched; Chat and Talk always run in the invoking terminal.

| Command | Title |
| --- | --- |
| Chat | `alice> thread title`, initially `alice>` |
| Talk | `@alice`, `@alice,bob`, or `#dev` |
| Top | `top` or `alice top` |
| Serve | `serve alice` or `serve hub` |
| Script Run | `run file.too` or `run file.too runnable` (basename only) |
| Rerun | `rerun run_id` (source ID) |

Each type's prefix defaults to an empty string. Configure
`chat`, `talk`, `top`, `serve`, `run`, and `rerun` independently:

```toml
[terminal.title.prefix]
chat = "💬 "
talk = "[too] "
```

Precedence is `TOOLANG_TITLE_PREFIX_<TYPE>` > agent-home config > root
config > empty. Omitted keys inherit; explicit empty strings clear. Root-only
views skip agent-home config. Roaming/scripts use their existing outer-to-inner
project `toolang.toml` layers. Environment values use root dotenv < home dotenv
< process ordering; roaming/scripts use the process environment only.

For a one-off override, set the type's environment variable:

```sh
TOOLANG_TITLE_PREFIX_CHAT='[too] ' too alice chat
TOOLANG_TITLE_PREFIX_CHAT='' too alice chat
```

Prefixes concatenate directly: `"💬"` gives `💬alice>`; `"💬 "` gives `💬 alice>`.
Printable spaces are preserved. Unsafe controls are removed and the complete
title is clipped to 32 display columns without splitting graphemes. Titles clear
on handled exit. TTY input, output, and presentation streams are required;
snapshots, scripted Chat, quiet/file output, and background/internal services
publish neither titles nor marks.

In tmux, foreground commands also publish pane-local string options:

| Option | Value |
| --- | --- |
| `@toolang_kind` | `chat`, `talk`, `top`, `serve`, `run`, or `rerun` |
| `@toolang_root` | Agent root for agent commands; Toolang root for commands without an agent |
| `@toolang_agent` | Bare agent name; empty for Talk, team Top, and Hub Serve |
| `@toolang_thread` | Chat thread ID, once created |
| `@toolang_run` | Actual executing run ID, once allocated or accepted |
| `@toolang_convo` | Canonical Talk conversation ID |
| `@toolang_context` | Opaque connection identity shared by Talk and team Top |

Unused fields are empty. Consumers must require a recognized local `kind` and
read pane options with `tmux show-options -p -t "$TMUX_PANE"` **without `-A`**;
format expansion can inherit stale session/window values. Marks clear on handled
exit. Abrupt termination can leave stale metadata; marks do not prove liveness.

`TOOLANG_TMUX=0` (also `false`, `no`, or `off`, case-insensitive) disables marks
while preserving OSC titles. `TOOLANG_TMUX_DEBUG=1` enables tmux diagnostics.
Scripts using the old session/window marks must migrate to these pane options;
`@toolang_pad` and `@toolang_talk` are retired. Users who need automatic placement
can create their own tmux bindings using the pane metadata.
