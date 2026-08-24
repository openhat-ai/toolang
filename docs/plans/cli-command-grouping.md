# Reorganize Top-Level CLI Commands

## Status

Approved for implementation on 2026-08-24.

## Goal

Make `too --help` describe commands by user intent: agent lifecycle, cap
management, active control, and read-oriented inspection. Public resource names
use plural nouns.

## Command Layout

Render panels in this order:

1. `Agent Commands`
2. `Cap Commands`
3. `Control Commands`
4. `Inspection Commands`

`Agent Commands` keeps its current commands. `Cap Commands` moves immediately
below it and contains, in order, `psyche`, `skill`, `service`, and `prompt`.

Rename `Thread Commands` to `Control Commands`. It contains, in order, `chat`,
`steer`, `cancel`, `retry`, `rerun`, `rewind`, and `fork`. Move `runs`,
`threads`, and `inspect` out of this panel without changing their behavior or
agent-prefix routing.

Replace `Runtime Commands` with `Inspection Commands`. It contains, in order:

```text
threads
runs
inspect
caps
models
providers
adapters
tools
sandboxes
```

The order keeps thread and run inspection first, followed by caps and the model
catalog stack. Installed tool and sandbox plugins remain adjacent at the end.

Rename public `tool` to `tools` and `sandbox` to `sandboxes`. They directly list
their resources without a `list` subcommand. Keep `too caps` and move it into
this panel. Remove the singular `tool`, `sandbox`, and `model` groups without
compatibility aliases. Keep `channel` callable but hidden from normal help until
its public shape is decided. The standalone `caps` CLI remains unchanged.

## Scope and Touchpoints

- Update panel constants, display order, registrations, and hidden-command
  visibility in
  `src/toolang/cli/toolang/main.py`.
- Update placement-aware command specs in
  `src/toolang/cli/toolang/routing.py`.
- Generalize plural resource listing in
  `src/toolang/cli/toolang/commands/plugin.py`.
- Update only directly affected README/help examples and CLI routing/system
  tests. Do not change command behavior, placement rules, cap storage, model
  catalog behavior, or the standalone `caps` executable.

## Acceptance Tests

1. `too --help` renders exactly the four panels and command order above.
2. `runs`, `threads`, and `inspect` retain their current required agent-prefix
   routing while appearing only under `Inspection Commands`.
3. `too tools` and `too sandboxes` provide the current list behavior without a
   required `list` subcommand.
4. `too tool`, `too sandbox`, and `too model` are no longer commands.
5. `channel` remains callable but is absent from normal help.
6. `too caps` appears under `Inspection Commands`; the standalone `caps` CLI and
   the four cap-kind groups are unchanged.
7. Placement-specific target help uses the same panel names, visibility, and
   order.
8. The default verification suite passes.

## Risks

- Removing the singular resource groups is intentionally breaking. The new
  plural commands are shorter because listing is their default behavior.

## Open Questions

None.
