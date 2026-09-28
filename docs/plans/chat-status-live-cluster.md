# Define Chat Status Live Cluster

## Work Type and Status

Feature definition approved for implementation on 2026-09-28. The approved
narrow-layout rule is that session edge labels yield before the centered
identity. Context-usage rendering is deferred; reserve its place without
resolving or displaying usage data.

## Verified Current Behavior

- Session runnable and model remain at the status bar's left and right edges.
- The center currently renders agent, workspace, and (while active) `running`
  or elapsed time as one dim, centered string separated by ` · `.
- Because the complete string is centered, changing the active suffix changes
  the position of the agent/workspace text. Context-window usage is not shown.

## Goal and Success Criteria

Keep session settings anchored at both ends and make the center a stable live
cluster. The feature succeeds when:

- `agent @ workspace` remains centered on the status line as its run state and
  elapsed-time width change;
- elapsed time occupies a compact slot to the anchor's left;
- a context-usage slot is reserved to the anchor's right but remains empty and
  invisible for this change; and
- the session runnable/model retain their existing edge alignment and
  presentation.

## Presentation and State Rules

Use two spaces between populated cluster parts:

```text
1m30s  hak @ toolang
```

- Keep the left session runnable and right session model/effort unchanged.
- Center `agent @ workspace` by its display-cell midpoint at the absolute
  status-line center. Render the agent and workspace at normal intensity and
  dim only the structural `@`.
- Position elapsed as a right-aligned slot immediately left of the center
  anchor. While running, retain the existing `running` label below one second
  and show compact whole-second elapsed time thereafter. While idle, render no
  elapsed text but preserve its slot anchor so run transitions and duration
  digit changes cannot move `agent @ workspace`.
- Keep an optional context-usage slot immediately right of the center anchor,
  available for a future change. For this scope it has no value, consumes no
  model metadata or events, and renders neither text, placeholder, separator,
  nor visible padding.
- Preserve the current effective-workspace rules: use the session workspace
  while idle and the active root's workspace while running. Do not show a
  workdir path or active runnable in the center.
- Fit by display-cell width. If the full line overflows, keep the complete
  center label fixed and maintain a two-cell margin on each side. Truncate the
  left session label from its inner (right) edge and the right session label
  from its inner (left) edge, using one `…` cell at each cut. The elapsed slot
  remains two cells left of the center; omit elapsed before changing the center
  or breaking margins when it cannot fit.
- If the available status width is narrower than the center label plus both
  margins, hide elapsed and both session labels and render the center label
  alone. If the center label itself is wider than the status area, truncate it
  to that area with one trailing `…`. Existing error rendering is unchanged.

## Scope and Touchpoints

In scope:

- `src/toolang/cli/toolang/commands/chat/widgets.py`: compose and style the
  elapsed and identity segments around the fixed center anchor, and leave the
  optional context slot available but visually empty.
- `tests/unit/cli/test_chat_tui.py` and
  `tests/system/cli/test_chat_tui_e2e.py`: cover absolute centering, run/elapsed
  transitions, styles, stable edge settings, and narrow-terminal fitting.
- `docs/chat.md` and `docs/execution-presentation.md`: document the live-cluster
  layout and note that context usage is not displayed yet.

Out of scope:

- resolving, tracking, or displaying context-usage values; changing execution
  events, model metadata, remote APIs, or persisted run records;
- changing session runnable/model settings, workspace provenance, run lifecycle,
  execution accounting, or model selection;
- adding context displays to non-Chat surfaces; and
- changing status errors, input/queue layout, or transcript presentation.

## Acceptance Tests

1. Idle and running status keep session runnable/model at the same left/right
   anchors; the model remains right-aligned.
2. The display-cell midpoint of `agent @ workspace` stays at the status-line
   center while elapsed changes from hidden to `running`, to `1m30s`, and to a
   wider hour value.
3. Idle omits elapsed text but preserves its slot anchor; ending a run removes
   only elapsed text and restores the session workspace without shifting the
   center.
4. The context-usage slot remains empty: no usage value, slash, placeholder,
   or visible padding is rendered, and its future position does not affect the
   center anchor.
5. Agent and workspace text use normal intensity; only `@` is dim.
6. On an overflowing but otherwise usable layout, the full center label stays
   fixed, two-cell margins remain, and edge labels truncate inward with one
   `…` each. If the center plus margins cannot fit, elapsed and both edge
   labels are hidden; if the center itself cannot fit, it is truncated with a
   trailing `…`. No layout overflows or changes error status behavior.
7. The default repository verification passes.

## Risks

- Long agent/workspace and session labels compete for narrow terminal space;
  display-cell fitting must preserve the center anchor and right model edge.
- The extreme-width fallback intentionally hides session settings and elapsed
  to keep a centered identity visible without overflow.
- Reserving a future usage position must not leave stray padding or separators
  visible before usage is implemented.

## Open Questions

None.
