# Chat TUI Status And Summary Presentation

## Goal

Make Chat TUI run state compact, predictable, and consistent with script
execution without weakening the conversation-first layout. Accepted run IDs
leave the submitted-message bar, the fixed status row keeps only session-level
context, and terminal chat summaries use the same lifecycle vocabulary, result
states, metric meanings, ordering, and value formatting as the script summary.

Implementation starts only after this definition is approved.


## Success Criteria

- The submitted-message bar never adds a run ID after acceptance, while the
  terminal root summary retains the root run ID beside its lifecycle status.
- The fixed status row never shows a run ID, thread ID, or run lifecycle state;
  it has a deterministic content priority and never exceeds its render width.
- Running, canceling, successful, failed, and canceled chats each have one
  defined presentation with no duplicated result, error, or lifecycle text.
- Chat and script use the same terminal status words, result-state meanings,
  metric meanings, metric order, partial-data rules, and value formatting.
- Chat-specific differences are limited to conversation placement, the compact
  unframed root summary, and the in-TUI `:show` action for a result that is not
  already rendered inline.
- Every physical output line fits the measured terminal-cell width at the
  40-column normal boundary, the 20-column compact boundary, and the emergency
  width below 20 columns.
- Literal snapshots cover status-row priority, all root lifecycle outcomes,
  partial and unavailable metrics, wide Unicode, and an interactive resize.
- Existing commands, persistence, event schemas, non-interactive fallback, and
  the Chat TUI input, queue, steering, and interrupt behavior remain compatible.


## Current Behavior

The Chat TUI has two different bar-like surfaces whose current behavior makes
the original request ambiguous. This definition covers both explicitly.

`RunStartBlock` is the full-width submitted-message control. Before acceptance,
it contains the submitted message and an empty lower padding row. On root
`RunBegin`, the implementation inserts the accepted run ID into that lower row
and finalizes the block into scrollback. A rejected submission instead keeps
the control free of a run ID and appends a submission diagnostic.

`StatusBar` is the fixed one-row surface below the input box. Its normal state
contains the resolved or selected model, an explicit non-default `agic:` or
`flow:` selection, and right-aligned shortcut hints. A transient editor or
control error replaces the complete row. The fixed row already has no run ID,
but its renderer measures with `shutil.get_terminal_size`, assembles every
normal segment before padding, and does not clip an overlong normal or error
line to the actual prompt-toolkit render target.

Root lifecycle presentation is owned by `RunStopBlock`:

- while a root is running, the block renders only a blank line and other live
  activity must communicate that work is in progress;
- a local cancel request replaces the blank state with `canceling...`;
- success, failure, and cancellation end with one dim line shaped as
  `◆ RUN_ID STATUS · FACTS`;
- a selected failure is printed immediately before the terminal line;
- generic cancellation messages are suppressed;
- an agic's confirmed assistant output is rendered inline before the summary;
- a successful flow prints `◇ result saved · :show RUN_ID` before the summary.

Chat and script already share `status_label`, `elapsed`, count helpers, and the
`Metrics` aggregate. Durable `finished` is displayed as `succeeded`. Current
metric facts are ordered as duration, optional descendant runs, slash-form
tokens, model calls, and tool calls. Zero token values disappear, missing data
is treated like zero, partial values cannot be distinguished from complete
values, and cost is not represented. A root flow reports descendant runs while
an agic does not, even if it has descendants.

The current canonical documentation and the older Chat TUI draft both describe
the accepted run ID in the submitted-message bar. The draft also calls a
durable result `saved`, even though that action does not write a file. Both
statements must change with the implementation.


## Scope

### In Scope

- Remove the accepted run ID from the submitted-message bar's lower row.
- Define the fixed status row's content, priority, truncation, and error state.
- Define live root lifecycle feedback and terminal root completion for running,
  canceling, successful, failed, and canceled chats.
- Apply the script summary contract from issue #259 to equivalent Chat TUI
  fields and document the justified Chat-specific differences.
- Apply the terminal geometry contract from issue #260 to the status row,
  submitted-message bar, diagnostics, result action, and root summary.
- Update Chat TUI unit, pseudo-terminal, and shared semantic fixtures and the
  canonical presentation documentation.

### Out Of Scope

- Implementing or redefining the script summary, script inspect/save commands,
  or script stdout/stderr policy owned by issue #259.
- Redesigning general script progress, `let` statements, or nested error
  presentation owned by issue #260.
- Adding `HttpChatClient`, transport selection, capability negotiation, or a
  local-versus-HTTP status indicator owned by issue #262.
- Changing run, step, control, message, persistence, or HTTP event schemas.
- Adding a verbosity option, changing `:show` syntax, or adding a Chat TUI save
  command.
- Redesigning the prompt box, queue panel, start/steer semantics, slash-command
  set, transcript restoration, or non-interactive chat fallback.


## Shared Summary Contract

Issue #259 owns the reusable summary semantics. Chat must consume the same
formatter or the same terminal-independent summary value; it must not copy and
then independently evolve the rules below.

### Lifecycle Vocabulary

Terminal display status is exactly:

| Durable status | Terminal label |
| --- | --- |
| `finished` | `succeeded` |
| `failed` | `failed` |
| `canceled` | `canceled` |

`canceled` always uses one `l`. The root run ID and terminal status form one
identity field. Chat retains that field in the root summary even though the
submitted-message bar and fixed status row omit the run ID.

`running` is a live state rather than a completed summary state. `canceling`
is local transient feedback and is not a durable lifecycle label.

### Result States

The shared semantic result state distinguishes:

- a recorded result that can be resolved;
- a recorded empty result;
- no recorded result;
- an unavailable recorded reference;
- a terminal diagnostic or cancellation reason.

Partial streamed deltas never become a result. A successful agic response is
confirmed only by the root output reference. A failed or canceled run never
presents a partial model preview as its result.

Shared human wording is:

- `Result: 1 item`, `Result: N-item list`, `Result: list`, or
  `Result: available` for a recorded result, according to the resolved shape;
- `Result: empty` for a recorded result that resolves to zero message parts;
- `Result: no value returned` when a successful run has no output reference;
- `Result: unavailable` when an output reference cannot be resolved;
- `Error: MESSAGE` plus `Result: not produced` for failure;
- `Canceled: REASON` plus `Result: not produced` for cancellation, using
  `Canceled: run canceled` when the stored reason is absent or generic.

An unresolved recorded reference is not `Result: no value returned`. It uses
`Result: unavailable` and remains inspectable by the root run ID.

### Metrics

The metrics order is fixed:

```text
DURATION · RUNS · TOOL CALLS · MODEL CALLS · TOKENS · COST
```

- `RUNS` is the number of descendant runs and excludes the identified root.
  The same rule applies to agics and flows.
- Runs, tool calls, and model calls count effective invocations that began,
  whether they later succeeded, failed, or were canceled. This is equivalent
  to counting effective run/model/tool records. Normal cleanup terminates every
  begun step, so current terminal-event counting usually produces the same
  totals, but completion is not the semantic requirement.
- Exact zero call and descendant-run counts are omitted.
- Duration uses rounded milliseconds below one second, one decimal seconds
  below one minute, and `Xm YYs` at one minute or above. Invalid or missing
  timestamps omit duration.
- Token usage is `↑ INPUT ↓ OUTPUT`. Counts use base-1000 `k` and `m`, at most
  one decimal, and no trailing `.0`. A known exact zero is printed as `0`.
- Cost is summed with `Decimal` USD arithmetic. Amounts at or above one cent
  round half-up to two fractional digits. Positive sub-cent amounts retain four
  significant digits so they cannot render as zero. Exact zero is `$0.00`, and
  fractional zeroes beyond the required two are removed. Chat must call the
  shared formatter rather than define a second rounding policy.

Missing values are not zeros:

- when every contributing model call supplies a component, display its exact
  total;
- when only some contributing calls supply it, display a lower bound with `≥`
  independently for input tokens, output tokens, and cost;
- when no contributing model call supplies token usage, display
  `tokens unavailable`;
- when no contributing model call supplies cost, display `cost unavailable`;
- when there are no model calls, omit tokens and cost entirely.

Representative facts are:

```text
19.0s · 7 runs · 6 tool calls · 11 model calls · ↑ 10.3k ↓ 2.8k · $0.50
1.2s · 1 model call · ↑ ≥10.3k ↓ ≥2.8k · ≥$0.50
820ms · 1 model call · tokens unavailable · cost unavailable
```


## Chat-Specific Presentation

### Submitted-Message Bar

At widths of 20 cells or more, the submitted-message bar keeps its existing
message, full-width background, wrapping, and blank lower padding row.
`RunBegin` still finalizes the bar, but it does not insert the accepted run ID.
The before-acceptance and after-acceptance plain-text content is therefore
identical.

The blank lower row remains in normal and compact layouts because it separates
authored input from following execution output and prevents acceptance from
changing the block height. Emergency layout below 20 cells removes the empty
background rows with the other optional decoration; acceptance still does not
change the block's content or height within that tier. A pre-acceptance failure
still appends its diagnostic after the finalized input bar and does not
fabricate a run identity or terminal summary.

The run ID remains discoverable in the terminal root summary and durable
inspection. Removing it from the input bar does not remove or alter any stored
identity.

### Fixed Status Row

The fixed row is session and editor context, not a run-progress surface. It
must never show a run ID, thread ID, lifecycle status, elapsed time, or metrics.
Run lifecycle belongs to the live execution area and finalized scrollback.

Normal segments have this descending priority:

1. the actual resolved model, falling back to the selected model label;
2. an explicit non-default `agic:NAME` or `flow:NAME`;
3. `^d exit`;
4. `^j newline`;
5. `↑↓ history`.

The model and runnable are left-aligned. Included hints are right-aligned in
their listed order. A segment is removed whole, starting at the lowest
priority, until the remaining segments fit with at least two cells between the
left and right groups. Only the highest-priority remaining text is truncated,
with one cell-width ellipsis, if it cannot fit by itself. The row is padded to
exactly the target width.

A transient editor or control error replaces every normal segment. It begins
with `! `, occupies one physical row, and truncates with one cell-width
ellipsis when necessary. Editing input clears the error and restores the
normal row. Error text is presentation feedback only; it never becomes a run
failure or root diagnostic.

Issue #262 may later add a client/transport segment. That issue must place the
new segment into this priority model explicitly; this definition neither
reserves text nor chooses its label.

### Running And Canceling

One accepted root has at most one live lifecycle tail:

- after `RunBegin` and before a visible owned activity exists, show
  `· running…` without a run ID;
- while a model, tool, statement, or lane already communicates active work,
  suppress the redundant root tail;
- after the first successful local cancel request, show `· canceling…` until
  terminal `RunEnd` or a cancel-request error;
- if the cancel request fails, restore the prior running/activity state and
  place `cancel failed: MESSAGE` in the fixed error row;
- repeated cancel input while the request is pending does not create another
  line or request.

No `◆` root summary is finalized before `RunEnd`. Submission errors that occur
before `RunBegin` remain submission diagnostics and have no running or terminal
root state.

### Terminal Completion

Chat preserves conversation order: primary result, diagnostic, or result
action is finalized first; the root summary follows it. Within the summary,
identity/status precedes the metrics line, and metric facts retain the shared
order.

The unframed summary is:

```text
◆ RUN_ID STATUS
  DURATION · RUNS · TOOL CALLS · MODEL CALLS · TOKENS · COST
```

The metrics line is omitted when it has no available facts. `◆` and the
identity are dim; status color may reinforce meaning but never replaces the
status word. Chat does not copy script's equal-width frame because the submitted
message and conversation result already provide the visual boundary.

For a successful agic with a renderable confirmed assistant response, the
response remains the primary Markdown block and is emitted exactly once. The
shape description is not repeated because the visible response is the result.

```text
The race occurs because both workers update the same pending entry.

◆ run_abc succeeded
  19.0s · 6 tool calls · 11 model calls · ↑ 10.3k ↓ 2.8k · $0.50
```

For a successful flow, recorded empty output, structured output that is not
rendered inline, or another recorded result that needs reopening, Chat combines
the shared result description with its short local action. The current wording
`result saved` is removed because no file is written.

```text
◇ Result: 32-item list · :show run_abc

◆ run_abc succeeded
  58.0s · 32 runs · 39 model calls · ↑ 39.8k ↓ 7.2k · $0.24
```

A successful run with no recorded output says so before the summary:

```text
· Result: no value returned

◆ run_abc succeeded
  2.1s
```

A failed run shows the selected actionable error once. If a child or statement
already owns the same diagnostic, the root omits the duplicate error but keeps
the result state and summary.

```text
! Error: No configured model matched the active ceiling.
· Result: not produced

◆ run_abc failed
  1.2s · 1 model call · tokens unavailable · cost unavailable
```

Cancellation is a lifecycle outcome, not automatically a diagnostic. A
meaningful independent reason is retained; otherwise the generic reason is
normalized to `Canceled: run canceled`.

```text
· Canceled: run canceled
· Result: not produced

◆ run_abc canceled
  4.1s · 2 runs · 3 tool calls
```

The root run ID makes the durable run copyable for inspection. Routine Chat
completion does not print script's full `toolang ... inspect` or shell
redirection commands, because `:show RUN_ID` is the local result action and the
conversation should remain primary. Chat never auto-saves a result.


## Terminal Geometry

This feature adopts the geometry contract coordinated with issue #260.

- Measure the actual render target in terminal cells, including wide and
  combining Unicode. Do not use Python character count.
- The prompt-toolkit application supplies its current output width to live
  blocks and the fixed row. Finalized Rich output uses the current terminal
  width at its render transaction. Environment fallback is resolved at the
  orchestration call site, not inside semantic blocks.
- Re-read interactive width before every render transaction or live refresh.
- A resize clears the old live region using its recorded old geometry, then
  redraws only mutable content at the new width. It never rewrites finalized
  scrollback.
- Stable prose and diagnostics wrap at cell boundaries with continuation text
  aligned beneath semantic content. Break an overlong token only when it cannot
  fit on an empty content row.
- The fixed status row and single-row mutable previews truncate with one
  terminal-cell ellipsis. Stable root identities, statuses, and metric facts
  wrap instead of abbreviating a durable ID.
- No fixed 80-, 100-, or 180-character pre-cap may prevent content from using
  a wider available row.

Width tiers are:

| Width | Behavior |
| --- | --- |
| 40 or more cells | Normal layout: current Chat decoration and indentation; summary facts wrap at fact boundaries with a two-cell hanging indent. |
| 20 through 39 cells | Compact layout: keep every semantic fact, reduce optional decoration and indentation, and stack summary facts when needed. |
| Fewer than 20 cells | Emergency layout: remove decoration, hard-wrap stable content, and never pretend the target is wider. |

The fixed row remains one physical row in every tier. At emergency width it
shows only a truncated error or model label, without shortcut hints or leading
decoration. A root identity is never abbreviated: identity and status may wrap
onto separate compact or emergency lines only when they cannot fit together.

Non-interactive Chat behavior is unchanged. The script-specific deterministic
100-column non-TTY policy belongs to #260 and must not leak into the interactive
TUI.


## Acceptance Snapshots

Status-row snapshots use `·` only in this document to make padding cells
visible; the application renders ordinary spaces. The fixtures use
`openai/gpt-5` and `flow:research`.

```text
width 80
··openai/gpt-5··flow:research··················^d·exit··^j·newline··↑↓·history··

width 40
··openai/gpt-5··flow:research····^d·exit

width 20
··openai/gpt-5······

width 12 emergency
openai/gpt-5
```

The 80-, 40-, and 20-cell lines above contain exactly their declared number of
terminal cells. At width 40, newline and history hints have been removed whole;
at width 20, only the model remains. A 40-cell error snapshot contains the full
`! Model selector matched no models` plus padding. At width 20 it truncates to
one row with an ellipsis. The 12-cell model label fits exactly and therefore has
no ellipsis; a separate overflowing-model fixture verifies emergency truncation
only when the label exceeds the available cells.

The accepted submitted-message snapshot has no ID in its lower padding row:

```text
<full-width neutral row>
> Explain the failure and propose a fix.
<full-width empty neutral row>
```

The literal wide terminal root snapshot is:

```text
◆ run_abc succeeded
  19.0s · 7 runs · 6 tool calls · 11 model calls · ↑ 10.3k ↓ 2.8k · $0.50
```

At the 40-cell normal boundary, facts wrap only between complete facts:

```text
◆ run_abc succeeded
  19.0s · 7 runs · 6 tool calls
  11 model calls · ↑ 10.3k ↓ 2.8k
  $0.50
```

At the 20-cell compact boundary, facts stack without disappearing:

```text
◆ run_abc succeeded
  19.0s
  7 runs
  6 tool calls
  11 model calls
  ↑ 10.3k ↓ 2.8k
  $0.50
```

A focused 12-cell emergency snapshot removes the marker and decoration and
hard-wraps otherwise indivisible labels:

```text
run_abc
succeeded
19.0s
7 runs
6 tool calls
11 model
calls
↑ 10.3k
↓ 2.8k
$0.50
```

Additional literal snapshots cover success with inline Markdown, success with
`:show`, success without a result, failure with and without an already-owned
diagnostic, generic and meaningful cancellation, exact/partial/unavailable
tokens and cost, zero usage, and an invalid timestamp. Fixtures include CJK,
emoji, and combining text in model labels, diagnostics, and result prose.


## Compatibility Constraints

- Keep the existing Chat command placement, input syntax, quick commands,
  queueing, steering, cancellation keys, model selection, and application mode.
- Keep `:show [run_id]` and durable result lookup behavior. Change only the
  completion content from `result saved` to the shared `Result: ...` wording.
- Keep the root run ID in the terminal summary and preserve exact durable IDs;
  narrow layouts wrap identities rather than truncating or abbreviating them.
- Keep pre-acceptance errors free of fabricated run identity and lifecycle.
- Keep failure ownership and normalized-diagnostic de-duplication.
- Keep the default test suite offline and deterministic. Live-provider tests
  remain opt-in and are not required to prove formatting.
- Do not change execution events, stores, HTTP schemas, or introduce a database
  migration.
- Do not change script output as part of #261. Shared formatter extraction must
  preserve script snapshots until the separately approved #259 and #260
  implementations intentionally update them.
- Color and background may reinforce information on a TTY, but plain text and
  normalized snapshots must remain complete without ANSI styling.


## Design Touchpoints And Likely Files

- `src/toolang/cli/common/execution_progress/state.py`
  - represent exact, partial, and unavailable token/cost components;
  - keep effective begun descendant-run and call counting surface-neutral.
- `src/toolang/cli/common/execution_progress/formatting.py`
  - expose the shared lifecycle, result, metrics ordering, arrows, lower-bound,
    unavailable-value, and cost formatters owned by #259;
  - make width-independent semantic formatting pure.
- `src/toolang/cli/toolang/commands/chat/blocks.py`
  - remove the run ID from `RunStartBlock`;
  - render running/canceling state, shared result wording, and the two-line
    terminal summary without duplicating diagnostics.
- `src/toolang/cli/toolang/commands/chat/presenter.py`
  - build one root result/diagnostic/action followed by one root summary;
  - preserve root output confirmation, flow result handling, metric ownership,
    and failure de-duplication.
- `src/toolang/cli/toolang/commands/chat/widgets.py`
  - apply fixed-row content priority, terminal-cell sizing, whole-segment
    omission, error replacement, padding, and ellipsis truncation.
- `src/toolang/cli/toolang/commands/chat/tui.py` and `rendering.py`
  - pass actual render-target dimensions at orchestration boundaries;
  - retain old live geometry across resize cleanup and redraw only mutable
    blocks.
- `tests/unit/cli/test_chat_tui.py`
  - assert semantic block order, lifecycle variants, status-row priority,
    metrics parity, and literal width-tier snapshots.
- `tests/system/cli/test_chat_tui_e2e.py` and Chat TUI support fixtures
  - assert pseudo-terminal line widths, accepted input without a run ID,
    result actions, scrollback stability, and 80-to-32-to-120 resize behavior.
- `docs/chat.md`, `docs/execution-presentation.md`, and
  `docs/chat-tui-execution-presentation-draft.md`
  - remove the accepted input-bar run ID, replace `result saved` with shared
    result wording, and make the canonical/shared versus Chat-specific summary
    decisions consistent.

If #259 or #260 lands a shared semantic or geometry helper first, #261 reuses
it. If #261 lands first, common extraction must be behavior-preserving for the
script surface and must not preempt the separately approved script changes.


## Implementation Sequence

1. Add shared summary value tests for lifecycle mapping, result state, metric
   order, descendant-run meaning, exact/partial/unavailable usage, and cost.
2. Extract or extend pure common formatting/state only as required by those
   fixtures, preserving existing script output until its owning issue changes
   it.
3. Remove accepted run-ID rendering from `RunStartBlock` and add before/after
   acceptance and pre-acceptance-error snapshots.
4. Refactor the fixed status row to receive target width and apply the specified
   segment priority, error replacement, Unicode cell measurement, and tiers.
5. Adapt root presentation to the defined live running/canceling states and the
   result-first, two-line terminal summary for every lifecycle.
6. Pass current and old live geometry through the TUI render transaction so a
   resize clears and redraws only mutable content.
7. Add unit and pseudo-terminal snapshots at 120, 40, 20, and one width below
   20, plus CJK/emoji/combining text and 80-to-32-to-120 resize coverage.
8. Reconcile the canonical presentation docs and older draft, then run the full
   repository verification.


## Acceptance Tests

1. **Submitted-message identity**
   - Render before `RunBegin`, after root `RunBegin`, and after a pre-acceptance
     failure.
   - Assert the first two have identical plain-text message and padding rows,
     no accepted run ID, and stable height.
   - Assert the failure has no fabricated root summary.

2. **Fixed-row priority**
   - Snapshot exact 80-, 40-, 20-, and 12-cell rows with model, explicit flow,
     and all hints available.
   - Assert lower-priority segments disappear whole, the last retained segment
     truncates with a one-cell ellipsis only when necessary, padding fills the
     target, and no row contains a run/thread identity or lifecycle.
   - Assert actual resolved model replaces the configured fallback and default
     agic remains hidden.

3. **Fixed-row errors**
   - Assert editor, steer, cancel, and interrupt errors replace normal content,
     fit on one row, truncate by terminal cells, and clear on input.
   - Assert these messages do not become root diagnostics.

4. **Running and cancellation request**
   - Assert `· running…` appears only when no more specific activity is visible.
   - Assert a successful cancel request changes it once to `· canceling…`.
   - Assert a failed request restores live state and uses the fixed error row,
     and a repeated pending request emits nothing new.

5. **Successful completion**
   - Cover confirmed inline Markdown, recorded structured/empty result with
     `:show`, absent result, and unavailable result reference.
   - Assert output is emitted once, the result action never claims a save, and
     the summary retains root ID plus `succeeded`.

6. **Failed and canceled completion**
   - Cover a root-owned failure, a diagnostic already owned by a child, generic
     cancellation, meaningful cancellation reason, and partial completed work.
   - Assert one selected diagnostic at most, explicit result state, exact
     `failed`/`canceled` vocabulary, no `succeeded`, and no partial delta result.

7. **Shared metrics parity**
   - Feed identical terminal run/step fixtures to script summary semantics and
     Chat presentation.
   - Assert descendant runs exclude the root; effective calls that began are
     counted across success, failure, and cancellation; field order and
     duration, arrow-token, lower-bound,
     unavailable, and cost formatting match exactly.
   - Cover zero calls, exact zero usage, partial input only, partial output only,
     partial cost, no usage from any model call, and invalid timestamps.

8. **Width tiers and Unicode**
   - Assert every physical line is at most its injected cell width at 120, 40,
     20, and 12 cells.
   - Cover CJK, emoji, combining characters, and a token wider than an empty
     content row.
   - Assert normal wrapping preserves fact order, compact mode loses no facts,
     and emergency mode removes decoration and hard-wraps.

9. **Resize and progressive finalization**
   - Run an 80-to-32-to-120 pseudo-terminal scenario.
   - Assert the old live geometry is cleared, finalized scrollback bytes do not
     change, only mutable blocks redraw, and the final summary uses the last
     measured width.

10. **Regression and repository verification**
    - Retain queue, steer, slash, interrupt, restored-history, long-output, flow
      `:show`, and non-interactive fallback coverage.
    - Run `uv run ruff check .`.
    - Run `uv run ruff format --check .`.
    - Run `uv run ty check`.
    - Run `uv run pytest`.


## Risks And Mitigations

- **“Bottom bar” can refer to two surfaces.** This definition removes the ID
  from the submitted-message footer and explicitly forbids identities in the
  fixed row, while retaining the root ID in the terminal summary.
- **Parallel feature definitions can drift.** #259 owns summary semantics and
  #260 owns geometry. Shared semantic fixtures and width-tier snapshots make
  #261 consume those decisions rather than restate different implementations.
- **Missing metrics can look like zero.** Carry per-component completeness and
  render exact, lower-bound, and unavailable states explicitly.
- **A conversation-first layout can hide durable identity.** Keep `RUN_ID +
  status` in every terminal root summary and keep `:show RUN_ID` for a result
  that is not inline.
- **Narrow rows can lose the important state.** Remove complete low-priority
  status segments first, keep identity/status before metric payload, and use a
  defined compact or emergency layout instead of an invented wider fallback.
- **Unicode width bugs can corrupt prompt-toolkit redraws.** Measure terminal
  cells, record old live geometry, and test wide and combining characters in
  both snapshots and resize sequences.
- **Failure text can be repeated at step, statement, and root boundaries.** Keep
  the existing normalized diagnostic ownership set and test already-owned root
  errors.
- **Calling a result “saved” implies a side effect.** Use the shared
  `Result: ...` description and state explicitly that neither completion nor
  `:show` writes a file.
- **Common extraction can accidentally change script output.** Preserve current
  script snapshots in #261 and allow only the separately approved #259/#260
  implementations to change their surface output.


## Open Questions

None. This definition resolves the two bar surfaces, identity placement,
session-row priority, all root lifecycle and result states, shared metric
semantics and ordering, Chat-specific differences, terminal width tiers,
snapshots, compatibility, implementation touchpoints, and acceptance coverage.
