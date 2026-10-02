# Script Run Context

## Status, goal, and success criteria

Approved for implementation on 2026-10-02. The human confirmed the presentation
decisions below and requested implementation and a pull request.

Show the root runnable and resolved run-level model configuration when Script
execution starts. Users can identify the run before its first output and retain
that context in terminal scrollback and redirected progress logs.

## Verified current behavior

- Chat's `RunControlBlock` shows runnable, request model, and reasoning. Its
  foreground `▮` marker is bright cyan, on the input surface background.
- Script's `ScriptRunPresenter._begin_root` creates a `RunBlock` without
  printing it. Projected Steps are followed by a root footer on `RunEnd`.
- Script progress uses stderr by default for both TTY and non-TTY streams;
  `-q` suppresses it. Non-TTY progress is append-only and ANSI-free.
- `RunBegin` contains the runnable but no model. Local `resolve_spec` supplies
  `spec.model_request`; remote execution constructs a concrete `RunRequest`
  before dispatch. Presentation can receive this context without new events.

## Proposed presentation

Print one stable context header immediately before the root's first projected
Step, triggered by its `RunBegin`:

```text
‣ agic:review                   deepseek/deepseek-chat · auto

• Review output.

▪︎ run_one succeeded               2s · 1 model · ↑340 ↓86
```

The model and metrics above are illustrative, not a command invocation.

- Use `‣` to indicate the start of execution. Keep the entire header dim,
  including marker, runnable, model, reasoning, and separators, on the
  terminal's default background. Add no accent color or bold text.
- Anchor `‣ RUNNABLE` at column zero, with one space after the marker. Anchor
  `MODEL · REASONING` at the right edge of the available progress width, using
  at least two spaces between the groups. There is no dot between the left and
  right groups. This follows the existing root footer's two-ended alignment.
  Add no box, filled background, horizontal rule, input echo, timer, or new
  Run ID. The existing footer retains identity and metrics.
- Keep one blank line between the header and subsequent progress; coordinate
  with existing first-Step spacing to avoid duplicate gaps. An empty run keeps
  one blank line between its header and footer. Add no leading blank line.
- Print once and retain the header in scrollback. Do not put it in Rich Live,
  repaint it on resize, or repeat it at completion.
- Use the existing configured maximum progress width, capped by TTY width;
  align to this content edge, not a wider terminal edge. If both groups cannot
  fit with the two-space minimum gap, render the runnable first, then the
  model/reasoning group on continuation lines, left-aligned two cells from the
  left, matching the footer's narrow layout. Wrap by display cells, preferring
  field boundaries; omit the separator when reasoning moves to its own line.
  Hard-wrap a field that cannot fit; preserve its text rather than ellipsizing
  model or runnable identities.
  At widths of one or two cells, drop marker and indentation to preserve a
  usable text area. Escape control characters and render values as literal text.
- Non-TTY uses the same marker, fields, order, and wrapping, without ANSI or
  cursor movement. Quiet mode prints no header; stdout remains unchanged.

A narrow layout can therefore look like:

```text
‣ agic:review
  deepseek/deepseek-chat · auto
```

## Context semantics and lifecycle

- The header describes the root run's initial configuration, not the active
  descendant or a guarantee that all model calls use that configuration.
  Keep existing nested Run and Step presentation unchanged.
- Use the root `RunBegin.runnable` and the same compact runnable display as
  Chat, including `agic:_` for an unnamed entry. Do not expose generated entry
  identities. If the event's runnable is empty, use the resolved submission
  runnable; if neither exists, display `runnable unspecified`.
- Pass the concrete local `spec.model_request` or remote `request.model` into
  the Script presenter before dispatch. Do not display the raw CLI selector,
  infer the model from the first model Step, or perform store/network reads in
  event callbacks.
- Show the resolved model ref and same reasoning display semantics as Chat,
  falling back to `auto` when a model exists without explicit reasoning.
  As in Chat, `auto` describes an unspecified reasoning setting; it does not
  claim provider-reported effort. With no model request, show
  `model unspecified` and omit reasoning, including for a model-free Flow.
- Create the context before starting execution, but emit only on root
  `RunBegin`. Validation, preparation, or submission failure before that event
  produces no header. A root failure or cancellation after it retains the
  header and existing terminal footer/error behavior.
- Limit this change to Script invocation. Never display this header in Chat
  TUI; its existing run control bar remains unchanged. Keep the header in the
  Script presenter, outside the shared `ProgressProjector`. Retry/rerun and
  presenter callers without supplied context retain their existing output.
  Nested `RunBegin` events never create this header.

## Scope and implementation touchpoints

- `src/toolang/cli/toolang/commands/script.py`: supply local and remote resolved
  context before dispatch, preserving quiet mode and cleanup.
- `src/toolang/cli/common/script_progress/presenter.py`: accept optional
  context and emit the header at root start.
- `src/toolang/cli/common/script_progress/blocks.py`: own context rendering,
  spacing, and width behavior through the existing `ProgressConsole`.
- `tests/unit/cli/test_script_run_presenter.py` and
  `tests/unit/cli/test_script_command.py`: renderer and context wiring coverage.
- `tests/integration/cli/test_script_local.py` and
  `tests/integration/cli/test_script_remote_execution.py`: offline parity and
  stream behavior.
- `docs/execution-presentation.md`: document the Script-owned root context
  exception to the shared projector's no-Run-header contract.

No new flags, dependencies, persisted fields, execution events, provider calls,
per-Step model labels, or changes to result saving and exit codes.

## Acceptance tests

1. A root header appears exactly once before Step output; nested runs and
   completion do not repeat it. Empty, failed, and canceled runs retain correct
   spacing and their existing footer semantics.
2. Local and remote paths display the resolved model and runnable after CLI,
   session, and input overrides, including remote model selector materialization.
   Local invocation retains its existing exact-model-ref requirement.
3. Named and unnamed runnables, explicit reasoning, automatic reasoning, and
   absent model requests follow the field rules. Child model changes do not
   rewrite the root snapshot.
4. TTY styling keeps the entire header dim on the default background. The
   runnable starts at the left edge and model/reasoning ends at the progress
   width, separated by at least two spaces. Non-TTY content is equivalent after
   style removal and contains no ANSI or cursor controls.
5. Long refs, wide Unicode, literal markup, control characters, and very narrow
   widths fit the width budget without silently dropping identity text. Cover
   the exact two-space threshold and stacked fallback, and verify marker width.
6. Quiet mode and errors before `RunBegin` emit no header. stdout, result
   saving, exit codes, and presenter callers without context are unchanged.
   Chat TUI emits no Script context header and retains its existing control bar.
7. Run the repository's default Ruff, formatting, ty, and offline pytest checks
   before implementation commits. Definition-only verification checks source
   accuracy, referenced paths, and `git diff --check`.

## Tradeoffs, risks, and open questions

A stable header remains useful in logs, but can scroll off-screen during long
runs. A pinned bar would require another live surface and would not give
non-TTY logs the same persistent context. Wrapping costs extra lines on narrow
terminals in exchange for retaining complete model identity.

The main semantic risk is confusing the root model setting with every model
used by a Flow. Keep the documented snapshot meaning and do not label it as
the currently executing model.

The human selected `‣`, an entirely dim header, alignment at both ends, and
Script-only visibility with no new header in Chat TUI, then approved
implementation. No open questions remain.
