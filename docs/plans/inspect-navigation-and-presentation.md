# Inspect Navigation And Result Presentation

## Goal

Make `toolang TARGET inspect PATH` a clear, composable browser for durable
execution history. Every path printed as a result must be accepted unchanged by
the next `inspect` command, and each view must expose enough immediate context
to choose the next path without dumping the complete stored record.

Implementation starts only after this definition is approved.


## Success Criteria

- Thread, run, step, input, output, and control targets have one documented,
  unambiguous path grammar.
- Every value in a human-readable `PATH` column is a canonical path that can be
  copied into another `inspect` invocation for the same Toolang target.
- Thread, run, and step views show one navigation level at a time and make
  empty sections explicit.
- Focused input, output, and control views preserve typed durable data while
  keeping default terminal output bounded.
- Invalid syntax, absent records, unavailable values, empty values, and
  truncated values have distinct behavior.
- Existing command placement, offline history access, `--limit`, `--json`, and
  legacy colon-and-dot step targets remain compatible as defined below.


## Current Behavior

`src/toolang/cli/toolang/commands/thread.py` currently accepts three target
forms:

```text
THREAD_ID
RUN_ID
RUN_ID:STEP_INDEX[.STEP_INDEX...]
```

Any identifier beginning with `run_` is treated as a run; every other
identifier is treated as a thread. A run step target is resolved through a
synthetic tree rooted at the selected run. Its dot-separated indexes can cross
from a parent run into a child run.

That synthetic navigation path differs from the durable `StepPath` already
used by execution records and the shared presentation language. For example,
the current command may accept `run_parent:2.0` while the selected child step
is printed as `run_child/0`. The printed value therefore cannot be copied back
into `inspect`. A focused step heading can also combine the run and stored path
as `run_parent:run_child/0`.

Current human output has separate thread, run, and step renderers:

- thread inspection prints a compact list of visible top-level runs;
- run inspection prints an input summary, an output or failure summary, and a
  recursively assembled step tree;
- step inspection prints `input`, `given`, `output`, and `noted` as raw,
  unbounded JSON plus a compact child list;
- controls are present in JSON run details but have no human view or direct
  target;
- input and output are not direct targets.

`--json` serializes caller-facing `ThreadDetail`, `RunDetail`, and `StepData`
values. Thread inspection reads the newest `--limit` runs, with a default of
100; run and step inspection load the selected run and the complete visible
thread so the synthetic child tree can be constructed. A limit below one is
rejected for every target.

Inspection is local and read-only. Resident and roaming targets read their
local `runs.db`; visiting targets read an already materialized stable layout
without fetching the remote source or requiring a running API server. Normal
history projection excludes ejected steps and runs.


## Scope

This feature changes the execution-history `inspect` command and the
caller-facing read models it needs. It covers:

- canonical parsing and resolution of thread, run, step, value, and control
  paths;
- human presentation for every supported target;
- additive JSON metadata and JSON documents for new target kinds;
- bounded human rendering of stored text and structured values;
- direct inspection of both run controls and thread controls;
- tests and public CLI documentation for the new behavior.

This feature does not change:

- durable identifiers, database tables, record formats, or execution events;
- runtime execution, retry, rerun, steering, cancellation, rewind, or fork
  semantics;
- HTTP endpoint contracts or live script and chat presentation;
- effective-history projection, including the exclusion of ejected records;
- audit access to ejected history;
- inspection of arbitrary keys inside stored JSON objects or individual
  message parts.


## Canonical Path Grammar

Canonical paths use the durable identities already defined in
`docs/run-step-records.md` and `docs/execution-presentation.md`.

```text
THREAD_PATH       = THREAD_ID
RUN_PATH          = RUN_ID
STEP_PATH         = RUN_ID "/" INDEX ("/" INDEX)*
CONTROL_PATH      = (THREAD_ID | RUN_ID) "@" INDEX
INPUT_PATH        = (RUN_PATH | STEP_PATH | RUN_CONTROL_PATH) "/input"
OUTPUT_PATH       = (RUN_PATH | STEP_PATH) "/output"
RUN_CONTROL_PATH  = RUN_ID "@" INDEX
```

`INDEX` is `0` or an ASCII decimal integer beginning with `1`. Leading zeroes,
signs, whitespace, empty segments, trailing separators, and non-ASCII digits
are invalid. Generated thread and run IDs contain none of `/`, `@`, or `:`;
the parser treats those characters as reserved delimiters.

The supported target kinds are:

| Kind | Canonical examples | Meaning |
| --- | --- | --- |
| thread | `term_ab12` | one effective thread and its selected run window |
| run | `run_ab12` | one durable run |
| step | `run_ab12/0`, `run_ab12/2/1` | one durable step in its owning run |
| control | `term_ab12@0`, `run_ab12@1` | one thread or run control |
| input | `run_ab12/input`, `run_ab12/0/input`, `run_ab12@1/input` | one recorded run, step, or run-control input |
| output | `run_ab12/output`, `run_ab12/0/output` | one resolved run output or recorded step output |

Threads and thread controls have no input or output target. Controls have no
output target. A run control has an input target only when its schema can carry
a message; inspecting that target is valid even when the particular control
recorded no message. These restrictions are based on the owner kind rather
than on whether a particular value happens to be present.

The parser must select a grammar shape before reading the store, then use the
owner ID to distinguish a run control from a thread control. `run_` remains the
public run-ID discriminator. The complete parsed path is validated before any
history query.


## Compatibility Alias

The existing form below remains accepted for compatibility:

```text
RUN_ID:INDEX[.INDEX...]
```

It retains its current synthetic traversal semantics, including traversal
from a parent step into child-run steps. Resolution immediately canonicalizes
the selected result to its real `STEP_PATH`. No human `PATH` cell or JSON
`path` field emits the legacy form, and new documentation uses only canonical
paths. The compatibility `target` field may retain the normalized legacy
selector as defined in the JSON contract. The alias does not accept `/input`,
`/output`, or `@` suffixes.

The alias remains silent rather than printing a deprecation warning, because a
warning would contaminate redirected output. Its eventual removal requires a
separate compatibility decision.


## Navigation Model

Inspection is shallow and path-oriented:

```text
thread
  -> thread controls
  -> visible top-level runs

run
  -> input and output
  -> run controls
  -> direct top-level steps

step
  -> input and output
  -> direct child steps in the same run
  -> child runs whose parent is this StepPath

run control
  -> input, when supported
```

Collection views do not recursively expand descendants. A child run is shown
with its own `RUN_PATH`; its steps are reached by inspecting that run. A nested
step in the same run is shown with its complete durable `STEP_PATH`. This
removes synthetic cross-run paths and prevents the same record from acquiring
different paths depending on the starting run.

Every navigable row has a `PATH` column. The cell contains only the canonical
path, without a status glyph, punctuation, quotes, or terminal styling. Status
and kind occupy separate columns, so selecting the path does not require text
editing.

Rows use deterministic durable order:

- controls are ordered by ascending control index;
- steps are ordered by their numeric local path;
- child runs are ordered by `created_at`, then run ID;
- a thread shows the newest `--limit` effective top-level runs but presents
  that selected window chronologically.

Thread controls are not subject to the run limit. Run, step, value, and control
targets accept `--limit` for CLI compatibility but do not use it. The existing
validation that `--limit` must be at least one remains global.


## Shared Human Presentation

Human output uses the execution presentation vocabulary:

- durable `finished` is displayed as `succeeded`;
- `running`, `failed`, and `canceled` keep their names;
- durations use the existing compact duration format;
- timestamps are the recorded UTC timestamps;
- errors are displayed in the normal detail position and are never reduced to
  a status alone;
- color may reinforce status on a TTY, but wording and layout must remain
  complete without color.

Each view begins with a single identity heading, followed by compact facts and
then named navigation or value sections. Missing sections are not silently
omitted: the view prints `No runs.`, `No controls.`, `No steps.`, or
`No children.` as appropriate.

Navigation-table summaries collapse whitespace and contain at most 80 Unicode
code points, including a trailing `...` when truncated. They never include
raw encoded media. Result tables may adapt spacing to terminal width, but they
must not remove or mutate the `PATH` cell.


## Thread Presentation

A thread view shows:

- path, status, title, origin, channel, peer, created time, updated time, and
  total effective run count;
- a `Controls` table with `PATH`, `STATUS`, `KIND`, and `SUMMARY`;
- a `Runs` table with `PATH`, `STATUS`, `TARGET`, `DURATION`, and `SUMMARY`.

Only effective top-level runs appear in the run table. When the total exceeds
the selected window, the heading states `Showing newest N of TOTAL runs.` An
existing thread with no runs is a successful empty result and prints
`No runs.`

Example shape:

```text
Thread term_ab12  idle
Review the repository  chat/terminal  3 runs

Controls
PATH           STATUS     KIND    SUMMARY
term_ab12@0    succeeded  create  created thread

Runs
PATH           STATUS     TARGET       DURATION  SUMMARY
run_cd34       succeeded  agic review  1.8s      Review the repository
run_ef56       failed     flow check   240ms     model credits exhausted
```


## Run Presentation

A run view shows:

- path, display status, runnable kind and name, thread path, root run path,
  parent StepPath when present, lifecycle timestamps, duration, and failure;
- a `Values` table containing `RUN_PATH/input` and `RUN_PATH/output` even when
  either value is unavailable or empty;
- a `Controls` table for every run control;
- a `Steps` table containing only steps whose durable parent is null.

The value table uses `PATH`, `STATE`, `TYPE`, and `SUMMARY`. State is
`recorded`, `empty`, `not recorded`, or `unavailable`. The step table uses
`PATH`, `STATUS`, `KIND`, `DURATION`, and `SUMMARY`. Run control rows use the
control layout defined below. A run with no steps prints `No steps.`

The overview shows only summaries of input and output. Inspecting their paths
shows the values themselves.


## Step Presentation

A step view shows:

- the complete durable StepPath, display status, kind, owning run path,
  lifecycle timestamps, duration, ejection reference when present, and error;
- a `Values` table containing `STEP_PATH/input` and `STEP_PATH/output`;
- a `Children` table containing direct same-run child steps and direct child
  runs.

Child rows use `PATH`, `STATUS`, `KIND`, `DURATION`, and `SUMMARY`. A child run
uses kind `run:<runnable-kind>`; a child step uses its stored step kind. The
view prints `No children.` when neither kind exists.

The durable `given` and `noted` facts appear as compact metadata in the step
view. Scalar facts may be shown inline. Nested or large facts use the bounded
structured-value renderer. They do not become arbitrary inspect subpaths.


## Input Presentation

An input view identifies its owner and then renders the typed value:

- run input is the resolved entry-control `Message` and names that control as
  its source path;
- step input preserves ordered `RunInputRefData`, `StepOutputRefData`, and
  inline `Message` items;
- run-control input is its optional `Message`.

References are rendered as canonical paths. A run-input reference points to
`RUN_ID@INDEX/input`; a step-output reference points to
`STEP_PATH/output`. An optional part index is displayed as metadata, not
appended to the inspect path, because individual message parts are outside
this feature's navigation scope.

Inline messages are rendered by role and typed parts. Text is readable text;
tool calls and tool results show their names, identifiers, status, and bounded
structured payloads; image, audio, document, and other non-text parts show a
type, locator or media type, and known size rather than encoded content.


## Output Presentation

An output view identifies its run or step owner and renders ordered typed
message parts with the same part renderer used for input.

Run output is the canonical value resolved from the durable `ValueRef`. When
available, the view prints the source control-input or step-output path before
the resolved value. When the reference selects one part, its part index is
displayed separately as source metadata because parts are not inspect targets.
Step output is the directly recorded `MessagePart` list.

A run with no output reference is `not recorded`. A present reference that
resolves to zero parts, and a step whose recorded output list is empty, are
`empty`. These states are successful inspection results rather than lookup
errors.


## Control Presentation

A run control view shows:

- path, display status, kind, timing, created and finished times;
- source run, retry anchor StepPath, request ID, error, and context when
  present;
- an `Input` navigation row at `CONTROL_PATH/input`.

A thread control view shows:

- path, display status, kind, created and finished times;
- source thread, anchor run, expected-head control path, request ID, and
  context when present.

The run-control input row is shown even if its value is not recorded. Thread
controls do not show an input row. Control context uses the same bounded
structured-value renderer as step facts.


## Missing, Empty, And Invalid Data

Syntax and lookup failures exit nonzero through `click.ClickException` and do
not print a partial document.

| Condition | Behavior |
| --- | --- |
| malformed delimiter, segment, or index | `invalid inspect path: VALUE` |
| syntactically valid absent thread | `thread not found: THREAD_ID` |
| syntactically valid absent run | `run not found: RUN_ID` |
| existing run with absent step | `step not found: STEP_PATH` |
| existing owner with absent control | `control not found: CONTROL_PATH` |
| suffix unsupported by its owner kind | `OWNER_PATH does not have an input` or `OWNER_PATH does not have an output` |
| valid value target with no recorded value | success, state `not recorded` |
| recorded empty list or message | success, state `empty` |
| unresolved stored reference | success, state `unavailable`, and print the unresolved source path |
| empty navigation collection | success with an explicit `No ...` line |

An unresolved reference is not reported as an empty value, because it signals
that durable data is missing or outside the effective projection. JSON retains
the reference and uses `value: null` plus `state: "unavailable"`.

The parser reports grammar errors before owner lookup. Once syntax is valid,
the most specific missing durable identity is reported. For example,
`run_missing/0/output` reports the missing run before attempting the step or
output lookup.


## Large Values

Default human output is bounded. For each focused input, output, step-facts,
or control-context value, rendering stops at the first of:

- 16,384 UTF-8 bytes of rendered text; or
- 200 logical lines.

The renderer cuts only at a Unicode code-point boundary and appends a final
line stating the number of omitted UTF-8 bytes and suggesting `--full` or
`--json`. The limit applies to the complete focused value, not independently
to each nested field, so a record containing many small fields is also
bounded.

`--full` removes the text and structured-value limit for human output. It does
not inline binary or encoded media; those parts remain descriptors. `--full`
and `--json` are mutually exclusive because JSON is already lossless and
untruncated. `--json` may therefore be large and is the supported format for
redirecting complete machine-readable values.

Navigation tables always retain their 80-code-point summary cap, including
under `--full`, because their purpose is selection rather than value display.


## JSON Contract

JSON continues to use durable status names such as `finished`; the
`succeeded` mapping is human-presentation-only. JSON is not subject to human
text truncation.

For the existing thread, run, and step target kinds, implementation must retain
the current top-level keys and nested caller-facing fields. Changes are
field-additive:

- every document adds top-level `path` containing the canonical reusable
  target path;
- listed thread runs and run controls add `inspect_path`;
- step `path` remains its canonical durable `StepPath` and is directly
  reusable;
- thread inspection adds a top-level `controls` list whose items contain
  `inspect_path` plus caller-facing thread-control data.

`target` remains for compatibility and contains the normalized user target.
When the compatibility alias selects a step, `target` retains the legacy form
and `path` contains the canonical StepPath.

New target kinds use these envelopes:

```json
{
  "kind": "input",
  "target": "run_ab12/0/input",
  "path": "run_ab12/0/input",
  "owner": {"kind": "step", "path": "run_ab12/0"},
  "state": "recorded",
  "source": null,
  "source_part": null,
  "value": []
}
```

```json
{
  "kind": "output",
  "target": "run_ab12/output",
  "path": "run_ab12/output",
  "owner": {"kind": "run", "path": "run_ab12"},
  "state": "recorded",
  "source": "run_ab12/0/output",
  "source_part": null,
  "value": []
}
```

```json
{
  "kind": "control",
  "target": "run_ab12@1",
  "path": "run_ab12@1",
  "owner": {"kind": "run", "path": "run_ab12"},
  "control": {}
}
```

`state` is one of `recorded`, `empty`, `not_recorded`, or `unavailable`.
`source` is a canonical path or null, and `source_part` is a zero-based part
index or null. `value` preserves the existing caller-facing serialization of
messages, references, and message parts.


## Compatibility Constraints

- Keep `toolang TARGET inspect PATH`, its command routing, default human mode,
  `--json`, and `--limit` public.
- Add `--full` without changing the meaning of existing options.
- Continue accepting legacy `RUN_ID:DOT.PATH` targets with current traversal
  semantics.
- Preserve current existing-target JSON keys and nested fields; additions must
  not rename or remove them.
- Human output is intentionally allowed to change. It has no stable
  line-for-line compatibility guarantee, but all durable identities and status
  meanings must remain accurate.
- Continue reading local durable history without starting an agent, contacting
  an API server, materializing a visiting source, or mutating `runs.db`.
- Continue using effective caller-facing history. Do not expose ejected records
  as a side effect of navigation.
- Do not introduce a database migration or change HTTP response schemas for
  this CLI feature.


## Design Touchpoints And Likely Files

Implementation should keep parsing and presentation in the CLI while keeping
durable record conversion in execution-owned caller schemas.

- `src/toolang/cli/toolang/commands/thread.py`
  - parse the canonical grammar and compatibility alias;
  - resolve paths through `RunHistory`;
  - build additive inspect documents;
  - render shallow human views and bounded values;
  - add `--full`.
- `src/toolang/execution/history.py`
  - expose read-only thread-control and run-control lookups through
    caller-facing schemas;
  - expose an inspection-specific resolved value result containing the source
    reference and recorded/empty/unavailable state, without leaking store
    parsing into the CLI or changing existing run-detail responses.
- `src/toolang/execution/schemas.py`
  - add standalone caller-facing thread-control and inspected-value data types;
    do not add fields to existing HTTP response dataclasses.
- `tests/unit/cli/`
  - cover grammar, canonicalization, formatting helpers, and truncation
    boundaries.
- `tests/integration/cli/test_local_core_commands.py`
  - cover durable navigation and complete human/JSON command behavior.
- `tests/integration/execution/test_run_history_scenarios.py`
  - cover new caller-facing control and output-source reads.
- `docs/api.md` and `docs/execution-presentation.md`
  - document canonical inspect examples and align the inspection surface with
    the existing StepPath and control vocabulary.

If `thread.py` becomes difficult to review, inspect-only parsing, documents,
and renderers may move to a sibling `commands/inspect.py`; that extraction must
not change command registration or expand feature scope.


## Acceptance Tests

1. **Canonical parser**
   - Accept each grammar form in the supported-target table.
   - Reject leading-zero, signed, Unicode-digit, empty, and trailing segments.
   - Reject unsupported input and output suffixes by owner kind.

2. **Copyable navigation**
   - Create a thread containing a root run, nested same-run steps, a child run,
     run controls, and thread controls.
   - Inspect the thread, then invoke `inspect` with every printed run and
     thread-control `PATH`.
   - Inspect the run, then invoke `inspect` with every printed value, control,
     and top-level step `PATH`.
   - Inspect a step, then invoke `inspect` with every printed value, child-step,
     and child-run `PATH`.
   - Assert each invocation selects the intended durable record.

3. **Legacy step compatibility**
   - Assert `run_parent:2.0` still traverses into the same child step as before.
   - Assert its result exposes the child's real StepPath in human output and
     JSON `path`.
   - Assert no newly rendered `PATH` uses colon-and-dot syntax.

4. **Thread view**
   - Assert only effective top-level runs appear, the selected newest window is
     chronological, total and selected counts are explicit, and controls are
     ordered by index.
   - Assert an existing thread with no runs and no non-create controls renders
     explicit empty sections successfully.

5. **Run and step views**
   - Assert run values and controls are always navigable, only direct steps are
     listed, failures retain their error text, and a step separates same-run
     child steps from child runs using canonical identities.
   - Assert runs with no steps and steps with no children state that explicitly.

6. **Input, output, and control views**
   - Cover run messages, step references, inline step messages, run-control
     messages, empty step output, pass-through run output, step-referenced run
     output, absent output, both control kinds, and optional control fields.
   - Assert references and sources render as canonical inspectable paths.

7. **Invalid and missing data**
   - Cover every error and state row in the missing-data table.
   - Assert syntax errors happen before store reads and failures do not emit a
     partial JSON or human document.

8. **Large values**
   - Verify both default boundaries, Unicode-safe cutting, exact omitted-byte
     reporting, `--full`, summary caps, media descriptors, lossless JSON, and
     the `--full --json` conflict.

9. **JSON compatibility**
   - Retain assertions for current thread, run, and legacy step document fields.
   - Assert additive `path`, `inspect_path`, thread controls, and the exact new
     value/control envelopes.

10. **Placement and read-only behavior**
    - Retain resident and roaming coverage.
    - Assert visiting inspection does not fetch or materialize the source.
    - Assert inspection does not create or migrate a missing or incompatible
      execution store.

11. **Repository verification**
    - `uv run ruff check .`
    - `uv run ruff format --check .`
    - `uv run ty check`
    - `uv run pytest`


## Risks And Mitigations

- **Legacy synthetic paths can cross run boundaries.** Keep the old resolver
  only behind the compatibility alias and canonicalize its result immediately.
- **Field suffixes can be mistaken for step segments.** Step segments accept
  only canonical ASCII indexes; `input` and `output` are reserved terminal
  suffixes.
- **Run and thread controls share `@` syntax.** Resolve the owner kind first and
  use the matching caller-facing lookup; never probe both control tables and
  accept whichever returns data.
- **A shallow view can hide descendants users previously saw.** Direct child
  paths and explicit empty states make the next navigation step visible, while
  the legacy target alias preserves old scripted selection.
- **JSON consumers may depend on current fields.** Keep existing envelopes and
  nested fields, make additions only, and test the old assertions alongside
  new target documents.
- **Large values can overwhelm terminals or memory.** Bound human rendering
  with deterministic byte and line limits, keep table summaries independently
  capped, and direct full machine output to `--json`.
- **Thread inspection can become query-heavy.** Batch controls and details for
  the selected run window through `RunHistory`; do not issue one store query per
  displayed row.
- **Missing and empty values can look identical.** Carry an explicit state from
  resolution through both human and JSON presentation.


## Open Questions

None. This definition selects the path grammar, navigation depth, target
presentations, limits, compatibility behavior, failure semantics, and test
coverage required for implementation.
