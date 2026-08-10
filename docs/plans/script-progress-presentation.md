# Script Progress Presentation

## Goal

Make script execution progress use the actual output width and keep authored
assignments and failures easy to scan. Interactive output must adapt to terminal
resizes without rewriting stable scrollback, while redirected output remains
deterministic and free of terminal control sequences.

Implementation starts only after this definition is approved.


## Success Criteria

- Interactive progress uses every available terminal cell and never relies on
  fixed character limits that leave a wide terminal partly unused.
- Wrapping and truncation are based on terminal cell width, including wide and
  combining Unicode, rather than Python string length.
- Stable text wraps predictably, single-row live data truncates predictably, and
  no renderer silently applies both behaviors to the same value.
- A resize redraws only mutable live output. Text already committed to
  scrollback is not reflowed or erased.
- Direct authored `let` statements expose a bounded value preview at `-v`, and
  every explicit named or discard binding exposes its successful destination at
  the default level.
- The root summary's single lifecycle diagnostic identifies the useful visible
  boundary and nests distinct descendant causes without repeating a propagated
  error in statement progress or elsewhere in the summary.
- Standard, compact, emergency-narrow, and non-interactive output have explicit
  behavior and representative snapshots.
- Script flags, execution events, durable records, final stdout, bounded
  parallelism, and the verbosity inclusion relationship remain compatible.


## Current Behavior

`ConsoleRunTracer` owns live script execution progress. `script._execute()`
creates it when the command is not quiet and either stderr is a TTY or at least
one `-v` was supplied. Progress goes to stderr; `_emit_result()` writes the
successful runnable result to stdout independently of the progress renderer.

`ProgressConsole` currently samples width once during construction. A TTY uses
`shutil.get_terminal_size()` and a non-TTY uses 100 columns, then every value is
clamped to a minimum width of 40. There is no resize handling. The width of an
injected test console is also clamped to 40.

Wrapping uses `textwrap.wrap()` and `len()` after collapsing whitespace. Live
rows use a separate `len()`-based truncation helper. Several values are
truncated before layout to fixed code-point counts:

- argument summaries: 80;
- streaming model previews: 100;
- primary input text: 160;
- completed model output previews: 180.

Consequently a wide terminal cannot display more than those caps, CJK and
combining text can be measured incorrectly, and a narrow terminal can receive
lines wider than it has because the console pretends to be at least 40 columns.
Calls to `console.write()` do not wrap at all. Parallel live rows are truncated
to the construction-time width, and stable rows cannot respond to a resize.

Flow statement headers already use the compact source-like head from
`StepBegin.given.source.head`. A direct `let NAME: BODY` has no child work line.
At default and `-v` it therefore appears only as a header. At `-vv` it adds the
generic result `1 item saved to NAME · perceived from authored content`; it
does not show a value preview. Explicit named bindings around `run` never show
a statement result, and other predictable named results normally appear only
at `-vv`.

Errors are plain strings on `StepEnd` and `RunEnd`. The tracer keeps a global
set of previously printed strings and emits only the first exact match. That
prevents common propagation duplicates, but it does not model ownership or
causality: distinct failures with the same text can suppress one another,
different wrapper messages can appear as unrelated diagnostics, and useful
child context can be lost. A root summary omits the error after the same string
was printed below a step or statement. Full Python exception causes are not
part of execution events.

Before a run is accepted, command parsing, source loading, setup, and validation
errors use the shared Typer Rich error panel. Those errors are outside the run
progress event stream. Existing tracer tests cover a 40-column ASCII wrap and
selected complete outputs, but there are no width-tier, resize, Unicode-cell,
or non-interactive golden snapshots.


## Scope

This feature changes the live and finalized stderr presentation produced for
one accepted script run. It covers:

- width discovery, injected width providers, cell measurement, wrapping,
  truncation, and resize-safe live-region replacement;
- layout behavior at normal, compact, and emergency terminal widths;
- direct authored `let` previews and successful outcomes for explicit `let`
  bindings;
- structural collection, selection, de-duplication, and rendering of execution
  failure causes;
- deterministic non-interactive progress when `-v` or `-vv` explicitly enables
  it;
- snapshot and focused test coverage for these contracts.

This feature does not change:

- `.too` syntax, flow binding semantics, or when a binding is committed;
- execution events, record schemas, persistence, exception logging, or runtime
  failure propagation;
- script command names, options, verbosity meanings, or stdout serialization;
- the field set, metrics, actions, or lifecycle wording of the root run summary,
  which is owned by #259;
- inspect path syntax or full-value rendering, which is owned by #258;
- Chat TUI status and summary composition, which is owned by #261;
- pre-run Typer usage, source, setup, or validation error panels.


## Output Ownership And Visibility

The existing surface contract remains:

| Situation | Progress behavior |
| --- | --- |
| TTY, default | interactive default progress on stderr |
| TTY, `-v` or `-vv` | interactive progress with the requested detail |
| non-TTY, default | no event-driven progress |
| non-TTY, `-v` or `-vv` | deterministic stable progress on stderr |
| any stream, `-q` | no event-driven progress |

`-q` continues to suppress execution progress, not the successful runnable
value on stdout. A failed command that has no tracer continues through the
existing command-level error path. Once a tracer is active, execution failures
are reported once through #259's root-summary lifecycle field, with this
feature supplying its nested layout, and without a second Typer panel.

Visibility remains monotonic: default is a subset of `-v`, which is a subset
of `-vv`. Width may change layout but must not promote verbosity-gated facts.
Color may reinforce meaning on a TTY, but every status and relationship is
complete in plain text.


## Width Model

### Measurement

Width is the number of terminal cells available on the progress stream.
Interactive measurement uses the file descriptor of that stream, not an
unrelated stdout or process-global console. Tests and embedding callers may
inject a width provider. The provider returns the current width for one render
transaction rather than one immutable construction-time value.

One render transaction has this unambiguous order:

1. if a live region exists, clear it strictly from its recorded old physical
   row count and width without consulting the width provider;
2. if a live replacement or stable block follows, sample width exactly once;
3. render every replacement or stable line with that one sample and record new
   live geometry when applicable.

A close that only removes a live region stops after step 1 and does not sample
width. A transaction with no old live region begins at step 2. A positive
measured value is honored exactly; it is never raised to a presentation
minimum. A failed or unavailable interactive measurement uses 100 cells for
that transaction and is retried on the next transaction.

Cell measurement and cutting must use the Unicode cell facilities already
available through Rich or an equivalently mature library. A cut must not leave
a combining mark detached from its base or split a zero-width-joiner sequence.
ANSI control sequences never contribute cells.

### Available Content Width

Every renderer computes payload width from:

```text
payload cells = terminal cells - indent cells - marker cells
```

There is no hidden minimum such as the current ten-character wrapping floor.
If decoration would consume the row, compact or emergency layout removes that
decoration before payload is discarded. Every physical output line must fit
the sampled width.

Fixed code-point caps must not run before terminal layout. The existing
80/100/160/180 caps are replaced by semantic row limits described below, so a
120- or 200-column surface can use those cells.


## Wrapping And Truncation

Content belongs to exactly one layout class:

| Class | Examples | Rule |
| --- | --- | --- |
| stable prose | runnable descriptions, statement descriptions, work lines | wrap fully with a hanging continuation |
| stable bounded preview | completed model text, direct `let` value preview, displayed input/argument preview | wrap to at most three physical payload rows, then end with `…` |
| diagnostic text | primary error and distinct cause messages | wrap to at most three physical payload rows per cause, then end with `…` |
| stable facts | paths, duration, model, type, usage, aggregate facts | keep fact tokens intact when possible; wrap between ` · ` tokens, then hard-wrap only an overlong token |
| single-row live data | thinking/model delta, batch summary, lane row | remain one row and truncate the payload with `…` |
| structural identity | statement header, run header, cause label | preserve marker and identity first; wrap the remaining label with a hanging continuation |

Whitespace inside compact progress prose is normalized to single spaces. The
renderer does not reproduce provider traceback formatting or authored blank
paragraphs in progress. The complete durable value remains available through
inspection, and complete exception detail remains in the run log when logging
is enabled.

Wrapping prefers word boundaries. A token longer than an empty payload row is
split at a grapheme boundary. Truncation reserves one terminal cell for the
Unicode ellipsis `…`; it never emits three ASCII periods. When a row has no
payload cell after required structure, the narrower layout tier is used.

A semantic three-row preview is independent of verbosity: `-vv` adds facts but
does not turn progress into an unbounded value dump. The canonical successful
result on stdout remains untruncated.


## Resize Behavior

Stable scrollback is immutable. A resize does not clear, rewrite, or reflow any
line that has already ended with a newline.

The console records the physical row count and width used by the current live
region. On a refresh or finalization it clears exactly those old physical rows
without measuring the terminal. Only after the clear does it sample once for
the replacement live region or stable block. This ordering matters when the
terminal shrinks: clearing uses recorded old geometry, while replacement uses
the new sample. Close without replacement clears and returns without a width
sample.

A `SIGWINCH` handler is not required. The next execution event or existing
periodic live refresh observes the new size. Width discovery must not install a
process-global signal handler or make execution depend on a resize event.


## Width Tiers

### Standard: 40 Cells Or Wider

The current marker grammar and two-space nesting are retained. Facts may share
one row with ` · ` separators, and lane rows retain their lane, item, and
activity columns. Decoration defined by #259 may use the remaining width.

### Compact: 20 Through 39 Cells

The same status, binding destination, primary diagnostic, and requested
verbosity facts remain visible, but layout compacts as follows:

- nesting is capped at one two-space indent plus a hanging continuation;
- fact groups stack at separator boundaries when they do not fit;
- source heads and work sentences wrap instead of being truncated;
- live lane rows keep lane and item identity before truncating activity;
- optional decorative rules and padding are removed before semantic content;
- a nested cause label may wrap onto its own continuation line.

Compact layout does not abbreviate public identifiers or change zero-based
indexes.

### Emergency: Fewer Than 20 Cells

Emergency output prioritizes text that can explain or locate the run:

- decorative rules, blank padding, columns, and indentation are removed;
- one marker is kept only when it leaves room for payload;
- facts become one fact per line;
- live parallel output collapses to its aggregate row and omits lane rows;
- headers, binding names, statuses, the primary error, and selected cause paths
  remain, hard-wrapped at the actual width;
- output is still cell-safe and no line exceeds the measured width.

Emergency mode is intentionally less decorative, not silent. It does not
pretend that a 12-cell surface is 40 cells wide.


## `let` Statement Presentation

### Information Hierarchy

Every `let` presentation has these ordered layers:

1. the authored source-like statement head;
2. optional work or evaluated-value preview;
3. a successful binding outcome;
4. optional `-vv` facts.

The source head remains the authority for syntax:

```text
[0] let project
[1] let findings = map search_web par 4
[2] let run notify
```

The renderer never reconstructs the source from binding metadata and never
prints the indented authored body.

### Direct Authored Values

A direct `let NAME: BODY` has no child run. At default it shows the committed
shape and destination:

```text
[0] let project
  ↳ 1 item saved to project
```

At `-v` and `-vv`, a bounded preview of the evaluated `StepEnd.output` appears
before the outcome:

```text
[0] let project
  · Build a release dashboard for offline operators.
  ↳ 1 item saved to project
```

The preview uses evaluated output rather than raw template source, so
interpolation is represented accurately. Text is whitespace-normalized. Image,
audio, and document parts use typed descriptors and filenames; encoded bytes,
tool payloads, and `StepEnd.noted.value` are never dumped. An empty percept is
shown as `· empty content`.

At `-vv`, a continuation facts row adds the durable StepPath and declared
runtime type when present. Missing type data is omitted rather than guessed.

### Explicit Binding Outcomes

A successful statement whose source head uses either explicit `let` form shows
its outcome at every non-quiet progress level:

```text
[1] let findings = map search_web par 4
  Run agic search_web in parallel (18 items, 4 lanes)
  · 18 runs succeeded · 9.4s
  ↳ 18-item list saved to findings · mapped from 18 items

[2] let run notify
  Run agic notify
  ↳ 1 item discarded
```

This applies to `let NAME = VALUE_STMT`, `let VALUE_STMT`, and direct
`let NAME: BODY`. It does not promote predictable results for an implicit `_`
binding. Existing meaningful-result rules for scatter, filtering, bounded rank,
and other non-`let` statements remain unchanged.

Work output is not repeated as a second value preview for `let NAME = run ...`
or another child-work statement. The outcome reports shape, destination, and
the existing transformation phrase only. A failed or canceled statement prints
no saved or discarded outcome because its binding was not committed.


## Failure Presentation

### Structural Cause Model

The tracer derives presentation causes from event ownership, not from matching
strings globally. A diagnostic node records:

- the failing step, statement, or run boundary;
- its status and normalized error text;
- its source statement, model, tool, child run, item, and StepPath labels when
  those facts are present in existing events;
- descendant diagnostic nodes connected through StepPath and run-parent
  ownership.

No execution schema changes are required. Python `__cause__` values that were
not serialized into an event remain log-only; the renderer must not parse
traceback text or invent a provider cause.

Failure events are buffered through the terminal root `RunEnd`. Statement and
child-run blocks may finalize status, counts, and identity, but they do not
print the selected error message. The buffered cause tree fills the second
semantic field of #259's root summary. It is not a separate block before or
after that summary.

### Hierarchy And Wording

The summary field starts with #259's exact primary label and selected message,
then presents distinct descendant causes underneath:

```text
Error: output is not valid Number
  caused by item 5 · run_score5
    scorer returned Text
    caused by run_score5/0 · model deepseek/deepseek-chat
      provider returned status 429
```

Rules:

- `Error: MESSAGE` is the failed summary's one lifecycle diagnostic and stays
  normal-brightness red on a TTY;
- `caused by` names a real descendant boundary and adds one two-space level;
- when selected `MESSAGE` propagated unchanged through ancestors, it appears
  only on the `Error:` line; descendant cause labels may add useful identity
  without repeating that text;
- distinct descendant messages remain under their owning cause labels, and
  string equality never replaces structural ownership before representative
  selection;
- empty error text falls back to the boundary status and identity, never an
  invented explanation;
- `-vv` may add duration and exact step facts after the matching message, but
  lower levels retain enough source, item, tool, model, or run context to act;
- cancellation uses #259's single `Canceled: REASON` lifecycle field and the
  existing yellow warning tone; it does not add a nested failure cause tree.

The selection and fallback for `MESSAGE` are owned by #259: prefer the root run
error, then the owning failed step error, then `Run failed.`. This feature does
not select a competing primary message. It only associates real descendant
events with that selected message and renders their distinct context below the
same field. No `!` statement diagnostic, post-summary error, or command-level
panel may repeat `MESSAGE` when the root summary is emitted.

### Bounded Causes

One lifecycle diagnostic may show at most three nested `caused by` nodes. If
the runtime tree is deeper, retain the two causes nearest the root summary and
the deepest leaf cause, inserting `… N intermediate causes omitted` before the
leaf. Each distinct message is limited to three physical payload rows at the
current width.

For a batch with multiple failures, select the lowest zero-based failed item as
the representative cause after the batch ends, independent of completion
order, and add `N additional items failed` to the aggregate facts. Do not emit
one cause tree per item in script progress. All failed items remain available
through durable inspection.


## Representative Layout Snapshots

The successful excerpt defines progress-body layout. The failed snapshot is
complete and composes #259's semantic root-summary fields with this plan's
nested lifecycle field and width rules.

### Standard Width, `-v`

```text
Run flow research
Research one topic and synthesize a sourced answer.

[0] let topic
  · Compare agent runtimes for reproducible offline jobs.
  ↳ 1 item saved to topic

[1] let findings = map search_web par 4
  Run agic search_web in parallel (18 items, 4 lanes)
  · 18 runs succeeded · 9.4s
  ↳ 18-item list saved to findings · mapped from 18 items
```

### Complete Failed Output, 120 Cells

```text
Run flow score

[2] let score = run score_item
  Run agic score_item
  ↳ run_score5 failed · 820ms

--- run_one failed ---
Error: output is not valid Number
  caused by item 5 · run_score5
    scorer returned Text
    caused by run_score5/0 · model deepseek/deepseek-chat
      provider returned status 429
Result: not produced
Inspect: toolang ./score.too inspect run_one
820ms · 1 run · 1 model call · tokens unavailable · cost unavailable
----------------------
```

`output is not valid Number` is the selected #259 lifecycle message and occurs
exactly once in the complete output. The red child-run status supplies early
structural context without repeating that message. The nested lines belong to
the `Error:` summary field; they are not a statement diagnostic or a second
summary block.

### Compact Boundary, 20 Cells

```text
[2] let score = run
    score_item
  Run agic
    score_item
--- run_one failed
    ---
Error: output is not
  valid Number
  caused by item 5
    run_score5/0
    provider
    returned status
    429
Result: not produced
```

The real snapshot fixture must assert that every rendered line is at most 20
terminal cells. The example uses ASCII so its displayed width is unambiguous.

### Emergency Width, 12 Cells

```text
[2] let
score = run
score_item
Run agic
score_item
--- run_one
failed ---
Error:
output is
not valid
Number
cause
run_score5/0
provider
returned 429
```

Emergency mode may simplify `caused by` to `cause` as shown. It may not remove
the primary message or cause path.

### Non-Interactive, `-v`

Non-interactive progress uses the same stable block vocabulary at a
deterministic width of 100. It contains neither the transient `thinking…` and
lane rows nor ANSI and cursor-control bytes:

```text
Run flow research
Research one topic and synthesize a sourced answer.

[0] let topic
  · Compare agent runtimes for reproducible offline jobs.
  ↳ 1 item saved to topic
```


## Non-Interactive Output

A pipe or file has no live terminal width. When `-v` or `-vv` opts into progress
on a non-TTY, the renderer therefore uses exactly 100 cells unless an embedding
caller injects a test width. It does not consult `COLUMNS`, because that would
make redirected snapshots depend on the invoking shell.

Non-interactive output:

- emits stable newline-delimited blocks only;
- never emits ANSI color, carriage returns, erase commands, or cursor motion;
- omits transient model deltas, `thinking…`, active batch summaries, and lane
  assignments;
- emits the finalized model/tool preview, statement aggregates, `let` outcome,
  and the root summary containing its one lifecycle diagnostic when allowed by
  verbosity;
- uses the same cell-aware stable wrapping and semantic row limits as a TTY.

This preserves useful `-v` logs without simulating an interactive live area.


## Compatibility Constraints

- Keep the current script command placement and `-q`, `-v`, and `-vv` flags.
- Keep stdout reserved for the canonical successful runnable result; presentation
  truncation applies only to progress on stderr.
- Keep the current TTY/default and non-TTY/explicit-verbosity routing.
- Keep current `RunEvent`, `StepPath`, record, and logging contracts. Presentation
  state must derive only from ordered events.
- Keep `finished` displayed as `succeeded`, zero-based statement/item/lane
  positions, and the `·`, `!`, and `↳` marker meanings. The root lifecycle
  diagnostic is the unmarked `Error:` field defined by #259; `!` remains for
  nonterminal diagnostics that do not repeat its selected message.
- Keep successful parallel output bounded by active lane count and failed batch
  output bounded by one representative cause tree.
- Keep progress dim, active live data normal brightness, failures red, and
  cancellations yellow on a TTY.
- Human stderr layout is intentionally allowed to change. It has no
  line-for-line compatibility guarantee across this feature.
- Do not add a database migration, query the store from the event path, print
  raw encoded media, or expose secrets from `noted` values.
- Use #258's canonical inspect paths once that feature is available; do not
  define a competing path or full-value command in this feature.
- Apply this width policy to #259's root summary and align #261's terminal-cell
  measurement, width tiers, and snapshot widths without sharing terminal
  renderer implementations.


## Design Touchpoints And Likely Files

Implementation should retain terminal-independent execution state in the
shared presentation state module and keep script terminal policy in the script
renderer.

- `src/toolang/cli/common/script_progress/console.py`
  - accept a dynamic width provider;
  - measure and cut terminal cells;
  - implement standard, compact, and emergency wrap/truncate primitives;
  - record old live-region geometry and redraw it safely after resize.
- `src/toolang/cli/common/script_progress/blocks.py`
  - classify every field as stable prose, bounded preview, facts, live data, or
    structural identity;
  - render direct `let` previews and explicit binding outcomes;
  - render the nested cause tree inside #259's root lifecycle field and its
    narrow variants.
- `src/toolang/cli/common/script_progress/tracer.py`
  - collect diagnostic ownership instead of globally de-duplicating strings;
  - defer selected error text and causes until root-summary finalization;
  - choose deterministic representative batch failures.
- `src/toolang/cli/common/execution_progress/formatting.py`
  - replace code-point truncation with cell-aware helpers;
  - remove fixed pre-layout caps and expose bounded part-preview formatting.
- `src/toolang/cli/common/execution_progress/state.py`
  - retain failure nodes, batch failure items, and parent relationships as
    presentation state without changing execution events.
- `tests/unit/cli/test_console_run_tracer.py`
  - update complete output expectations and add `let`, diagnostic, width-tier,
    resize, and non-TTY snapshots.
- `tests/unit/cli/test_progress_console.py` or the existing tracer test module
  - cover focused Unicode cell wrapping, truncation, and live clear geometry.
- `tests/integration/cli/test_script_local.py`
  - cover real script routing, stdout/stderr separation, and stable redirected
    progress.
- `docs/api.md`, `docs/execution-presentation.md`, and
  `docs/execution-transcript.md`
  - replace the old fixed-width and first-string error descriptions after
    implementation.

No change to `toolang.execution` is expected. If implementation discovers that
an actionable cause is absent from every existing event, omit it and document
the limitation rather than expanding execution schemas under this feature.


## Acceptance Tests

1. **Width source and full-row use**
   - Inject widths of 80, 120, and 200 and assert a stable value longer than the
     old fixed cap uses the larger available row.
   - Assert interactive measurement uses the progress stream descriptor and a
     measurement failure falls back for one transaction only.

2. **Unicode wrapping and truncation**
   - Cover ASCII, CJK, a wide emoji, a combining sequence, and a zero-width
     joiner sequence in stable prose and live rows.
   - Assert exact terminal-cell widths, hanging continuations, one-cell
     ellipses, and no split grapheme sequence.

3. **Layout classes**
   - Snapshot full stable prose, a three-row bounded preview, fact wrapping at
     separators, an overlong token, a one-row model delta, and a lane row.
   - Assert no field is pre-truncated to 80, 100, 160, or 180 code points.

4. **Resize behavior**
   - Drive one TTY fixture through widths `80 -> 32 -> 120` while a live batch
     is active.
   - Assert each refresh clears from recorded old row count and width without
     calling the provider, then samples exactly once for the replacement.
   - Assert finalization follows the same clear-then-sample order for its stable
     block, while close without replacement clears and never samples.
   - Assert stable scrollback bytes are unchanged and no stale row remains.

5. **Direct `let` values**
   - Cover text, interpolated text, empty content, and media descriptors.
   - Assert default shows only the committed outcome, `-v` adds a bounded
     evaluated preview, and `-vv` adds only available path/type facts.
   - Assert raw template source, encoded bytes, and `noted.value` are absent.

6. **Explicit binding outcomes**
   - Cover `let NAME = run`, a named parallel transform, direct authored `let`,
     and `let VALUE_STMT` discard.
   - Assert their successful outcome is visible at default and no failed or
     canceled statement claims a save or discard.
   - Retain current visibility for implicit `_` and meaningful non-`let`
     results.

7. **Structural diagnostics**
   - Cover one root model failure, a tool failure, a direct child-run failure,
     a distinct wrapper chain, identical propagated text, failed Boolean
     coercion after a successful `until` run, and cancellation.
   - Assert one #259 lifecycle field per root summary, outer-to-inner cause
     order, real labels, correct tone, and no statement or fallback diagnostic
     repeats the selected message.
   - Snapshot complete statement progress plus the failed root summary and
     assert `Error: MESSAGE` is ordered before result/actions/metrics and
     `MESSAGE` occurs exactly once in the entire output.
   - Assert structurally unrelated failures with identical text remain distinct
     before deterministic batch representative selection.

8. **Bounded nested and batch failures**
   - Cover more than three nested causes and assert the exact omitted count and
     retained deepest leaf.
   - Complete failed batch items out of order and assert the lowest item is the
     representative cause plus the correct additional-failure count.

9. **Width tiers and snapshots**
   - Store literal plain-text golden snapshots at 120, 40, and 20 cells, plus a
     focused 12-cell emergency expectation.
   - Include Unicode in each standard/compact snapshot and assert every line's
     terminal-cell width.
   - Use fixed IDs, timestamps, durations, and event order; do not normalize
     semantic output with broad regular expressions.

10. **TTY control and styling**
    - Snapshot finalized text with ANSI and cursor sequences removed by one
      narrow test helper.
    - Keep separate exact assertions for dim progress, red/yellow diagnostics,
      erase commands, cursor motion, and live redraw row counts.

11. **Non-interactive output**
    - Snapshot `-v` and `-vv` at deterministic width 100.
    - Assert default non-TTY does not create event-driven progress; explicit
      verbosity emits no transient lines, ANSI, carriage return, or cursor
      commands.

12. **CLI compatibility**
    - Assert `-q` suppresses progress but preserves successful stdout, final
      stdout is untruncated, and failures still return nonzero.
    - Retain command-level Rich error coverage for failures before run
      acceptance.

13. **Repository verification**
    - `uv run ruff check .`
    - `uv run ruff format --check .`
    - `uv run ty check`
    - `uv run pytest`


## Risks And Mitigations

- **Unicode code points and terminal cells diverge.** Centralize cell
  measurement and cutting in the console layer and test wide, combining, and
  joined sequences instead of letting blocks slice strings.
- **A shrink can erase stable scrollback.** Retain old live geometry and clear
  before sampling the replacement width; never infer old row count from the
  new width.
- **Semantic row limits can hide useful detail.** Preserve identity and status
  before payload, use a visible ellipsis, keep stdout canonical, and rely on
  durable inspect/log surfaces for full values and exception detail.
- **Direct `let` previews can expose more evaluated input than today.** Keep the
  preview behind `-v`, bound it to three rows, render only safe message-part
  descriptors, and never dump `noted.value`.
- **String de-duplication can hide independent failures.** De-duplicate only
  identical propagation along one ownership chain and select batch failures by
  durable item position.
- **Buffering failures can delay the actionable message.** Keep failed status
  and identity visible in live or finalized statement progress, then emit the
  selected message and nested causes once in the terminal root summary as soon
  as root `RunEnd` is available.
- **Companion features can conflict on summaries or inspect hints.** Treat #259
  as the owner of summary content, #258 as the owner of inspect paths, and this
  plan as the owner only of shared geometry applied to those blocks.
- **Emergency output can become unreadable through excessive decoration.** Drop
  decoration and indentation before semantic text, and snapshot the 12-cell
  contract explicitly.


## Open Questions

None. This definition selects the width source, cell model, resize lifecycle,
layout classes, width tiers, `let` hierarchy, diagnostic cause rules,
non-interactive behavior, compatibility boundary, implementation touchpoints,
and acceptance coverage required for implementation.
