# Open an Interactive Shell in the Toolang Root

## Status

Approved by the user on 2026-09-29 for implementation and a pull request.
This extends the implemented agent-home shell behavior.

## Goal and Success Criteria

`too shell` opens an interactive child shell in the selected Toolang root.
`too AGENT shell` continues to open one in the selected resident agent home.
Omitting the agent must work even when no agents exist.

## Verified Baseline

- `commands/shell.py` resolves an agent layout and launches `$SHELL -i`, with
  `/bin/sh` as the fallback when `SHELL` is unset.
- `main.py` uses `RequiredPrefixAgentCommand`; the routing specification allows
  only a resident target before `shell`.
- Root selection already follows explicit `--root`, then `TOOLANG_ROOT`, then
  `~/.toolang`. CLI context exposes both the root and optional agent.
- The command requires TTY stdin and stdout, inherits environment and streams,
  and propagates child exit status, including signal termination.

## Decisions and Scope

1. Make the agent prefix optional. Use `too shell` for root scope and preserve
   `too AGENT shell`, including explicit `agent:NAME` selectors, for agent scope.
   Do not add a new flag or accept an agent after `shell`.
2. Select the working directory in the CLI: use `context_root` when no agent is
   selected and the existing agent layout home otherwise. Reuse existing root
   resolution, command factories, and optional-prefix help conventions.
3. A missing root or a root that is not a directory produces a CLI error without
   creating directories or initializing Toolang. Existing launch-error handling
   can report the failed working directory operation.
4. Preserve shell selection, TTY requirements, inherited streams/environment,
   launch errors, and exit status behavior. Do not load dotenv files, rewrite
   environment variables, start runtime services, or alter the parent shell.
5. Help shows `too [AGENT] shell [OPTIONS]`, describes AGENT as
   `Local agent name; omit for Toolang root`, and describes the command as
   `Open a shell in Toolang root or agent home`. Keep its panel/order.
6. README common commands show both forms and an explicit-root example:
   `too --root /path/to/toolang-root shell`. The destination must already exist.

## Implementation Touchpoints

- `src/toolang/cli/toolang/commands/shell.py`: choose root or agent-home cwd.
- `src/toolang/cli/toolang/main.py`: optional-prefix command class and help.
- `src/toolang/cli/toolang/routing.py`: allow target-free and resident-prefix use.
- `tests/unit/cli/test_shell_command.py`: root selection and process behavior.
- `tests/unit/cli/test_cli_routing.py`: grammar, optional argument, and help.
- `tests/unit/cli/test_cli_help.py`: remove shell from required-agent help cases.
- `README.md`: root and agent shell examples.

## Acceptance Tests

1. Mock process launch and TTY checks: `too shell` selects the default root;
   `TOOLANG_ROOT` overrides it; explicit `--root` overrides both. Include a root
   with spaces and a relative explicit root. No resident agent is required.
2. Agent-prefixed invocations retain their cwd and root-selection behavior;
   missing or invalid explicitly selected agents never fall back to root scope.
3. Missing/non-directory roots fail cleanly and are not created. Cover the
   subprocess error path without launching an interactive shell.
4. Both scopes retain non-TTY refusal, `$SHELL` selection and fallback, inherited
   environment/streams, launch-error reporting, and normal/signal exit status.
5. Help marks AGENT optional, states its default scope, and keeps command order.
   Invalid trailing targets and nonresident selectors remain rejected.
6. Run the repository's default Ruff, formatting, ty, and offline pytest checks
   for implementation; validate README examples and run `git diff --check`.

## Risks and Open Questions

`too shell` currently shows help; it will launch a shell on interactive terminals
after this change. Shell startup files may change cwd, as with the existing
agent shell. No open design questions remain.
