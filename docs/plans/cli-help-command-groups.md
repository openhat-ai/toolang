# Reorganize CLI Help Command Groups

## Status

Approved for implementation on 2026-09-19; the definition and its implementation
ship together in one pull request.

## Goal

Make the root help concise by moving run-control commands and installed-plugin
inventories into `too more`, grouping `too more`, and removing the single-row
`Control Commands` panel from the root. Command behavior, names, options, and
routing stay unchanged; only help grouping, visibility, and wording change.

Success means the root help renders five non-empty panels without a single-row
panel, `too more` renders four grouped panels, `too AGENT --help` keeps
discoverable run controls, every command is registered under the panel where it
appears, and every document that describes the command surface matches the
implementation.

## Verified Baseline

At `0.2.7-287-g51e2083b` (verified by running `.venv/bin/toolang`):

- Root help renders six panels: `Agent Commands` (8 rows), `Cap Commands` (4),
  `Work Commands` (3), `Control Commands` (7: `chat steer cancel retry rerun
  fork rewind`), `Inspection Commands` (9: `caps tools models providers catalogs
  adapters toolsets sandboxes inspect`), `Script Commands` (2). The epilog is
  `Run 'too more' to see additional commands.`
- `too more` renders one `Additional Commands` panel with `fmt highlight parse
  query compact`, no description or usage line, and the hint `Run 'too COMMAND
  --help' for details.`
- `too AGENT --help` renders `Control Commands` with the seven control rows, so
  control commands require agent targets and are meaningful in target help.
- `more` copies hidden root commands, clears `hidden`, and sets the panel on the
  copies (`_MoreCommandsCommand.format_help`). Target help selects non-hidden
  root commands whose routing spec accepts `before`
  (`_run_target_help`), so hiding a command removes it from target help too.
- `TyperGroup.list_commands` preserves insertion order and the shared
  `HelpFormatter` groups command rows by `rich_help_panel`, so panel grouping
  needs no formatter change.

## Target Layout

Root help (33 -> 23 rows, five panels, no single-row panel):

```text
Agent Commands:       new clone remove list info serve start stop
Cap Commands:         psyche skill service prompt
Work Commands:        chat chore task workspace
Inspection Commands:  caps tools models providers inspect
Script Commands:      init run
```

Refined root descriptions:

```text
list       List agents and their status      (was "Show agents and their status")
info       Show agent information            (was "Show agent info")
serve      Run an agent in the foreground    (was "Serve an agent in the foreground")
chat       Start an interactive chat         (was "Start an interactive TUI")
providers  List model providers              (was "List available model providers")
```

`too more` (four panels, unchanged header/footer conventions):

```text
Run Commands:
  steer      Steer an active run
  cancel     Cancel an active run
  retry      Retry a run from a failed step
  rerun      Rerun an earlier run as a new one

Thread Commands:
  fork       Fork a thread from an earlier run
  rewind     Rewind a thread to an earlier run
  compact    Compact a thread

Runtime Commands:
  catalogs   List installed model catalogs
  adapters   List installed model adapters
  toolsets   List installed toolsets
  sandboxes  List installed sandboxes

Language Commands:
  fmt        Format .too source
  highlight  Highlight .too source
  parse      Parse .too source
  query      Show collection query syntax and fields

Run 'too COMMAND --help' for details.
```

`too AGENT --help` keeps a single `Control Commands` panel with the six advanced
controls (`steer cancel retry rerun fork rewind`); `chat` appears under `Work
Commands`. `compact` stays excluded from target help.

## Decisions

1. `chat` is registered directly under `Work Commands` and becomes its first
   row; the root `Control Commands` panel is removed. `chat` is never assigned
   the `Control Commands` panel anywhere. `Work Commands` order is `chat chore
   task workspace`.
2. `steer`, `cancel`, `retry`, and `rerun` move to `too more` under
   `Run Commands`; `fork`, `rewind`, and `compact` move under
   `Thread Commands`. All seven are hidden from root help; `compact` is also
   absent from target help.
3. `catalogs`, `adapters`, `toolsets`, and `sandboxes` move to `too more` under
   `Runtime Commands`. `tools` stays in root `Inspection Commands`. `models`,
   `providers`, `caps`, and `inspect` stay in root `Inspection Commands`, in the
   order `caps tools models providers inspect`.
4. `fmt`, `highlight`, `parse`, and `query` move to `too more` under
   `Language Commands`, keeping the order `fmt highlight parse query`.
5. Descriptions are refined in the root directory and, for `query`, in `more`:
   `list`, `info`, `serve`, `chat`, and `providers` as listed above, and `query`
   becomes `Show collection query syntax and fields`. `inspect` becomes
   `Inspect agent runs` (was `Inspect agent run history`).
6. An explicit exception keeps the six advanced controls in target help. Target
   help keeps them in one `Control Commands` panel, distinct from the `more`
   split into `Run Commands` and `Thread Commands`; the source commands retain
   `rich_help_panel=CONTROL_COMMAND_PANEL` and `too more` re-groups the copies.
   Hidden status is a root-help concern only; routing and dispatch are
   unaffected.
7. `channel` stays callable, hidden, and absent from root help and `too more`.
8. The root epilog stays `Run 'too more' to see additional commands.`
9. This plan supersedes the `Control Commands` membership in
   `docs/plans/cli-command-grouping.md`; that approved historical plan is not
   rewritten, per the existing convention for approved plans.

## Implementation Notes

Keep the mapping literal; do not add a display indirection that relocates a
command away from its registered panel.

- Each command is registered with exactly the panel it appears under at the
  root. `chat` carries `rich_help_panel=WORK_COMMAND_PANEL`; no other code
  moves it.
- The six advanced controls keep `rich_help_panel=CONTROL_COMMAND_PANEL` so
  target help can group them; the root directory hides them.
- `compact` is registered hidden with no root panel; `too more` assigns its
  `Thread Commands` panel like any other listed command.
- Root ordering and `too more` ordering come from explicit ordered constants:
  `_VISIBLE_COMMAND_ORDER` derives `_COMMAND_ORDER`, and
  `_MORE_PANEL_COMMAND_ORDER` maps panels to commands for
  `_MoreCommandsCommand.format_help`. Neither is reordered or filtered at
  display time.
- `_run_target_help` uses the named `_TARGET_HELP_COMMANDS` set to override root
  hidden status for target help only, unhiding the copied commands.

## Scope and Touchpoints

Implementation is limited to:

- `src/toolang/cli/toolang/main.py`
  - Set `_WORK_PANEL_COMMAND_ORDER = ("chat", "chore", "task", "workspace")`
    and register `chat` with `rich_help_panel=WORK_COMMAND_PANEL`.
  - Set `_CONTROL_PANEL_COMMAND_ORDER = ("steer", "cancel", "retry", "rerun",
    "fork", "rewind")`; register those six with `hidden=True` and
    `rich_help_panel=CONTROL_COMMAND_PANEL` for target help; register `compact`
    hidden with no root panel.
  - Set `_INSPECTION_PANEL_COMMAND_ORDER = ("caps", "tools", "models",
    "providers", "inspect")`; set `hidden=True` on `catalogs`, `adapters`,
    `toolsets`, and `sandboxes`.
  - Add `RUN_COMMAND_PANEL`, `THREAD_COMMAND_PANEL`, `RUNTIME_COMMAND_PANEL`,
    and `LANGUAGE_COMMAND_PANEL`; replace `_ADDITIONAL_COMMAND_ORDER` with
    `_MORE_PANEL_COMMAND_ORDER`, the ordered panel-to-command mapping used by
    `_MoreCommandsCommand.format_help`.
  - Refine the `list`, `info`, `serve`, `chat`, `providers`, and `inspect`
    registration descriptions.
- `src/toolang/cli/toolang/commands/metadata.py`: change the `QUERY_HELP` first
  line to `Show collection query syntax and fields`.
- `src/toolang/cli/toolang/routing.py`: no change; command names, target
  grammar, and the existing `more` spec are unchanged.
- Tests:
  - `tests/unit/cli/test_cli_routing.py`: update
    `test_cli_visible_commands_follow_the_public_panel_order` for the five root
    panels and the moved commands' hidden status; update
    `test_cli_exposes_plural_list_resources_and_hides_channels` for the new
    `inspect` and `providers` help text and moved-plugin hidden status; keep the
    target-help and control-order assertions, confirming the six controls stay
    and `compact` is absent in `too AGENT --help`.
  - `tests/unit/cli/test_cli_help.py`: assert the four `more` panels, row order,
    and refined descriptions, and that moved descriptions are absent from root
    help.
  - `tests/integration/cli/test_local_core_commands.py`,
    `tests/integration/cli/test_model_catalog_commands.py`, and
    `tests/system/cli/test_cli_entry_points.py`: update the `inspect` and
    `serve` description assertions.
- Documentation: see the consistency sweep below.

## Documentation Consistency Sweep

After the change, every document that mirrors the command surface must match the
implementation:

- `CHANGELOG.md` `Unreleased` `Changed`: record the regrouped root help, the
  four `too more` panels, and the refined descriptions.
- `README.md`: update the `serve` summary line to `Run an agent in the
  foreground`; its `Common Commands` headings use their own vocabulary and list
  callable commands, so the rest stays accurate.
- `docs/source-commands.md` and `docs/queries.md`: they describe the language
  and query commands as discoverable through `too more`; verify wording still
  matches and adjust only if a sentence names a panel.
- `docs/models.md` lists `tools`, `catalogs`, `adapters`, and `providers` as
  commands without panel membership; verify it needs no change.
- Approved historical plans are not rewritten; this plan records the
  supersession.

## Acceptance Tests

1. Root help for bare `too`/`--help`/`-h` renders exactly the five panels above
   with the stated rows and order, and contains no `Control Commands` panel and
   no moved command row.
2. Root help shows the refined `list`, `info`, `serve`, `chat`, and `providers`
   descriptions and still ends with the executable-aware `Run 'too more' to see
   additional commands.` hint.
3. `too more`, `too more -h`, and `too more --help` produce identical output:
   `Run Commands`, `Thread Commands`, `Runtime Commands`, and `Language
   Commands` sections, the stated rows and order, no usage line, and the
   `Run 'too COMMAND --help' for details.` footer.
4. `too AGENT --help` keeps `Control Commands` with the six advanced controls,
   shows `chat` under `Work Commands`, and omits `compact`, `channel`, and
   `_serve`.
5. `too inspect --help` and root help describe `inspect` as `Inspect agent
   runs`; `too query` describes itself as `Show collection query syntax and
   fields`.
6. Every moved command keeps its current name, options, target grammar, exit
   behavior, and direct invocation (`too steer ...`, `too catalogs`, etc.).
7. `channel` remains callable and absent from root help and `too more`.
8. Root command registration and the routing registry still have identical
   names, and the default offline suite plus `ruff check`, `ruff format
   --check`, and `ty check` pass.

## Risks and Open Questions

- Hiding control commands at the root also hides them from target help; the
  target-help exception is required and is covered by acceptance test 4.
- `more` categorizes the controls as `Run Commands` and `Thread Commands`, while
  target help keeps one `Control Commands` panel. The same commands therefore
  appear under different panel names on the two surfaces; this is intentional
  and tested.
- No unresolved product questions.
