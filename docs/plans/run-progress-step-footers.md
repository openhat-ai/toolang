# Define Run Progress Footers

## Work Type

Feature definition. This document defines small presentation changes for root
Run footers and Flow Step facts. It does not implement product code.

## Verified Current Behavior

- Script and Chat share `ProgressProjector`, `ProgressRow`, and the Rich
  renderers under `cli/common/execution_progress`.
- A root Run footer is framed as `[ RUN_ID STATUS · FACTS ]`. Narrow output uses
  a two-cell hanging indent and moves the closing bracket to the final line.
- A Flow Step with aggregate facts emits an unmarked, dim row with a two-cell
  indent. The canonical `StepPath` is currently omitted.
- The configured progress maximum and the attached terminal determine the
  available width. Non-TTY Script output uses the configured maximum.
- Model Part and Step finalization do not append a blank row. For a direct Flow
  `run`, the child Model output and the owning Flow facts are committed as
  separate blocks; the latter currently receives `gap_before=True`. That
  section gap causes the observed empty row before the facts.
- Pull request #309 contains the same root-cause spacing fix for direct Flow
  `run` facts, but is not merged at the time of this definition.

## Goal

Make terminal progress closures quieter and more identifiable: use an
end-of-proof marker for the root Run, turn existing Flow facts into a two-ended
Step footer, and attach that footer directly to preceding child Model output.

## Success Criteria

- Root Run footers begin with `∎ ` (U+220E END OF PROOF) and use no square
  brackets.
- Existing Flow facts rows also show their complete canonical `StepPath`, with
  facts at the left and the path at the right of the available progress width.
- Step footers retain the existing two-cell indentation, maximum-width policy,
  facts, visibility rules, and dim styling.
- A direct Flow `run` Step footer immediately follows its child Model output
  without a presentation-owned blank row.
- Script and Chat render the same finalized content and geometry.

## Scope

### In Scope

- Shared root Run footer framing, wrapping, and related expectations.
- Shared Flow Step footer projection and width-aware rendering.
- Removal of the extra section gap before a direct Flow `run` Step footer.
- Current progress documentation and Script, Chat, and projector tests.

### Out of Scope

- Execution events, records, status, accounting, or lifecycle behavior.
- Adding facts or footer rows to Model, Tool, or direct-value Steps.
- Changing which Flow Steps qualify for aggregate facts.
- Changing fact labels, ordering, token or cost formatting, or aggregation.
- Parallel-lane identity or layout.
- Statement headers, Tool detail surfaces, reopened-result dividers, or Chat
  control bars.
- Removing authored Markdown spacing from Model output.

## Presentation Design

### Root Run Footer

The stable wide form is:

```text
∎ run_nrqpt0mf succeeded · 1m 16s · 26 runs · 32 model calls · 8 tool calls · ↑43.8k ↓17.6k $0.01
```

Failed, canceled, retry, and rerun captions keep their current wording and
status style. `∎ ` replaces the opening `[ ` and the closing ` ]` is removed.
The marker begins in column zero. Wrapped lines use the marker's two-cell width
as their hanging indent. Facts continue to wrap only at fact boundaries when
possible, and every physical line remains within the available progress width.

### Flow Step Footer

An existing facts row becomes a Step footer:

```text
• Mapped all 6 items in parallel
  31.0s · 6 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00        run_root.2
```

- The footer begins with the existing two spaces.
- The left field is the existing joined facts string without content changes.
- The right field is `str(StepEnd.step)`, preserving the complete canonical
  run-qualified `StepPath`.
- When both fields fit, insert at least two spaces between them and pad that
  gap so the path ends at the available width.
- When they do not fit together, wrap the facts with the existing two-cell
  hanging indent, then render the path on a separate right-aligned continuation
  line. If the path alone is wider than the content width, fold it by display
  cells without truncating it or exceeding the width.
- Both fields retain the current dim facts style. Padding adds no new color or
  emphasis.
- The footer exists only when the current projector would emit Flow facts. A
  path is not emitted by itself when aggregate facts are absent.
- The width is `min(surface width, TOOLANG_PROGRESS_MAX_WIDTH)`, matching other
  progress rows. Display-cell width, not Python string length, controls padding,
  wrapping, and alignment.

The semantic progress row must carry the right-hand path independently of its
left text. The terminal-independent projector must not pre-pad for a guessed
terminal width; Script and Chat perform the same width-aware rendering through
their shared renderer.

### Spacing

The Step footer is part of its owning Flow Step rather than a new section. It
immediately follows the Step's last visible output row. This includes a direct
Flow `run`, where the visible output belongs to a child Model Step committed in
an earlier block. No `gap_before` is added for that wrapper footer.

The existing trailing blank row after a Flow Step remains the separator before
the next Step or root Run footer. Authored Markdown paragraph spacing remains
unchanged.

## Design Touchpoints

- `src/toolang/cli/common/execution_progress/types.py`: represent an optional
  right-aligned field on a semantic progress row without terminal-width data.
- `src/toolang/cli/common/execution_progress/projector.py`: project Flow facts
  with the owning canonical StepPath and treat direct Flow `run` facts as a
  continuation of child output.
- `src/toolang/cli/common/execution_progress/rich_rendering.py`: render
  two-ended Step footers by display-cell width and replace bracketed Run framing
  with `∎`.
- `tests/unit/cli/test_execution_progress_projector.py`: verify semantic footer
  content, path ownership, and direct Flow `run` gap policy.
- `tests/unit/cli/test_script_run_presenter.py` and
  `tests/unit/cli/test_chat_tui.py`: verify shared wide, narrow, Unicode,
  spacing, style, status, and retry/rerun output.
- `tests/integration/cli/test_script_local.py`,
  `tests/integration/cli/test_local_core_commands.py`, and
  `tests/system/cli/test_chat_tui_e2e.py`: replace bracket-specific root,
  retry, rerun, and PTY footer assertions.
- `docs/execution-presentation.md` and `docs/execution-transcript.md`: update the
  normative grammar and compatibility example.

## Acceptance Tests

1. Successful, failed, and canceled root footers begin with exactly `∎ `, contain
   no framing square brackets, preserve status styling, and remain within the
   configured width.
2. Retry and rerun footers retain their operation caption and facts after the
   new marker.
3. A facts-bearing Flow Step projects the unchanged facts as its left field and
   its complete canonical StepPath as its right field.
4. At a wide configured width, the footer uses two leading spaces, at least two
   separating spaces, and a path whose final cell is the width's final cell.
5. At a narrow width, facts and long Unicode-aware StepPaths wrap or fold
   without truncation, collision, or width overflow.
6. Flow Steps without aggregate facts, and all Model, Tool, and direct-value
   Steps, gain no new footer row.
7. A direct Flow `run` with child Model output renders the Step footer on the
   immediately following physical row, with no extra blank row from Model Part
   closure, Model Step closure, or wrapper Step closure.
8. Exactly one trailing blank row still separates that completed Flow Step from
   the next Step or root footer.
9. Script TTY, Script non-TTY, and Chat produce equivalent finalized footer
   content subject only to ANSI and terminal-width mechanics.
10. The default offline verification suite passes.

## Risks

- Right alignment introduces width-dependent padding into non-TTY output; tests
  must compare semantic content deliberately where exact width is irrelevant.
- StepPaths can contain enough indices to exceed a narrow terminal; folding
  must preserve the canonical value rather than silently truncating identity.
- Pull request #309 overlaps the direct Flow `run` gap fix. Implementation must
  either reuse it if merged or include the equivalent root-cause change once;
  it must not stack a second spacing workaround in Model finalization.
- Adding a second semantic field to `ProgressRow` must not affect Markdown,
  Tool surfaces, live parallel rows, or rows without that field.

## Open Questions

None. Implementation requires explicit human approval of this definition.
