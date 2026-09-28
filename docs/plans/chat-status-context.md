# Define Chat Status Context Metadata

## Work Type and Status

Feature definition; awaiting approval. No implementation is included in this
plan.

## Verified Current Behavior

- The status bar shows the current session runnable on the left and the
  session model at the right edge.
- During a run, the left side can switch to the active runnable and show elapsed
  activity; a differing session runnable may also appear beside the model.
- Session setting changes can occur during a run, while submitted and queued
  requests retain their own settings.
- The Chat session tracks workdir changes for later submissions, but the status
  bar does not show agent or workspace identity.

## Goal and Success Criteria

Keep the lower-left and lower-right corners dedicated to session settings,
while the center presents the stable agent identity and run-dependent context.
The feature succeeds when:

- the left corner always shows only the session runnable;
- the right corner always shows only the session model and effort;
- the center always shows the unchanged agent name, and shows workspace and
  elapsed activity according to the root run state;
- an active run's workspace and duration are kept separate from session
  settings, then the center returns to the current session workspace when the
  run ends; and
- the one-line status fits narrow terminals without moving the session model
  from the right edge.

## Presentation and State Rules

Use the existing left/right status layout with a new center group:

```text
agic:review          hak · tq              MODEL · auto
agic:review          hak · tq · 1m30s      MODEL · auto
```

- Left is always the current session runnable; never show the active run's
  runnable.
- Right contains only the current session model and effort. A runnable setting
  change during a run updates the left label only; it never adds a runnable
  beside the model.
- The center always shows the Chat agent name; it does not change with run
  state. When idle, show the workspace from the current session workdir. While
  a root run is active, show the workspace from that run's effective workdir.
  Never show the active run's runnable name.
- Append `running` before one elapsed second and the existing compact duration
  thereafter, separated by ` · `. Remove the activity suffix immediately when
  the run stops; preserve the then-current session context and setting values.
- Render the new center group and its separators dim. Use ` · ` consistently;
  omit the workdir path and context-window usage/capacity.
- The workspace value is only the workspace name. Changing directories within
  one workspace does not change the label. An active root run's workspace
  follows its effective workdir when `_toolang.chdir` changes it. `/cd` changes
  the session setting for subsequent runs; it does not rewrite the active run's
  snapshot. When the run ends, the center returns to the latest session
  workspace.
- Existing error rendering and model-setting refresh behavior remain unchanged.
  When space is constrained, truncate the center group before the left session
  runnable or right session model; keep the model right-aligned.

## Scope and Touchpoints

In scope:

- `src/toolang/cli/toolang/commands/chat/widgets.py`: render the fixed session
  runnable, center identity/activity group, and right-aligned session model;
  fit and truncate the added group.
- `src/toolang/cli/toolang/commands/chat/tui.py`: keep the session context
  separate from the active root run context and restore current session values
  on run completion.
- `src/toolang/cli/toolang/commands/chat/base.py` and Chat client/event paths:
  expose the effective workspace for the active root when it changes, including
  `_toolang.chdir`, without treating session `/cd` changes as active-run
  changes.
- `tests/unit/cli/test_chat_tui.py` and
  `tests/system/cli/test_chat_tui_e2e.py`: verify provenance, state transitions,
  rendering, styling, and narrow-terminal behavior.
- `docs/chat.md` and `docs/execution-presentation.md`: document the source and
  status-bar presentation rules.

Out of scope:

- showing the run's runnable name, current directory path, or context-window
  usage/capacity;
- changing run/session setting semantics, model selection, execution policy,
  or queued request snapshots; and
- changing status errors, the input/queue layout, or non-Chat progress output.

## Acceptance Tests

1. Idle state shows only the session runnable in the left corner and only the
   session model/effort in the right corner. The center shows the fixed agent
   name and session workspace.
2. A run using a runnable different from the session setting leaves the left
   value unchanged and does not add the run runnable elsewhere.
3. The agent name remains unchanged across run states. Active status shows the
   active root's workspace and `running` before one second, then elapsed
   duration; child runnable events do not replace the root context.
4. Runnable changes update only the left session setting; model/effort
   changes update only the right session setting. Neither overwrites the
   active-run workspace in the center. Completion removes elapsed activity and
   restores the latest session workspace while preserving the agent name.
5. `_toolang.chdir` within the same workspace leaves the center workspace
   unchanged; a change to another workspace updates it while the root run is
   active. `/cd` changes the session workspace and is reflected when idle or
   after the active run ends, not by mixing it into the run context.
6. Center labels, separators, and activity use dim styling. No workdir path,
   context usage, or active runnable label appears.
7. Narrow terminals remain one line, preserve the right edge for the model, and
   truncate the center group before either session anchor. Existing errors
   still replace normal status.
8. Default repository verification passes.

## Risks

- The current completion-only workdir notification is insufficient to update
  the workspace label during a run; live propagation must stay associated with
  the active root and must not expose a child run's workspace.
- Remote and local Chat clients must provide equivalent agent/workspace
  identity, or status could differ by execution mode.
- The additional center group may compete with long runnable/model labels on
  narrow terminals; display-cell width and the existing model-edge invariant
  must be preserved.

## Open Questions

None.
