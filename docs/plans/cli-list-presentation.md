# Consistent CLI List Presentation

Status: Definition awaiting human approval. The user requires essential facts
to remain visible by default, compact prose, complete identities, and zero left
table margin. `--long` expands text; it does not unlock essential fields.
No implementation is included.

## Goal and success criteria

Make every one-shot CLI collection easy to scan in a 120-cell terminal, with
consistent geometry, missing values, summaries, and access to complete details.
Default output includes the facts needed to choose, locate, and diagnose each
item. Use labelled continuation lines when those facts do not fit horizontally;
do not require a flag to see them. Neither mode changes selected records.

## Verified inventory

Each collection below was checked against its renderer and offline output.
With `COLUMNS=120`, representative fixtures produced widths of 134 for models,
162 for tools, 249 for caps, 250 for chores, and 203 for workspaces. These widths
depend on data; the shared console is fixed at 4096 and does not constrain them.
`echo_records` also truncates most strings at 120 Python characters before
layout. Ordinary shared and inspect tables start at column 1: Rich's
`HORIZONTALS` outer-edge placeholder remains even with `pad_edge=False`.
Disabling the outer edge was verified to remove the margin and top/bottom rules.
The conversation directory already fits 120 and starts at column 0.

Also audited: Chat resource/runnable lists already have bounded layouts and
session-specific columns; `too hub` is an instance tree; `too top` and
`too top --once` are execution dashboards. Retain those specialized views.
Apply the same zero-margin outer-edge style to standalone inspect record,
pointer/value, and execution-tree tables, preserving their fields, complete
pointers, and existing width policy. Help, Markdown tables, progress, and live
TUI behavior are outside this change.

## Command and data contract

- Add `--long` without a short alias to lists that elide prose: combined and
  kind-specific caps, tools, conversations (including argument-free `too talk`),
  chores, and inspect collections. It expands descriptions, titles, error text,
  and existing message previews; conversation membership also expands fully.
  Other lists already display their complete human fields and need no new flag.
  Default and long modes have the same field set; neither is a dump of all
  JSON metadata.
- Existing `--human` remains the explicit default human mode. Reject
  `--long --json` with a usage error before loading resources. Retain JSON
  support only where it already exists.
- `inspect --long` applies only to collection subjects (`threads`, `runs`,
  `steps`, `controls`, including scoped collections). Reject it for record,
  tree, call, and value projections after resolving the subject, before
  projecting its contents. Reject `talk --long` with a conversation target or
  message before contacting the Hub; it belongs to the directory only.
- Preserve commands, targeting, order, queries, `--all/-a`, visibility, counts,
  source precedence, and data-loading boundaries. `--all` selects diagnostic
  records; `--long` changes their presentation. Combining them is supported.
- Keep JSON records, query keys, API payloads, prices, numeric values, timestamps,
  and public plugin contracts unchanged. Human-only environment formatting uses
  setup-owned requirement rules, independently of route readiness.

## Shared visual contract

- Measure terminal display cells, including wide and combining Unicode. Resolve
  width at each CLI call: cap the detected terminal width at 120; use 120 when
  no terminal size is available. Honor a valid positive `COLUMNS` hint before
  applying that cap. Do not reuse the execution-progress environment setting or
  add another width flag/configuration in this feature.
- Print left-aligned, content-sized tables with uppercase headers, one dim rule
  below the headers, two spaces between columns, and no outer borders, leading
  indentation, top/bottom rules, or padding to fill unused width. Keep ordinary
  text in the terminal foreground and retain existing explicit semantic styles;
  do not add background fills or automatic markup interpretation.
  Apply the conversation directory's existing terminal-control sanitization to
  displayed text; keep underlying records untouched.
- The first header, first data column, and first separator character start at
  terminal column 0, in plain and colored output. Remove Rich's outer-edge
  placeholder rather than merely setting `pad_edge=False`. Labelled detail
  continuations have two spaces of structural indentation; the table itself
  has no gutter. Identity continuation text remains aligned with its column.
- Keep identity first, status/counts next where specified, and free text last
  where possible. Text is left-aligned; counts, limits, prices, ports, and
  timestamps are right-aligned. Booleans are `yes`/`no`; absent or empty display
  values are `-`. A zero is a value, not a missing field.
- Fit scalar facts and headers first, then flexible columns. Identity columns
  grow only as needed, up to 40 cells. Short values release their space. Shrink
  prose before identity width; do not truncate status, numeric facts, or dates.
  Wrap identities, tags, complete paths/URLs, environment requirements,
  modalities, features, schedules, and execution relationships without ellipsis.
  Prefer `/` boundaries for identities and addresses. All columns are
  top-aligned; remove the special right-aligned model continuation.
- Only prose (descriptions, titles, message previews, and error summaries) is
  normalized to one line and shortened with `…` in default mode. Every nonempty
  error still appears by default. Long mode wraps complete prose without the
  old 120-character preprocessing limit. Preserve occurrence labels and
  activity/status facts rather than treating them as expendable descriptions.
- The default continuations listed below are always present in both modes.
  Place them immediately below their record in the specified order. Pack whole
  labelled fields onto one continuation when they fit with a two-space gap;
  otherwise use another line and wrap values within the available width.
  Repeat neither the header nor the record identity on detail continuations.
- If headers and protected facts cannot fit, switch the entire result to record
  blocks: complete identity first, then labelled default fields in their listed
  order. Wrap within the same width. Do not silently drop columns or print a
  wider table. Both modes include all default continuation fields in the blocks.
- Every nonempty collection ends with one blank line and its unindented English
  summary. Empty collections print only `0 <plural noun>\n`. Retain existing
  resource group counts and singular/plural rules; add `N agents`,
  `N conversations`, `N chores`, `N workspaces`, `N threads`, `N runs`,
  `N steps`, or `N controls` where applicable. Count logical records, not
  wrapped lines. No leading blank line or extra message accompanies an empty
  result. Non-TTY output contains no ANSI or cursor controls.

## Per-command decisions

All fields below appear without `--long`. Primary columns and default
continuations have fixed order in both modes, independent of width and `--all`.
Headers retain current wording except for the explicit price unit.

| Command | Primary columns | Always-visible continuation fields | Reason |
| --- | --- | --- | --- |
| `too list` | `AGENT STATUS SANDBOX PORT WEBUI` | None | Identify and connect to an agent; the existing five fields fit ordinary 120-cell output. Wrap a long WebUI URL. |
| `too convo list`, bare `too talk` | `CONVERSATION NAME MEMBERS JOINED UPDATED MESSAGE` | None | Choose a conversation; keep membership, joining state, and update time before the flexible message preview. |
| `too caps` | `CAP DESCRIPTION TAGS` | `LOCATION` | Select across kinds and locate the definition; full kind/name identities remain meaningful to queries. |
| `too [TARGET] psyche list` | `CAP DESCRIPTION TAGS` | `LOCATION` | Identify the psyche and its scope/source; retain `psyche/` in the identity. |
| `too [TARGET] skill list` | `CAP DESCRIPTION TAGS` | `LOCATION` | Find the workflow and its actual `SKILL.md`, not just its parent directory. |
| `too [TARGET] service list` | `CAP DESCRIPTION TAGS` | `LOCATION` | Find the service definition; location means definition source, as in the other cap lists. |
| `too [TARGET] prompt list` | `CAP DESCRIPTION TAGS` | `LOCATION` | Identify and locate the prompt; keep the existing `-` for an absent description. |
| `too tools` | `REF DESCRIPTION TAGS` | None | Ref and description explain the operation; complete tags retain allow diagnostics. Only prose needs elision. |
| `too models` | `REF CONTEXT MAX_OUTPUT PRICE ($/1M) TAGS` | `INPUT`, `OUTPUT`, `FEATURES` | Modalities and capabilities determine model suitability; all eight existing fields remain visible. |
| `too providers` | `ID MODELS ADAPTER` | `API`, `ENV` | Endpoint and credential requirements explain connection setup; long strings should not squeeze identity/counts. |
| `too adapters` | `NAME PACKAGE` | None | Inspect installed adapter identities and their packages, without setup/factory loading. |
| `too catalogs` | `NAME PACKAGE` | None | Identify the installed model-catalog plugins and their packages. |
| `too toolsets` | `NAME PACKAGE` | None | Identify installed toolset plugins; preserve internal visibility and package provenance. |
| `too sandboxes` | `NAME PACKAGE` | None | Identify installed sandbox implementations and their packages. |
| `too TARGET chore list` | `NAME TITLE STATUS LAST RUN NEXT RUN` | `SCHEDULE`, `ERROR` when nonempty, `LOCATION` | Next run alone cannot explain scheduling or failure; rules and source remain visible alongside diagnostics. |
| `too TARGET workspace list` | `NAME PATH AVAILABLE` | None | Choose a workspace and check its actual location/existence; wrap complete paths. |
| `too TARGET inspect threads` | `THREAD TITLE RUNS STATUS UPDATED` | None | Choose durable history by identity, title, activity, and update time. |
| `too TARGET inspect ... runs` | `RUN RUNNABLE STATUS STEPS CREATED` | `THREAD`, `PARENT STEP`, where the existing scoped projection includes them | Preserve navigation and parentage, rather than hiding relationships to fit the width. |
| `too TARGET inspect ... steps` | `STEP ACTIVITY CHILD RUNS CHILD STEPS CREATED` | `PARENT STEP` where applicable, `OCCUR` | Child counts, parent pointers, and occurrence positions explain the execution structure. |
| `too TARGET inspect controls` | `CONTROL KIND STATUS CREATED` | None | Identify the instruction, its kind, application state, and time; existing facts fit normal widths. |

The four plugin lists need only edge/padding consistency and width-safe wrapping;
do not invent readiness or configuration columns for installed-plugin inventory.
Inspect collection rendering must be separated from the intentionally complete
single-record/value/tree renderers, which share some current helpers. Their
outer edge follows the zero-margin style without changing their width policy.

Conversation `JOINED` remains conditional on the existing human context. Keep
the current latest-message ordering, compact local-time formatter, name-width
cap of 24, and `,… (N)` member suffix in compact output; preserve that suffix
when names shrink. Long output lists every already-returned member and the full
already-returned preview; it does not load message history or recover text
omitted by the Hub. Other list timestamps retain their existing representation.

Model limits retain comma thousands separators. Prices retain two decimal
places and missing `-` components, with input, `/`, and output aligned across
the displayed selection. The header supplies USD-per-million-token units.
Use human-only padding computed from selected rows; preserve the JSON price
string. Provider `MODELS` remains the existing stored `ready/total` value.
Tags retain their existing order and remain complete in both modes.

Provider ENV currently displays nested lists using Python representations.
Render its setup-owned OR-of-AND rule as names joined by ` + ` within a group
and ` OR ` between alternatives, with parentheses around multi-name alternatives.
For example: `(REGION + API_KEY) OR (REGION + ALT_API_KEY)`. Preserve published
order and show only variable names, never values. An empty requirement rule is
`-`. An unmet rule is not empty: default route `env` becomes null when credentials
are missing, so obtain the original resolved requirement through the same
setup-owned projection used by route resolution, including built-in overrides.
Do not infer it from environment contents or duplicate resolution rules in CLI.
Keep route readiness and JSON's existing `env`/route payloads unchanged.

## Illustrative output

These synthetic examples illustrate the projection; column widths follow the
shared fitting rules rather than being fixed to these examples.

```text
REF                    CONTEXT  MAX_OUTPUT  PRICE ($/1M)  TAGS
───────────────────  ─────────  ──────────  ────────────  ────────────
example/model-large  1,048,576      32,768  1.25 / 10.00  ready,remote
  INPUT: text,image  OUTPUT: text  FEATURES: reasoning,tool_call,temperature,structured_output

1 model
```

```text
CAP           DESCRIPTION                                            TAGS
────────────  ─────────────────────────────────────────────────────  ─────────────────────────
skill/review  Review changes against the approved plan and report …  ready,local,root,authored
  LOCATION: /Users/example/.toolang/skills/review/SKILL.md

1 cap
```

Provider connection facts are also visible by default:

```text
ID       MODELS  ADAPTER
───────  ──────  ────────────────
example     2/2  chat_completions
  API: https://api.example.test/v1  ENV: EXAMPLE_API_KEY

1 provider
```

For `too caps --long`, the same record expands its description while keeping
the same address visible:

```text
CAP           DESCRIPTION                                            TAGS
────────────  ─────────────────────────────────────────────────────  ─────────────────────────
skill/review  Review changes against the approved plan and report    ready,local,root,authored
              actionable findings.
  LOCATION: /Users/example/.toolang/skills/review/SKILL.md

1 cap
```

## Implementation touchpoints

- `src/toolang/cli/common/output.py` and `records.py`: width resolution, shared
  table/block rendering, summaries, and lossless long values. Keep record
  serialization independent of the human projection; replace the fixed 4096
  console rather than adding a second global list console.
- Command owners: `cli/caps/commands.py`; `cli/toolang/commands/agent.py`,
  `plugin.py`, `model_catalog.py`, `chore.py`, `workspace.py`, `inspect.py`,
  `talk/__init__.py`, and `talk/directory.py`. Preserve existing lazy command
  factories and entry points; keep CLI formatting out of setup/catalog stores.
- `src/toolang/setup/routes.py`: share the existing resolved ENV requirement
  projection with human provider inspection without altering readiness or
  serialized records; retain one owner for overrides and AND/OR semantics.
- `tests/unit/cli/test_output.py`, `test_record_tables.py`, `test_talk.py`,
  `test_convo_commands.py`, `test_inspect_rendering.py`, and
  `test_inspect_subject_navigation.py`;
  `tests/integration/cli/test_local_core_commands.py`,
  `test_model_catalog_commands.py`, and `test_resource_queries.py`.
- Update [CLI](../design/cli.md), [queries](../design/queries.md),
  [caps](../design/caps.md), [messaging](../design/messaging.md), and
  [terminal UI](../design/terminal-ui.md) during implementation. Existing plans
  remain historical records; this definition supersedes their conflicting
  one-shot collection presentation decisions only.

## Acceptance tests

1. Exercise every inventory command and alias with empty, single, multiple,
   filtered, and diagnostic selections where supported. Verify exact columns,
   default continuation fields, shared separators/gaps, summaries, ordering,
   and unchanged selected records. API/ENV, modalities/features, source
   locations, schedules, errors, and execution relationships require no flag.
2. At 120, 80, and 40 cells, all output lines fit, including long mode and block
   fallback. At 160 cells the default cap remains 120; absent terminal geometry
   uses 120. Cover fresh calls after width changes and valid `COLUMNS` hints.
   Verify the first header, first record identity, and separator start at column
   0 for every table, in plain and ANSI output; only detail continuations indent.
3. Cover long ASCII/CJK/combining identities, multiline prose, wide prices,
   long paths/URLs, empty arrays, false booleans, zero limits/prices, and strings
   containing Rich markup or terminal controls. No identity loses characters;
   compact clipping uses display cells; full human fields survive long output.
   Critical facts remain complete in default output too. Verify ENV alternatives,
   multi-name groups, empty requirements, and missing credentials under `--all`;
   rules survive missing credentials, values never appear, JSON stays unchanged.
4. Verify exact JSON equality and query results before/after presentation changes.
   Conflicting modes fail before resource loading; unsupported long targets fail
   at the boundaries above. Long mode triggers no additional discovery, setup,
   Hub, or history reads.
5. Conversation aliases remain identical. Preserve IDs, membership/count suffix,
   JOINED state, latest-first ordering, local dates, sanitization, and the Hub's
   existing preview boundary; full membership requires no extra calls.
6. Inspect collection identities and parent pointers remain usable with inspect;
   default collections contain all previously displayed fields. Single-record,
   pointer/value, and execution-tree tables gain zero-margin edges while
   retaining complete pointers and their width contracts. Chat, Hub, and Top
   regressions remain green.
7. Before each implementation commit, run the repository's default lint, format,
   type, and offline test checks. This definition requires source/example/link
   verification and `git diff --check` only.

## Risks and open questions

Human output changes column positions and introduces labelled continuations;
existing JSON remains the machine interface. Complete addresses can wrap and
require joining continuation lines when copied. Records may take multiple lines
even by default; this preserves selection and diagnostic facts within 120 cells.
Long mode can be tall; do not silently truncate it or add paging. Document its
text-expansion purpose rather than suggesting that essential metadata needs it.

No remaining design questions. Human approval of this definition is required
before implementation.
