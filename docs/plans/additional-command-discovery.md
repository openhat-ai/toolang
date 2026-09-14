# Additional Command Discovery

## Status and Goal

Approved for implementation by the user on 2026-09-14.

Make the additional supported commands discoverable through `too more` while
keeping the main command directory concise. Success means an executable-aware
root-help hint, one minimal additional-command directory, and unchanged
behavior for every listed command.

Confirmed decisions (2026-09-14):

- Row order is `fmt`, `highlight`, `parse`, `query`, `compact`.
- `hidden` is removed rather than kept as a compatibility alias.
- The directory prints no leading executable-name line.

## Verified Baseline

At `e753e0a1`:

- `too hidden` and `toolang hidden` exit 0 and print a description, a usage
  line, a `Hidden Commands` panel ordered `query`, `parse`, `fmt`, `highlight`,
  `compact`, and `Run 'too COMMAND --help' for details.`
- Root help for bare `too`, `too --help`, and `too -h` exits 0, lists the
  visible panels and options, and carries no discovery hint.
- Commands register lazily through `_registered_command`, and
  `routing.validate_command_registration` requires the Typer surface and
  `COMMAND_SPECS` to have identical names.
- `_ToolangGroup`, `_ToolangHelpContext`, and `_ToolangHelpFormatter`
  (`src/toolang/cli/toolang/main.py:161-197`) are used only by the root app, so
  a root-only help hook cannot change individual command help.
- Root help renders through the shared `HelpFormatter.write_help` because
  `_ToolangGroup` does not override `format_help`; that path ends in
  `write_epilog(ctx)`, which reads `ctx.command.epilog`.
- `toolang` and `too` are both project scripts pointing at the same `main`.
- An unknown command exits 2 with `Error: No such command '<NAME>'.` followed by
  `Try '<PROG> --help' for help.`

## CLI Contract

Add `more` as the canonical, target-free discovery command on both executables
and keep it out of every main command panel.

Append this hint as the last line of root help, including bare `too`, and use
the invoked executable name:

```text
Run 'too more' to see additional commands.
```

`too more`, `too more -h`, and `too more --help` print the same directory and
exit successfully:

```text
Additional Commands:
  fmt        Format .too source
  highlight  Highlight .too source
  parse      Parse .too source
  query      Show collection-query syntax and fields
  compact    Compact a thread

Run 'too COMMAND --help' for details.
```

Substitute `toolang` throughout when invoked through that executable. Print no
description, usage line, options metavariable, options panel, or leading
executable-name line. Retain standard `-h` / `--help` parsing and normal
rejection of unexpected arguments and options; add no operational options or
nested subcommands.

Display exactly these five commands in the stated order, with descriptions taken
from the existing registrations and the shared two-column layout. Do not expose
`_serve`, `channel`, or the discovery command itself. Opening the directory must
not prepare an agent, execute a listed command, or change later root help.

Preserve shared theme, color, width, and output-stream handling. Render the root
hint from the invoked command name rather than a fixed `epilog` string, so
`toolang --help` does not advertise `too more`.

Remove `hidden` instead of aliasing it, with no deprecation period.
`too hidden` then fails like any unknown command (`Error: No such command
'hidden'.`, exit 2) unless an existing target route resolves that selector. `more`
becomes a reserved top-level name: `agent:more` remains the escape hatch for an
agent of that name, and `too more <TARGET>` is not a valid route.

Keep `query`, its description, its collection schemas, and `--query/-q`
unchanged; do not add `cquery` or `colleq`. Listed commands retain their current
top-level invocation and target requirements, including the agent prefix for
`compact`.

## Scope and Touchpoints

This definition adds only this plan. A later implementation is limited to:

- `src/toolang/cli/toolang/main.py`: rename the discovery command, its command
  class, and the directory-order constant; render the `Additional Commands`
  directory without description or usage; add the executable-aware hint on the
  root-only help path (`_ToolangHelpFormatter`/`_ToolangHelpContext`).
- `src/toolang/cli/toolang/routing.py`: replace the `hidden` spec at line 130
  with a target-free `more` spec, preserving lazy factories and registration
  validation.
- `tests/unit/cli/test_cli_help.py`: update
  `test_hidden_commands_keep_theme_and_root_invocation_hint` and
  `test_hidden_directory_uses_selected_help_output` for the new name and
  minimal output, and assert the root hint for both executables.
- `tests/unit/cli/test_cli_routing.py`: keep hidden-command and panel-order
  assertions consistent with the rename.
- `tests/integration/cli/test_query_discovery.py`: discover unchanged query help
  through `more`.
- `tests/system/cli/test_cli_entry_points.py`: exempt the intentionally minimal
  `more` directory from the generic requirement that every command help includes
  a usage line.
- `docs/source-commands.md`, `docs/queries.md`, and the `Unreleased` section of
  `CHANGELOG.md`: name `more` as the discovery entry point and record the
  removal of `hidden`.

Historical plans remain historical context. Reuse the shared formatter without
changing generic help behavior. Excluded: source processing, query semantics,
runtime execution, command flags, standalone `caps`, and existing visible panels
and their order.

## Acceptance Tests

1. Root help for both executables ends with the matching `more` hint. Visible
   panels keep their existing contents and order, and neither the additional
   commands nor either discovery entry point appears in a panel.
2. Bare `more`, `more -h`, and `more --help` exit zero with identical output:
   `Additional Commands:` first, exactly the five rows in the stated order, then
   the matching details hint. No description, usage, options panel, or leading
   executable line appears.
3. `too hidden` exits 2 as an unknown command, `more` needs no target or agent
   preparation, and `agent:more` still routes as an agent selector.
4. Plain and colored output use the selected console and theme, stay readable at
   the existing 44-column test width, and do not alter later root help.
5. Existing direct command help and routing checks pass, and query grammar,
   collection help, and JSON schema behavior stay unchanged.
6. Current documentation names `more` as the discovery entry point. Default
   verification passes: Ruff lint and format check, `ty`, and offline pytest.

## Risks and Open Questions

- Removing `hidden` breaks existing callers with no deprecation path and must be
  recorded in the changelog.
- `more` becomes a reserved top-level command name, so an agent named `more`
  requires the explicit `agent:more` selector; the routing contract is tested.
- A static help string would print `too` under `toolang`; the hint must use the
  invoked name.
- Directory order lives in one constant so panels and tests cannot drift.

No unresolved implementation choices.
