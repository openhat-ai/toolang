# Consistent CLI List Presentation

Status: Definition awaiting human approval. The user selected compact default
lists, ellipsis for long descriptions, complete identities, and `--long` for
details. No implementation is included.

## Goal and success criteria

Make every one-shot CLI collection easy to scan in a 120-cell terminal, with
consistent geometry, missing values, summaries, and access to complete details.
Default output prioritizes identity and operational facts; `--long` expands the
same selected records. Neither mode changes selection or execution behavior.

## Verified inventory

The current shared table console is fixed at 4096 cells. With representative
records and `COLUMNS=120`, models render at 170 cells and caps at 178 cells.
`echo_records` truncates most strings at 120 Python characters before layout;
refs wrap at 40 display cells and locations remain unbounded. Other lists use
separate table builders or empty-result wording.

| Surface | Current output | Change |
| --- | --- | --- |
| `too list` | Agent/status/sandbox/port/WebUI; no summary; `No agents found.` when empty | Shared layout and summary |
| `too convo list`, argument-free `too talk` | Width-aware conversation directory; no rules; one-cell gaps; own summary | Shared geometry; preserve both entry points |
| `too caps`; `psyche/skill/service/prompt list` | Cap/description/location/tags; shared resource summary | Compact projection and long details |
| `too tools` | Ref/description/tags | Shared layout and long descriptions |
| `too models` | Eight columns, including modalities/features/tags | Compact projection and long details |
| `too providers` | ID/model counts/adapter/API/environment requirements | Move connection details to long output |
| `too adapters/catalogs/toolsets/sandboxes` | Name/package and summary | Shared layout and `--long` |
| `too TARGET chore list` | Eight columns; no summary; `No chores found.` when empty | Compact projection and long details |
| `too TARGET workspace list` | Name/path/availability, including empty headers | Shared layout and summary |
| `too TARGET inspect ...` collections | Threads/runs/steps/controls; some tables deliberately exceed terminal width | Shared collection layout and long details |

Also audited: Chat resource/runnable lists already have bounded layouts and
session-specific columns; `too hub` is an instance tree; `too top` and
`too top --once` are execution dashboards. Retain those specialized views.
Single-record inspection, pointer/value tables, execution trees, help, Markdown
tables, progress, and live TUI behavior are outside this change.

## Command and data contract

- Add `--long` without a short alias to every collection command in the inventory,
  including argument-free `too talk`. It displays complete versions of the human
  fields specified below, not every field in the underlying JSON object.
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
  and public plugin contracts unchanged. Human abbreviations never enter them.

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
- Keep identity first, status/counts next where specified, and free text last
  where possible. Text is left-aligned; counts, limits, prices, ports, and
  timestamps are right-aligned. Booleans are `yes`/`no`; absent or empty display
  values are `-`. A zero is a value, not a missing field.
- Fit scalar facts and headers first, then flexible columns. Identity columns
  grow only as needed, up to 40 cells; descriptions, titles, previews, paths,
  and tags share the remaining width. Short values release their space. Shrink
  prose first, then displayed paths, then tags, then identity width. Do not
  truncate status, numeric facts, or timestamps to make a table fit.
- Compact text uses one line with `…` when necessary; paths/URLs use a middle
  ellipsis so both their beginning and final component remain recognizable.
  Normalize embedded whitespace in compact prose. Never ellipsize an agent,
  conversation, resource, plugin, workspace, chore, or execution identity.
  Oversized identities wrap losslessly, preferring `/` boundaries, with all
  columns top-aligned and no special right-aligned model continuation.
- If headers and protected facts cannot fit, switch the entire result to record
  blocks: complete identity first, then labelled default fields in their listed
  order. Wrap within the same width. Do not silently drop columns or print a
  wider table. `--long` adds its detail fields to those blocks.
- Long output keeps the primary columns, wraps their complete text, and places
  each omitted field on an indented, labelled continuation immediately below its
  record, in the order below. Wrap descriptions, tags, addresses, environment
  requirements, and diagnostics without ellipsis or the old 120-character
  preprocessing limit. Never emit credential values. It remains within the
  same width and does not change the order or number of selected records.
- Every nonempty collection ends with one blank line and its unindented English
  summary. Empty collections print only `0 <plural noun>\n`. Retain existing
  resource group counts and singular/plural rules; add `N agents`,
  `N conversations`, `N chores`, `N workspaces`, `N threads`, `N runs`,
  `N steps`, or `N controls` where applicable. Count logical records, not
  wrapped lines. No leading blank line or extra message accompanies an empty
  result. Non-TTY output contains no ANSI or cursor controls.

## Human projections

Column order is fixed per row below; it does not change with `--all` or width.
Headers retain their current wording except for the explicit price unit.
Long details refer only to facts the existing command already reads.

| Collection | Default columns, in order | Additional `--long` fields, in order |
| --- | --- | --- |
| Agents | `AGENT STATUS SANDBOX PORT WEBUI` | None; complete WebUI address |
| Conversations | `CONVERSATION NAME MEMBERS JOINED UPDATED MESSAGE` | Full member names and complete existing message preview |
| Caps, all four kinds | `CAP DESCRIPTION TAGS` | `LOCATION` |
| Tools | `REF DESCRIPTION TAGS` | None; complete description and tags |
| Models | `REF CONTEXT MAX_OUTPUT PRICE ($/1M) TAGS` | `INPUT`, `OUTPUT`, `FEATURES` |
| Providers | `ID MODELS ADAPTER` | `API`, `ENV` |
| Plugin inventories | `NAME PACKAGE` | None; complete package label |
| Chores | `NAME TITLE STATUS LAST RUN NEXT RUN` | `SCHEDULE`, `ERROR`, `LOCATION` |
| Workspaces | `NAME PATH AVAILABLE` | None; complete path |
| Inspect threads | `THREAD TITLE RUNS STATUS UPDATED` | None; complete title |
| Inspect controls | `CONTROL KIND STATUS CREATED` | None |
| Inspect runs | `RUN RUNNABLE STATUS STEPS CREATED` | `THREAD`, `PARENT STEP`, when present in the existing collection projection |
| Inspect steps | `STEP ACTIVITY CHILD RUNS CHILD STEPS CREATED` | `PARENT STEP`, `OCCUR`, when present in the existing collection projection |

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
Compact tags retain their existing order; `--long` exposes the complete list.

## Illustrative output

These synthetic examples illustrate the projection; column widths follow the
shared fitting rules rather than being fixed to these examples.

```text
REF                    CONTEXT  MAX_OUTPUT  PRICE ($/1M)  TAGS
───────────────────  ─────────  ──────────  ────────────  ────────────
example/model-large  1,048,576      32,768  1.25 / 10.00  ready,remote

1 model
```

```text
CAP           DESCRIPTION                                            TAGS
────────────  ─────────────────────────────────────────────────────  ─────────────────────────
skill/review  Review changes against the approved plan and report …  ready,local,root,authored

1 cap
```

For `too caps --long`, the same record retains the full description and adds
its complete address below it:

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
- `tests/unit/cli/test_output.py`, `test_record_tables.py`, `test_talk.py`,
  `test_convo_commands.py`, and `test_inspect_subject_navigation.py`;
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
   shared separators/gaps, summaries, ordering, and unchanged selected records.
2. At 120, 80, and 40 cells, all output lines fit, including long mode and block
   fallback. At 160 cells the default cap remains 120; absent terminal geometry
   uses 120. Cover fresh calls after width changes and valid `COLUMNS` hints.
3. Cover long ASCII/CJK/combining identities, multiline prose, wide prices,
   long paths/URLs, empty arrays, false booleans, zero limits/prices, and strings
   containing Rich markup or terminal controls. No identity loses characters;
   compact clipping uses display cells; full human fields survive long output.
4. Verify exact JSON equality and query results before/after presentation changes.
   Conflicting modes fail before resource loading; unsupported long targets fail
   at the boundaries above. Long mode triggers no additional discovery, setup,
   Hub, or history reads.
5. Conversation aliases remain identical. Preserve IDs, membership/count suffix,
   JOINED state, latest-first ordering, local dates, sanitization, and the Hub's
   existing preview boundary; full membership requires no extra calls.
6. Inspect collection identities and parent pointers remain usable with inspect;
   long collections contain all previously displayed fields. Single-record,
   value/pointer, execution-tree, Chat, Hub, and Top regressions remain green.
7. Before each implementation commit, run the repository's default lint, format,
   type, and offline test checks. This definition requires source/example/link
   verification and `git diff --check` only.

## Risks and open questions

Human output is intentionally incompatible with scripts parsing old column
positions; existing JSON remains the machine interface. Removing default
columns can hide context, so `--long` must be documented in each affected help.
Long addresses wrap and require joining continuation lines when copied;
existing JSON retains complete single-string addresses. A long description or
preview can make long output tall; do not silently truncate it or add paging.

No remaining design questions. Human approval of this definition is required
before implementation.
