# Define Agic Run Step Progress

## Work Type and Status

Feature definition. Approved for implementation on 2026-08-28 after the human
confirmed the header, footer, identity, and alignment grammar. This document
does not implement product code.

This plan refines the progress requirement in
[Agic runtime calls](agic-runtime-calls.md). A dynamic Run Step looks like an
authored Run Step by preserving child ownership, aggregate facts, and causal
errors; it does not reuse the authored Flow Step's `[N]` boundary grammar.

## Verified Current Behavior

- Script and Chat share the terminal-independent `ProgressProjector` and Rich
  rendering pipeline under `cli/common/execution_progress`.
- Authored Flow Steps use `[N] ...` headers. A facts-bearing Flow Step footer
  places facts at the left and its complete owning `StepPath` at the right.
- Agic Model and Tool Steps use `•` execution rows and have no section header or
  per-Step facts footer.
- Child Runs have no standalone presentation header or closure row. Their
  content is currently projected through the Flow Step that owns them.
- The current `StepGiven` vocabulary distinguishes Flow statements, Model
  calls, and Tool calls. It does not yet distinguish a model-produced Run Step
  from an authored Flow `run` statement.
- A root Run ends with `∎ ROOT_RUN_ID STATUS` at the left and whole-tree facts
  at the right. It does not display a `StepPath`.

## Goal and Success Criteria

Give a model-produced Agic Run Step a visible flat scope without recursive
indentation or confusion with an authored Flow Step. The feature succeeds when:

- an Agic Run Step opens with a full-width `---  run RUNNABLE_REF` divider;
- its child Agic or Flow retains its own existing presentation grammar;
- the Agic Run Step closes with facts at the left and
  `STATUS CHILD_RUN_ID` at the right;
- the child Run ID, rather than the owning root `StepPath`, identifies the
  dynamic child at the footer's right edge;
- Flow Step and root Run headers and footers retain their existing grammar;
- nested execution remains legible while every structural marker starts in
  column zero; and
- Script and Chat render equivalent stable content at the same available
  width.

## Presentation Grammar

The three structural forms are intentionally distinct:

```text
Flow Step:
[N] HEADER
  FACTS                                                     STEP_PATH

Agic Run Step:
---  run RUNNABLE_REF ------------------------------------------------
---  FACTS ----------------------------------------- STATUS CHILD_RUN_ID

Root Run:
∎ ROOT_RUN_ID STATUS                                        WHOLE_TREE_FACTS
```

`[N]` continues to mean an authored Flow statement. `---  ` means a dynamic
Run Step created inside an Agic. `∎` remains the only root Run terminator.
The callee kind never changes the caller-owned boundary form.

### Agic Run Step Header

The wide header is:

```text
---  run agic:summarize -----------------------------------------------
```

The fixed prefix is three ASCII hyphens followed by two spaces. The caption is
lowercase `run` followed by the displayable runnable reference. A resolved
request uses its canonical `agic:NAME` or `flow:NAME` reference. Before an
acceptance failure, use the bounded, terminal-safe, single-line requested text;
when no text exists, use `request`. One space and an elastic ASCII-hyphen
leader fill the remaining available width. The leader contains at least three
hyphens when the caption fits on one physical line.

The header deliberately omits the child Run ID. A resolved target is known
when the Run Step begins, while the child identity belongs to the later direct
`RunBegin`. A resolved kind-agnostic request displays the canonical kind. Raw
executor action names and generated protocol tool names are never displayed.

The header begins in column zero and is followed by exactly one unpainted blank
row. A child Agic then uses normal `•` Model and Tool traces. A child Flow uses
its unchanged `[N]` statement headers and Step footers.

### Agic Run Step Footer

The wide footer is:

```text
---  2.0s · 1 run · 1 model call ---------------- succeeded run_abc123
```

The fixed prefix is again `---  `. The left field contains the Step's existing
aggregate child-execution facts in their existing order and spelling. One
space, an elastic leader of at least three ASCII hyphens, and one space separate
those facts from the right field. The right field is
`STATUS CHILD_RUN_ID`, where status is `succeeded`, `failed`, or `canceled` and
the identity is the direct child Run accepted below this Run Step.

The complete child Run ID ends at the same available-width boundary used by a
Flow Step footer's complete `StepPath`. The owning Agic `StepPath` is never
shown in this footer and never substitutes for a child Run ID. The footer
aggregates the direct child's complete Run tree, so the facts include that
child Run and its descendants according to the existing accounting rules.

If resolution, input validation, or another error ends the Step before a child
Run is accepted, the concrete error remains inside the scope and the footer
right-aligns only the status:

```text
---  -------------------------------------------------------------- failed
```

It does not invent an ID or fall back to the owning `StepPath`. A successfully
accepted dynamic Run Step owns exactly one direct child Run. More than one
direct child is an execution or presentation contract violation rather than a
layout case.

Exactly one unpainted blank row separates the final visible child content from
the closing divider. Existing child trailing gaps coalesce with that separator
instead of producing multiple blank rows. The completed footer retains the
normal single trailing blank row before the next parent Step or root footer.

### Style

The header caption and leader are dim. In the footer, the prefix, facts,
leader, and child Run ID are dim. `succeeded` uses normal intensity and the
terminal's default foreground; `failed` and `canceled` use normal-intensity red
and yellow respectively. Status color does not leak into facts, leaders, or
identity text. No success-green style is introduced.

### Width and Wrapping

Available width remains
`min(attached terminal width, TOOLANG_PROGRESS_MAX_WIDTH)`, with the configured
maximum used for non-TTY Script output. All measurement and padding use display
cells rather than Python string length.

The renderer first shortens only the elastic leader. If the minimum leader and
both fields do not fit, it wraps complete facts at fact boundaries under a
five-cell hanging indent and places the leader plus the complete right field on
the final physical line:

```text
---  31.0s · 6 runs · 12 model calls
     8 tool calls · ↑18.4k ↓5.2k $0.00
     ---------------------------------------- succeeded run_abc123
```

The right field remains right-aligned when it fits on its own line. A field
wider than the content width folds by display cells without truncating its
identity. A long header caption wraps under the same five-cell indent, and the
leader fills the remaining cells of its final physical line.

## Nested and Exceptional Presentation

A model-produced call always uses the Agic divider even when its target is a
Flow. Authored Flow `run` Steps remain numbered:

```text
---  run flow:publish -------------------------------------------------

[0] Run validate

• The report is valid.
  1.4s · 1 run · 1 model call                              run_publish.0

[1] Run save_report

• The report has been saved.
  2.1s · 1 run · 2 model calls · 1 tool call                run_publish.1

---  4.1s · 3 runs · 3 model calls · 1 tool call ----- succeeded run_def456
```

An Agic child may open another Agic divider. Opening and closing order plus the
child Run ID in each footer express the flat scope stack; descendant content is
not recursively indented. When an Agic runs inside an existing parallel Flow
lane, the lane keeps its current compact one-line ownership. This feature does
not introduce multi-line dividers or footers inside physical lane rows.

Model output containing only the internal runnable call remains suppressed at
the Model Step, just like a tool-call-only Model result. The semantic Run Step
owns the divider, the child owns its normal visible trace, and the returned
protocol result is not displayed a second time. A causal child or Step error is
shown once inside the scope; the footer status does not repeat its message.

## Root Run Footer

The root footer is unchanged:

```text
∎ run_root123 succeeded        8.2s · 4 runs · 6 model calls · 2 tool calls · ↑18.4k ↓5.2k $0.01
```

It begins in column zero with `∎`. Its left caption is the root Run ID,
optional operation, and status; its right field contains whole-tree facts. The
marker and caption use normal intensity, status retains default/red/yellow
color, and facts are dim. It uses elastic spaces rather than a hyphen leader.
On narrow output, the caption stays first and facts wrap under a two-cell
hanging indent. It never displays a `StepPath` or child Run ID.

## Scope

In scope:

- shared Script and Chat projection of model-produced Agic Run Steps;
- semantic distinction between model-produced and authored Run Step `given`;
- dynamic Run header, footer, child identity, status, facts, spacing, styling,
  width, and wrapping;
- nested Agic and Flow child presentation outside compact parallel lanes; and
- normative documentation plus unit, integration, and PTY coverage.

Out of scope:

- implementing the runnable-call action, target resolution, child acceptance,
  protocol result, limits, persistence, or retry semantics;
- changing authored Flow headers, Flow Step facts, or Flow Step paths;
- changing root Run footer content or layout;
- recursive indentation, tree glyphs, child Run closure rows outside the
  owning dynamic Step footer, or a new verbosity mode;
- changing fact labels, ordering, accounting, cost, or token formatting; and
- expanding physical parallel lanes into multi-line child traces.

## Design Touchpoints

- `src/toolang/execution/types.py` and records/events boundaries: preserve a
  typed model-produced Run Step payload with its resolved canonical target
  rather than fabricating a Flow `RunStmt`.
- `src/toolang/cli/common/execution_progress/state.py`: retain dynamic Run Step
  presentation state and its zero-or-one direct child Run ID.
- `src/toolang/cli/common/execution_progress/projector.py`: distinguish Flow
  and Agic Run ownership, aggregate child facts, project exactly one opening and
  closing boundary, and preserve causal error ownership.
- `src/toolang/cli/common/execution_progress/types.py` and
  `rich_rendering.py`: represent and render semantic left/right fields with an
  elastic hyphen leader without pre-padding at projection time.
- `src/toolang/cli/common/execution_progress/step_projection.py` and header
  helpers: provide canonical dynamic target, status, and compact lane text.
- `tests/unit/cli/test_execution_progress_projector.py`, Script presenter tests,
  and Chat TUI tests: cover ownership, content, spacing, styles, Unicode cell
  widths, wide/narrow wrapping, and exceptional streams.
- integration and PTY tests: cover root Agic-to-Agic and Agic-to-Flow traces on
  Script and Chat surfaces.
- `docs/execution-presentation.md` and `docs/execution-transcript.md`: add the
  normative dynamic Run grammar without changing Flow or root grammar.

## Acceptance Tests

1. An Agic-to-Agic Run Step emits one `---  run agic:NAME` header with an
   elastic leader and no child Run ID; a pre-acceptance failure uses safe,
   bounded requested text or `request` when no text exists.
2. An Agic-to-Flow Run Step emits the same Agic header form, while its child
   retains numbered Flow headers and existing Flow Step footers.
3. An authored Flow `run` retains its `[N]` header and facts-plus-`StepPath`
   footer; it never gains an Agic divider.
4. A completed Agic Run Step footer places unchanged aggregate facts at the
   left and `STATUS CHILD_RUN_ID` at the right, ending the identity at the
   available width.
5. The footer uses the direct child's complete Run ID and contains no owning
   Agic `StepPath`.
6. Successful, failed, and canceled footers apply status style independently
   from dim facts, leaders, and Run identity.
7. A pre-acceptance failure shows its causal error, closes with right-aligned
   `failed`, and emits neither a child ID nor a fallback `StepPath`.
8. Wide and narrow Unicode-aware output preserves every field, uses a minimum
   three-hyphen leader when possible, wraps at fact boundaries, and never
   exceeds the available display-cell width.
9. Exactly one blank row follows the header, precedes the footer, and separates
   the completed footer from the next parent Step or root footer, without
   stacking child-owned trailing gaps.
10. Nested dynamic calls retain event order, pair each footer with its direct
    child Run ID, and add no recursive indentation.
11. Tool-call-only Model output and the returned internal protocol result add no
    duplicate visible rows or raw executor action names.
12. Compact parallel lanes remain one physical row per lane and gain no
    multi-line dynamic divider.
13. The root `∎ ROOT_RUN_ID STATUS ... FACTS` footer remains byte-for-byte
    unchanged for equivalent inputs.
14. Script TTY, Script non-TTY, and Chat produce equivalent finalized semantic
    content subject only to ANSI and width mechanics.
15. The default offline verification suite passes.

## Risks

- The runnable-call feature and this presenter may be implemented concurrently;
  they must share one typed origin discriminator instead of teaching progress
  to infer origin from tool names or child runnable kinds.
- The footer cannot know its right identity until the direct child `RunBegin`;
  state must associate that event with the owning Step without querying durable
  storage.
- Hyphen leaders add width-dependent output to non-TTY progress. Tests must
  distinguish semantic fields from padding while still verifying exact layout
  where width is the behavior under test.
- Child Flow Steps already own trailing blank rows. Gap coalescing must occur at
  the boundary owner rather than by removing authored Markdown spacing.
- A long status-plus-ID field can dominate a narrow terminal; folding must
  preserve the complete ID instead of truncating execution identity.

## Open Questions

None. The human approved the final grammar on 2026-08-28.
