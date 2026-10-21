# CD Command

Approved for implementation and one pull request by the user on 2026-10-21.

## Goal and Scope

Use `cd` as the concise command for opening a shell in the Toolang root or an
agent home. Success means help stays brief and the entry message identifies
the destination without decorating the agent name.
This definition supersedes [home-command.md](home-command.md) for command naming
and presentation only.

## Decisions

- Replace `home` with `cd`, without a compatibility alias. Keep its position in
  Agent commands. `too cd` selects the Toolang root; `too AGENT cd` selects the
  agent home for resident, visiting, and roaming targets.
- Leave argument-order handling to unified CLI routing. This change adds no
  command-specific support or validation for `too cd AGENT`.
- Retain the interactive child shell: exiting returns to the calling shell.
  Do not add shell integration or change the calling shell's directory.
- Reuse existing target resolution, nonresident preparation, root precedence,
  and placement-specific global-option rules. Preserve TTY checks, shell
  selection, inherited environment/streams, launch errors, and exit status.
  Missing explicit targets never fall back to the root; no runtime is started.
- Keep existing command-name precedence. Use `agent:cd` to select a resident
  named `cd`.

## Help and Entry Messages

Use this short description in root help:

```text
Enter an agent home
```

Command help uses `Usage: too [AGENT] cd [OPTIONS]` and this description:

```text
Enter an agent home, or the Toolang root if no agent is specified.
```

Argument help is `Agent name, .too path, GitHub ref, or URL`.
Do not add examples to command help. Keep shell mechanics and the exit reminder
in the entry message.

Print these two lines once before launching the child shell:

```text
Opening a shell in the Toolang root: {path}
Type exit to return.
```

For every agent placement, use:

```text
Opening a shell in {name}'s home: {path}
Type exit to return.
```

Use the resolved `layout.name`, without placement labels, instance IDs, source
references, or URLs. Use the selected root or `layout.home` for the path and
reuse `shorten_home_path`; retain the complete destination structure. Placement
and instance distinctions appear only in the path. Do not change preparation
progress messages.

## Implementation Touchpoints

- Rename `src/toolang/cli/toolang/commands/home.py` to `commands/cd.py`; preserve
  the existing target context and format entry messages with the path helper.
- Update `src/toolang/cli/toolang/main.py` registration, command class, ordering,
  short help, and detailed help; update the command name and preparation
  references in `routing.py` without changing its target-order rules.
- Rename `tests/unit/cli/test_home_command.py` to `test_cd_command.py`; update
  `test_cli_routing.py` and relevant help assertions in `test_cli_help.py`.
- Update README common commands and the routing table in
  [CLI design](../design/cli.md). Leave historical plans unchanged.
- Generate the compatibility entry in `CHANGELOG.md` through
  `too aid.too update_changelog` and verify its migration guidance.

## Acceptance Tests

1. Root selection respects explicit root, environment, and default precedence;
   no agent is required. Missing/unusable roots are not created.
2. Target-first invocation selects the expected home for every placement,
   including a resident selected with `agent:cd`. Existing invalid/missing
   target, global-option, and usage checks retain their behavior without shell
   launch or root fallback; no command-specific argument-order cases are added.
3. Entry output matches the two-line templates exactly. All placements use the
   simple name even when paths contain instance IDs; cover home-relative,
   temporary, and external paths. Output precedes shell launch.
4. Root and command help match their respective copy, mark AGENT optional,
   omit examples, and keep command order. `home` is no longer registered as a
   command.
5. Existing offline tests retain coverage for nonresident preparation/cache
   reuse and failures, side-effect-free help/usage/non-TTY handling, shell
   fallback/errors, inherited streams/environment, and normal/signal exit codes.
6. Implementation passes the repository's default Ruff, formatting, ty, pytest,
   and diff checks. This definition requires source/link verification and
   `git diff --check` only.

## Risks and Open Questions

Existing `home` invocations will break, and `cd` becomes reserved. The name may
suggest a change to the calling shell; the entry message explicitly announces
a new shell and how to return. No open questions remain in the proposed scope.
