# Script Progress Presentation

## Goal And Success Criteria

Make script progress use the actual output width and make assignments and
failures easy to scan without changing execution behavior.

Implementation starts only after this definition is approved. It succeeds
when:

- interactive stderr uses its current terminal-cell width, including after a
  resize;
- stable content wraps, one-row live content truncates, and wide or combining
  Unicode is measured correctly;
- narrow terminals retain status, identity, bindings, and actionable errors;
- direct `let` values and explicit binding outcomes have a clear hierarchy;
- a failed run has one root-summary diagnostic with bounded nested causes;
- redirected `-v` output is deterministic and contains no terminal controls;
- `-q`, `-v`, `-vv`, stdout, events, persistence, and execution semantics stay
  compatible.


## Current Behavior

`ConsoleRunTracer` writes progress to stderr when stderr is a TTY or `-v` is
supplied; `-q` disables it. Successful output is written separately to stdout.

`ProgressConsole` samples width once at construction, uses 100 columns for a
non-TTY, and clamps every width to at least 40. Wrapping and truncation use
Python character counts, so terminal cells are wrong for some Unicode. There
is no resize handling, and direct writes can exceed the configured width.
Values are also capped before layout: arguments at 80 code points, live model
previews at 100, primary input at 160, and completed model previews at 180.

A direct `let NAME: BODY` currently shows only its header below `-vv`; `-vv`
adds a generic save result but no evaluated-value preview. Explicit named
bindings around `run` show no statement result, while other predictable named
results normally appear only at `-vv`.

Errors are strings on `StepEnd` and `RunEnd`. The tracer globally suppresses
strings it already printed. This removes common propagation duplicates but can
hide independent failures and does not preserve causal ownership. Python
exception causes not serialized into events remain available only in logs.

Existing tests cover selected complete traces and one 40-character ASCII wrap,
but not Unicode cells, resize order, width tiers, or redirected snapshots.


## Scope

This feature covers script-run stderr presentation after an accepted
`RunBegin`:

- width measurement, cell-safe wrapping and truncation, resize-safe live
  replacement, and narrow layouts;
- direct `let` previews and explicit named/discard outcomes;
- collection and rendering of structural failure causes;
- deterministic non-interactive progress;
- focused, snapshot, and integration tests plus affected CLI documentation.

It does not change `.too` syntax, binding semantics, execution events, durable
records, logging, runtime propagation, CLI flags, or stdout serialization.
Companion ownership remains:

- #258 owns inspect paths and full-value views;
- #259 owns root-summary fields, lifecycle wording, actions, and metrics;
- #261 owns Chat TUI composition.

Pre-run parsing, source, setup, and validation failures retain the shared Typer
error panel.


## Visibility And Streams

| Context | Event-driven stderr progress |
| --- | --- |
| TTY, default | interactive default detail |
| TTY, `-v` / `-vv` | interactive requested detail |
| non-TTY, default | hidden |
| non-TTY, `-v` / `-vv` | deterministic stable detail |
| any stream, `-q` | hidden |

`-q` suppresses progress, not a successful stdout value. When a visible root
summary reports a failure, command-level handling must not append another
error, run, or log block. Without a visible summary, the existing fallback
remains responsible for actionable nonzero-exit output.

Visibility stays monotonic: default is a subset of `-v`, which is a subset of
`-vv`. Width changes layout, never verbosity.


## Width And Resize Contract

### Measurement

Interactive width is the current number of terminal cells on the progress
stream's file descriptor. Tests and embedding callers may inject a width
provider. A positive measurement is honored exactly and is never raised to a
layout minimum. An unavailable measurement falls back to 100 for that
transaction and is retried on the next transaction.

One render transaction has this order:

1. Clear an existing live region strictly from its recorded old row count and
   width, without consulting the provider.
2. If a replacement live region or stable block follows, sample width once.
3. Render the complete replacement or block with that sample and record new
   live geometry when applicable.

Close without replacement performs step 1 and does not sample. Stable
scrollback is never erased or reflowed. No `SIGWINCH` handler is required; the
next event or existing refresh observes the new width.

Cell measurement and cutting use Rich's cell facilities or an equivalent
mature library. ANSI sequences consume no cells, and a cut must not detach a
combining mark or split a zero-width-joiner sequence.

### Layout Classes

| Content | Layout |
| --- | --- |
| descriptions and work lines | wrap fully with hanging continuation |
| completed model, input, argument, and direct-`let` previews | wrap to three physical payload rows, then `…` |
| primary error and each distinct cause message | wrap to three payload rows, then `…` |
| paths, duration, model, type, usage, aggregate facts | wrap between ` · ` facts; split only an overlong fact |
| live model, aggregate, and lane rows | one row; truncate payload with `…` |
| statement, run, and cause identities | preserve marker and identity before wrapping detail |

Payload width is terminal width minus indentation and marker cells. There is no
hidden content-width floor. Progress whitespace is normalized to single spaces;
wrapping prefers word boundaries and splits an overlong token only at a safe
grapheme boundary. The ellipsis occupies one cell. The current
80/100/160/180 pre-layout caps are removed, while successful stdout stays
untruncated.

### Width Tiers

| Width | Contract |
| --- | --- |
| 40+ | standard two-space nesting, shared fact rows, and full live lane columns |
| 20-39 | at most one two-space nesting level, stacked facts, wrapped source/work text, and lane/item identity before truncated activity |
| below 20 | no decorative rules, columns, blank padding, or indentation; one fact per line; aggregate-only live batch output; hard-wrapped identity, binding, status, and primary error |

Every physical line fits the measured width. Narrow layout removes decoration
before semantic content and never abbreviates public identifiers or changes
zero-based indexes.

At the 20-cell boundary, the root lifecycle field has this shape:

```text
Error: output is not
  valid Number
  caused by item 5
    run_score5/0
Result: not produced
```


## `let` Presentation

The order is always:

1. authored source-like head;
2. child work or evaluated-value preview;
3. successful binding outcome;
4. optional `-vv` facts.

The source head comes from `StepBegin.given.source.head`; the renderer never
reconstructs it or prints the indented authored body.

A direct `let NAME: BODY` shows its committed shape and destination at default:

```text
[0] let project
  ↳ 1 item saved to project
```

At `-v` and `-vv`, its evaluated `StepEnd.output` preview precedes the outcome:

```text
[0] let project
  · Build a release dashboard for offline operators.
  ↳ 1 item saved to project
```

Text is whitespace-normalized. Image, audio, and document parts use typed
descriptors and filenames; encoded bytes, tool payloads, and
`StepEnd.noted.value` are not printed. Empty output is `· empty content`.
`-vv` may add the StepPath and recorded runtime type without guessing either.

Every explicit `let` form shows its successful outcome at default:

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
`let NAME: BODY`. Predictable results for implicit `_` retain current
visibility. Child work is not repeated as a value preview. Failed or canceled
statements never claim a save or discard.


## Failure And Nested Causes

The tracer builds diagnostic nodes from existing event ownership. A node keeps
its boundary, status, normalized error, and available statement, model, tool,
run, item, and StepPath identity. No store query, traceback parsing, or event
schema change is allowed.

Failure events are buffered through root `RunEnd`. Statement and child-run
blocks may finalize status, counts, and identity, but not the selected error
message. The cause tree fills #259's second root-summary field; it is not a
separate statement or post-summary diagnostic.

#259 selects the primary failed message: root error, then owning failed-step
error, then `Run failed.`. This feature renders that selection as
`Error: MESSAGE` exactly once and adds distinct descendant context below it.
When the same message propagated through ancestors, cause labels may add
identity without repeating the text. Distinct descendant messages remain under
their owning labels.

Complete failed output at 120 cells:

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

The selected message `output is not valid Number` occurs once in the complete
output. The red child-run status supplies earlier context without repeating it.

A diagnostic shows at most three nested `caused by` nodes. For a deeper tree,
it keeps the two causes nearest the summary and the deepest leaf, inserting
`… N intermediate causes omitted`. Each distinct message follows the
three-row diagnostic limit.

For a failed batch, the lowest zero-based failed item is the representative
cause regardless of completion order; aggregate facts add
`N additional items failed`. Script progress never prints one tree per item.
Cancellation uses #259's single `Canceled: REASON` field and no nested failure
tree.


## Non-Interactive Output

An opted-in non-TTY uses exactly 100 cells unless a test width is injected; it
does not consult `COLUMNS`. It emits only stable newline-delimited blocks, with
no ANSI, carriage return, erase command, cursor motion, transient delta,
`thinking…`, active aggregate, or lane row. Final model/tool previews,
statement aggregates, `let` outcomes, the root lifecycle field, and the root
summary follow normal visibility and wrapping rules.


## Compatibility And Implementation Touchpoints

The implementation must preserve public script routing and flags, stdout
encoding, non-TTY default silence, durable `finished` status, display
`succeeded`, zero-based positions, marker meanings, progress colors, and
lane-bounded parallel output. It must not add persistence migrations, query the
store on the event path, expose raw media or `noted` values, or define competing
inspect or summary semantics.

Likely files:

- `src/toolang/cli/common/script_progress/console.py`: dynamic width provider,
  cell-safe layout, width tiers, and recorded live geometry;
- `src/toolang/cli/common/script_progress/blocks.py`: layout classification,
  `let` output, and causes inside the root lifecycle field;
- `src/toolang/cli/common/script_progress/tracer.py`: structural cause
  collection, root deferral, and deterministic batch selection;
- `src/toolang/cli/common/execution_progress/formatting.py`: cell-aware helpers
  and bounded part previews;
- `src/toolang/cli/common/execution_progress/state.py`: failure and batch state;
- `tests/unit/cli/test_console_run_tracer.py` and optionally
  `tests/unit/cli/test_progress_console.py`: focused behavior and snapshots;
- `tests/integration/cli/test_script_local.py`: routing and stream contracts;
- `docs/api.md`, `docs/execution-presentation.md`, and
  `docs/execution-transcript.md`: implemented public behavior.

No `toolang.execution` change is expected. If existing events lack a cause,
omit it and document the limitation instead of expanding schemas in this
feature.


## Acceptance Tests

1. **Width and Unicode:** inject 80, 120, and 200 cells; prove old fixed caps no
   longer limit a wide row; cover ASCII, CJK, emoji, combining, and joined
   sequences without overflow or broken graphemes.
2. **Resize:** drive `80 -> 32 -> 120`; assert old geometry is cleared without
   a provider call, replacement/finalization samples once, close-only cleanup
   never samples, scrollback is unchanged, and no stale row remains.
3. **Layout snapshots:** store literal plain-text snapshots at 120, 40, and 20
   cells plus a focused 12-cell emergency case; assert cell widths and keep
   separate exact ANSI/cursor assertions.
4. **`let`:** cover text, interpolation, empty and media content, named child
   work, and discard across default/`-v`/`-vv`; assert safe bounded previews and
   no outcome on failure or cancellation.
5. **Failures:** cover root model, tool, child-run, wrapper, propagated,
   `until` coercion, and cancellation cases; assert one #259 lifecycle field,
   real outer-to-inner labels, correct tone, and the selected message exactly
   once across statement progress and summary.
6. **Cause bounds:** cover more than three causes and out-of-order batch
   failures; assert omitted counts, deepest leaf retention, lowest-item
   selection, and additional-failure count.
7. **Non-TTY:** snapshot `-v` and `-vv` at 100 cells; assert default silence and
   absence of transient or control bytes.
8. **CLI compatibility:** assert `-q` preserves successful stdout, stdout is
   untruncated, visible summaries suppress fallback duplication, pre-run errors
   keep the Rich panel, and failures remain nonzero.
9. **Repository verification:** run `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest`.


## Risks

- Centralize cell operations so individual blocks cannot reintroduce
  code-point slicing.
- Clear from recorded live geometry before measurement so a shrink cannot
  erase stable scrollback.
- Preserve identity and status before truncating payload; full values and
  exceptions remain available through inspect and logs.
- Keep direct `let` previews behind `-v` and avoid `noted.value` to limit
  accidental disclosure.
- De-duplicate only propagation within one structural chain; batch selection
  remains identity-based.
- Keep failed status visible before root completion, then emit the actionable
  message once in the terminal summary.


## Open Questions

None.
